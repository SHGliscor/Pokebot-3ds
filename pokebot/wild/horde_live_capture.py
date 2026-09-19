from __future__ import annotations

"""Public live single-shiny Horde Auto-Capture.

This is the live generalisation of the hardware-proven D18f validator, not a
new reducer.  The only intentional generalisation is that CENTER/slot 2 is no
longer hard-coded as the protected stand-in: the one RAM-confirmed shiny slot is
protected instead.

Safety/authority retained from D18f:
- D10 alive-count relation: view state == alive + 1, mask 0x100 at command boundary
- D16 exact one-hot direct target mask for each physical Horde position
- D17a FIGHT touch (158,100) and mapped single-target move touch (246,69)
- owner+0x93 exact phase state: 1=COMMAND, 2=MOVE
- phase fingerprint learned once per battle from the exact D18f calibration sequence
- attack result is classified only after the post-turn COMMAND phase returns
- unchanged alive count permits retry of the SAME non-shiny target only
- more-than-one alive drop fails closed
- survivor proven by exhaustive exclusion, never by final canonicalized target mask
- D12/D18f dynamic Horde Bag owner and slot-aware failed-capture identity
- zero game RAM writes; touch events are never retransmitted
"""

import time

from pokebot.common.species_names import SPECIES_NAMES
from pokebot.wild import battle_bag_throw as bagmod
from tools import horde_protected_survivor_full_auto as d18
from tools.horde_dynamic_owner_capture_validator import prove_one_alive, discover_unique_owner
from pokebot.wild.oras_touch_profile import get_battle_move_xy
from pokebot.wild.oras_move_policy import choose_safe_attack

NORMAL_BATTLE_OWNER = 0x0852FC74
VISUAL_ORDER = ["FAR_LEFT", "INNER_LEFT", "CENTER", "INNER_RIGHT", "FAR_RIGHT"]
VISUAL_DISPLAY = {
    "FAR_LEFT": "OUTER_LEFT",
    "INNER_LEFT": "INNER_LEFT",
    "CENTER": "CENTER",
    "INNER_RIGHT": "INNER_RIGHT",
    "FAR_RIGHT": "OUTER_RIGHT",
}


def _sleep_checked(seconds: float, check_stop) -> None:
    deadline = time.monotonic() + max(0.0, float(seconds))
    while time.monotonic() < deadline:
        check_stop()
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def _wait_initial_five_alive_gate(core, br, *, timeout: float, check_stop, log) -> dict:
    """Wait read-only until the live presentation reaches D10 state6/mask100."""
    started = time.monotonic()
    deadline = started + float(timeout)
    samples = []
    stable = 0
    last = None
    announced = False
    while time.monotonic() < deadline:
        check_stop()
        snap = d18.gate_snapshot(core, br)
        last = snap
        ready = bool(
            snap.get("battle_u32") == core.BATTLE_ACTIVE
            and snap.get("state_u32") == 6
            and snap.get("mask_u32") == 0x100
        )
        samples.append({
            "elapsed": round(time.monotonic() - started, 3),
            "battle": snap.get("battle"),
            "state": snap.get("state"),
            "mask": snap.get("mask"),
            "ready": ready,
        })
        if len(samples) > 24:
            samples.pop(0)
        if ready:
            stable += 1
            if stable >= 2:
                return {
                    "proven": True,
                    "elapsed": round(time.monotonic() - started, 3),
                    "stable_samples": stable,
                    "last": snap,
                    "samples_tail": samples,
                }
        else:
            stable = 0
            if not announced:
                log("HORDE AUTO-CATCH: shiny confirmed; waiting read-only for five-alive battle readiness")
                announced = True
        time.sleep(0.12)
    return {
        "proven": False,
        "elapsed": round(time.monotonic() - started, 3),
        "stable_samples": stable,
        "last": last,
        "samples_tail": samples,
    }


def _wait_initial_exact_command(core, br, owner: int, *, timeout: float, check_stop, log) -> dict:
    """Bridge the only timing difference between D18f validator and live hunt.

    D18f was manually started after the command menu was visually stable.  A live
    hunt detects the shiny earlier.  Hardware probe 2026-08-27 shows owner+0x93
    can be 0 during battle resolution/presentation and later returns to the exact
    proven COMMAND byte 1.  Therefore live mode waits; it does not reinterpret 0.
    """
    started = time.monotonic()
    deadline = started + float(timeout)
    stable = 0
    samples = []
    while time.monotonic() < deadline:
        check_stop()
        try:
            g = d18.gate_snapshot(core, br)
            vptr = br.u32(owner + d18.ACTSELECT_VPTR_OFF)
            target_mask = br.u32(owner + d18.TARGET_MASK_OFF) & 0xFFFFFFFF
            phase_byte = br.read(owner + d18.PHASE_STATE_OFF, 1)[0]
            good = bool(
                g.get("battle_u32") == core.BATTLE_ACTIVE
                and g.get("state_u32") == 6
                and g.get("mask_u32") == 0x100
                and vptr == d18.EXPECTED_ACTSELECT_VPTR
                and target_mask == 0
                and phase_byte == 1
            )
            rec = {
                "elapsed": round(time.monotonic() - started, 3),
                "gate": g,
                "vptr": d18.hx(vptr),
                "target_mask": d18.hx(target_mask),
                "phase_state_offset": d18.hx(d18.PHASE_STATE_OFF),
                "phase_state_byte": int(phase_byte),
                "good": good,
            }
            samples.append(rec)
            if len(samples) > 40:
                samples.pop(0)
            stable = stable + 1 if good else 0
            if stable >= 2:
                log("HORDE AUTO-CATCH: exact D18f COMMAND phase proven (owner+0x93=1)")
                return {"proven": True, "last": rec, "samples_tail": samples}
        except Exception as exc:
            samples.append({"error": f"{type(exc).__name__}: {exc}"})
            stable = 0
        time.sleep(0.12)
    return {"proven": False, "last": samples[-1] if samples else None, "samples_tail": samples}


