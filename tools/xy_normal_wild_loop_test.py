from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

BOT_ROOT = Path(__file__).resolve().parents[1]
if str(BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(BOT_ROOT))

from pokebot.common.acknowledged_input import AcknowledgedInput
from pokebot.common.bridge import Bridge
from pokebot.common.gen6_cro import locate_loaded_modules
from pokebot.common.gen6_profiles import profile_from_game_info
from pokebot.common.xy_ram import WILD0, read_trainer_ids, read_wild_decoded

# Same Gen-6 lower-screen RUN touch proven by the existing ORAS worker.
# This build is specifically intended to hardware-validate that shared UI edge in X/Y.
RUN_TOUCH_STATE = 0x01EA97FF
RUN_TOUCH_XY = (160, 220)
MOVE_HOLD_MS = 600
MOVE_SETTLE_MS = 140
RUN_TOUCH_HOLD_MS = 120
RUN_TOUCH_SETTLE_MS = 160
FIELD_SETTLE_S = 1.00


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def pk6_identity(p):
    if not p or not p.get("valid") or not p.get("checksum_valid"):
        return None
    return (
        str(p.get("ec") or ""),
        str(p.get("pid") or ""),
        int(p.get("species") or 0),
        int(p.get("checksum") or 0) if isinstance(p.get("checksum"), int) else str(p.get("checksum") or ""),
    )


def cro_state(br):
    found = locate_loaded_modules(br, ("DllField", "DllBattle"))
    return {
        "field": bool(found.get("DllField")),
        "battle": bool(found.get("DllBattle")),
        "field_base": (found.get("DllField") or {}).get("base_hex"),
        "battle_base": (found.get("DllBattle") or {}).get("base_hex"),
    }


def wait_field(br, timeout=12.0, stable_samples=2):
    deadline = time.monotonic() + timeout
    stable = 0
    samples = []
    while time.monotonic() < deadline:
        st = cro_state(br)
        samples.append({"elapsed": round(timeout - max(0.0, deadline - time.monotonic()), 3), **st})
        if len(samples) > 20:
            samples.pop(0)
        if st["field"] and not st["battle"]:
            stable += 1
            if stable >= stable_samples:
                return True, st, samples
        else:
            stable = 0
        time.sleep(0.20)
    return False, samples[-1] if samples else {}, samples


