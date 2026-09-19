from __future__ import annotations

"""Interactive ORAS post-capture RAM mapper.

Purpose: map the nickname / optional Pokédex / Box-message branch using ordinary
catches, without waiting for a shiny and without modifying game RAM.

The probe intentionally does not assume that CRO residency equals a visible
screen. It records RAM snapshots before/after each user-verified screen and only
sends a controller input when the operator explicitly chooses it.
"""

import argparse
import json
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pokebot.wild.validated_loader import load_walk_v0p23
from pokebot.wild.post_capture_ram import (
    CRO_SCAN_END,
    CRO_SCAN_START,
    _query_region,
    read_cro_header,
)
from qt_ui.appdata_store import get_profile_paths
from qt_ui.settings_store import load_settings

AS_TITLE_ID = 0x000400000011C500

# Alpha Sapphire 1.4 anchors already used by the validated wild backend / Bag mapper.
BATTLE_ADDR = 0x081FB478
FLOW_ADDR = 0x081FB390
OUTER_PTR_ADDR = 0x081FB384
WILD_PK6_ADDR = 0x081FFA6C
BATTLE_OWNER = 0x0852FC74

HID_NEUTRAL = 0x00000FFF
HID_A = HID_NEUTRAL & ~(1 << 0)
HID_B = HID_NEUTRAL & ~(1 << 1)
HID_UP = HID_NEUTRAL & ~(1 << 6)
HID_DOWN = HID_NEUTRAL & ~(1 << 7)

BUTTONS = {
    "A": HID_A,
    "B": HID_B,
    "UP": HID_UP,
    "DOWN": HID_DOWN,
}

HEAP_MIN = 0x08000000
HEAP_MAX = 0x10000000
MAX_POINTER_TARGETS = 24
POINTER_WINDOW_BYTES = 0x100
SAMPLES_PER_CHECKPOINT = 3
SAMPLE_GAP_SECONDS = 0.08

FIXED_RANGES = [
    ("battle_globals", 0x081FB300, 0x200),
    ("wild_pk6_neighborhood", 0x081FF900, 0x600),
    ("battle_owner_neighborhood", 0x0852FA00, 0x800),
]


def hx(v: int | None) -> str | None:
    return None if v is None else f"0x{int(v) & 0xFFFFFFFF:08X}"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def safe_read_span(br, address: int, length: int) -> bytes | None:
    try:
        return br.read_span(address, length)
    except Exception:
        return None


def safe_u32(br, address: int) -> int | None:
    try:
        return br.u32(address)
    except Exception:
        return None


def enumerate_cros(br) -> dict:
    cursor = CRO_SCAN_START
    seen = set()
    rows = []
    query_count = 0
    errors = []
    while cursor < CRO_SCAN_END and query_count < 1024:
        query_count += 1
        try:
            q = _query_region(br, cursor)
        except Exception as exc:
            errors.append({"address": hx(cursor), "error": f"{type(exc).__name__}: {exc}"})
            cursor += 0x1000
            continue
        base = int(q.get("base", 0))
        size = int(q.get("size", 0))
        if q.get("status") != 0 or size <= 0:
            cursor += 0x1000
            continue
        if base not in seen and CRO_SCAN_START <= base < CRO_SCAN_END:
            seen.add(base)
            rec = read_cro_header(br, base)
            if rec:
                rows.append({
                    "module": rec.get("module"),
                    "base": rec.get("base"),
                    "magic": rec.get("magic"),
                    "file_size_hex": rec.get("file_size_hex"),
                    "bss_size_hex": rec.get("bss_size_hex"),
                })
        nxt = max(cursor + 0x1000, base + size)
        cursor = nxt if nxt > cursor else cursor + 0x1000
    return {
        "query_count": query_count,
        "bounded": query_count < 1024 or cursor >= CRO_SCAN_END,
        "modules": rows,
        "errors": errors[:8],
    }


def find_heap_pointers(blob: bytes, base: int) -> list[dict]:
    out = []
    seen = set()
    for off in range(0, len(blob) - 3, 4):
        value = struct.unpack_from("<I", blob, off)[0]
        if HEAP_MIN <= value < HEAP_MAX and (value & 3) == 0 and value not in seen:
            seen.add(value)
            out.append({"source": hx(base + off), "target": value})
            if len(out) >= MAX_POINTER_TARGETS:
                break
    return out