def _wait_phase_live(core, br, owner: int, fp: dict, wanted: str, alive: int, *, timeout: float, check_stop) -> dict:
    """D18f wait_phase with only a live STOP check added."""
    deadline = time.monotonic() + float(timeout)
    samples = []
    stable = 0
    expected_state = alive + 1
    while time.monotonic() < deadline:
        check_stop()
        try:
            g = d18.gate_snapshot(core, br)
            s = d18.phase_score(br, owner, fp)
            target_mask = br.u32(owner + d18.TARGET_MASK_OFF) & 0xFFFFFFFF
            rec = {
                "time": d18.now_iso(),
                "gate": g,
                "phase": s,
                "target_mask": d18.hx(target_mask),
            }
            samples.append(rec)
            gate_ok = (
                g.get("battle_u32") == core.BATTLE_ACTIVE
                and bool(g.get("valid_view"))
                and g.get("state_u32") == expected_state
                and g.get("mask_u32") == 0x100
            )
            if gate_ok and d18.strong_phase(s, wanted):
                stable += 1
                if stable >= 2:
                    return {"proven": True, "phase": wanted, "alive": alive, "samples": samples, "last": rec}
            else:
                stable = 0
        except Exception as exc:
            samples.append({"time": d18.now_iso(), "error": f"{type(exc).__name__}: {exc}"})
            stable = 0
        time.sleep(0.12)
    return {"proven": False, "phase": wanted, "alive": alive, "samples": samples, "last": samples[-1] if samples else None}


def _wait_attack_resolution_live(core, br, owner: int, fp: dict, before_alive: int, *, timeout: float, check_stop) -> dict:
    """D18f wait_attack_resolution with only a live STOP check added."""
    expected_alive = before_alive - 1
    deadline = time.monotonic() + float(timeout)
    started = time.monotonic()
    samples = []
    stable_kind = None
    stable = 0
    while time.monotonic() < deadline:
        check_stop()
        try:
            g = d18.gate_snapshot(core, br)
            state = g.get("state_u32")
            sc = d18.phase_score(br, owner, fp)
            rec = {
                "time": d18.now_iso(),
                "gate": g,
                "phase": sc,
                "elapsed": round(time.monotonic() - started, 3),
            }
            samples.append(rec)
            if g.get("battle_u32") != core.BATTLE_ACTIVE:
                return {"proven": False, "status": "BATTLE_ENDED_UNEXPECTEDLY", "samples": samples, "last": rec}
            if state is None or not (2 <= state <= 6):
                stable_kind = None
                stable = 0
                time.sleep(0.10)
                continue
            alive = state - 1
            rec["alive_from_state"] = alive
            if alive < expected_alive:
                return {"proven": False, "status": "UNEXPECTED_MULTI_OPPONENT_DROP", "before_alive": before_alive, "observed_alive": alive, "samples": samples, "last": rec}
            if alive > before_alive:
                return {"proven": False, "status": "UNEXPECTED_ALIVE_COUNT_INCREASE", "before_alive": before_alive, "observed_alive": alive, "samples": samples, "last": rec}

            armed = (time.monotonic() - started) >= d18.ATTACK_RESOLUTION_ARM_S
            command_ready = armed and g.get("mask_u32") == 0x100 and d18.strong_phase(sc, "COMMAND")
            if command_ready and alive == expected_alive:
                kind = "KO"
            elif command_ready and alive == before_alive:
                kind = "SURVIVED"
            else:
                kind = None

            if kind is not None and kind == stable_kind:
                stable += 1
            elif kind is not None:
                stable_kind = kind
                stable = 1
            else:
                stable_kind = None
                stable = 0

            if stable >= 2:
                if stable_kind == "KO":
                    return {
                        "proven": True,
                        "status": "EXACTLY_ONE_ALIVE_DECREMENT_PROVEN",
                        "before_alive": before_alive,
                        "after_alive": expected_alive,
                        "samples": samples,
                        "last": rec,
                    }
                return {
                    "proven": True,
                    "status": "TARGET_SURVIVED_OR_MOVE_MISSED_COMMAND_RETURN_PROVEN",
                    "before_alive": before_alive,
                    "after_alive": before_alive,
                    "samples": samples,
                    "last": rec,
                }
        except Exception as exc:
            samples.append({"time": d18.now_iso(), "error": f"{type(exc).__name__}: {exc}"})
            stable_kind = None
            stable = 0
        time.sleep(0.10)
    return {"proven": False, "status": "ATTACK_RESOLUTION_TIMEOUT", "before_alive": before_alive, "samples": samples, "last": samples[-1] if samples else None}


