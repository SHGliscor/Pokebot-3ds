from __future__ import annotations

"""DEV D11: map the Horde-specific DllBattle ActSelect/Bag owner at one alive.

D10 hardware proved Horde action-view state = alive_count + 1 with mask 0x100,
and state 2 / mask 0x100 restores the normal command gate when exactly one
opponent remains.  D10 then failed closed because the frozen single-battle
DllBattle owner 0x0852FC74 is zero in Horde battles.

This tool starts only at the one-alive Horde command menu.  It performs no RAM
writes.  It searches the live heap for the exact hardware-frozen dual-vtable
layout used by the normal Battle Bag object:
    owner + 0x05C == 0x007D87C0  (BtlvUiActSelect subobject vptr)
    owner + 0x1D8 == 0x007D90B4  (embedded Battle Bag vptr)

Search order:
  1) bounded pointer graph rooted at outer/view objects,
  2) bounded heap window around the live Horde action-view pointer.

Only if exactly one dual-vptr candidate exists does D11 send ONE BAG touch. It
then proves game consumption from the candidate embedded Bag state changing to
1. Firmware ACK alone is never treated as proof.  No Ball is selected/thrown.
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

from pokebot.wild.battle_bag_throw import (
    ACTSELECT_VPTR_SUBOBJECT_OFF,
    BAG_CONTROLLER_OFF,
    BAG_OFF,
    BAG_STATE_OFF,
    BAG_TOUCH_STATE,
    EXPECTED_ACTSELECT_VPTR,
    EXPECTED_BAG_VPTR,
    HEAP_MAX,
    HEAP_MIN,
)
from pokebot.wild.horde_authority import read_opponent_set
from pokebot.wild.validated_loader import load_walk_v0p23
from qt_ui.appdata_store import get_profile_paths
from qt_ui.settings_store import load_settings

AS_TITLE_ID = 0x000400000011C500
EXPECTED_MASK = 0x00000100
READ_CHUNK = 0x200
OBJECT_WINDOW = 0x400
POINTER_GRAPH_MAX_OBJECTS = 256
POINTER_GRAPH_MAX_DEPTH = 3
SCAN_RADIUS = 0x200000  # +/- 2 MiB around live Horde action-view
SCAN_PROGRESS_EVERY = 0x40000
BAG_ACCEPT_SECONDS = 3.0
BAG_POLL_SECONDS = 0.08


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


def valid_heap_ptr(v: int) -> bool:
    return HEAP_MIN <= int(v) < HEAP_MAX and (int(v) & 3) == 0


def read_window(br, address: int, length: int) -> bytes:
    out = bytearray()
    pos = 0
    while pos < length:
        n = min(READ_CHUNK, length - pos)
        out += br.read(address + pos, n)
        pos += n
    return bytes(out)


def verify_candidate(br, owner: int) -> dict | None:
    try:
        act = br.u32(owner + ACTSELECT_VPTR_SUBOBJECT_OFF)
        bag = owner + BAG_OFF
        bag_vptr = br.u32(bag)
        if act != EXPECTED_ACTSELECT_VPTR or bag_vptr != EXPECTED_BAG_VPTR:
            return None
        state = br.read(bag + BAG_STATE_OFF, 1)[0]
        controller = br.u32(bag + BAG_CONTROLLER_OFF)
        return {
            "owner": hx(owner),
            "owner_u32": owner,
            "actselect_vptr": hx(act),
            "bag": hx(bag),
            "bag_u32": bag,
            "bag_vptr": hx(bag_vptr),
            "bag_state": int(state),
            "bag_controller": hx(controller),
            "bag_controller_u32": controller,
            "controller_heap_valid": bool(valid_heap_ptr(controller)) if controller else False,
        }
    except Exception:
        return None


def extract_heap_ptrs(data: bytes) -> list[int]:
    vals = []
    seen = set()
    for off in range(0, len(data) - 3, 4):
        v = struct.unpack_from("<I", data, off)[0]
        if valid_heap_ptr(v) and v not in seen:
            seen.add(v)
            vals.append(v)
    return vals


def graph_search(br, seeds: list[int]) -> dict:
    queue = []
    seen = set()
    for s in seeds:
        if valid_heap_ptr(s):
            queue.append((s, 0, "seed"))
    candidates = {}
    objects = []

    while queue and len(seen) < POINTER_GRAPH_MAX_OBJECTS:
        addr, depth, via = queue.pop(0)
        addr &= ~3
        if addr in seen or not valid_heap_ptr(addr):
            continue
        seen.add(addr)
        try:
            data = read_window(br, addr, OBJECT_WINDOW)
        except Exception as exc:
            objects.append({"address": hx(addr), "depth": depth, "via": via, "error": str(exc)})
            continue

        obj = {"address": hx(addr), "depth": depth, "via": via}
        # Any ActSelect vptr occurrence yields an owner hypothesis.
        for off in range(0, len(data) - 3, 4):
            v = struct.unpack_from("<I", data, off)[0]
            if v == EXPECTED_ACTSELECT_VPTR:
                owner = addr + off - ACTSELECT_VPTR_SUBOBJECT_OFF
                rec = verify_candidate(br, owner)
                if rec:
                    rec["found_by"] = "pointer_graph"
                    rec["vptr_seen_in_object"] = hx(addr)
                    rec["vptr_offset"] = hx(off)
                    candidates[owner] = rec
        if depth < POINTER_GRAPH_MAX_DEPTH:
            for p in extract_heap_ptrs(data):
                if p not in seen and len(queue) < POINTER_GRAPH_MAX_OBJECTS * 4:
                    queue.append((p, depth + 1, hx(addr)))
        objects.append(obj)

    return {
        "seed_count": len(seeds),
        "objects_read": len(objects),
        "max_depth": POINTER_GRAPH_MAX_DEPTH,
        "candidates": list(candidates.values()),
        "objects": objects,
    }


def linear_scan(br, start: int, end: int) -> dict:
    start = max(HEAP_MIN, start & ~(READ_CHUNK - 1))
    end = min(HEAP_MAX, (end + READ_CHUNK - 1) & ~(READ_CHUNK - 1))
    pat = struct.pack("<I", EXPECTED_ACTSELECT_VPTR)
    candidates = {}
    read_errors = []
    scanned = 0
    next_progress = SCAN_PROGRESS_EVERY
    t0 = time.monotonic()

    addr = start
    while addr < end:
        try:
            data = br.read(addr, min(READ_CHUNK, end - addr))
        except Exception as exc:
            read_errors.append({"address": hx(addr), "error": str(exc)})
            addr += READ_CHUNK
            continue

        # aligned occurrences only
        pos = 0
        while True:
            j = data.find(pat, pos)
            if j < 0:
                break
            if (j & 3) == 0:
                owner = addr + j - ACTSELECT_VPTR_SUBOBJECT_OFF
                rec = verify_candidate(br, owner)
                if rec:
                    rec["found_by"] = "linear_heap_scan"
                    rec["act_vptr_address"] = hx(addr + j)
                    candidates[owner] = rec
            pos = j + 1

        scanned += len(data)
        if scanned >= next_progress:
            elapsed = max(0.001, time.monotonic() - t0)
            rate = scanned / elapsed / (1024 * 1024)
            pct = 100.0 * scanned / max(1, end - start)
            print(f"  Heap scan {pct:5.1f}%  {scanned//1024} KiB  {rate:.2f} MiB/s", flush=True)
            next_progress += SCAN_PROGRESS_EVERY
        addr += READ_CHUNK

    return {
        "start": hx(start),
        "end": hx(end),
        "bytes_scanned": scanned,
        "seconds": round(time.monotonic() - t0, 3),
        "read_error_count": len(read_errors),
        "read_errors": read_errors[:64],
        "candidates": list(candidates.values()),
    }


def bag_consumption_probe(br, candidate: dict) -> dict:
    owner = int(candidate["owner_u32"])
    bag = owner + BAG_OFF
    before = verify_candidate(br, owner)
    print("\nUnique dual-vptr Horde Bag owner found:")
    print(f"  owner={hx(owner)} bag={hx(bag)} state={before.get('bag_state') if before else None}")
    print("Sending ONE BAG touch. No Ball input will be sent.", flush=True)

    event = br.touch_pulse_no_retransmit(BAG_TOUCH_STATE, 120, 180)
    samples = []
    deadline = time.monotonic() + BAG_ACCEPT_SECONDS
    stable = 0
    while time.monotonic() < deadline:
        rec = verify_candidate(br, owner)
        if rec is None:
            samples.append({"candidate_valid": False})
            stable = 0
        else:
            sample = {
                "candidate_valid": True,
                "bag_state": rec["bag_state"],
                "bag_controller": rec["bag_controller"],
                "controller_heap_valid": rec["controller_heap_valid"],
            }
            samples.append(sample)
            good = rec["bag_state"] == 1 and rec["controller_heap_valid"]
            stable = stable + 1 if good else 0
            if stable >= 2:
                return {
                    "input": event,
                    "consumed": True,
                    "authority": "same unique dual-vptr owner + embedded Bag state 1 + valid controller pointer",
                    "samples": samples,
                    "final": rec,
                }
        time.sleep(BAG_POLL_SECONDS)
    return {
        "input": event,
        "consumed": False,
        "authority": "firmware ACK alone rejected; Bag state 1 not stably observed",
        "samples": samples,
        "final": verify_candidate(br, owner),
    }


def save_report(profile, report: dict, code: int) -> int:
    support = profile.root / "support"
    support.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    jp = support / f"horde_bag_authority_mapper_{stamp}.json"
    tp = support / f"horde_bag_authority_mapper_{stamp}.txt"
    jp.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    lines = [
        "Pokebot3DS-CFW DEV D11 — Horde Bag Authority Mapper",
        f"Result: {report.get('result')}",
        f"RAM writes: {report.get('ram_writes')}",
        f"One automatic BAG touch only after unique dual-vptr proof: {report.get('automatic_bag_touch_after_unique_proof')}",
        f"View: {report.get('one_alive_view')}",
        f"Graph candidates: {len(((report.get('pointer_graph') or {}).get('candidates') or []))}",
        f"Linear candidates: {len(((report.get('linear_scan') or {}).get('candidates') or []))}",
    ]
    chosen = report.get("chosen_candidate") or {}
    if chosen:
        lines += [f"Chosen owner: {chosen.get('owner')}", f"Chosen bag: {chosen.get('bag')}"]
    if report.get("bag_probe"):
        lines.append(f"BAG consumed: {(report.get('bag_probe') or {}).get('consumed')}")
    if report.get("error"):
        lines.append(f"Error: {report.get('error')}")
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

    report: dict = {
        "tool": "Pokebot3DS-CFW DEV D11 Horde Bag Authority Mapper",
        "started": now_iso(),
        "ram_writes": False,
        "live_hunt_modified": False,
        "auto_capture_base": "D8_FROZEN",
        "d10_alive_relation_status": "HARDWARE_PROVEN_5_TO_1",
        "automatic_attack_inputs": False,
        "automatic_ball_inputs": False,
        "automatic_bag_touch_after_unique_proof": True,
        "expected_actselect_vptr": hx(EXPECTED_ACTSELECT_VPTR),
        "expected_bag_vptr": hx(EXPECTED_BAG_VPTR),
        "normal_single_battle_owner": "0x0852FC74",
    }

    print("=" * 78)
    print("POKEBOT3DS-CFW DEV D11 — HORDE BAG AUTHORITY MAPPER")
    print("Alpha Sapphire 1.4 | ONE ALIVE HORDE ONLY | NO RAM WRITES")
    print("=" * 78)
    print("Reduce a NON-SHINY Horde to exactly ONE opponent and stop at")
    print("FIGHT / BAG / POKEMON / RUN before starting this mapper.")
    print("D11 sends no attacks and no Ball inputs.")
    print("It sends ONE BAG touch only after a UNIQUE dual-vptr owner is proven.\n")

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
        report["opponent_classification"] = opponents.get("classification")
        report["opponent_size"] = opponents.get("size")
        if not opponents.get("valid") or opponents.get("classification") != "HORDE":
            report["result"] = "REFUSED_NOT_PROVEN_HORDE"
            return save_report(profile, report, 2)
        if any((s.get("pk6") or {}).get("is_shiny") for s in opponents.get("occupied", [])):
            report["result"] = "REFUSED_SHINY_HORDE_PRESERVE_IT"
            return save_report(profile, report, 3)

        gate_samples = []
        stable = 0
        one = None
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            g = core.read_gate(br)
            gate_samples.append(g)
            state = parse_hex(g.get("state"))
            mask = parse_hex(g.get("mask"))
            good = bool(g.get("valid_view")) and state == 2 and mask == EXPECTED_MASK and bool(g.get("gate"))
            stable = stable + 1 if good else 0
            if stable >= 2:
                one = g
                break
            time.sleep(0.18)
        report["one_alive_gate_samples"] = gate_samples
        if one is None:
            report["result"] = "REFUSED_ONE_ALIVE_STATE2_NOT_PROVEN"
            return save_report(profile, report, 2)

        view = parse_hex(one.get("view")) or 0
        outer = parse_hex(one.get("outer")) or br.u32(core.OUTER_PTR_ADDR)
        report["one_alive_view"] = hx(view)
        report["one_alive_outer"] = hx(outer)
        report["one_alive_state"] = one.get("state")
        report["one_alive_mask"] = one.get("mask")
        report["one_alive_command_gate"] = bool(one.get("gate"))

        # Explicitly record the D10 failure condition at the frozen normal owner.
        try:
            normal_act = br.u32(0x0852FC74 + ACTSELECT_VPTR_SUBOBJECT_OFF)
            normal_bag = br.u32(0x0852FC74 + BAG_OFF)
        except Exception:
            normal_act = normal_bag = None
        report["normal_owner_at_horde"] = {
            "actselect_vptr": hx(normal_act),
            "bag_vptr": hx(normal_bag),
        }

        seeds = [outer, view]
        # Seed every heap pointer directly exposed by outer/view windows.
        for base in [outer, view]:
            if valid_heap_ptr(base):
                try:
                    seeds.extend(extract_heap_ptrs(read_window(br, base, OBJECT_WINDOW)))
                except Exception:
                    pass
        # stable de-dupe
        seeds = list(dict.fromkeys(p for p in seeds if valid_heap_ptr(p)))
        print(f"Pointer-graph scan from {len(seeds)} live seeds...", flush=True)
        graph = graph_search(br, seeds)
        report["pointer_graph"] = graph

        merged = {int(c["owner_u32"]): c for c in graph.get("candidates", [])}
        if len(merged) != 1:
            start = max(HEAP_MIN, view - SCAN_RADIUS)
            end = min(HEAP_MAX, view + SCAN_RADIUS)
            print(f"No unique graph candidate; scanning heap {hx(start)}..{hx(end)}...", flush=True)
            linear = linear_scan(br, start, end)
            report["linear_scan"] = linear
            for c in linear.get("candidates", []):
                merged[int(c["owner_u32"])] = c
        else:
            report["linear_scan"] = {"skipped": "unique pointer-graph candidate"}

        report["all_unique_candidates"] = list(merged.values())
        if len(merged) == 0:
            report["result"] = "NO_HORDE_DUAL_VPTR_OWNER_FOUND"
            return save_report(profile, report, 2)
        if len(merged) != 1:
            report["result"] = "AMBIGUOUS_HORDE_DUAL_VPTR_OWNERS"
            return save_report(profile, report, 2)

        candidate = next(iter(merged.values()))
        report["chosen_candidate"] = candidate
        probe = bag_consumption_probe(br, candidate)
        report["bag_probe"] = probe
        if not probe.get("consumed"):
            report["result"] = "UNIQUE_OWNER_FOUND_BUT_BAG_TOUCH_NOT_CONSUMED"
            return save_report(profile, report, 2)

        report["result"] = "PASS_HORDE_BAG_OWNER_AND_CONSUMED_BAG_TOUCH"
        print("\nD11 PASS: Horde-specific dual-vptr owner located and BAG touch consumed by game.")
        print("Leave the 3DS as-is; the report contains the owner/bag addresses for integration.")
        return save_report(profile, report, 0)

    except KeyboardInterrupt:
        report["result"] = "OPERATOR_KEYBOARD_INTERRUPT"
        return save_report(profile, report, 1)
    except Exception as exc:
        report["result"] = report.get("result") or "D11_FAILED_CLOSED"
        report["error"] = f"{type(exc).__name__}: {exc}"
        print("\nD11 failed closed:", report["error"])
        return save_report(profile, report, 2)
    finally:
        try:
            br.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
