from __future__ import annotations

"""Hardware validator for post-Ball battle-menu input readiness.

v0p43AO proved that the 0x081FB518 token can change while the first Ball
animation is still running, so it is explicitly NOT outcome authority here.

This validator refuses an initial shiny, forbids Master Ball, forces a weak
1x Ball on throw 1, then waits for a *game-consumed* BAG touch before every
subsequent Ball can be selected. Firmware ACK alone is insufficient. The exact
embedded Bag state transition to state 1 is the readiness handshake.

This is still diagnostic-only and is not wired into the live shiny worker.
"""

import json
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pokebot.wild.battle_bag_throw as bagmod
from pokebot.wild.battle_bag_throw import (
    POST_CAPTURE_SENTINEL,
    POST_CAPTURE_CAPTURE_FLOWS,
    throw_one_best_ball,
    clear_post_capture_screens,
)
from pokebot.wild.post_capture_validator import clear_post_capture_validator
from pokebot.wild.best_ball import (
    choose_best_ball,
    plan_path as plan_ball_path,
    read_balls_state,
    wait_for_position as wait_for_ball_position,
)
from pokebot.wild.validated_loader import load_walk_v0p23
from qt_ui.appdata_store import get_profile_paths
from qt_ui.settings_store import load_settings

AS_TITLE_ID = 0x000400000011C500
EPOCH_ADDR = 0x081FB518
EPOCH_SIZE = 16
EPOCH_PREVIOUS_ADDR = 0x081FB528
POLL_SECONDS = 0.05
INACTIVE_STABLE_SAMPLES = 3
OUTCOME_TIMEOUT_SECONDS = 120.0
MAX_TEST_THROWS = 10
# Diagnostic safety arm only: this does NOT prove breakout. It merely prevents
# probing the BAG during the earliest part of the Ball animation. Authority is
# granted only if the game consumes a BAG touch and the embedded Bag reaches 1.
BAG_PROBE_ARM_SECONDS = 7.5
BAG_PROBE_RETRY_GAP_SECONDS = 1.5
BAG_PROBE_ACCEPT_SECONDS = 1.25
BAG_PROBE_MAX_ATTEMPTS = 12


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def hx(v: int | None) -> str | None:
    if v is None:
        return None
    return f"0x{int(v) & 0xFFFFFFFF:08X}"


def _forced_weak_probe_decision_factory(original_choose):
    preferred_ids = (4, 12, 14, 11)  # Poke, Premier, Heal, Luxury

    def forced(entries, *, throw_index, target, method_key="", environment=""):
        base = original_choose(
            entries,
            throw_index=throw_index,
            target=target,
            method_key=method_key,
            environment=environment,
        )
        usable = [
            dict(e) for e in entries
            if int(e.get("quantity") or 0) > 0 and int(e.get("item_id") or 0) != 1
        ]
        chosen = None
        for item_id in preferred_ids:
            chosen = next((e for e in usable if int(e.get("item_id") or 0) == item_id), None)
            if chosen:
                break
        if chosen is None:
            chosen = usable[0] if usable else None
        if chosen is None:
            raise RuntimeError("no positive-quantity non-Master Ball available")
        chosen.update({
            "eligible": True,
            "multiplier": 1.0,
            "reason": "capture_epoch_validator_forced_weak_1x_ball",
        })
        base["chosen"] = chosen
        base["probe_override"] = {
            "mode": "FORCE_WEAK_FIRST_BALL",
            "forced_item_id": int(chosen["item_id"]),
            "forced_ball_name": chosen.get("ball_name"),
            "master_ball_forbidden": True,
        }
        return base

    return forced


def _decode_target(core, br, tid: int, sid: int) -> dict:
    raw = br.read(core.WILD_PK6_ADDR, core.PK6_STORED_SIZE)
    p = core.decode_stored_pk6(raw, tid, sid)
    return {
        "valid": bool(p.get("valid")),
        "species": p.get("species"),
        "pid": p.get("pid"),
        "identity": p.get("identity"),
        "is_shiny": bool(p.get("is_shiny")),
        "shiny_xor": p.get("shiny_xor"),
        "reason": p.get("reason"),
    }


