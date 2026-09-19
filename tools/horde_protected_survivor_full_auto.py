from __future__ import annotations

"""DEV D18f: exclusion-authoritative automatic Horde KO -> survivor proof -> capture validator.

This deliberately runs on a NON-SHINY Horde and treats CENTER / PK6 slot 2 as a
stand-in shiny.  It combines the hardware-proven D16 direct target touch mapping,
D17a FIGHT/move touch coordinates, D10 alive-count authority, D11/D12 relocated
Bag owner discovery, and the frozen D8 capture lifecycle.

Safety properties:
- no game RAM writes
- all combat/touch inputs are non-retransmitted
- refuses any actual shiny Horde (real shiny live integration comes only after
  this protected-survivor end-to-end path passes)
- never touches the protected CENTER coordinate during KO phase
- permits a target to survive or a move to miss; retries ONLY that same non-protected target
- requires any eventual KO to decrement the alive count by exactly one
- validates the hardware-proven owner+0x54 target mask on every direct target touch
- proves the protected survivor by exhaustive exclusion: four distinct non-CENTER targets
  each receive an exact direct-target confirmation and exactly one alive-count decrement
- never relies on D13's 0x08667068 candidate (D18d disproved it across battles)
- unexpected target mask / alive drop / phase classification fails closed
"""

import json
import math
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pokebot.common.species_names import SPECIES_NAMES
from pokebot.wild import battle_bag_throw as bagmod
from pokebot.wild.horde_authority import read_opponent_set
from pokebot.wild.validated_loader import load_walk_v0p23
from qt_ui.appdata_store import get_profile_paths
from qt_ui.settings_store import load_settings
from tools.horde_dynamic_owner_capture_validator import (
    capture_one_alive,
    concise_slot,
    discover_unique_owner,
    prove_one_alive,
)

def encode_touch_xy(x: int, y: int) -> int:
    x = max(0, min(319, int(x)))
    y = max(0, min(239, int(y)))
    xr = int(math.floor(x * 4095.0 / 320.0)) & 0xFFF
    yr = int(math.floor(y * 4095.0 / 240.0)) & 0xFFF
    return 0x01000000 | (yr << 12) | xr


AS_TITLE_ID = 0x000400000011C500
EXPECTED_ACTSELECT_VPTR = 0x007D87C0
ACTSELECT_VPTR_OFF = 0x5C
TARGET_MASK_OFF = 0x54
PHASE_STATE_OFF = 0x93
OWNER_WINDOW = 0x180

FIGHT_XY = (158, 100)       # D17a hardware proven
MOVE_XY = (246, 69)         # D17a hardware proven single-target KO move
FIGHT_TOUCH = encode_touch_xy(*FIGHT_XY)
MOVE_TOUCH = encode_touch_xy(*MOVE_XY)

HOLD_MS = 120
SETTLE_MS = 220
B_RAW_HID = 0xFFD

MASK_BY_VISUAL = {
    "FAR_LEFT": 0x20,
    "INNER_LEFT": 0x10,
    "CENTER": 0x08,
    "INNER_RIGHT": 0x04,
    "FAR_RIGHT": 0x02,
}
SLOT_BY_VISUAL = {
    "FAR_LEFT": 4,
    "INNER_LEFT": 3,
    "CENTER": 2,
    "INNER_RIGHT": 1,
    "FAR_RIGHT": 0,
}
XY_BY_VISUAL = {
    "FAR_LEFT": (49, 152),
    "INNER_LEFT": (48, 78),
    "CENTER": (160, 76),
    "INNER_RIGHT": (270, 78),
    "FAR_RIGHT": (270, 151),
}
VISUAL_BY_MASK = {v: k for k, v in MASK_BY_VISUAL.items()}
PROTECTED_VISUAL = "CENTER"
PROTECTED_SLOT = 2
PROTECTED_MASK = MASK_BY_VISUAL[PROTECTED_VISUAL]
KO_ORDER = ["FAR_LEFT", "INNER_LEFT", "INNER_RIGHT", "FAR_RIGHT"]
MAX_ATTACK_ATTEMPTS_PER_TARGET = 6
ATTACK_RESOLUTION_ARM_S = 1.5


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def hx(v: int | None) -> str | None:
    return None if v is None else f"0x{int(v) & 0xFFFFFFFF:08X}"


def parse_hex(v) -> int | None:
    try:
        return int(str(v), 16)
    except Exception:
        return None


def log_line(msg: str) -> None:
    print(msg, flush=True)


def no_stop() -> None:
    return None