def one_memory_sample(br, *, include_pointers: bool = True) -> dict:
    ranges = []
    seed_blobs = []
    for name, addr, length in FIXED_RANGES:
        raw = safe_read_span(br, addr, length)
        row = {"name": name, "base": hx(addr), "length": length, "ok": raw is not None}
        if raw is not None:
            row["hex"] = raw.hex()
            seed_blobs.append((addr, raw))
        ranges.append(row)

    # Follow a bounded set of heap pointers exposed by the battle/global objects.
    # Timed post-input checkpoints can disable this crawl so the first sample
    # begins close to the requested delay instead of spending seconds walking
    # secondary objects.
    pointer_targets = []
    if include_pointers:
        seen_targets = set()
        for base, raw in seed_blobs:
            for ptr in find_heap_pointers(raw, base):
                target = int(ptr["target"])
                if target in seen_targets:
                    continue
                seen_targets.add(target)
                window_base = target
                data = safe_read_span(br, window_base, POINTER_WINDOW_BYTES)
                pointer_targets.append({
                    "source": ptr["source"],
                    "base": hx(window_base),
                    "length": POINTER_WINDOW_BYTES,
                    "ok": data is not None,
                    "hex": data.hex() if data is not None else None,
                })
                if len(pointer_targets) >= MAX_POINTER_TARGETS:
                    break
            if len(pointer_targets) >= MAX_POINTER_TARGETS:
                break

    return {
        "time": now_iso(),
        "anchors": {
            "battle": hx(safe_u32(br, BATTLE_ADDR)),
            "flow": hx(safe_u32(br, FLOW_ADDR)),
            "outer_ptr": hx(safe_u32(br, OUTER_PTR_ADDR)),
            "owner_word0": hx(safe_u32(br, BATTLE_OWNER)),
        },
        "ranges": ranges,
        "pointer_targets": pointer_targets,
    }


def checkpoint(br, label: str, note: str | None = None, include_cros: bool = True, *, sample_count: int = SAMPLES_PER_CHECKPOINT, include_pointers: bool = True) -> dict:
    samples = []
    count = max(1, int(sample_count))
    for i in range(count):
        samples.append(one_memory_sample(br, include_pointers=include_pointers))
        if i + 1 < count:
            time.sleep(SAMPLE_GAP_SECONDS)
    return {
        "label": label,
        "note": note,
        "time": now_iso(),
        "samples": samples,
        "cros": enumerate_cros(br) if include_cros else None,
    }


def stable_bytes(cp: dict) -> dict[int, int]:
    """Address->byte for bytes stable across all samples in a checkpoint."""
    samples = cp.get("samples") or []
    if not samples:
        return {}
    maps = []
    for sample in samples:
        m = {}
        for row in (sample.get("ranges") or []) + (sample.get("pointer_targets") or []):
            if not row.get("ok") or not row.get("hex"):
                continue
            base = int(row["base"], 16)
            raw = bytes.fromhex(row["hex"])
            for i, b in enumerate(raw):
                m[base + i] = b
        maps.append(m)
    common = set(maps[0])
    for m in maps[1:]:
        common &= set(m)
    out = {}
    for a in common:
        vals = [m[a] for m in maps]
        if len(set(vals)) == 1:
            out[a] = vals[0]
    return out


def transition_diff(before: dict, after: dict, limit: int = 256) -> dict:
    a = stable_bytes(before)
    b = stable_bytes(after)
    changed = []
    for addr in sorted(set(a) & set(b)):
        if a[addr] != b[addr]:
            changed.append({"address": hx(addr), "before": a[addr], "after": b[addr]})
            if len(changed) >= limit:
                break
    return {
        "from": before.get("label"),
        "to": after.get("label"),
        "stable_changed_count_capped": len(changed),
        "changes": changed,
    }


def send_button(br, button: str, hold_ms: int = 180, settle_ms: int = 350) -> dict:
    button = button.upper()
    raw = BUTTONS[button]
    rec = br.hid_pulse_no_retransmit(raw, hold_ms, settle_ms)
    if not rec.get("completed"):
        raise RuntimeError(f"{button} input did not reach acknowledged completion: {rec}")
    return {"time": now_iso(), "button": button, "raw_hid": hx(raw), "result": rec}


def ask_yes_no(prompt: str) -> bool:
    while True:
        v = input(prompt + " [y/n]: ").strip().lower()
        if v in ("y", "yes"):
            return True
        if v in ("n", "no"):
            return False


def ask_screen() -> tuple[str, str]:
    print("\nWhat is visible on the 3DS NOW?")
    print("  N = nickname Yes/No")
    print("  P = Pokédex / new Pokédex registration")
    print("  X = sent-to-Box / post-capture text")
    print("  F = overworld / field")
    print("  O = other")
    while True:
        v = input("Screen [N/P/X/F/O]: ").strip().upper()
        if v in {"N", "P", "X", "F", "O"}:
            names = {
                "N": "NICKNAME",
                "P": "POKEDEX",
                "X": "BOX_MESSAGE",
                "F": "FIELD",
                "O": "OTHER",
            }
            note = ""
            if v == "O":
                note = input("Briefly describe the screen: ").strip()
            return names[v], note