def _same_target(core, br, tid: int, sid: int, target: dict) -> dict:
    out = _decode_target(core, br, tid, sid)
    out["same_target"] = bool(
        out.get("valid")
        and int(out.get("species") or 0) == int(target.get("species") or 0)
        and str(out.get("pid") or "").upper() == str(target.get("pid") or "").upper()
    )
    return out


def _sample(core, br, tid: int, sid: int, target: dict, pre_flow: int, pre_epoch: bytes) -> dict:
    battle = br.u32(core.BATTLE_ADDR)
    flow_addr = getattr(core, "FLOW_ADDR", 0x081FB390)
    flow = br.u32(flow_addr)
    epoch = br.read(EPOCH_ADDR, EPOCH_SIZE)
    previous = br.read(EPOCH_PREVIOUS_ADDR, EPOCH_SIZE)
    if battle == core.BATTLE_ACTIVE:
        try:
            gate = dict(core.read_gate(br))
        except Exception as exc:
            gate = {"gate": False, "error": f"{type(exc).__name__}: {exc}"}
    else:
        gate = {"gate": False}
    return {
        "time": now_iso(),
        "battle": hx(battle),
        "battle_u32": battle,
        "flow": hx(flow),
        "flow_u32": flow,
        "flow_matches_pre_throw": flow == int(pre_flow),
        "command_gate": bool(gate.get("gate")),
        "gate_state": gate.get("state"),
        "gate_mask": gate.get("mask"),
        "epoch_hex": epoch.hex(),
        "epoch_previous_hex": previous.hex(),
        "epoch_changed_from_pre_throw": epoch != pre_epoch,
        "epoch_equals_previous": epoch == previous,
        "target": _same_target(core, br, tid, sid, target),
    }


def _capture_authority(core, br) -> tuple[str | None, str | None]:
    battle = br.u32(core.BATTLE_ADDR)
    flow = br.u32(getattr(core, "FLOW_ADDR", 0x081FB390))
    if flow in POST_CAPTURE_CAPTURE_FLOWS:
        return "CAPTURED", "POST_CAPTURE_FLOW"
    if battle == POST_CAPTURE_SENTINEL:
        return "CAPTURED", "POST_CAPTURE_SENTINEL"
    return None, None