def discover_owner(core, br) -> tuple[int, dict]:
    outer = br.u32(core.OUTER_PTR_ADDR)
    gate = core.read_gate(br)
    view = parse_hex(gate.get("view"))
    if view is None:
        raise RuntimeError("no valid Horde action view for owner discovery")
    found = discover_unique_owner(core, br, view, outer)
    chosen = found["chosen"]
    return int(chosen["owner_u32"]), chosen


def owner_blob(br, owner: int) -> bytes:
    if br.u32(owner + ACTSELECT_VPTR_OFF) != EXPECTED_ACTSELECT_VPTR:
        raise RuntimeError("Horde ActSelect owner vptr changed")
    return br.read(owner, OWNER_WINDOW)


def phase_pair(br, owner: int, label: str) -> dict:
    samples = []
    for _ in range(3):
        samples.append(owner_blob(br, owner))
        time.sleep(0.16)
    stable = [all(s[i] == samples[0][i] for s in samples[1:]) for i in range(OWNER_WINDOW)]
    return {
        "label": label,
        "samples": samples,
        "stable": stable,
    }


def make_phase_fingerprint(command: dict, move: dict) -> dict:
    c0 = command["samples"][0]
    m0 = move["samples"][0]
    command_state = int(c0[PHASE_STATE_OFF])
    move_state = int(m0[PHASE_STATE_OFF])
    # D18b exposed owner+0x93 as the isolated small phase byte: command=1, move=2.
    # Fail closed if this battle does not reproduce that exact hardware-derived pair.
    if (command_state, move_state) != (1, 2):
        raise RuntimeError(
            f"unexpected ActSelect phase-state pair at owner+0x93: "
            f"command={command_state} move={move_state}, expected 1/2"
        )
    offsets = []
    for i in range(OWNER_WINDOW):
        if TARGET_MASK_OFF <= i < TARGET_MASK_OFF + 4:
            continue
        if not command["stable"][i] or not move["stable"][i]:
            continue
        if c0[i] == m0[i]:
            continue
        offsets.append(i)
    # Hardware run v0p43EC on Route 102 proved a valid 1->2 owner+0x93 phase
    # pair with only three additional stable command/move discriminator bytes.
    # owner+0x93 remains the exact phase authority (strong_phase), while these
    # bytes are diagnostic corroboration only. Requiring four here caused a
    # false Safety Hold before any attack input. Three is now the minimum.
    if len(offsets) < 3:
        raise RuntimeError(f"insufficient command/move RAM phase discriminators: {len(offsets)}")
    return {
        "offsets": offsets,
        "command_values": [c0[i] for i in offsets],
        "move_values": [m0[i] for i in offsets],
        "phase_state_offset": PHASE_STATE_OFF,
        "command_state": command_state,
        "move_state": move_state,
    }


def phase_score(br, owner: int, fp: dict) -> dict:
    blob = owner_blob(br, owner)
    offsets = fp["offsets"]
    cm = sum(blob[o] == v for o, v in zip(offsets, fp["command_values"]))
    mm = sum(blob[o] == v for o, v in zip(offsets, fp["move_values"]))
    total = len(offsets)
    winner = "COMMAND" if cm > mm else "MOVE" if mm > cm else "TIE"
    phase_state = int(blob[int(fp["phase_state_offset"])])
    exact_phase = (
        "COMMAND" if phase_state == int(fp["command_state"])
        else "MOVE" if phase_state == int(fp["move_state"])
        else None
    )
    return {
        "winner": winner,
        "command_matches": cm,
        "move_matches": mm,
        "total": total,
        "command_ratio": round(cm / total, 4) if total else 0.0,
        "move_ratio": round(mm / total, 4) if total else 0.0,
        "phase_state_offset": hx(int(fp["phase_state_offset"])),
        "phase_state_byte": phase_state,
        "exact_phase": exact_phase,
    }


def strong_phase(score: dict, wanted: str) -> bool:
    # D18b showed the byte-majority classifier decays as opponents faint because
    # three of its four bytes are contiguous (owner+0x88..0x8A) and are not
    # independent phase evidence. D18c uses the exact isolated owner+0x93 state
    # learned at startup: 1=COMMAND, 2=MOVE. The old byte-match score remains
    # diagnostic only.
    return score.get("exact_phase") == wanted


def gate_snapshot(core, br) -> dict:
    g = dict(core.read_gate(br))
    g["state_u32"] = parse_hex(g.get("state"))
    g["mask_u32"] = parse_hex(g.get("mask"))
    g["battle_u32"] = parse_hex(g.get("battle"))
    return g