def main() -> int:
    ap = argparse.ArgumentParser(description="Alpha Sapphire 1.4 post-capture RAM mapper")
    ap.add_argument("--expected", choices=["registered", "unregistered"], required=True)
    args = ap.parse_args()

    profile = get_profile_paths()
    settings = load_settings(profile.settings_path)
    host = settings["three_ds_ip"]
    timeout = min(float(settings["bridge_timeout_s"]), 1.5)

    _, core, _ = load_walk_v0p23()
    br = core.Bridge(host, timeout=timeout)

    print("=" * 72)
    print("Pokebot3DS-CFW POST-CAPTURE MAPPER PROBE v0p42ZV")
    print("Alpha Sapphire 1.4 | ordinary catches are sufficient | NO RAM WRITES")
    print("=" * 72)
    print(f"Branch: {args.expected.upper()}")
    gi = br.game_info()
    if int(gi.get("title_id", 0)) != AS_TITLE_ID:
        raise SystemExit("Alpha Sapphire is not the active bridge process: " + str(gi))
    caps = br.input_ping()
    if not caps.get("hid_pulse"):
        raise SystemExit("Acknowledged HID pulse capability is not available: " + str(caps))
    br.release_all()

    if args.expected == "registered":
        print("\nPREPARE THE 3DS - REGISTERED SPECIES:")
        print("  1. Catch ANY ordinary wild Pokemon that is ALREADY registered in the Pokedex.")
        print("  2. Leave the game on the NICKNAME YES/NO screen with YES selected.")
        print("  3. Do not press anything else on the 3DS.")
        input("\nWhen the nickname Yes/No screen is visibly waiting, press ENTER here...")
    else:
        print("\nPREPARE THE 3DS - UNREGISTERED SPECIES:")
        print("  1. Catch ANY ordinary wild Pokemon that is NOT registered in the Pokedex.")
        print("  2. Leave the game on the FIRST POKEDEX registration screen.")
        print("  3. Do not press anything else on the 3DS.")
        input("\nWhen the Pokedex screen is visibly waiting, press ENTER here...")

    report = {
        "tool": "Pokebot3DS-CFW Post-Capture Mapper Probe v0p42ZV",
        "expected_branch": args.expected,
        "expected_sequence": (
            ["NICKNAME", "BOX_MESSAGE", "FIELD"]
            if args.expected == "registered"
            else ["POKEDEX", "NICKNAME", "BOX_MESSAGE", "FIELD"]
        ),
        "started": now_iso(),
        "game_info": gi,
        "input_caps": caps,
        "ram_writes": False,
        "operator_verified": [],
        "inputs": [],
        "checkpoints": [],
        "transitions": [],
        "result": "RUNNING",
    }

    def take(label: str, note: str | None = None, cros: bool = True, *, sample_count: int = SAMPLES_PER_CHECKPOINT, include_pointers: bool = True):
        print(f"  RAM snapshot: {label} ...")
        cp = checkpoint(br, label, note, include_cros=cros, sample_count=sample_count, include_pointers=include_pointers)
        report["checkpoints"].append(cp)
        if len(report["checkpoints"]) >= 2:
            report["transitions"].append(
                transition_diff(report["checkpoints"][-2], report["checkpoints"][-1])
            )
        return cp

    def press(button: str, hold=180, settle=350):
        print(f"  Sending acknowledged {button} ...")
        rec = send_button(br, button, hold, settle)
        report["inputs"].append(rec)
        return rec

    def verify(question: str, failure_result: str) -> bool:
        ok = ask_yes_no(question)
        report["operator_verified"].append({"time": now_iso(), "question": question, "answer": ok})
        if not ok:
            report["result"] = failure_result
        return ok

    # ------------------------------------------------------------------
    # UNREGISTERED-ONLY first branch: Pokédex is the first post-catch UI.
    # Hardware observation established that one A advances directly from
    # Pokédex registration to the nickname Yes/No prompt.
    # ------------------------------------------------------------------
    if args.expected == "unregistered":
        take("pokedex_initial", "operator verified first Pokédex registration screen is visible")
        report["operator_verified"].append({"time": now_iso(), "screen": "POKEDEX", "answer": True})

        press("A", 180, 500)
        # Fast snapshots preserve the immediate transition without a CRO crawl
        # delaying the first observation. The resulting nickname screen waits.
        for delay, label in (
            (0.20, "after_pokedex_A_200ms"),
            (0.65, "after_pokedex_A_850ms"),
            (0.90, "after_pokedex_A_1750ms"),
        ):
            time.sleep(delay)
            take(label, "A sent from Pokédex; expecting nickname prompt", cros=False, sample_count=1, include_pointers=False)

        if not verify("Is the NICKNAME Yes/No prompt now visible with Yes selected?", "POKEDEX_A_DID_NOT_REACH_NICKNAME"):
            return save_report(profile, report)
        take("nickname_yes_selected_initial", "reached directly from Pokédex A")
        report["operator_verified"].append({"time": now_iso(), "screen": "NICKNAME", "answer": True})
    else:
        # Registered species skip Pokédex entirely and begin here.
        take("nickname_yes_selected_initial", "registered species: nickname Yes/No is first post-catch screen")
        report["operator_verified"].append({"time": now_iso(), "screen": "NICKNAME", "answer": True})

    # ------------------------------------------------------------------
    # Common nickname mapping: Yes -> Down -> No -> A confirm.
    # ------------------------------------------------------------------
    press("DOWN", 180, 450)
    time.sleep(0.20)
    take("nickname_no_selected_after_down", "DOWN sent once from nickname Yes")
    if not verify("Did the visible nickname cursor move from Yes to No?", "NICKNAME_DOWN_NOT_ACCEPTED"):
        return save_report(profile, report)

    press("A", 180, 500)
    for delay, label in (
        (0.20, "after_nickname_no_A_200ms"),
        (0.65, "after_nickname_no_A_850ms"),
        (0.90, "after_nickname_no_A_1750ms"),
    ):
        time.sleep(delay)
        take(label, "nickname No confirmed with A; expecting Box/post-capture message", cros=False, sample_count=1, include_pointers=False)

    if not verify("Is the 'sent to a Box' / post-capture message now visible?", "NICKNAME_NO_DID_NOT_REACH_BOX_MESSAGE"):
        # Record whatever is actually visible without guessing another input.
        screen, note = ask_screen()
        report["operator_verified"].append({"time": now_iso(), "screen": screen, "note": note})
        take(f"unexpected_after_nickname_{screen.lower()}", note or f"operator identified {screen}")
        return save_report(profile, report)

    take("box_message_waiting", "operator verified sent-to-Box/post-capture message")
    report["operator_verified"].append({"time": now_iso(), "screen": "BOX_MESSAGE", "answer": True})

    # One A acknowledges the Box message. Then map the field return.
    press("A", 180, 500)
    for delay, label in (
        (0.20, "after_box_A_200ms"),
        (0.65, "after_box_A_850ms"),
        (0.90, "after_box_A_1750ms"),
    ):
        time.sleep(delay)
        take(label, "A sent from Box/post-capture message; expecting field", cros=False, sample_count=1, include_pointers=False)

    if not verify("Is the overworld / field now visible and controllable?", "BOX_A_DID_NOT_REACH_FIELD"):
        screen, note = ask_screen()
        report["operator_verified"].append({"time": now_iso(), "screen": screen, "note": note})
        take(f"unexpected_after_box_{screen.lower()}", note or f"operator identified {screen}")
        return save_report(profile, report)

    take("field_return", "operator verified overworld/field returned")
    report["operator_verified"].append({"time": now_iso(), "screen": "FIELD", "answer": True})
    report["result"] = "MAPPED_TO_FIELD"
    return save_report(profile, report)