def _wait_for_bag_consumption(core, br, tid: int, sid: int, target: dict,
                              pre_flow: int, pre_epoch: bytes, log) -> dict:
    """Wait until capture wins or the game itself proves battle-menu readiness.

    v0p43AO showed that epoch change + stale command gate can happen while the
    Ball animation is still active. We therefore never authorize a second Ball
    from either signal. After a conservative diagnostic arm period, we issue
    one BAG touch at a time. Only embedded Bag state 1 proves the game consumed
    that touch. Ignored touches simply re-arm; they do not count as breakout.
    """
    started = time.monotonic()
    deadline = started + OUTCOME_TIMEOUT_SECONDS
    samples = []
    probe_attempts = []
    inactive_stable = 0
    next_probe = started + BAG_PROBE_ARM_SECONDS
    next_progress = 10.0

    while time.monotonic() < deadline:
        status, authority = _capture_authority(core, br)
        if status:
            elapsed = time.monotonic() - started
            log(f"CAPTURE AUTHORITY: {authority}")
            return {
                "status": status, "authority": authority,
                "elapsed": round(elapsed, 3),
                "samples": samples, "bag_probe_attempts": probe_attempts,
            }

        s = _sample(core, br, tid, sid, target, pre_flow, pre_epoch)
        elapsed = time.monotonic() - started
        s["elapsed"] = round(elapsed, 3)
        samples.append(s)
        battle = int(s["battle_u32"])

        if battle == core.BATTLE_INACTIVE:
            inactive_stable += 1
            if inactive_stable >= INACTIVE_STABLE_SAMPLES:
                log("CAPTURE AUTHORITY: battle stably inactive")
                return {
                    "status": "CAPTURED", "authority": "BATTLE_INACTIVE_STABLE",
                    "elapsed": round(elapsed, 3),
                    "samples": samples, "bag_probe_attempts": probe_attempts,
                }
        else:
            inactive_stable = 0

        if len(probe_attempts) < BAG_PROBE_MAX_ATTEMPTS and time.monotonic() >= next_probe:
            target_now = s.get("target") or {}
            # BAG touch is diagnostic-only and is sent only while the original
            # battle object/target/flow still exist. It is never sufficient by
            # itself; Bag state 1 must appear.
            if (
                battle == core.BATTLE_ACTIVE
                and s.get("flow_matches_pre_throw")
                and target_now.get("same_target")
                and s.get("command_gate")
            ):
                attempt_no = len(probe_attempts) + 1
                before = dict(s)
                log(
                    f"READINESS PROBE {attempt_no}: sending one BAG touch; "
                    "only RAM Bag state 1 can authorize Ball 2"
                )
                event = bagmod._touch(
                    br, bagmod.BAG_TOUCH_STATE,
                    f"BREAKOUT_READINESS_BAG_PROBE_{attempt_no}",
                )
                accept_started = time.monotonic()
                accept_samples = []
                consumed = None
                capture_during_probe = None
                while time.monotonic() - accept_started < BAG_PROBE_ACCEPT_SECONDS:
                    status2, authority2 = _capture_authority(core, br)
                    if status2:
                        capture_during_probe = authority2
                        break
                    try:
                        cur = bagmod._cursor(br)
                    except Exception as exc:
                        cur = {"error": f"{type(exc).__name__}: {exc}"}
                    cur = dict(cur)
                    cur["elapsed"] = round(time.monotonic() - accept_started, 3)
                    accept_samples.append(cur)
                    if cur.get("valid") and int(cur.get("bag_state") or 0) == 1:
                        consumed = cur
                        break
                    time.sleep(0.08)
                probe = {
                    "attempt": attempt_no,
                    "at_elapsed": round(elapsed, 3),
                    "before": before,
                    "event": event,
                    "accept_samples": accept_samples,
                    "consumed": bool(consumed),
                    "consumed_state": consumed,
                    "capture_during_probe": capture_during_probe,
                }
                probe_attempts.append(probe)

                if capture_during_probe:
                    log(f"CAPTURE AUTHORITY appeared during BAG probe: {capture_during_probe}")
                    return {
                        "status": "CAPTURED", "authority": capture_during_probe,
                        "elapsed": round(time.monotonic() - started, 3),
                        "samples": samples, "bag_probe_attempts": probe_attempts,
                    }
                if consumed:
                    log(
                        "BREAKOUT INPUT-READINESS PROVEN: the game consumed BAG "
                        "touch and embedded Bag state reached 1"
                    )
                    return {
                        "status": "BREAKOUT_BAG_CONSUMED_CONFIRMED",
                        "authority": "GAME_CONSUMED_BAG_TOUCH_STATE_1",
                        "elapsed": round(time.monotonic() - started, 3),
                        "samples": samples, "bag_probe_attempts": probe_attempts,
                        "bag_open_state": consumed,
                    }
                log(
                    "BAG touch was acknowledged by firmware but not consumed by "
                    "the game; still not ready. No Ball authorized."
                )
                next_probe = time.monotonic() + BAG_PROBE_RETRY_GAP_SECONDS
            else:
                next_probe = time.monotonic() + 0.25

        if elapsed >= next_progress:
            log(
                f"waiting for capture or consumed-BAG readiness "
                f"({elapsed:.1f}/{OUTCOME_TIMEOUT_SECONDS:.0f}s); "
                f"epoch_changed={s['epoch_changed_from_pre_throw']} "
                f"gate={s['command_gate']} probes={len(probe_attempts)}"
            )
            next_progress += 10.0
        time.sleep(POLL_SECONDS)

    return {
        "status": "TIMEOUT_NO_AUTHORITY", "authority": None,
        "elapsed": round(time.monotonic() - started, 3),
        "samples": samples, "bag_probe_attempts": probe_attempts,
    }