def _capture_one_alive_live(core, br, target: dict, owner_rec: dict, first_ball_turn: int, *, ball_override="best", check_stop, log) -> dict:
    owner = int(owner_rec["owner_u32"])
    binding = bagmod.bind_runtime_owner(owner, locator="D25 live Horde unique dual-vptr runtime owner")
    verified = bagmod._verify_fixed_owner(br)
    bag = owner + bagmod.BAG_OFF
    initial_state = br.read(bag + bagmod.BAG_STATE_OFF, 1)[0]
    initial_controller = br.u32(bag + bagmod.BAG_CONTROLLER_OFF)
    bag_open = initial_state == 1 and bagmod._valid_heap_ptr(initial_controller)

    out = {
        "authority": "D12/D18f live slot-aware consumed-BAG capture",
        "ram_writes": False,
        "runtime_binding": binding,
        "runtime_owner_verified": verified,
        "first_ball_battle_turn_index": int(first_ball_turn),
        "ball_override": str(ball_override or "best"),
        "throws": [],
        "throw_count": 0,
    }
    battle_turn = max(1, int(first_ball_turn))

    for attempt in range(1, int(bagmod.MAX_AUTO_CATCH_THROWS) + 1):
        check_stop()
        log(f"HORDE AUTO-CATCH: Ball {attempt}/{bagmod.MAX_AUTO_CATCH_THROWS} (battle turn {battle_turn})")
        if attempt == 1 and not bag_open:
            throw = bagmod.throw_one_best_ball(
                core, br, target=target, throw_index=battle_turn,
                method_key="horde", environment="horde",
                ball_override=ball_override,
                check_stop=check_stop, log=log,
            )
        else:
            if not bag_open:
                raise bagmod.BagThrowError(
                    f"D25 Ball {attempt} requested without game-consumed Bag-state-1 authority"
                )
            throw = bagmod._throw_best_ball_from_open_bag(
                core, br, target=target, throw_index=battle_turn,
                method_key="horde", environment="horde",
                ball_override=ball_override,
                check_stop=check_stop, log=log,
            )
            bag_open = False

        outcome = bagmod._wait_throw_outcome(
            core, br, target=target, pre_throw_flow=throw.get("pre_throw_flow"),
            check_stop=check_stop, log=log,
        )
        out["throws"].append({
            "attempt": attempt,
            "battle_turn_index": battle_turn,
            "ball": (throw.get("final_selection") or {}).get("ball_name"),
            "throw": throw,
            "outcome": outcome,
        })
        out["throw_count"] = attempt
        if outcome.get("status") == "CAPTURED":
            out["result"] = "CAPTURED"
            return out
        if outcome.get("status") == "BREAKOUT_BAG_CONSUMED_CONFIRMED":
            bag_open = True
            battle_turn += 1
            continue
        raise bagmod.BagThrowError(
            "D25 unrecognized Horde Ball outcome; no retry authorized: " + str(outcome.get("status"))
        )
    raise bagmod.BagThrowError(f"D25 reached bounded {bagmod.MAX_AUTO_CATCH_THROWS}-Ball limit")