def save_report(profile, report: dict) -> int:
    report["finished"] = now_iso()
    out_dir = profile.root / "support"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    branch = report.get("expected_branch", "unknown")
    out = out_dir / f"post_capture_mapper_{branch}_{stamp}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    summary = out.with_suffix(".txt")
    screens = [x.get("screen") for x in report.get("operator_verified", []) if x.get("screen")]
    summary.write_text(
        "Pokebot3DS-CFW Post-Capture Mapper v0p42ZV\n"
        f"Result: {report.get('result')}\n"
        f"Expected branch: {branch}\n"
        f"Operator screens: {' -> '.join(screens) if screens else '-'}\n"
        f"Inputs: {' -> '.join(x.get('button','?') for x in report.get('inputs', [])) or '-'}\n"
        f"JSON: {out}\n",
        encoding="utf-8",
    )
    try:
        br_release = None
    except Exception:
        pass
    print("\n" + "=" * 72)
    print("PROBE SAVED")
    print(f"Result: {report.get('result')}")
    print(f"JSON : {out}")
    print(f"Text : {summary}")
    print("Upload the JSON after a registered-species run and an unregistered-species run.")
    print("=" * 72)
    return 0 if report.get("result") == "MAPPED_TO_FIELD" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nProbe cancelled by user. No RAM writes were performed.")
        raise SystemExit(130)