def _throw_best_ball_from_open_bag(core, br, *, target, throw_index, check_stop, log) -> dict:
    """Continue from hardware-proven Bag state 1 to one verified Ball throw."""
    report = {
        "authority": "GAME_CONSUMED_BAG_TOUCH_STATE_1 + RAM Balls-pocket selection",
        "ram_writes": False, "throw_index": int(throw_index),
        "events": [], "navigation": [],
    }
    cur = bagmod._cursor(br)
    if not (cur.get("valid") and int(cur.get("bag_state") or 0) == 1):
        raise bagmod.BagThrowError(
            f"open-Bag handoff lost state 1 before Ball selection: {cur}"
        )
    report["bag_open_state"] = cur
    list_ready, attempts = bagmod._open_balls_ram_confirmed(
        br, check_stop=check_stop, log=log
    )
    report["list_ready"] = list_ready
    report["balls_open_attempts"] = attempts
    report["events"].extend(a["event"] for a in attempts)

    state = read_balls_state(br, bagmod._cursor)
    if state.get("selected") is None:
        raise bagmod.BagThrowError("Balls pocket opened on empty cell")
    decision = choose_best_ball(
        state["entries"], throw_index=int(throw_index), target=target,
        method_key="", environment="",
    )
    chosen = dict(decision["chosen"])
    if int(chosen.get("item_id") or 0) == 1:
        raise bagmod.BagThrowError("Master Ball forbidden in readiness validator")
    report["start_balls_state"] = state
    report["decision"] = decision

    # Hardware evidence from v0p43AQ showed a single acknowledged D-pad pulse can
    # occasionally move farther than the one logical grid edge we requested.
    # Never keep waiting for the predicted cell. After every pulse, read the
    # actual RAM cursor and re-plan from that proven location to the target.
    initial_path = plan_ball_path(
        state["page"], state["local_slot"], int(chosen["page"]),
        int(chosen["slot"]), state["entry_count"],
    )
    report["planned_path"] = initial_path
    report["adaptive_replans"] = []
    hid_for_direction = {
        "RIGHT": bagmod.HID_RIGHT, "LEFT": bagmod.HID_LEFT,
        "UP": bagmod.HID_UP, "DOWN": bagmod.HID_DOWN,
    }
    current = state
    nav_step = 0
    max_nav_pulses = 12
    while (
        int(current["page"]) != int(chosen["page"])
        or int(current["local_slot"]) != int(chosen["slot"])
    ):
        check_stop()
        if nav_step >= max_nav_pulses:
            raise bagmod.BagThrowError(
                f"adaptive Balls-pocket navigation exceeded {max_nav_pulses} pulses; "
                f"target=({chosen['page']},{chosen['slot']}) current="
                f"({current.get('page')},{current.get('local_slot')})"
            )
        path_now = plan_ball_path(
            int(current["page"]), int(current["local_slot"]),
            int(chosen["page"]), int(chosen["slot"]), int(current["entry_count"]),
        )
        if not path_now:
            break
        step = path_now[0]
        nav_step += 1
        direction = step["direction"]
        before_page = int(current["page"])
        before_slot = int(current["local_slot"])
        ev = bagmod._hid(
            br, hid_for_direction[direction],
            f"RETHROW_ADAPTIVE_NAV_{nav_step}_{direction}",
        )
        report["events"].append(ev)

        # Do not require the predicted next cell. Wait only for the cursor to
        # become RAM-stable after the pulse, then accept its actual location.
        settle_deadline = time.monotonic() + 2.5
        stable = 0
        last_key = None
        observed = None
        observed_samples = []
        while time.monotonic() < settle_deadline:
            check_stop()
            observed = read_balls_state(br, bagmod._cursor)
            key = (
                int(observed["page"]), int(observed["local_slot"]),
                int(observed["selected_index"]),
                (observed.get("selected") or {}).get("item_id"),
            )
            observed_samples.append({
                "page": int(observed["page"]),
                "slot": int(observed["local_slot"]),
                "selected_index": int(observed["selected_index"]),
                "item_id": (observed.get("selected") or {}).get("item_id"),
            })
            stable = stable + 1 if key == last_key else 1
            last_key = key
            if stable >= 2:
                break
            time.sleep(0.08)
        if observed is None or stable < 2:
            raise bagmod.BagThrowError(
                f"Balls-pocket cursor did not stabilize after {direction}; last={observed}"
            )

        current = observed
        actual_page = int(current["page"])
        actual_slot = int(current["local_slot"])
        report["navigation"].append({
            "step": nav_step,
            "direction": direction,
            "before_page": before_page,
            "before_slot": before_slot,
            "predicted_page": int(step["expected_page"]),
            "predicted_slot": int(step["expected_slot"]),
            "actual_page": actual_page,
            "actual_slot": actual_slot,
            "actual_selected": current.get("selected"),
            "samples": observed_samples,
        })
        if (
            actual_page != int(step["expected_page"])
            or actual_slot != int(step["expected_slot"])
        ):
            report["adaptive_replans"].append({
                "after_step": nav_step,
                "requested_direction": direction,
                "predicted_page": int(step["expected_page"]),
                "predicted_slot": int(step["expected_slot"]),
                "actual_page": actual_page,
                "actual_slot": actual_slot,
                "reason": "RAM_PROVEN_CURSOR_DID_NOT_LAND_ON_PREDICTED_CELL",
            })
            log(
                f"BALL CURSOR RECOVERY: {direction} expected "
                f"p{int(step['expected_page'])}/s{int(step['expected_slot'])} but RAM proved "
                f"p{actual_page}/s{actual_slot}; replanning from actual cursor"
            )
        if actual_page == before_page and actual_slot == before_slot:
            # A consumed HID command that leaves the cursor unchanged is not a
            # reason to guess another input immediately. The next loop re-plans
            # from the same proven state and remains bounded by max_nav_pulses.
            report["adaptive_replans"].append({
                "after_step": nav_step,
                "requested_direction": direction,
                "actual_page": actual_page,
                "actual_slot": actual_slot,
                "reason": "CURSOR_UNCHANGED_AFTER_ACKNOWLEDGED_PULSE",
            })

    final = read_balls_state(br, bagmod._cursor)
    selected = final.get("selected")
    if not selected or int(selected.get("item_id") or 0) != int(chosen["item_id"]):
        raise bagmod.BagThrowError(f"final Ball RAM proof mismatch wanted={chosen} got={selected}")
    if int(selected.get("item_id") or 0) == 1:
        raise bagmod.BagThrowError("Master Ball forbidden at final A gate")
    report["final_selection"] = {
        "page": final["page"], "slot": final["local_slot"],
        "index": final["selected_index"], "item_id": selected["item_id"],
        "ball_name": selected["ball_name"],
        "quantity_before_throw": selected["quantity"],
    }
    report["events"].append(bagmod._hid(br, bagmod.HID_A, "RETHROW_SELECT_VERIFIED_BALL"))
    selected_state, ss = bagmod._wait(
        br, lambda x: x.get("valid") and x.get("bag_state") == 3,
        bagmod.SELECT_WAIT_SECONDS, "rethrow Bag state 3", check_stop,
    )
    report["selected_use_state"] = selected_state
    report["selected_use_samples"] = ss
    report["events"].append(bagmod._hid(br, bagmod.HID_A, "RETHROW_CONFIRM_USE"))
    report["result"] = "BEST_BALL_THROWN_FROM_CONSUMED_BAG_HANDSHAKE"
    report["pre_throw_flow"] = br.u32(getattr(core, "FLOW_ADDR", 0x081FB390))
    report["pre_throw_flow_hex"] = hx(report["pre_throw_flow"])
    report["finished_monotonic"] = time.monotonic()
    log(
        f"RETHROW: {selected['ball_name'].title()} sent only after game-consumed BAG readiness"
    )
    return report

