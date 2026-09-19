from __future__ import annotations

"""DEV D12: dynamically bind the relocated Horde Bag owner and capture one alive.

Hardware chain:
- D9/D10 proved Horde command-view state = alive_count + 1, mask 0x100.
- D10 proved exactly one alive => state 2 / mask 0x100 / normal command gate true.
- D11 proved the Horde ActSelect + embedded Bag owner is heap-relocated but keeps
  the exact normal dual-vtable layout, and proved BAG touch consumption at that
  relocated owner.

D12 starts with exactly one opponent alive. It rediscovers the unique dual-vptr
owner every run, binds the frozen D8 capture code to that owner, and then uses
best-Ball selection, failed-capture consumed-Bag rethrows, Gotcha/EXP/move-
learning handling, Pokédex/nickname/Box recovery, and final field proof.

No game RAM writes are performed. Attack/target selection remains manual and is
outside this validator.
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

from pokebot.common.species_names import SPECIES_NAMES
from pokebot.wild.horde_authority import read_opponent_set
import pokebot.wild.battle_bag_throw as bagmod
from pokebot.wild.battle_bag_throw import BagThrowError
from pokebot.wild.validated_loader import load_walk_v0p23
from qt_ui.appdata_store import get_profile_paths
from qt_ui.settings_store import load_settings
from tools.horde_bag_authority_mapper import (
    graph_search, verify_candidate, extract_heap_ptrs, read_window, linear_scan,
    valid_heap_ptr, OBJECT_WINDOW, SCAN_RADIUS, HEAP_MIN, HEAP_MAX,
)

AS_TITLE_ID = 0x000400000011C500
EXPECTED_MASK = 0x00000100
CENTER_SLOT = 2
FIRST_HORDE_BALL_TURN_INDEX = 5
MAX_VALIDATOR_BALLS = 20


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def hx(v: int | None) -> str | None:
    return None if v is None else f"0x{int(v) & 0xFFFFFFFF:08X}"


def parse_hex(v) -> int | None:
    if v is None:
        return None
    if isinstance(v, int):
        return int(v)
    try:
        return int(str(v), 16)
    except Exception:
        return None


def log_line(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def no_stop() -> None:
    return None


def concise_slot(rec: dict) -> dict:
    p = rec.get("pk6") or {}
    species = int(p.get("species") or 0)
    return {
        "slot": int(rec.get("slot", -1)),
        "address": rec.get("address"),
        "species": species,
        "species_name": SPECIES_NAMES.get(species, f"Species #{species}"),
        "pid": p.get("pid"),
        "ec": p.get("ec"),
        "identity": p.get("identity"),
        "shiny_xor": p.get("shiny_xor"),
        "is_shiny": bool(p.get("is_shiny")),
        "valid": bool(p.get("valid")),
    }


def prove_one_alive(core, br, timeout: float = 8.0) -> dict:
    deadline = time.monotonic() + float(timeout)
    samples = []
    stable = 0
    last_key = None
    while time.monotonic() < deadline:
        g = core.read_gate(br)
        state = parse_hex(g.get("state"))
        mask = parse_hex(g.get("mask"))
        battle = parse_hex(g.get("battle"))
        view = parse_hex(g.get("view"))
        rec = dict(g)
        rec.update({"state_u32": state, "mask_u32": mask, "battle_u32": battle, "view_u32": view})
        samples.append(rec)
        # D10 hardware proved state 2 == exactly one Horde opponent alive.
        # If D11 has just opened Bag, mask may be temporarily 0 while state 2
        # remains installed. D12 permits mask 0 only provisionally; after owner
        # discovery it additionally requires embedded Bag state 1 + valid controller.
        good = (
            battle == core.BATTLE_ACTIVE
            and bool(g.get("valid_view"))
            and state == 2
            and mask in {0, EXPECTED_MASK}
        )
        key = (battle, view, state, mask)
        if good and key == last_key:
            stable += 1
        elif good:
            stable = 1
        else:
            stable = 0
        last_key = key
        if stable >= 2:
            return {
                "proven": True,
                "view": g.get("view"),
                "view_u32": view,
                "state": g.get("state"),
                "mask": g.get("mask"),
                "command_gate": bool(g.get("gate")),
                "samples": samples,
            }
        time.sleep(0.18)
    return {"proven": False, "samples": samples, "last": samples[-1] if samples else None}


def discover_unique_owner(core, br, view: int, outer: int) -> dict:
    # Exact D11 search strategy: live outer/view pointer graph first, then a
    # bounded +/-2 MiB heap scan only if the graph is not unique.
    seeds = [outer, view]
    for base in (outer, view):
        if valid_heap_ptr(base):
            try:
                seeds.extend(extract_heap_ptrs(read_window(br, base, OBJECT_WINDOW)))
            except Exception:
                pass
    seeds = list(dict.fromkeys(p for p in seeds if valid_heap_ptr(p)))
    graph = graph_search(br, seeds)
    merged = {int(c["owner_u32"]): c for c in graph.get("candidates", [])}
    linear = {"skipped": "unique pointer-graph candidate"}
    if len(merged) != 1:
        start = max(HEAP_MIN, view - SCAN_RADIUS)
        end = min(HEAP_MAX, view + SCAN_RADIUS)
        log_line(f"HORDE D12: graph found {len(merged)} candidate(s); bounded scan {hx(start)}..{hx(end)}")
        linear = linear_scan(br, start, end)
        for rec in linear.get("candidates", []):
            merged[int(rec["owner_u32"])] = rec
    verified = {}
    for owner, rec in merged.items():
        chk = verify_candidate(br, owner)
        if chk:
            chk["found_by"] = rec.get("found_by") or "D11 runtime discovery"
            verified[owner] = chk
    if len(verified) != 1:
        raise BagThrowError(
            f"D12 requires exactly one Horde dual-vptr owner; found {len(verified)}; no input sent"
        )
    return {"graph": graph, "linear": linear, "chosen": next(iter(verified.values()))}


def capture_one_alive(core, br, target: dict, owner_rec: dict, first_ball_battle_turn_index: int | None = None) -> dict:
    owner = int(owner_rec["owner_u32"])
    binding = bagmod.bind_runtime_owner(
        owner,
        locator="DEV D12 D11-proven unique Horde dual-vptr runtime owner",
    )
    verified = bagmod._verify_fixed_owner(br)
    bag = owner + bagmod.BAG_OFF
    initial_state = br.read(bag + bagmod.BAG_STATE_OFF, 1)[0]
    initial_controller = br.u32(bag + bagmod.BAG_CONTROLLER_OFF)
    bag_already_open = initial_state == 1 and bagmod._valid_heap_ptr(initial_controller)

    first_ball_turn = FIRST_HORDE_BALL_TURN_INDEX if first_ball_battle_turn_index is None else max(1, int(first_ball_battle_turn_index))

    out = {
        "authority": (
            "D10 one-alive state2 + D11 unique relocated dual-vptr owner + "
            "frozen D8 adaptive Bag/Ball/outcome/post-capture lifecycle"
        ),
        "ram_writes": False,
        "runtime_binding": binding,
        "runtime_owner_verified": verified,
        "initial_bag_state": int(initial_state),
        "initial_bag_controller": hx(initial_controller),
        "initial_bag_already_open": bool(bag_already_open),
        "first_ball_battle_turn_index": first_ball_turn,
        "throws": [],
    }

    battle_turn = first_ball_turn
    bag_open = bag_already_open
    for attempt in range(1, MAX_VALIDATOR_BALLS + 1):
        no_stop()
        log_line(
            f"HORDE D12: Ball attempt {attempt}/{MAX_VALIDATOR_BALLS}; "
            f"scoring as battle turn {battle_turn}; owner={hx(owner)}"
        )
        if attempt == 1 and not bag_open:
            throw = bagmod.throw_one_best_ball(
                core, br,
                target=target,
                throw_index=battle_turn,
                method_key="horde",
                environment="horde",
                check_stop=no_stop,
                log=log_line,
            )
        else:
            if not bag_open:
                raise BagThrowError(
                    f"D12 Ball {attempt} requested without game-consumed Bag-state-1 authority"
                )
            throw = bagmod._throw_best_ball_from_open_bag(
                core, br,
                target=target,
                throw_index=battle_turn,
                method_key="horde",
                environment="horde",
                check_stop=no_stop,
                log=log_line,
            )
            bag_open = False

        outcome = bagmod._wait_throw_outcome(
            core, br,
            target=target,
            pre_throw_flow=throw.get("pre_throw_flow"),
            check_stop=no_stop,
            log=log_line,
        )
        out["throws"].append({
            "attempt": attempt,
            "battle_turn_index": battle_turn,
            "ball": (throw.get("final_selection") or {}).get("ball_name"),
            "throw": throw,
            "outcome": outcome,
        })
        if outcome.get("status") == "CAPTURED":
            out["result"] = "CAPTURED"
            out["throw_count"] = attempt
            out["last_battle_turn_index"] = battle_turn
            return out
        if outcome.get("status") == "BREAKOUT_BAG_CONSUMED_CONFIRMED":
            bag_open = True
            battle_turn += 1
            continue
        raise BagThrowError(
            "D12 unrecognized Horde Ball outcome; no retry authorized: "
            + str(outcome.get("status"))
        )
    raise BagThrowError(f"D12 reached bounded {MAX_VALIDATOR_BALLS}-Ball limit")


def save_report(profile, report: dict, code: int) -> int:
    support = profile.root / "support"
    support.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    jp = support / f"horde_dynamic_owner_capture_{stamp}.json"
    tp = support / f"horde_dynamic_owner_capture_{stamp}.txt"
    jp.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    lines = [
        "Pokebot3DS-CFW DEV D12 — Horde Dynamic Owner Capture Validator",
        f"Result: {report.get('result')}",
        f"RAM writes: {report.get('ram_writes')}",
        f"One-alive proof: {(report.get('one_alive_gate') or {}).get('proven')}",
        f"Owner: {((report.get('owner_discovery') or {}).get('chosen') or {}).get('owner')}",
        f"Bag: {((report.get('owner_discovery') or {}).get('chosen') or {}).get('bag')}",
    ]
    if report.get("capture"):
        lines.append(f"Capture: {(report.get('capture') or {}).get('result')}")
        lines.append(f"Throws: {(report.get('capture') or {}).get('throw_count')}")
    if report.get("post_capture"):
        lines.append(f"Post-capture: {(report.get('post_capture') or {}).get('result')}")
    lines.append(f"JSON: {jp}")
    tp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\nSaved:")
    print(jp)
    print(tp)
    return code


def main() -> int:
    profile = get_profile_paths()
    settings = load_settings(profile.settings_path)
    host = settings["three_ds_ip"]
    timeout = min(float(settings.get("bridge_timeout_s", 1.5)), 1.5)
    _, core, _ = load_walk_v0p23()
    br = core.Bridge(host, timeout=timeout)

    report = {
        "tool": "Pokebot3DS-CFW DEV D12 Horde Dynamic Owner Capture Validator",
        "started": now_iso(),
        "ram_writes": False,
        "live_hunt_modified": False,
        "auto_capture_base": "D8_FROZEN",
        "d10_alive_gate": "HARDWARE_PROVEN",
        "d11_horde_bag_owner": "HARDWARE_PROVEN_HEAP_RELOCATED_DUAL_VPTR",
        "automatic_attack_inputs": False,
        "automatic_capture_inputs": True,
    }

    print("=" * 78)
    print("POKEBOT3DS-CFW DEV D12 — HORDE DYNAMIC OWNER CAPTURE")
    print("Alpha Sapphire 1.4 | NO RAM WRITES | D8 CAPTURE LIFECYCLE FROZEN")
    print("=" * 78)
    print("Start with exactly ONE Horde opponent alive: leave CENTER alive.")
    print("D12 will rediscover the relocated Horde Bag owner and capture it automatically.")
    print("It also accepts the Bag already open from a just-completed D11 run.")
    print()

    try:
        gi = br.game_info()
        report["game_info"] = gi
        if int(gi.get("title_id", 0)) != AS_TITLE_ID:
            report["result"] = "REFUSED_WRONG_GAME"
            return save_report(profile, report, 2)
        if br.u32(core.BATTLE_ADDR) != core.BATTLE_ACTIVE:
            report["result"] = "REFUSED_NOT_IN_ACTIVE_BATTLE"
            return save_report(profile, report, 2)

        ids = br.read(core.TRAINER_IDS_ADDR, 4)
        tid, sid = struct.unpack("<HH", ids)
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
            report["result"] = "REFUSED_NOT_PROVEN_HORDE"
            return save_report(profile, report, 2)

        # D12 still uses the D9 center mapping only for this one-alive proof.
        # The validator is allowed on non-shiny hordes only; shiny target routing
        # will be mapped separately before automated attacks are enabled.
        shiny = [concise_slot(s) for s in opponents.get("occupied", []) if (s.get("pk6") or {}).get("is_shiny")]
        if shiny:
            report["result"] = "REFUSED_SHINY_HORDE_PRESERVE_IT"
            report["shiny_slots"] = shiny
            print("SHINY DETECTED — D12 refuses this validator. Preserve the Horde.")
            return save_report(profile, report, 3)

        gate = prove_one_alive(core, br)
        report["one_alive_gate"] = gate
        if not gate.get("proven"):
            report["result"] = "REFUSED_ONE_ALIVE_STATE2_NOT_PROVEN"
            return save_report(profile, report, 2)

        view = int(gate["view_u32"])
        outer = br.u32(core.OUTER_PTR_ADDR)
        report["outer_ptr"] = hx(outer)
        log_line(f"HORDE D12: one-alive state 2 proven at view={hx(view)}; locating relocated Bag owner")
        found = discover_unique_owner(core, br, view, outer)
        # Keep report compact: object list is useful but can be very large.
        graph = found["graph"]
        report["owner_discovery"] = {
            "seed_count": graph.get("seed_count"),
            "objects_read": graph.get("objects_read"),
            "candidate_count": len(graph.get("candidates") or []),
            "candidates": graph.get("candidates"),
            "linear": found.get("linear"),
            "chosen": found["chosen"],
        }
        owner = found["chosen"]
        gate_mask = parse_hex(gate.get("mask"))
        if gate_mask == 0:
            # Accept D11's already-open Bag state only with direct embedded-Bag proof.
            if not (int(owner.get("bag_state") or -1) == 1 and bool(owner.get("controller_heap_valid"))):
                raise BagThrowError(
                    "D12 observed one-alive state 2 with mask 0, but relocated Bag was not "
                    "already-open state 1 with valid controller; no input sent"
                )
            report["one_alive_gate"]["authority"] = "D10_STATE2 + D11_ALREADY_OPEN_BAG_STATE1"
        else:
            report["one_alive_gate"]["authority"] = "D10_STATE2_MASK100_COMMAND_MENU"
        log_line(
            f"HORDE D12: unique owner={owner.get('owner')} bag={owner.get('bag')} "
            f"state={owner.get('bag_state')}"
        )

        center_rec = slots[CENTER_SLOT]
        target = dict(center_rec.get("pk6") or {})
        target["species_name"] = SPECIES_NAMES.get(
            int(target.get("species") or 0),
            f"Species #{int(target.get('species') or 0)}",
        )
        report["center_target_candidate"] = concise_slot(center_rec)
        report["center_mapping_status"] = "D9_PP_CONSUMPTION_MAPPING; NONSHINY_VALIDATOR_ONLY"

        capture = capture_one_alive(core, br, target, owner)
        report["capture"] = capture
        if capture.get("result") != "CAPTURED":
            report["result"] = "FAILED_DYNAMIC_OWNER_CAPTURE"
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

        report["result"] = "PASS_HORDE_DYNAMIC_OWNER_CAPTURE_TO_FIELD"
        log_line("HORDE D12 PASS: relocated Horde owner + D8 capture lifecycle completed to field.")
        return save_report(profile, report, 0)

    except KeyboardInterrupt:
        report["result"] = "OPERATOR_KEYBOARD_INTERRUPT"
        return save_report(profile, report, 1)
    except Exception as exc:
        report["result"] = report.get("result") or "D12_FAILED_CLOSED"
        report["error"] = f"{type(exc).__name__}: {exc}"
        print("\nD12 failed closed:", report["error"])
        return save_report(profile, report, 2)
    finally:
        try:
            br.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