def wait_phase(core, br, owner: int, fp: dict, wanted: str, alive: int, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    samples = []
    stable = 0
    expected_state = alive + 1
    while time.monotonic() < deadline:
        try:
            g = gate_snapshot(core, br)
            s = phase_score(br, owner, fp)
            target_mask = br.u32(owner + TARGET_MASK_OFF) & 0xFFFFFFFF
            rec = {
                "time": now_iso(),
                "gate": g,
                "phase": s,
                "target_mask": hx(target_mask),
            }
            samples.append(rec)
            gate_ok = (
                g.get("battle_u32") == core.BATTLE_ACTIVE
                and bool(g.get("valid_view"))
                and g.get("state_u32") == expected_state
                and g.get("mask_u32") == 0x100
            )
            if gate_ok and strong_phase(s, wanted):
                stable += 1
                if stable >= 2:
                    return {"proven": True, "phase": wanted, "alive": alive, "samples": samples, "last": rec}
            else:
                stable = 0
        except Exception as exc:
            samples.append({"time": now_iso(), "error": f"{type(exc).__name__}: {exc}"})
            stable = 0
        time.sleep(0.12)
    return {"proven": False, "phase": wanted, "alive": alive, "samples": samples, "last": samples[-1] if samples else None}


def wait_target_selector(core, br, owner: int, alive: int, timeout: float = 4.0) -> dict:
    deadline = time.monotonic() + timeout
    samples = []
    stable = 0
    while time.monotonic() < deadline:
        g = gate_snapshot(core, br)
        vptr = br.u32(owner + ACTSELECT_VPTR_OFF)
        mask = br.u32(owner + TARGET_MASK_OFF) & 0xFFFFFFFF
        rec = {
            "time": now_iso(),
            "vptr": hx(vptr),
            "target_mask": hx(mask),
            "target_visual": VISUAL_BY_MASK.get(mask),
            "gate": g,
        }
        samples.append(rec)
        good = (
            g.get("battle_u32") == core.BATTLE_ACTIVE
            and vptr == EXPECTED_ACTSELECT_VPTR
            and mask in VISUAL_BY_MASK
            and g.get("state_u32") == alive + 1
            and g.get("mask_u32") == 0x100
        )
        if good:
            stable += 1
            if stable >= 1:
                return {"proven": True, "alive": alive, "sample": rec, "samples": samples}
        else:
            stable = 0
        time.sleep(0.06)
    return {"proven": False, "alive": alive, "samples": samples, "last": samples[-1] if samples else None}


def send_touch(br, touch_state: int, label: str) -> dict:
    ev = br.touch_pulse_no_retransmit(touch_state, HOLD_MS, SETTLE_MS)
    if not ev.get("completed"):
        raise RuntimeError(f"{label} firmware completion not acknowledged")
    return {"label": label, **ev}


def send_b(br, label: str) -> dict:
    ev = br.hid_pulse_no_retransmit(B_RAW_HID, HOLD_MS, SETTLE_MS)
    if not ev.get("completed"):
        raise RuntimeError(f"{label} firmware completion not acknowledged")
    return {"label": label, **ev}


def observe_direct_target(core, br, owner: int, expected_mask: int, seconds: float = 1.1) -> dict:
    deadline = time.monotonic() + seconds
    samples = []
    expected_seen = False
    exited_after_expected = False
    wrong_known = set()
    while time.monotonic() < deadline:
        try:
            mask = br.u32(owner + TARGET_MASK_OFF) & 0xFFFFFFFF
            g = gate_snapshot(core, br)
            rec = {"time": now_iso(), "target_mask": hx(mask), "target_visual": VISUAL_BY_MASK.get(mask), "gate": g}
            samples.append(rec)
            if mask == expected_mask:
                expected_seen = True
            elif mask in VISUAL_BY_MASK:
                wrong_known.add(mask)
            elif expected_seen and mask == 0:
                exited_after_expected = True
                break
        except Exception as exc:
            samples.append({"time": now_iso(), "error": f"{type(exc).__name__}: {exc}"})
        time.sleep(0.035)
    status = "DIRECT_TOUCH_TARGET_AND_CONFIRM_PROVEN" if expected_seen and exited_after_expected and not wrong_known else "TARGET_CONFIRM_NOT_PROVEN"
    return {
        "status": status,
        "expected_mask": hx(expected_mask),
        "expected_seen": expected_seen,
        "selector_exited_after_expected": exited_after_expected,
        "wrong_known_masks": [hx(v) for v in sorted(wrong_known)],
        "samples": samples,
    }

def wait_attack_resolution(core, br, owner: int, fp: dict, before_alive: int, timeout: float = 45.0) -> dict:
    """Wait for the post-turn command phase and classify exact KO vs survivor/miss.

    D18 incorrectly assumed every attack must one-hit.  D18a instead accepts two
    safe outcomes once the RAM command phase is genuinely back:
      * before_alive-1 => exactly one opponent was KO'd
      * before_alive   => target survived / move missed; retry the SAME target

    Any larger drop, battle exit, or other alive count fails closed.
    """
    expected_alive = before_alive - 1
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    samples = []
    stable_kind = None
    stable = 0
    while time.monotonic() < deadline:
        try:
            g = gate_snapshot(core, br)
            state = g.get("state_u32")
            sc = phase_score(br, owner, fp)
            rec = {"time": now_iso(), "gate": g, "phase": sc, "elapsed": round(time.monotonic()-started, 3)}
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

            # Do not classify the stale owner immediately after selector exit.
            armed = (time.monotonic() - started) >= ATTACK_RESOLUTION_ARM_S
            command_ready = armed and g.get("mask_u32") == 0x100 and strong_phase(sc, "COMMAND")
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
            samples.append({"time": now_iso(), "error": f"{type(exc).__name__}: {exc}"})
            stable_kind = None
            stable = 0
        time.sleep(0.10)
    return {"proven": False, "status": "ATTACK_RESOLUTION_TIMEOUT", "before_alive": before_alive, "samples": samples, "last": samples[-1] if samples else None}


def save_report(profile, report: dict, code: int) -> int:
    support = profile.root / "support"
    support.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    jp = support / f"horde_protected_survivor_full_auto_{stamp}.json"
    tp = support / f"horde_protected_survivor_full_auto_{stamp}.txt"
    jp.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    ko_count = sum(
        1 for t in (report.get("ko_turns") or [])
        if ((t.get("attack_resolution") or {}).get("status") == "EXACTLY_ONE_ALIVE_DECREMENT_PROVEN")
    )
    lines = [
        "Pokebot3DS-CFW DEV D18f — Horde Slot-Aware Rethrow Authority Full Auto Validator",
        f"Result: {report.get('result')}",
        f"Protected stand-in slot: {PROTECTED_SLOT} / {PROTECTED_VISUAL}",
        f"Automatic attack turns attempted: {len(report.get('ko_turns') or [])}",
        f"Automatic KOs completed: {ko_count}",
        f"Survivor identity proven: {(report.get('survivor_proof') or {}).get('proven')}",
        f"Capture: {(report.get('capture') or {}).get('result')}",
        f"Post-capture: {(report.get('post_capture') or {}).get('result')}",
        "RAM writes: False",
        f"JSON: {jp}",
    ]
    if report.get("error"):
        lines.append("Error: " + str(report["error"]))
    tp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\nSaved:\n" + str(jp) + "\n" + str(tp))
    return code


def main() -> int:
    profile = get_profile_paths()
    settings = load_settings(profile.settings_path)
    host = settings["three_ds_ip"]
    timeout = min(float(settings.get("bridge_timeout_s", 1.5)), 1.5)
    _, core, _ = load_walk_v0p23()
    br = core.Bridge(host, timeout=timeout)

    report = {
        "tool": "Pokebot3DS-CFW DEV D18f Horde Slot-Aware Rethrow Authority Full Auto Validator",
        "started": now_iso(),
        "ram_writes": False,
        "live_hunt_modified": False,
        "auto_capture_base": "D8_FROZEN",
        "d10_alive_gate": "HARDWARE_PROVEN_STATE_EQUALS_ALIVE_PLUS_ONE",
        "d12_horde_capture": "HARDWARE_PROVEN_DYNAMIC_OWNER_TO_FIELD",
        "d16_targets": "HARDWARE_PROVEN_ALL_FIVE_DIRECT_TOUCH_CONFIRM",
        "d17a_fight_move": "HARDWARE_PROVEN_TOUCH_TO_TARGET_SELECTOR",
        "automatic_attack_inputs": True,
        "automatic_capture_inputs": True,
        "touch_retransmission_permitted": False,
        "protected_test_slot": PROTECTED_SLOT,
        "protected_test_visual": PROTECTED_VISUAL,
        "protected_test_mask": hx(PROTECTED_MASK),
        "fight_xy": list(FIGHT_XY),
        "move_xy": list(MOVE_XY),
        "ko_order": KO_ORDER,
        "max_attack_attempts_per_target": MAX_ATTACK_ATTEMPTS_PER_TARGET,
        "future_live_policy": {
            "zero_shiny": "NORMAL_HORDE",
            "one_shiny": "PROTECT_EXACT_SHINY_SLOT_KO_OTHER_FOUR_VERIFY_SURVIVOR_THEN_CAPTURE",
            "two_or_more_shiny": "SHINY_HOLD_NO_ATTACK_NO_BALL",
        },
        "ko_turns": [],
    }

    print("=" * 78)
    print("POKEBOT3DS-CFW DEV D18f — HORDE SLOT-AWARE RETHROW AUTHORITY FULL AUTO")
    print("NON-SHINY HORDE ONLY | CENTER IS STAND-IN SHINY | NO RAM WRITES")
    print("=" * 78)
    print("Use the SAME single-target damaging move mapped in D17a at screen (246,69).")
    print("D18f keeps exact ActSelect owner+0x93 phase state and validates the exact hardware-proven target mask on every direct target.")
    print("Use a high-level attacker so no level-up/move-learning interruption is expected.")
    print("D18f never targets CENTER. After four exact non-CENTER KOs and D10 one-alive proof, CENTER is proven by exclusion and captured.\n")

    try:
        gi = br.game_info()
        report["game_info"] = gi
        title_id = gi.get("title_id", 0)
        if isinstance(title_id, str):
            title_id = int(title_id, 16)
        if int(title_id) != AS_TITLE_ID:
            report["result"] = "REFUSED_WRONG_GAME"
            return save_report(profile, report, 2)
        if br.u32(core.BATTLE_ADDR) != core.BATTLE_ACTIVE:
            report["result"] = "REFUSED_NOT_IN_ACTIVE_BATTLE"
            return save_report(profile, report, 2)

        tid, sid = struct.unpack("<HH", br.read(core.TRAINER_IDS_ADDR, 4))
        report["trainer_ids"] = {"tid": tid, "sid": sid}
        opponents = read_opponent_set(br, core, tid, sid)
        slots = opponents.get("slots") or []
        report["opponent_set"] = {
            "valid": opponents.get("valid"),
            "classification": opponents.get("classification"),
            "size": opponents.get("size"),
            "slots": [concise_slot(s) for s in slots],
        }
        if not opponents.get("valid") or opponents.get("classification") != "HORDE" or len(slots) < 5:
            report["result"] = "REFUSED_NOT_PROVEN_FIVE_HORDE"
            return save_report(profile, report, 2)
        shiny = [concise_slot(s) for s in opponents.get("occupied", []) if bool((s.get("pk6") or {}).get("is_shiny"))]
        report["actual_shiny_slots"] = shiny
        if shiny:
            report["result"] = "REFUSED_ACTUAL_SHINY_HORDE_PRESERVE_IT"
            print("ACTUAL SHINY PRESENT — D18 validator refuses all attack inputs.")
            return save_report(profile, report, 3)

        print("Put the battle on FIGHT / BAG / POKEMON / RUN and leave the 3DS alone.")
        input("Press ENTER when the Horde command screen is stable... ")

        owner, owner_rec = discover_owner(core, br)
        report["owner"] = owner_rec
        initial_gate = gate_snapshot(core, br)
        report["initial_gate"] = initial_gate
        if initial_gate.get("state_u32") != 6 or initial_gate.get("mask_u32") != 0x100:
            report["result"] = "REFUSED_INITIAL_FIVE_ALIVE_STATE6_MASK100_NOT_PROVEN"
            return save_report(profile, report, 2)

        log_line("D18: learning this battle's RAM command-phase fingerprint...")
        command_pair = phase_pair(br, owner, "COMMAND")
        fight_ev = send_touch(br, FIGHT_TOUCH, "CALIBRATE_FIGHT_TOUCH")
        report["initial_fight_touch"] = fight_ev
        time.sleep(0.55)
        move_pair = phase_pair(br, owner, "MOVE")
        fp = make_phase_fingerprint(command_pair, move_pair)
        report["phase_fingerprint"] = {
            "owner_window": OWNER_WINDOW,
            "discriminator_count": len(fp["offsets"]),
            "offsets": [hx(o) for o in fp["offsets"]],
            "command_values": fp["command_values"],
            "move_values": fp["move_values"],
            "phase_state_offset": hx(fp["phase_state_offset"]),
            "command_state": fp["command_state"],
            "move_state": fp["move_state"],
            "authority": "EXACT_OWNER_PLUS_0x93_STATE_1_COMMAND_2_MOVE",
        }
        report["phase_calibration_scores"] = {
            "command_now_after_fight": phase_score(br, owner, fp),
        }
        if not strong_phase(phase_score(br, owner, fp), "MOVE"):
            report["result"] = "FAILED_FIGHT_TOUCH_DID_NOT_REACH_DISTINCT_MOVE_PHASE"
            return save_report(profile, report, 2)

        move_ev = send_touch(br, MOVE_TOUCH, "CALIBRATE_MOVE_TOUCH")
        report["initial_move_touch"] = move_ev
        target = wait_target_selector(core, br, owner, alive=5, timeout=4.0)
        report["initial_target_selector"] = target
        if not target.get("proven"):
            report["result"] = "FAILED_INITIAL_MOVE_DID_NOT_REACH_TARGET_SELECTOR"
            return save_report(profile, report, 2)

        alive = 5
        total_attack_turns = 0
        # First attack starts from the selector we just entered.  If a target
        # survives or the move misses, D18a returns through a RAM-proven command
        # phase and attacks that SAME non-protected visual again.
        first_selector_ready = True
        for target_index, visual in enumerate(KO_ORDER, 1):
            slot = SLOT_BY_VISUAL[visual]
            if slot == PROTECTED_SLOT or visual == PROTECTED_VISUAL:
                raise RuntimeError("internal safety violation: KO order included protected CENTER")
            expected_mask = MASK_BY_VISUAL[visual]
            xy = XY_BY_VISUAL[visual]
            target_record = {
                "target_index": target_index,
                "visual": visual,
                "pk6_slot": slot,
                "screen_xy": list(xy),
                "expected_target_mask": hx(expected_mask),
                "attempts": [],
            }
            report.setdefault("ko_targets", []).append(target_record)

            ko_proven = False
            for attack_attempt in range(1, MAX_ATTACK_ATTEMPTS_PER_TARGET + 1):
                if first_selector_ready:
                    command = None
                    evf = fight_ev
                    move_phase = None
                    evm = move_ev
                    sel = target
                    first_selector_ready = False
                else:
                    command = wait_phase(core, br, owner, fp, "COMMAND", alive, timeout=45.0)
                    if not command.get("proven"):
                        report["result"] = "FAILED_RAM_COMMAND_PHASE_RETURN"
                        report["failed_visual"] = visual
                        report["failed_attack_attempt"] = attack_attempt
                        report["command_wait"] = command
                        return save_report(profile, report, 2)
                    evf = send_touch(br, FIGHT_TOUCH, f"TARGET_{target_index}_ATTEMPT_{attack_attempt}_FIGHT")
                    move_phase = wait_phase(core, br, owner, fp, "MOVE", alive, timeout=4.0)
                    if not move_phase.get("proven"):
                        report["result"] = "FAILED_FIGHT_TO_MOVE_RAM_PHASE"
                        report["failed_visual"] = visual
                        report["failed_attack_attempt"] = attack_attempt
                        report["fight_event"] = evf
                        report["move_phase_wait"] = move_phase
                        return save_report(profile, report, 2)
                    evm = send_touch(br, MOVE_TOUCH, f"TARGET_{target_index}_ATTEMPT_{attack_attempt}_MOVE")
                    sel = wait_target_selector(core, br, owner, alive=alive, timeout=4.0)
                    if not sel.get("proven"):
                        report["result"] = "FAILED_MOVE_TO_TARGET_SELECTOR"
                        report["failed_visual"] = visual
                        report["failed_attack_attempt"] = attack_attempt
                        report["move_event"] = evm
                        report["target_wait"] = sel
                        return save_report(profile, report, 2)

                log_line(
                    f"D18f: target {target_index}/4 {visual} slot {slot}, "
                    f"attack attempt {attack_attempt}/{MAX_ATTACK_ATTEMPTS_PER_TARGET}; CENTER protected"
                )
                before_selector_mask = br.u32(owner + TARGET_MASK_OFF) & 0xFFFFFFFF
                target_ev = send_touch(
                    br, encode_touch_xy(*xy),
                    f"TARGET_{target_index}_ATTEMPT_{attack_attempt}_TOUCH_{visual}",
                )
                total_attack_turns += 1
                confirmation = observe_direct_target(core, br, owner, expected_mask)
                if confirmation.get("status") != "DIRECT_TOUCH_TARGET_AND_CONFIRM_PROVEN":
                    report["result"] = "FAILED_EXACT_DIRECT_TARGET_CONFIRM_AUTHORITY"
                    report["failed_visual"] = visual
                    report["failed_attack_attempt"] = attack_attempt
                    report["failed_confirmation"] = confirmation
                    return save_report(profile, report, 2)

                resolution = wait_attack_resolution(core, br, owner, fp, alive, timeout=45.0)
                rec = {
                    "target_index": target_index,
                    "attack_attempt": attack_attempt,
                    "battle_turn_index": total_attack_turns,
                    "visual": visual,
                    "pk6_slot": slot,
                    "screen_xy": list(xy),
                    "expected_target_mask": hx(expected_mask),
                    "selector_mask_before_touch": hx(before_selector_mask),
                    "command_wait": command,
                    "fight_event": evf,
                    "move_phase_wait": move_phase,
                    "move_event": evm,
                    "target_selector": sel,
                    "target_event": target_ev,
                    "target_confirmation": confirmation,
                    "attack_resolution": resolution,
                }
                target_record["attempts"].append(rec)
                report["ko_turns"].append(rec)

                if not resolution.get("proven"):
                    report["result"] = "FAILED_ATTACK_RESOLUTION_AUTHORITY"
                    report["failed_visual"] = visual
                    report["failed_attack_attempt"] = attack_attempt
                    return save_report(profile, report, 2)
                if resolution.get("status") == "EXACTLY_ONE_ALIVE_DECREMENT_PROVEN":
                    alive -= 1
                    target_record["ko_proven"] = True
                    target_record["attempts_to_ko"] = attack_attempt
                    target_record["alive_after"] = alive
                    ko_proven = True
                    break
                if resolution.get("status") == "TARGET_SURVIVED_OR_MOVE_MISSED_COMMAND_RETURN_PROVEN":
                    log_line(
                        f"D18f: {visual} still alive after attempt {attack_attempt}; "
                        "RAM command return proven, retrying SAME target only."
                    )
                    continue
                report["result"] = "FAILED_UNEXPECTED_ATTACK_RESOLUTION"
                report["failed_visual"] = visual
                return save_report(profile, report, 2)

            if not ko_proven:
                report["result"] = "FAILED_TARGET_NOT_KO_AFTER_BOUNDED_RETRIES"
                report["failed_visual"] = visual
                report["max_attempts"] = MAX_ATTACK_ATTEMPTS_PER_TARGET
                return save_report(profile, report, 2)

        report["total_attack_turns_before_ball"] = total_attack_turns
        if alive != 1:
            raise RuntimeError(f"internal alive accounting ended at {alive}, expected 1")

        # D18d disproved the proposed D13 battler-ID candidate across battles:
        # the exact FAR_LEFT target mask 0x20 was hardware-observed while that byte
        # read 9 instead of D13's earlier 3.  D18e therefore uses the stronger
        # cumulative exclusion invariant only.  Each of four distinct non-CENTER
        # targets must have: exact one-hot target confirmation, selector exit, no
        # wrong known target mask, and exactly one alive-count decrement.  Multi-drop
        # is already forbidden by wait_attack_resolution().  With D10 proving one
        # opponent alive and CENTER never targeted, CENTER is the sole survivor by
        # exhaustive elimination.
        required_eliminated = sorted(set(range(5)) - {PROTECTED_SLOT})
        eliminated_slots = []
        attempted_slots = []
        target_mask_proven_slots = []
        per_target = []
        for target_rec in report.get("ko_targets") or []:
            slot_i = int(target_rec.get("pk6_slot"))
            attempts = target_rec.get("attempts") or []
            if attempts:
                attempted_slots.append(slot_i)
            mask_proven = any(
                (a.get("target_confirmation") or {}).get("status") == "DIRECT_TOUCH_TARGET_AND_CONFIRM_PROVEN"
                and not ((a.get("target_confirmation") or {}).get("wrong_known_masks") or [])
                for a in attempts
            )
            ko_proven = bool(target_rec.get("ko_proven"))
            if mask_proven:
                target_mask_proven_slots.append(slot_i)
            if ko_proven:
                eliminated_slots.append(slot_i)
            per_target.append({
                "visual": target_rec.get("visual"),
                "pk6_slot": slot_i,
                "target_mask_proven": mask_proven,
                "ko_proven": ko_proven,
                "attempts_to_ko": target_rec.get("attempts_to_ko"),
                "alive_after": target_rec.get("alive_after"),
            })

        protected_never_targeted = PROTECTED_SLOT not in attempted_slots
        exact_distinct_targets = sorted(set(attempted_slots)) == required_eliminated
        all_masks_proven = sorted(set(target_mask_proven_slots)) == required_eliminated
        all_kos_proven = sorted(set(eliminated_slots)) == required_eliminated
        one_alive_accounting = alive == 1
        exclusion_proven = (
            protected_never_targeted
            and exact_distinct_targets
            and all_masks_proven
            and all_kos_proven
            and one_alive_accounting
        )
        report["elimination_survivor_invariant"] = {
            "proven": exclusion_proven,
            "authority": "D16 exact target masks + D10 exact one-step alive decrements + exhaustive non-protected elimination",
            "required_eliminated_pk6_slots": required_eliminated,
            "observed_eliminated_pk6_slots": sorted(set(eliminated_slots)),
            "target_mask_proven_pk6_slots": sorted(set(target_mask_proven_slots)),
            "attempted_pk6_slots": sorted(set(attempted_slots)),
            "protected_pk6_slot": PROTECTED_SLOT,
            "protected_pk6_slot_never_targeted": protected_never_targeted,
            "exact_four_distinct_nonprotected_targets": exact_distinct_targets,
            "all_target_masks_proven": all_masks_proven,
            "all_four_exact_kos_proven": all_kos_proven,
            "alive_after": alive,
            "per_target": per_target,
            "d13_battler_id_candidate": "DISPROVEN_ACROSS_BATTLES_NOT_USED",
        }
        if not exclusion_proven:
            report["result"] = "FAILED_EXHAUSTIVE_NONPROTECTED_ELIMINATION_INVARIANT"
            return save_report(profile, report, 2)

        one_command = wait_phase(core, br, owner, fp, "COMMAND", alive=1, timeout=45.0)
        report["one_alive_command_phase"] = one_command
        if not one_command.get("proven"):
            report["result"] = "FAILED_ONE_ALIVE_COMMAND_PHASE"
            return save_report(profile, report, 2)

        one_gate_pre = prove_one_alive(core, br, timeout=8.0)
        report["survivor_proof"] = {
            "proven": bool(one_gate_pre.get("proven")) and exclusion_proven,
            "expected_visual": PROTECTED_VISUAL,
            "expected_pk6_slot": PROTECTED_SLOT,
            "one_alive_gate": one_gate_pre,
            "elimination_invariant": report["elimination_survivor_invariant"],
            "authority": "EXHAUSTIVE_EXCLUSION_NO_FINAL_SELECTOR_IDENTITY_ASSUMPTION",
        }
        if not report["survivor_proof"]["proven"]:
            report["result"] = "PROTECTED_SURVIVOR_EXCLUSION_NOT_PROVEN_HOLD_NO_BALL"
            print("D18f HOLD: exhaustive protected-survivor proof incomplete. No Ball input sent.")
            return save_report(profile, report, 3)

        log_line("D18f: CENTER is proven sole survivor by exhaustive non-CENTER elimination. Entering D12 capture path...")

        # Re-use hardware-proven D12 capture path.  D18f carries the ACTUAL
        # number of attack turns into Ball scoring, so misses/survivals cannot
        # incorrectly make Quick Ball look like a first-turn throw.
        one_gate = one_gate_pre
        report["one_alive_gate"] = one_gate
        if not one_gate.get("proven"):
            report["result"] = "FAILED_D10_ONE_ALIVE_GATE_BEFORE_CAPTURE"
            return save_report(profile, report, 2)
        view = int(one_gate["view_u32"])
        outer = br.u32(core.OUTER_PTR_ADDR)
        found = discover_unique_owner(core, br, view, outer)
        report["capture_owner"] = found["chosen"]

        center_rec = slots[PROTECTED_SLOT]
        target_pk6 = dict(center_rec.get("pk6") or {})
        species = int(target_pk6.get("species") or 0)
        target_pk6["species_name"] = SPECIES_NAMES.get(species, f"Species #{species}")
        # D18e proved the four-KO exclusion survivor, then exposed the final
        # Horde-specific rethrow gap: frozen D8's breakout corroboration reread
        # core.WILD_PK6_ADDR (slot 0).  For a protected survivor in any other
        # Horde slot that can never match.  Supply the exact original stored
        # PK6 address; battle_bag_throw defaults remain unchanged for singles.
        target_pk6["stored_pk6_address"] = center_rec.get("address")
        report["protected_target"] = concise_slot(center_rec)
        report["capture_target_stored_pk6_address"] = center_rec.get("address")
        capture = capture_one_alive(core, br, target_pk6, found["chosen"], first_ball_battle_turn_index=total_attack_turns + 1)
        report["capture"] = capture
        if capture.get("result") != "CAPTURED":
            report["result"] = "FAILED_D12_CAPTURE"
            return save_report(profile, report, 2)

        post = bagmod.clear_post_capture_screens(br, core, check_stop=no_stop, log=log_line)
        report["post_capture"] = post
        if post.get("result") not in {
            "POST_CAPTURE_RAM_RECOVERY_COMPLETE",
            "POST_CAPTURE_RECOVERY_COMPLETE_POKEDEX_NOT_OBSERVED",
        }:
            report["result"] = "FAILED_POST_CAPTURE_RECOVERY"
            return save_report(profile, report, 2)
        field = core.wait_field_stable(br, 20.0)
        report["field_stable"] = field
        if not field.get("stable"):
            report["result"] = "FAILED_FIELD_RETURN"
            return save_report(profile, report, 2)

        report["result"] = "PASS_D18F_SLOT_AWARE_RETHROW_PROTECTED_SURVIVOR_CAPTURE_TO_FIELD"
        print("\nD18f PASS: exclusion-authoritative protected-survivor Horde pipeline completed automatically to field.")
        return save_report(profile, report, 0)

    except KeyboardInterrupt:
        report["result"] = "OPERATOR_CANCELLED"
        return save_report(profile, report, 1)
    except Exception as exc:
        report["result"] = report.get("result") or "D18F_FAILED_CLOSED"
        report["error"] = f"{type(exc).__name__}: {exc}"
        print("\nD18f failed closed:", report["error"])
        return save_report(profile, report, 2)
    finally:
        try:
            br.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