def save_report(profile, report: dict) -> tuple[Path, Path]:
    out_dir = profile.root / "support"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    js = out_dir / f"capture_multiball_completion_{stamp}.json"
    txt = out_dir / f"capture_multiball_completion_{stamp}.txt"
    js.write_text(json.dumps(report, indent=2), encoding="utf-8")
    lines = [
        "Pokebot3DS-CFW Multi-Ball Capture + Pokédex Ready Validator v0p43AS",
        f"Result: {report.get('result')}",
        f"Initial target: {report.get('initial_target')}",
        f"Throws: {len(report.get('throws') or [])}",
        "RAM writes: False",
        "Initial shiny allowed: False",
        "Master Ball allowed: False",
        "",
    ]
    for item in report.get("throws") or []:
        lines.append(
            f"Ball {item.get('throw_index')}: {((item.get('throw') or {}).get('final_selection') or {}).get('ball_name')} "
            f"pre_epoch={item.get('pre_epoch_hex')} pre_prev={item.get('pre_epoch_previous_hex')} "
            f"outcome={(item.get('outcome') or {}).get('status')} "
            f"authority={(item.get('outcome') or {}).get('authority')} "
            f"elapsed={(item.get('outcome') or {}).get('elapsed')}"
        )
    lines += ["", f"JSON: {js}", f"TXT: {txt}"]
    txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return js, txt