def live_nonshiny_horde_auto_attack_test(
    core,
    br,
    opponent_set: dict,
    *,
    lead_pk6=None,
    move_db=None,
    base_dir=None,
    check_stop,
    log,
    test_visual: str = "FAR_RIGHT",
) -> dict:
    """Execute exactly one production-style auto-selected attack on a proven non-shiny Horde.

    This is a one-shot hardware validator for the D25 move selector.  It never
    runs when any Horde opponent is shiny.  It reuses the exact production
    COMMAND/MOVE/target-mask authority and chooses the attack from the live
    lead PK6 + bundled ORAS move metadata.  After one committed attack resolves
    back to the command phase, control returns to the normal hunt loop, which
    can then escape the Horde through its existing causal Run path.

    No game RAM writes are performed.
    """
    check_stop()
    occupied = list(opponent_set.get("occupied") or [])
    if not opponent_set.get("valid") or opponent_set.get("classification") != "HORDE" or len(occupied) != 5:
        raise RuntimeError("Horde Auto-Attack TEST requires one valid five-opponent Horde snapshot")
    shiny_slots = [
        int(rec.get("slot")) for rec in occupied
        if bool((rec.get("pk6") or {}).get("is_shiny"))
    ]
    if shiny_slots:
        raise RuntimeError(
            "Horde Auto-Attack TEST refuses every attack because a real shiny is present: "
            + str(shiny_slots)
        )
    if test_visual not in d18.XY_BY_VISUAL:
        raise RuntimeError(f"Horde Auto-Attack TEST has invalid target visual {test_visual!r}")
    if not isinstance(lead_pk6, dict) or not lead_pk6:
        raise RuntimeError("Horde Auto-Attack TEST has no authoritative lead PK6")
    if not (lead_pk6.get("valid") and lead_pk6.get("checksum_valid")):
        raise RuntimeError("Horde Auto-Attack TEST lead PK6 is not valid/checksum-valid")
    if base_dir is None:
        raise RuntimeError("Horde Auto-Attack TEST has no touch-profile base directory")

    available_move_slots = {
        slot for slot in range(1, 5)
        if get_battle_move_xy(base_dir, slot) is not None
    }
    plan = choose_safe_attack(dict(lead_pk6), dict(move_db or {}), available_move_slots)
    selected = plan.get("selected")
    if not selected:
        detail = ", ".join(
            f"slot{r.get('move_slot')} {r.get('name')} PP{r.get('pp')}: {r.get('reason')}"
            + ("" if r.get("touch_profile_available") else " / touch unavailable")
            for r in plan.get("moves", [])
        )
        raise RuntimeError("Horde Auto-Attack TEST found no authorized damaging move: " + detail)

    # v0p43EG: execute the actual live policy choice. Slot 2 is already hardware-proven
    # end-to-end; slots 1/3/4 use button centers calibrated from the real 320x240
    # ORAS MOVE screen captured on hardware in v0p43EF. This remains a one-shot
    # NON-SHINY validator: any real shiny blocks all attack input before this point.
    selected = dict(selected)

    move_slot = int(selected.get("move_slot") or 0)
    move_xy = get_battle_move_xy(base_dir, move_slot)
    if move_xy is None:
        raise RuntimeError("Horde Auto-Attack TEST selected move has no touch coordinate")
    move_touch = d18.encode_touch_xy(*move_xy)
    target_slot = int(d18.SLOT_BY_VISUAL[test_visual])
    target_xy = tuple(d18.XY_BY_VISUAL[test_visual])
    expected_mask = int(d18.MASK_BY_VISUAL[test_visual])

    report = {
        "authority": "D25_PRODUCTION_MOVE_SELECTOR_ONE_ATTACK_NONSHINY_HORDE_TEST",
        "ram_writes": False,
        "test_mode": True,
        "actual_shiny_slots": [],
        "lead_species": lead_pk6.get("species_name"),
        "lead_moves": list(lead_pk6.get("moves") or []),
        "lead_move_pp": list(lead_pk6.get("move_pp") or []),
        "move_plan": plan,
        "test_selection_policy": "LIVE_POLICY_WITH_HARDWARE_CALIBRATED_MOVE_BUTTONS",
        "selected_attack": {
            "move_id": int(selected.get("move_id") or 0),
            "name": selected.get("name"),
            "move_slot": move_slot,
            "pp": int(selected.get("pp") or 0),
            "screen_xy": list(move_xy),
            "reason": selected.get("reason"),
        },
        "test_target": {
            "visual": VISUAL_DISPLAY.get(test_visual, test_visual),
            "visual_internal": test_visual,
            "pk6_slot": target_slot,
            "screen_xy": list(target_xy),
            "expected_target_mask": d18.hx(expected_mask),
        },
        "result": None,
    }

    ready = _wait_initial_five_alive_gate(
        core, br, timeout=35.0, check_stop=check_stop, log=log
    )
    report["initial_five_alive_gate"] = ready
    if not ready.get("proven"):
        raise RuntimeError("Horde Auto-Attack TEST five-alive state6/mask100 did not become stable")

    owner, owner_rec = d18.discover_owner(core, br)
    report["owner"] = owner_rec
    exact_command = _wait_initial_exact_command(
        core, br, owner, timeout=35.0, check_stop=check_stop, log=log
    )
    report["initial_exact_command_phase"] = exact_command
    if not exact_command.get("proven"):
        raise RuntimeError("Horde Auto-Attack TEST timed out waiting for exact COMMAND phase")

    log(
        "HORDE AUTO-ATTACK TEST PLAN: "
        f"lead={lead_pk6.get('species_name')} choice={selected.get('name')} "
        f"ID={selected.get('move_id')} slot={move_slot} PP={selected.get('pp')} "
        f"xy={move_xy} target={VISUAL_DISPLAY.get(test_visual, test_visual)}; "
        "zero shiny opponents proven; move XY from v0p43EF hardware framebuffer calibration"
    )
    for row in plan.get("moves", []):
        log(
            "HORDE AUTO-ATTACK TEST MOVE CHECK: "
            f"slot{row.get('move_slot')} {row.get('name')} ID={row.get('move_id')} "
            f"PP={row.get('pp')} safe={row.get('safe')} "
            f"touch={row.get('touch_profile_available')} reason={row.get('reason')}"
        )

    command_pair = d18.phase_pair(br, owner, "COMMAND")
    check_stop()
    fight_ev = d18.send_touch(br, d18.FIGHT_TOUCH, "LIVE_AUTO_ATTACK_TEST_FIGHT")
    report["fight_event"] = fight_ev
    _sleep_checked(0.55, check_stop)
    move_pair = d18.phase_pair(br, owner, "MOVE")
    fp = d18.make_phase_fingerprint(command_pair, move_pair)
    report["phase_fingerprint"] = {
        "owner_window": d18.OWNER_WINDOW,
        "discriminator_count": len(fp["offsets"]),
        "offsets": [d18.hx(o) for o in fp["offsets"]],
        "phase_state_offset": d18.hx(fp["phase_state_offset"]),
        "command_state": fp["command_state"],
        "move_state": fp["move_state"],
    }
    move_score = d18.phase_score(br, owner, fp)
    report["phase_after_fight"] = move_score
    if not d18.strong_phase(move_score, "MOVE"):
        raise RuntimeError("Horde Auto-Attack TEST FIGHT did not reach exact MOVE phase")

    check_stop()
    move_ev = d18.send_touch(
        br, move_touch, f"LIVE_AUTO_ATTACK_TEST_MOVE_SLOT_{move_slot}"
    )
    report["move_event"] = move_ev
    log(
        "HORDE AUTO-ATTACK TEST MOVE TOUCH: "
        f"slot={move_slot} xy={move_xy} firmware_completed={bool(move_ev.get('completed'))}"
    )
    selector = d18.wait_target_selector(core, br, owner, alive=5, timeout=4.0)
    report["target_selector"] = selector
    if not selector.get("proven"):
        log(
            "HORDE AUTO-ATTACK TEST TARGET SELECTOR MISS: "
            f"slot={move_slot} xy={move_xy} last={selector.get('last')} "
            f"samples={len(selector.get('samples') or [])}"
        )
        raise RuntimeError(
            f"Horde Auto-Attack TEST selected move slot {move_slot} did not reach target selector"
        )

    before_mask = br.u32(owner + d18.TARGET_MASK_OFF) & 0xFFFFFFFF
    check_stop()
    target_ev = d18.send_touch(
        br,
        d18.encode_touch_xy(*target_xy),
        f"LIVE_AUTO_ATTACK_TEST_TARGET_{test_visual}",
    )
    report["target_event"] = target_ev
    report["selector_mask_before_touch"] = d18.hx(before_mask)
    confirmation = d18.observe_direct_target(core, br, owner, expected_mask)
    report["target_confirmation"] = confirmation
    if confirmation.get("status") != "DIRECT_TOUCH_TARGET_AND_CONFIRM_PROVEN":
        raise RuntimeError(
            "Horde Auto-Attack TEST exact target-mask confirmation failed: "
            + str(confirmation.get("status"))
        )

    resolution = _wait_attack_resolution_live(
        core, br, owner, fp, 5, timeout=45.0, check_stop=check_stop
    )
    report["attack_resolution"] = resolution
    if not resolution.get("proven"):
        raise RuntimeError(
            "Horde Auto-Attack TEST attack did not resolve back to a proven command phase: "
            + str(resolution.get("status"))
        )

    report["result"] = (
        "PASS_MOVE_SELECTED_TARGETED_AND_KO_PROVEN"
        if resolution.get("status") == "EXACTLY_ONE_ALIVE_DECREMENT_PROVEN"
        else "PASS_MOVE_SELECTED_TARGETED_AND_COMMAND_RETURN_PROVEN"
    )
    log(
        "HORDE AUTO-ATTACK TEST PASS: "
        f"{selected.get('name')} from move slot {move_slot} executed on "
        f"{VISUAL_DISPLAY.get(test_visual, test_visual)}; "
        f"resolution={resolution.get('status')}"
    )
    return report