def wait_new_wild(br, baseline_identity, tid, sid, timeout=25.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            p = read_wild_decoded(br, 0)
            last = p
            ident = pk6_identity(p)
            if ident is not None and ident != baseline_identity:
                trainer_match = int(p.get("tid", -1)) == tid and int(p.get("sid", -1)) == sid
                if not trainer_match:
                    return "TRAINER_MISMATCH", p
                return "VALID", p
        except Exception as exc:
            last = {"read_error": f"{type(exc).__name__}: {exc}"}
        time.sleep(0.10)
    return "TIMEOUT", last


def run_until_field(br, ctl, max_taps=8):
    attempts = []
    for attempt in range(1, max_taps + 1):
        before = cro_state(br)
        if before["field"] and not before["battle"]:
            return True, attempt - 1, attempts

        ack = ctl.touch_pulse(
            RUN_TOUCH_STATE,
            hold_ms=RUN_TOUCH_HOLD_MS,
            resume_settle_ms=0,
            packet_interval_ms=20,
            release_ms=RUN_TOUCH_SETTLE_MS,
        )
        rec = {
            "attempt": attempt,
            "time": now_iso(),
            "touch_xy": list(RUN_TOUCH_XY),
            "touch_state": f"0x{RUN_TOUCH_STATE:08X}",
            "acknowledged": bool(ack.get("acknowledged", True)),
            "sequence": ack.get("sequence"),
            "before": before,
        }
        # Give the game time to consume RUN and transition.
        time.sleep(0.35)
        ok, state, samples = wait_field(br, timeout=2.0, stable_samples=2)
        rec["after"] = state
        rec["field_samples"] = samples
        attempts.append(rec)
        if ok:
            return True, attempt, attempts
    return False, None, attempts


def main():
    ap = argparse.ArgumentParser(description="Pokémon X/Y controlled normal-wild loop hardware validation")
    ap.add_argument("host", nargs="?", default="192.168.0.28")
    ap.add_argument("--port", type=int, default=4952)
    ap.add_argument("--timeout", type=float, default=2.0)
    ap.add_argument("--encounters", type=int, default=3)
    ap.add_argument("--direction", choices=("LEFT", "RIGHT", "UP", "DOWN"), default="LEFT")
    ap.add_argument("--target-species", type=int, default=0)
    args = ap.parse_args()

    br = Bridge(args.host, args.port, args.timeout)
    ctl = AcknowledgedInput(args.host, port=args.port, timeout=args.timeout)
    report = {
        "tool": "HF57 XY Normal Wild Loop Hardware Test",
        "started": now_iso(),
        "requested_encounters": max(1, int(args.encounters)),
        "initial_direction": args.direction,
        "target_species": int(args.target_species),
        "wild_address": f"0x{WILD0:08X}",
        "run_touch": {"xy": list(RUN_TOUCH_XY), "encoded": f"0x{RUN_TOUCH_STATE:08X}"},
        "controller_inputs": [],
        "encounters": [],
        "ram_writes": 0,
        "status": "STARTING",
    }

    outdir = BOT_ROOT / "support"
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = outdir / f"xy_normal_wild_loop_{stamp}.json"

    try:
        gi = br.game_info()
        profile = profile_from_game_info(gi)
        report["game_info"] = gi
        report["game_profile"] = profile
        print(f"GAME_INFO: {gi.get('process_name')} PID {gi.get('pid')} title={gi.get('title_id')}")
        if not profile or profile.get("family") != "xy":
            raise RuntimeError(f"REFUSED: expected Pokémon X/Y, got {profile or gi}")

        caps = ctl.input_ping()
        report["input_caps"] = caps
        ctl.release_all()

        field_ok, field_state, field_samples = wait_field(br, timeout=8.0, stable_samples=2)
        report["initial_field"] = {"ready": field_ok, "state": field_state, "samples": field_samples}
        if not field_ok:
            raise RuntimeError("SAFETY HOLD: start in the overworld with stable DllField and no DllBattle")

        tid, sid = read_trainer_ids(br)
        report["trainer_ids"] = {"tid": tid, "sid": sid}
        print(f"Detected {profile['name']} • TID/SID {tid}/{sid}")
        print("Start standing in a normal encounter area with room to move on the selected axis.")
        print("This test will MOVE, read authoritative wild PK6, HOLD on shiny/target, otherwise touch RUN, verify field return, and repeat.")
        print("Press Ctrl+C at any time to release all input and stop.\n")

        try:
            baseline = pk6_identity(read_wild_decoded(br, 0))
        except Exception:
            baseline = None
        report["initial_stale_wild_identity"] = list(baseline) if baseline else None

        direction = args.direction
        reverse = {"LEFT": "RIGHT", "RIGHT": "LEFT", "UP": "DOWN", "DOWN": "UP"}

        for number in range(1, max(1, int(args.encounters)) + 1):
            print(f"[{number}/{args.encounters}] Seeking encounter • {direction} + B")
            encounter = {"number": number, "started": now_iso(), "movement": [], "status": "SEEKING"}
            report["encounters"].append(encounter)

            found = None
            for pulse_no in range(1, 81):
                ack = ctl.pulse(
                    (direction, "B"),
                    hold_ms=MOVE_HOLD_MS,
                    resume_settle_ms=0,
                    packet_interval_ms=20,
                    release_ms=MOVE_SETTLE_MS,
                )
                move_rec = {
                    "pulse": pulse_no,
                    "direction": direction,
                    "buttons": [direction, "B"],
                    "hold_ms": MOVE_HOLD_MS,
                    "sequence": ack.get("sequence"),
                    "acknowledged": bool(ack.get("acknowledged", True)),
                }
                encounter["movement"].append(move_rec)
                report["controller_inputs"].append({"type": "MOVE", **move_rec})

                status, p = wait_new_wild(br, baseline, tid, sid, timeout=0.55)
                if status == "TRAINER_MISMATCH":
                    encounter["wild_pk6"] = p
                    raise RuntimeError("SAFETY HOLD: checksum-valid XY wild PK6 had trainer TID/SID mismatch")
                if status == "VALID":
                    found = p
                    break

                # Reverse after every two movement pulses to reduce drift from the start point.
                if pulse_no % 2 == 0:
                    direction = reverse[direction]

            if found is None:
                raise RuntimeError("SAFETY HOLD: no new checksum-valid XY wild PK6 after bounded movement")

            ctl.release_all()
            encounter["wild_pk6"] = found
            encounter["identity"] = list(pk6_identity(found))
            print(
                f"  Encounter: {found.get('species_name')} #{found.get('species')} • "
                f"shiny={found.get('is_shiny')} xor={found.get('shiny_xor')}"
            )

            if not found.get("valid") or not found.get("checksum_valid"):
                raise RuntimeError("SAFETY HOLD: authoritative XY wild PK6 invalid")

            is_target = int(args.target_species or 0) > 0 and int(found.get("species") or 0) == int(args.target_species)
            if found.get("is_shiny"):
                encounter["status"] = "SHINY_HOLD"
                report["status"] = "SHINY_HOLD"
                print("SHINY HOLD — no Run input sent.")
                break
            if is_target:
                encounter["status"] = "TARGET_HOLD"
                report["status"] = "TARGET_HOLD"
                print("TARGET HOLD — no Run input sent.")
                break

            # Allow battle presentation/menu to settle after earliest PK6 availability.
            time.sleep(1.00)
            state_before_run = cro_state(br)
            encounter["battle_state_before_run"] = state_before_run
            if not state_before_run.get("battle"):
                # DllBattle can appear slightly after PK6; bounded wait only, no input yet.
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    state_before_run = cro_state(br)
                    if state_before_run.get("battle"):
                        break
                    time.sleep(0.20)
                encounter["battle_state_before_run"] = state_before_run
            if not state_before_run.get("battle"):
                raise RuntimeError("SAFETY HOLD: valid wild PK6 appeared but DllBattle never became present; Run not authorized")

            escaped, accepted_tap, run_attempts = run_until_field(br, ctl, max_taps=8)
            encounter["run_attempts"] = run_attempts
            for rec in run_attempts:
                report["controller_inputs"].append({"type": "RUN_TOUCH", **{k: v for k, v in rec.items() if k != "field_samples"}})
            if not escaped:
                encounter["status"] = "SAFETY_HOLD_RUN_NOT_ACCEPTED"
                raise RuntimeError("SAFETY HOLD: X/Y battle did not return to stable DllField after 8 bounded Run touches")

            encounter["accepted_run_touch"] = accepted_tap
            encounter["status"] = "PASS"
            encounter["finished"] = now_iso()
            print(f"  RUN accepted on touch {accepted_tap}; stable field returned.")

            ctl.release_all()
            time.sleep(FIELD_SETTLE_S)
            ok, st, samples = wait_field(br, timeout=4.0, stable_samples=2)
            encounter["post_battle_field"] = {"ready": ok, "state": st, "samples": samples}
            if not ok:
                raise RuntimeError("SAFETY HOLD: field did not remain stable after post-battle settle")

            baseline = pk6_identity(found)
            direction = reverse[direction]

        if report["status"] == "STARTING":
            report["status"] = "XY_NORMAL_WILD_LOOP_PASS"
        report["finished"] = now_iso()
        report["completed_encounters"] = sum(1 for e in report["encounters"] if e.get("status") == "PASS")
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nRESULT: {report['status']}")
        print(f"Saved: {out}")
        return 0 if report["status"] in {"XY_NORMAL_WILD_LOOP_PASS", "SHINY_HOLD", "TARGET_HOLD"} else 2

    except KeyboardInterrupt:
        report["status"] = "MANUAL_STOP"
        report["error"] = "KeyboardInterrupt"
        print("\nMANUAL STOP — releasing input.")
        return_code = 130
    except Exception as exc:
        report["status"] = "SAFETY_HOLD"
        report["error"] = f"{type(exc).__name__}: {exc}"
        print(f"\n{report['error']}")
        return_code = 3
    finally:
        try:
            ctl.release_all()
        except Exception as exc:
            report["release_error"] = f"{type(exc).__name__}: {exc}"
        try:
            ctl.close()
        except Exception:
            pass
        report["finished"] = now_iso()
        try:
            out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"Support report: {out}")
        except Exception:
            pass
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