def main() -> int:
    profile = get_profile_paths()
    settings = load_settings(profile.settings_path)
    host = settings["three_ds_ip"]
    timeout = min(float(settings.get("bridge_timeout_s", 1.5)), 1.5)
    _, core, _ = load_walk_v0p23()
    br = core.Bridge(host, timeout=timeout)

    report = {
        "tool": "Pokebot3DS-CFW Multi-Ball Capture + Pokédex Ready Validator v0p43AS",
        "started": now_iso(),
        "ram_writes": False,
        "live_hunt_modified": False,
        "initial_shiny_allowed": False,
        "master_ball_allowed": False,
        "epoch": {
            "current_addr": hx(EPOCH_ADDR),
            "previous_addr": hx(EPOCH_PREVIOUS_ADDR),
            "size": EPOCH_SIZE,
            "status": "REJECTED_AS_OUTCOME_AUTHORITY_BY_v0p43AO_HARDWARE",
        },
        "throws": [],
    }

    print("=" * 78)
    print("Pokebot3DS-CFW MULTI-BALL + POKEDEX READY VALIDATOR v0p43AS")
    print("ORDINARY NON-SHINY WILD ONLY | NO RAM WRITES | MASTER BALL FORBIDDEN")
    print("=" * 78)
    print("Leave an ordinary wild battle on FIGHT / BAG / POKEMON / RUN.")
    print("Ball 1 is forced to a weak 1x Ball. Epoch/gate data are LOGGING ONLY.")
    print("Every rethrow is impossible until the GAME consumes a BAG touch (Bag state 1).")
    print("After each proven breakout it continues to the next Best Ball, up to 10 throws.")
    print("If capture succeeds it runs the guarded post-capture flow toward overworld.")
    print()

    try:
        gi = br.game_info()
        report["game_info"] = gi
        if int(gi.get("title_id", 0)) != AS_TITLE_ID:
            report["result"] = "REFUSED_WRONG_GAME"
            return 2
        caps = br.input_ping()
        report["input_caps"] = caps
        if not (caps.get("hid_pulse") and caps.get("touch_pulse")):
            report["result"] = "REFUSED_INPUT_CAPS"
            return 2
        br.release_all()

        ids = br.read(core.TRAINER_IDS_ADDR, 4)
        tid, sid = struct.unpack("<HH", ids)
        report["trainer_ids"] = {"tid": tid, "sid": sid}
        target = _decode_target(core, br, tid, sid)
        report["initial_target"] = target
        if not target.get("valid"):
            report["result"] = "REFUSED_INVALID_WILD_PK6"
            return 2
        if target.get("is_shiny"):
            report["result"] = "REFUSED_INITIAL_SHINY_SAFETY"
            print("REFUSED: initial opponent is shiny. This validator will never test on a shiny.")
            return 2
        gate = core.read_gate(br)
        report["initial_gate"] = gate
        if not gate.get("gate"):
            report["result"] = "REFUSED_COMMAND_MENU_NOT_READY"
            return 2

        # Intentionally hide actual species/moves from Master Ball policy.
        probe_target = {
            "species": 0,
            "species_name": "MULTIBALL_VALIDATOR_MASTER_FORBIDDEN",
            "moves": [],
            "tid": tid,
            "sid": sid,
            "pid": target.get("pid"),
            "pokemon_pid": target.get("pid"),
        }

        original_choose = bagmod.choose_best_ball
        throw_index = 1
        bag_already_open = False

        while throw_index <= MAX_TEST_THROWS:
            pre_epoch = br.read(EPOCH_ADDR, EPOCH_SIZE)
            pre_previous = br.read(EPOCH_PREVIOUS_ADDR, EPOCH_SIZE)
            print(
                f"\nBall {throw_index}: pre-throw epoch={pre_epoch.hex()} "
                f"previous={pre_previous.hex()} equal={pre_epoch == pre_previous}"
            )

            if throw_index == 1:
                # Ball 1 is deliberately weak so this diagnostic can exercise
                # the real failed-capture/rethrow path on ordinary non-shinies.
                try:
                    bagmod.choose_best_ball = _forced_weak_probe_decision_factory(original_choose)
                    throw = throw_one_best_ball(
                        core,
                        br,
                        target=probe_target,
                        throw_index=throw_index,
                        method_key="",
                        environment="",
                        check_stop=lambda: None,
                        log=lambda m: print(m, flush=True),
                    )
                finally:
                    bagmod.choose_best_ball = original_choose
            else:
                if not bag_already_open:
                    raise RuntimeError(
                        f"internal safety: Ball {throw_index} requested without a "
                        "game-consumed Bag-state-1 handoff"
                    )
                throw = _throw_best_ball_from_open_bag(
                    core, br, target=probe_target, throw_index=throw_index,
                    check_stop=lambda: None, log=lambda m: print(m, flush=True),
                )
                bag_already_open = False

            chosen = (throw.get("final_selection") or {}).get("item_id")
            if chosen == 1:
                raise RuntimeError("SAFETY: Master Ball reached validator selection")

            pre_flow = int(throw.get("pre_throw_flow") or 0)
            outcome = _wait_for_bag_consumption(
                core, br, tid, sid, target, pre_flow, pre_epoch,
                log=lambda m: print(m, flush=True),
            )
            report["throws"].append({
                "throw_index": throw_index,
                "pre_epoch_hex": pre_epoch.hex(),
                "pre_epoch_previous_hex": pre_previous.hex(),
                "pre_epoch_equals_previous": pre_epoch == pre_previous,
                "throw": throw,
                "outcome": outcome,
            })

            if outcome.get("status") == "CAPTURED":
                report["result"] = "CAPTURED_AFTER_MULTIBALL_HANDSHAKE"
                report["capture_throw_index"] = throw_index
                print(f"CAPTURE PROVEN after Ball {throw_index}; entering guarded post-capture cleanup.")
                try:
                    report["post_capture"] = clear_post_capture_validator(
                        br, core, check_stop=lambda: None,
                        log=lambda m: print(m, flush=True),
                    )
                    report["post_capture_result"] = "COMPLETED"
                except Exception as exc:
                    report["post_capture_error"] = f"{type(exc).__name__}: {exc}"
                    report["post_capture_result"] = "FAILED_CLOSED"
                return 0

            if outcome.get("status") == "BREAKOUT_BAG_CONSUMED_CONFIRMED":
                if throw_index >= MAX_TEST_THROWS:
                    report["result"] = "MAX_TEST_THROWS_REACHED_WITH_BAG_OPEN"
                    return 2
                bag_already_open = True
                print(
                    f"Breakout {throw_index} proven by game-consumed BAG state 1; "
                    f"continuing directly to Ball {throw_index + 1}."
                )
                throw_index += 1
                continue

            report["result"] = outcome.get("status") or "UNKNOWN_OUTCOME"
            return 2

        report["result"] = "MAX_TEST_THROWS_REACHED"
        return 2
    except Exception as exc:
        report["result"] = "ERROR"
        report["error"] = f"{type(exc).__name__}: {exc}"
        print("ERROR:", report["error"])
        return 2
    finally:
        report["finished"] = now_iso()
        js, txt = save_report(profile, report)
        print(f"\nReport JSON: {js}")
        print(f"Report TXT : {txt}")


if __name__ == "__main__":
    raise SystemExit(main())