def live_auto_capture_single_shiny_horde(core, br, opponent_set: dict, shiny_payload: dict, *, ball_override="best", lead_pk6=None, move_db=None, base_dir=None, check_stop, log) -> dict:
    """Run the exact D18f reducer with the live shiny slot as the protected slot."""
    check_stop()
    occupied = list(opponent_set.get("occupied") or [])
    if not opponent_set.get("valid") or opponent_set.get("classification") != "HORDE" or len(occupied) != 5:
        raise RuntimeError("D25 requires one valid five-opponent Horde snapshot")

    shiny_slots = [int(rec.get("slot")) for rec in occupied if bool((rec.get("pk6") or {}).get("is_shiny"))]
    if len(shiny_slots) != 1:
        raise RuntimeError(f"D25 requires exactly one shiny Horde opponent; found {len(shiny_slots)}")
    protected_slot = int(shiny_slots[0])
    if int(shiny_payload.get("horde_slot")) != protected_slot:
        raise RuntimeError("D25 live shiny payload disagrees with RAM shiny slot")

    visual_by_slot = {slot: visual for visual, slot in d18.SLOT_BY_VISUAL.items()}
    protected_visual = visual_by_slot[protected_slot]
    protected_display = VISUAL_DISPLAY.get(protected_visual, protected_visual)
    protected_mask = int(d18.MASK_BY_VISUAL[protected_visual])
    protected_rec = next(rec for rec in occupied if int(rec.get("slot")) == protected_slot)
    protected_address = protected_rec.get("address")
    ko_order = [v for v in VISUAL_ORDER if int(d18.SLOT_BY_VISUAL[v]) != protected_slot]

    # D25: choose the lead's attack from its RAM-parsed PK6 rather than
    # assuming one hard-coded move slot. Only single-target damaging moves with
    # PP and a calibrated touch coordinate can be authorized. Status and every non-selected-opponent target class are rejected. Single-target
    # multi-hit attacks are allowed; spread/random/all-foe attacks are not.
    if not isinstance(lead_pk6, dict) or not lead_pk6:
        raise RuntimeError("D25 Horde reducer has no preflight lead PK6 move authority")
    move_db = dict(move_db or {})
    if base_dir is None:
        raise RuntimeError("D25 Horde reducer has no touch-profile base directory")
    available_move_slots = {
        slot for slot in range(1, 5)
        if get_battle_move_xy(base_dir, slot) is not None
    }
    # Keep a local PP ledger from the RAM-parsed lead PK6. Every committed
    # attack consumes one point here, so the reducer can move to another safe
    # attacking move when a low-PP move runs out instead of assuming one move
    # slot for all four KOs. No game RAM is written.
    remaining_pp = [int(x or 0) for x in list(lead_pk6.get("move_pp") or [])[:4]]
    while len(remaining_pp) < 4:
        remaining_pp.append(0)

    def select_attack():
        current = dict(lead_pk6)
        current["move_pp"] = list(remaining_pp)
        plan = choose_safe_attack(current, move_db, available_move_slots)
        selected = plan.get("selected")
        if not selected:
            detail = ", ".join(
                f"slot{r.get('move_slot')} {r.get('name')} PP{r.get('pp')}: {r.get('reason')}"
                + ("" if r.get("touch_profile_available") else " / touch unavailable")
                for r in plan.get("moves", [])
            )
            raise RuntimeError(
                "D25 no safe selected-opponent attack remains for the lead; " + detail
            )
        move_slot = int(selected["move_slot"])
        xy = get_battle_move_xy(base_dir, move_slot)
        if xy is None:
            raise RuntimeError("D25 selected attack lost its calibrated touch coordinate")
        return dict(selected), tuple(xy), d18.encode_touch_xy(*xy), plan

    selected_move, selected_move_xy, selected_move_touch, move_plan = select_attack()

    report = {
        "authority": "EXACT_D18F_LIVE_GENERALISATION_DYNAMIC_PROTECTED_SLOT",
        "ram_writes": False,
        "live_hunt": True,
        "protected_shiny_slot": protected_slot,
        "protected_shiny_visual": protected_display,
        "protected_shiny_visual_internal": protected_visual,
        "protected_shiny_mask": d18.hx(protected_mask),
        "protected_shiny_address": protected_address,
        "ball_override": str(ball_override or "best"),
        "ko_order": [VISUAL_DISPLAY.get(v, v) for v in ko_order],
        "ko_turns": [],
        "ko_targets": [],
        "result": None,
        "lead_species": lead_pk6.get("species_name"),
        "lead_moves": list(lead_pk6.get("moves") or []),
        "lead_move_pp": list(lead_pk6.get("move_pp") or []),
        "move_plan": move_plan,
        "selected_attack": {
            "move_id": int(selected_move.get("move_id") or 0),
            "name": selected_move.get("name"),
            "move_slot": int(selected_move.get("move_slot") or 0),
            "pp_at_preflight": int(selected_move.get("pp") or 0),
            "screen_xy": list(selected_move_xy),
            "reason": selected_move.get("reason"),
        },
    }

    try:
        ready = _wait_initial_five_alive_gate(core, br, timeout=35.0, check_stop=check_stop, log=log)
        report["initial_five_alive_gate"] = ready
        if not ready.get("proven"):
            raise RuntimeError("D25 five-alive state6/mask100 did not become stable")

        owner, owner_rec = d18.discover_owner(core, br)
        report["owner"] = owner_rec
        exact_command = _wait_initial_exact_command(
            core, br, owner, timeout=35.0, check_stop=check_stop, log=log
        )
        report["initial_exact_command_phase"] = exact_command
        if not exact_command.get("proven"):
            raise RuntimeError("D25 timed out waiting for exact D18f owner+0x93 COMMAND byte 1")

        log(
            f"HORDE AUTO-CATCH: protected shiny = {protected_display} / RAM slot {protected_slot} / "
            f"{protected_address}; starting exact D18f reducer"
        )
        log(
            f"HORDE AUTO-BATTLE MOVE: {selected_move.get('name')} (ID {selected_move.get('move_id')}) "
            f"move slot {selected_move.get('move_slot')} xy={selected_move_xy} PP={selected_move.get('pp')} "
            "— RAM PK6 + single-target safety policy"
        )

        # Exact D18f startup calibration: sample COMMAND, touch FIGHT, wait 0.55s,
        # sample MOVE, require the hardware-proven 1/2 phase pair and >=4 stable discriminators.
        command_pair = d18.phase_pair(br, owner, "COMMAND")
        check_stop()
        fight_ev = d18.send_touch(br, d18.FIGHT_TOUCH, "LIVE_D18F_CALIBRATE_FIGHT_TOUCH")
        report["initial_fight_touch"] = fight_ev
        _sleep_checked(0.55, check_stop)
        move_pair = d18.phase_pair(br, owner, "MOVE")
        fp = d18.make_phase_fingerprint(command_pair, move_pair)
        report["phase_fingerprint"] = {
            "owner_window": d18.OWNER_WINDOW,
            "discriminator_count": len(fp["offsets"]),
            "offsets": [d18.hx(o) for o in fp["offsets"]],
            "command_values": fp["command_values"],
            "move_values": fp["move_values"],
            "phase_state_offset": d18.hx(fp["phase_state_offset"]),
            "command_state": fp["command_state"],
            "move_state": fp["move_state"],
            "authority": "EXACT_OWNER_PLUS_0x93_STATE_1_COMMAND_2_MOVE",
        }
        move_score = d18.phase_score(br, owner, fp)
        report["phase_after_fight"] = move_score
        if not d18.strong_phase(move_score, "MOVE"):
            raise RuntimeError("D25 FIGHT did not reach exact D18f MOVE phase")

        check_stop()
        move_ev = d18.send_touch(br, selected_move_touch, "LIVE_D19M_CALIBRATE_SAFE_MOVE_TOUCH")
        report["initial_move_touch"] = move_ev
        target_sel = d18.wait_target_selector(core, br, owner, alive=5, timeout=4.0)
        report["initial_target_selector"] = target_sel
        if not target_sel.get("proven"):
            raise RuntimeError("D25 initial move did not reach Horde target selector")

        alive = 5
        total_attack_turns = 0
        first_selector_ready = True
        eliminated_slots = []
        attempted_slots = []
        mask_proven_slots = []

        for target_index, visual in enumerate(ko_order, 1):
            slot = int(d18.SLOT_BY_VISUAL[visual])
            if slot == protected_slot:
                raise RuntimeError("D25 internal safety violation: protected shiny entered KO order")
            expected_mask = int(d18.MASK_BY_VISUAL[visual])
            xy = d18.XY_BY_VISUAL[visual]
            display = VISUAL_DISPLAY.get(visual, visual)
            target_record = {
                "target_index": target_index,
                "visual": display,
                "visual_internal": visual,
                "pk6_slot": slot,
                "screen_xy": list(xy),
                "expected_target_mask": d18.hx(expected_mask),
                "attempts": [],
            }
            report["ko_targets"].append(target_record)
            ko_proven = False

            for attack_attempt in range(1, d18.MAX_ATTACK_ATTEMPTS_PER_TARGET + 1):
                check_stop()
                if first_selector_ready:
                    command = None
                    evf = fight_ev
                    move_phase = None
                    evm = move_ev
                    sel = target_sel
                    first_selector_ready = False
                else:
                    command = _wait_phase_live(
                        core, br, owner, fp, "COMMAND", alive,
                        timeout=45.0, check_stop=check_stop,
                    )
                    if not command.get("proven"):
                        raise RuntimeError(f"D25 exact COMMAND return not proven before {visual} attempt {attack_attempt}")
                    # Re-evaluate the four RAM-parsed moves against our local PP
                    # ledger before every new attack. This permits any safe
                    # calibrated attacking slot and automatically changes moves
                    # when one is exhausted.
                    selected_move, selected_move_xy, selected_move_touch, move_plan_now = select_attack()
                    evf = d18.send_touch(br, d18.FIGHT_TOUCH, f"LIVE_D18F_{visual}_{attack_attempt}_FIGHT")
                    move_phase = _wait_phase_live(
                        core, br, owner, fp, "MOVE", alive,
                        timeout=4.0, check_stop=check_stop,
                    )
                    if not move_phase.get("proven"):
                        raise RuntimeError(f"D25 FIGHT did not reach exact MOVE phase for {visual}")
                    evm = d18.send_touch(
                        br, selected_move_touch,
                        f"LIVE_D19M_{visual}_{attack_attempt}_MOVE_SLOT_{selected_move.get('move_slot')}"
                    )
                    sel = d18.wait_target_selector(core, br, owner, alive=alive, timeout=4.0)
                    if not sel.get("proven"):
                        raise RuntimeError(f"D25 MOVE did not reach target selector for {visual}")

                before_mask = br.u32(owner + d18.TARGET_MASK_OFF) & 0xFFFFFFFF
                attempted_slots.append(slot)
                log(
                    f"HORDE AUTO-CATCH: target {target_index}/4 {display} / RAM slot {slot}, "
                    f"attempt {attack_attempt}; move={selected_move.get('name')} "
                    f"slot{selected_move.get('move_slot')} PP_before={remaining_pp[int(selected_move.get('move_slot'))-1]}; "
                    f"protected shiny={protected_display}"
                )
                check_stop()
                target_ev = d18.send_touch(
                    br, d18.encode_touch_xy(*xy),
                    f"LIVE_D18F_TARGET_{visual}_{attack_attempt}",
                )
                total_attack_turns += 1
                confirmation = d18.observe_direct_target(core, br, owner, expected_mask)
                if confirmation.get("status") != "DIRECT_TOUCH_TARGET_AND_CONFIRM_PROVEN":
                    raise RuntimeError(f"D25 exact target mask confirmation failed for {visual}")
                mask_proven_slots.append(slot)
                used_move_slot = int(selected_move.get("move_slot") or 0)
                if not (1 <= used_move_slot <= 4):
                    raise RuntimeError("D25 selected move slot left valid range after target confirmation")
                pp_before = int(remaining_pp[used_move_slot - 1])
                if pp_before <= 0:
                    raise RuntimeError("D25 local PP ledger reached zero before a committed attack")
                remaining_pp[used_move_slot - 1] = pp_before - 1

                resolution = _wait_attack_resolution_live(
                    core, br, owner, fp, alive,
                    timeout=45.0, check_stop=check_stop,
                )
                rec = {
                    "target_index": target_index,
                    "attack_attempt": attack_attempt,
                    "battle_turn_index": total_attack_turns,
                    "visual": display,
                    "visual_internal": visual,
                    "pk6_slot": slot,
                    "screen_xy": list(xy),
                    "expected_target_mask": d18.hx(expected_mask),
                    "selector_mask_before_touch": d18.hx(before_mask),
                    "command_wait": command,
                    "fight_event": evf,
                    "move_phase_wait": move_phase,
                    "move_event": evm,
                    "move_used": {
                        "move_id": int(selected_move.get("move_id") or 0),
                        "name": selected_move.get("name"),
                        "move_slot": int(selected_move.get("move_slot") or 0),
                        "screen_xy": list(selected_move_xy),
                        "pp_before": pp_before,
                        "pp_after_local": int(remaining_pp[used_move_slot - 1]),
                    },
                    "target_selector": sel,
                    "target_event": target_ev,
                    "target_confirmation": confirmation,
                    "attack_resolution": resolution,
                }
                target_record["attempts"].append(rec)
                report["ko_turns"].append(rec)

                if not resolution.get("proven"):
                    raise RuntimeError(f"D25 attack resolution not proven for {visual}: {resolution.get('status')}")
                if resolution.get("status") == "EXACTLY_ONE_ALIVE_DECREMENT_PROVEN":
                    alive -= 1
                    eliminated_slots.append(slot)
                    target_record["ko_proven"] = True
                    target_record["attempts_to_ko"] = attack_attempt
                    target_record["alive_after"] = alive
                    ko_proven = True
                    break
                if resolution.get("status") == "TARGET_SURVIVED_OR_MOVE_MISSED_COMMAND_RETURN_PROVEN":
                    log(f"HORDE AUTO-CATCH: {display} survived/missed; exact COMMAND return proven, retrying same target only")
                    continue
                raise RuntimeError(f"D25 unexpected attack resolution {resolution.get('status')}")

            if not ko_proven:
                raise RuntimeError(f"D25 {visual} not KO'd after bounded retries")

        report["lead_move_pp_after_local_attacks"] = list(remaining_pp)
        required = sorted(set(range(5)) - {protected_slot})
        exclusion = bool(
            protected_slot not in attempted_slots
            and sorted(set(attempted_slots)) == required
            and sorted(set(mask_proven_slots)) == required
            and sorted(set(eliminated_slots)) == required
            and alive == 1
        )
        report["elimination_survivor_invariant"] = {
            "proven": exclusion,
            "authority": "D16 exact target masks + D10 exact one-step alive decrements + exhaustive non-protected elimination",
            "required_eliminated_pk6_slots": required,
            "observed_eliminated_pk6_slots": sorted(set(eliminated_slots)),
            "target_mask_proven_pk6_slots": sorted(set(mask_proven_slots)),
            "attempted_pk6_slots": sorted(set(attempted_slots)),
            "protected_pk6_slot": protected_slot,
            "protected_pk6_slot_never_targeted": protected_slot not in attempted_slots,
            "alive_after": alive,
        }
        if not exclusion:
            raise RuntimeError("D25 exhaustive protected-survivor invariant failed; no Ball authorized")

        one_command = _wait_phase_live(
            core, br, owner, fp, "COMMAND", alive=1,
            timeout=45.0, check_stop=check_stop,
        )
        report["one_alive_command_phase"] = one_command
        if not one_command.get("proven"):
            raise RuntimeError("D25 one-alive exact COMMAND phase not proven")

        one_gate = prove_one_alive(core, br, timeout=8.0)
        report["one_alive_gate"] = one_gate
        report["survivor_proof"] = {
            "proven": bool(one_gate.get("proven")) and exclusion,
            "expected_visual": protected_display,
            "expected_pk6_slot": protected_slot,
            "one_alive_gate": one_gate,
            "elimination_invariant": report["elimination_survivor_invariant"],
            "authority": "EXHAUSTIVE_EXCLUSION_NO_FINAL_SELECTOR_IDENTITY_ASSUMPTION",
        }
        if not report["survivor_proof"]["proven"]:
            raise RuntimeError("D25 protected shiny sole-survivor proof incomplete; no Ball authorized")

        view = int(one_gate["view_u32"])
        outer = br.u32(core.OUTER_PTR_ADDR)
        found = discover_unique_owner(core, br, view, outer)
        owner_rec2 = found["chosen"]
        report["capture_owner"] = owner_rec2

        slot_rec = next(rec for rec in occupied if int(rec.get("slot")) == protected_slot)
        target = dict(slot_rec.get("pk6") or {})
        species = int(target.get("species") or 0)
        target.update({
            "species_name": shiny_payload.get("species_name") or SPECIES_NAMES.get(species, f"Species #{species}"),
            "pokemon_pid": shiny_payload.get("pokemon_pid") or target.get("pid"),
            "stored_pk6_address": slot_rec.get("address"),
        })
        first_ball_turn = total_attack_turns + 1
        report["total_attack_turns_before_ball"] = total_attack_turns
        report["first_ball_battle_turn_index"] = first_ball_turn
        report["capture_target_stored_pk6_address"] = slot_rec.get("address")

        capture = _capture_one_alive_live(
            core, br, target, owner_rec2, first_ball_turn,
            ball_override=ball_override, check_stop=check_stop, log=log,
        )
        report["capture"] = capture
        report["throw_count"] = capture.get("throw_count")
        report["throws"] = list(capture.get("throws") or [])
        if capture.get("result") != "CAPTURED":
            raise RuntimeError("D25 Horde capture did not reach CAPTURED")

        report["result"] = "CAPTURED"
        return report
    finally:
        try:
            bagmod.bind_runtime_owner(
                NORMAL_BATTLE_OWNER,
                locator="normal single-battle owner restored after D25 Horde path",
            )
        except Exception:
            pass
