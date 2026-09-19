from __future__ import annotations

import json
import os
import re
import struct
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

TOOL = "Pokebot3DS-CFW Capture Trace Diff Analyzer v0p43AL"
VPTR_EVENT_BATTLE_RETURN = 0x005DF09C
EVENT_BATTLE_RETURN_STATE_OFF = 0x48
MILESTONE_RE = re.compile(r"^POST_THROW_T_(\d+(?:\.\d+)?)$")
SCALAR_FIELDS = (
    "battle_u32", "flow_u32", "outer_ptr_u32", "view_state", "view_mask",
    "command_gate", "bag_state", "bag_controller", "wild_pk6_sha256",
)


def now_stamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")


def app_support() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home()))) / "Pokebot-3DS" / "support"


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        obj = json.load(f)
    if not isinstance(obj, dict):
        raise ValueError(f"{path}: top-level JSON must be an object")
    return obj


def role(report: dict[str, Any]) -> str | None:
    result = str(report.get("result") or "").upper()
    if "SUCCESS" in result:
        return "success"
    if "BREAKOUT" in result:
        return "breakout"
    return None


def discover() -> tuple[Path, Path]:
    root = app_support()
    files = sorted(root.glob("capture_lifecycle_mapper_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    found: dict[str, Path] = {}
    for p in files:
        try:
            r = role(load_json(p))
        except Exception:
            continue
        if r and r not in found:
            found[r] = p
        if len(found) == 2:
            break
    if "success" not in found or "breakout" not in found:
        raise FileNotFoundError(
            "Could not auto-find both a SUCCESSFUL capture lifecycle JSON and an "
            "OPERATOR_CONFIRMED_REAL_BREAKOUT lifecycle JSON in " + str(root)
        )
    return found["success"], found["breakout"]


def choose_paths(argv: list[str]) -> tuple[Path, Path]:
    if len(argv) >= 3:
        a, b = Path(argv[1]), Path(argv[2])
        ra, rb = load_json(a), load_json(b)
        roles = {role(ra), role(rb)}
        if roles != {"success", "breakout"}:
            raise ValueError("Supplied traces must contain one SUCCESS result and one BREAKOUT result")
        return (a, b) if role(ra) == "success" else (b, a)
    return discover()


def parse_hex_blob(value: Any) -> bytes | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return bytes.fromhex(value)
    except ValueError:
        return None


def parse_addr(v: Any) -> int | None:
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        try:
            return int(v, 0)
        except ValueError:
            return None
    return None


def milestone_map(report: dict[str, Any]) -> dict[float, dict[str, Any]]:
    out: dict[float, dict[str, Any]] = {}
    for s in report.get("broad_snapshots") or []:
        if not isinstance(s, dict):
            continue
        m = MILESTONE_RE.match(str(s.get("label") or ""))
        if m:
            out[float(m.group(1))] = s
    return out


def broad_window_diff(success: dict[str, Any], breakout: dict[str, Any], prefix: str) -> dict[str, Any]:
    sm = milestone_map(success)
    bm = milestone_map(breakout)
    common = sorted(set(sm) & set(bm))
    hex_key = f"{prefix}_hex"
    base_key = f"{prefix}_base"
    size_key = f"{prefix}_size"
    observations: dict[int, list[tuple[float, int, int]]] = defaultdict(list)
    compared = []
    warnings = []

    for t in common:
        ss, bs = sm[t], bm[t]
        sb, bb = parse_hex_blob(ss.get(hex_key)), parse_hex_blob(bs.get(hex_key))
        sbase, bbase = parse_addr(ss.get(base_key)), parse_addr(bs.get(base_key))
        if sb is None or bb is None or sbase is None or bbase is None:
            warnings.append(f"{prefix}: missing/unreadable broad data at T+{t:.3f}")
            continue
        if sbase != bbase:
            warnings.append(f"{prefix}: base mismatch at T+{t:.3f}: {hex(sbase)} vs {hex(bbase)}")
            continue
        n = min(len(sb), len(bb))
        diff_count = 0
        for i in range(n):
            if sb[i] != bb[i]:
                observations[sbase + i].append((t, sb[i], bb[i]))
                diff_count += 1
        compared.append({"t": t, "bytes": n, "different_bytes": diff_count})

    candidates = []
    compared_times = [x["t"] for x in compared]
    for addr, obs in observations.items():
        obs.sort()
        times = [x[0] for x in obs]
        first = times[0]
        diff_times = set(times)
        after_first = [t for t in compared_times if t >= first]
        persistent = bool(after_first) and all(t in diff_times for t in after_first)
        # consecutive means the first two available common milestones from first divergence differ.
        consecutive = False
        if len(after_first) >= 2:
            consecutive = after_first[0] in diff_times and after_first[1] in diff_times
        vals_s = sorted({v for _, v, _ in obs})
        vals_b = sorted({v for _, _, v in obs})
        candidates.append({
            "address": f"0x{addr:08X}",
            "earliest_diff_t": first,
            "diff_count": len(obs),
            "compared_milestones": len(compared_times),
            "diff_fraction": round(len(obs) / max(1, len(compared_times)), 4),
            "persistent_from_first_diff": persistent,
            "consecutive_from_first_diff": consecutive,
            "success_byte_values": [f"0x{x:02X}" for x in vals_s[:16]],
            "breakout_byte_values": [f"0x{x:02X}" for x in vals_b[:16]],
            "observations": [
                {"t": t, "success": f"0x{sv:02X}", "breakout": f"0x{bv:02X}"}
                for t, sv, bv in obs[:12]
            ],
        })

    candidates.sort(key=lambda c: (
        -int(c["persistent_from_first_diff"]),
        -int(c["consecutive_from_first_diff"]),
        c["earliest_diff_t"],
        -c["diff_count"],
        c["address"],
    ))

    # Also generate aligned u32 candidates for easier static correlation.
    word_obs: dict[int, list[tuple[float, int, int]]] = defaultdict(list)
    for t in common:
        ss, bs = sm[t], bm[t]
        sb, bb = parse_hex_blob(ss.get(hex_key)), parse_hex_blob(bs.get(hex_key))
        base = parse_addr(ss.get(base_key))
        if sb is None or bb is None or base is None or base != parse_addr(bs.get(base_key)):
            continue
        n = min(len(sb), len(bb)) & ~3
        for i in range(0, n, 4):
            sv = struct.unpack_from("<I", sb, i)[0]
            bv = struct.unpack_from("<I", bb, i)[0]
            if sv != bv:
                word_obs[base + i].append((t, sv, bv))
    words = []
    for addr, obs in word_obs.items():
        obs.sort()
        first = obs[0][0]
        times = {x[0] for x in obs}
        after_first = [t for t in compared_times if t >= first]
        words.append({
            "address": f"0x{addr:08X}",
            "earliest_diff_t": first,
            "diff_count": len(obs),
            "diff_fraction": round(len(obs) / max(1, len(compared_times)), 4),
            "persistent_from_first_diff": bool(after_first) and all(t in times for t in after_first),
            "success_values": sorted({f"0x{x[1]:08X}" for x in obs})[:12],
            "breakout_values": sorted({f"0x{x[2]:08X}" for x in obs})[:12],
            "observations": [
                {"t": t, "success": f"0x{sv:08X}", "breakout": f"0x{bv:08X}"}
                for t, sv, bv in obs[:10]
            ],
        })
    words.sort(key=lambda c: (-int(c["persistent_from_first_diff"]), c["earliest_diff_t"], -c["diff_count"], c["address"]))

    return {
        "window": prefix,
        "common_milestones": common,
        "compared": compared,
        "candidate_bytes_ranked": candidates[:500],
        "candidate_u32_ranked": words[:300],
        "warnings": warnings,
    }


def anchor_after_throw_t(report: dict[str, Any]) -> float:
    a = report.get("timeline_anchor_after_throw") or {}
    try:
        return float(a.get("t") or 0.0)
    except Exception:
        return 0.0


def nearest_sample(report: dict[str, Any], target_rel: float) -> dict[str, Any] | None:
    samples = report.get("timeline_samples") or []
    if not samples:
        return None
    anchor = anchor_after_throw_t(report)
    target_abs = anchor + target_rel
    best = None
    best_d = 1e99
    for s in samples:
        if not isinstance(s, dict):
            continue
        try:
            d = abs(float(s.get("t")) - target_abs)
        except Exception:
            continue
        if d < best_d:
            best_d, best = d, s
    return best


def scalar_diff(success: dict[str, Any], breakout: dict[str, Any], times: list[float]) -> list[dict[str, Any]]:
    rows = []
    for t in times:
        ss, bs = nearest_sample(success, t), nearest_sample(breakout, t)
        if not ss or not bs:
            continue
        changes = {}
        for k in SCALAR_FIELDS:
            sv, bv = ss.get(k), bs.get(k)
            if sv != bv:
                changes[k] = {"success": sv, "breakout": bv}
        rows.append({
            "t_after_throw": t,
            "success_sample_t": ss.get("t"),
            "breakout_sample_t": bs.get("t"),
            "differences": changes,
        })
    return rows


def find_vptr_hits(report: dict[str, Any]) -> dict[str, Any]:
    hits = []
    pat = struct.pack("<I", VPTR_EVENT_BATTLE_RETURN)
    for snap in report.get("broad_snapshots") or []:
        if not isinstance(snap, dict):
            continue
        label = snap.get("label")
        for prefix in ("global", "owner", "outer_object", "view_object"):
            blob = parse_hex_blob(snap.get(f"{prefix}_hex"))
            base = parse_addr(snap.get(f"{prefix}_base"))
            if blob is None or base is None:
                continue
            start = 0
            while True:
                i = blob.find(pat, start)
                if i < 0:
                    break
                obj = base + i
                state = None
                off = i + EVENT_BATTLE_RETURN_STATE_OFF
                if off + 4 <= len(blob):
                    state = struct.unpack_from("<I", blob, off)[0]
                hits.append({
                    "label": label,
                    "t": snap.get("t"),
                    "window": prefix,
                    "object_candidate": f"0x{obj:08X}",
                    "state_at_plus_0x48": state,
                    "state_hex": None if state is None else f"0x{state:08X}",
                    "plausible_state_0_to_7": state is not None and 0 <= state <= 7,
                })
                start = i + 1
    return {
        "static_vptr": f"0x{VPTR_EVENT_BATTLE_RETURN:08X}",
        "state_offset": "0x48",
        "hits": hits,
        "plausible_hits": [h for h in hits if h["plausible_state_0_to_7"]],
    }


def summary_text(out: dict[str, Any]) -> str:
    lines = [
        TOOL,
        f"Success trace: {out['inputs']['success']}",
        f"Breakout trace: {out['inputs']['breakout']}",
        f"Success result: {out['roles']['success_result']}",
        f"Breakout result: {out['roles']['breakout_result']}",
        "",
    ]
    for key in ("global", "owner"):
        d = out["broad_diff"].get(key) or {}
        lines.append(f"{key.upper()} BROAD WINDOW")
        lines.append(f"  Common post-throw milestones: {len(d.get('common_milestones') or [])}")
        lines.append(f"  Ranked differing bytes: {len(d.get('candidate_bytes_ranked') or [])}")
        for c in (d.get("candidate_bytes_ranked") or [])[:12]:
            lines.append(
                f"  {c['address']} first=T+{c['earliest_diff_t']:.3f}s "
                f"diff={c['diff_count']}/{c['compared_milestones']} "
                f"persistent={c['persistent_from_first_diff']} "
                f"success={','.join(c['success_byte_values'])} breakout={','.join(c['breakout_byte_values'])}"
            )
        for w in d.get("warnings") or []:
            lines.append("  WARNING: " + w)
        lines.append("")

    lines.append("STATIC EventBattleReturn CANDIDATE")
    for r in ("success", "breakout"):
        e = out["event_battle_return"][r]
        lines.append(
            f"  {r}: total vptr hits={len(e.get('hits') or [])}; "
            f"plausible +0x48 state 0..7={len(e.get('plausible_hits') or [])}"
        )
        for h in (e.get("plausible_hits") or [])[:12]:
            lines.append(
                f"    {h['label']} {h['object_candidate']} state={h['state_at_plus_0x48']}"
            )
    lines += [
        "",
        "Interpretation:",
        "  This analyzer ranks evidence; it does NOT promote any address to production authority.",
        "  Prefer an early divergence that persists across later milestones and is corroborated by",
        "  the real-breakout hardware label or by a live EventBattleReturn state transition.",
    ]
    if out.get("warnings"):
        lines.append("")
        lines.extend("WARNING: " + w for w in out["warnings"])
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    success_path, breakout_path = choose_paths(argv)
    success, breakout = load_json(success_path), load_json(breakout_path)
    srole, brole = role(success), role(breakout)
    if srole != "success" or brole != "breakout":
        raise ValueError("Trace role resolution failed")

    sm, bm = milestone_map(success), milestone_map(breakout)
    common_times = sorted(set(sm) & set(bm))
    # Scalar fallback gets useful common timing even when old AK traces have no broad snapshots.
    scalar_times = common_times or [0, .25, .5, .75, 1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5, 6, 7, 8, 10, 12, 15, 20, 30]
    warnings = []
    if not (success.get("broad_snapshots") and breakout.get("broad_snapshots")):
        warnings.append("One or both traces lack v0p43AL broad snapshots; using scalar timeline fallback where possible.")
    if success.get("sample_target_seconds") not in (None, 0.05) or breakout.get("sample_target_seconds") not in (None, 0.05):
        warnings.append("At least one trace was captured with an older/slower lifecycle mapper cadence.")

    out = {
        "tool": TOOL,
        "generated": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "inputs": {"success": str(success_path), "breakout": str(breakout_path)},
        "roles": {
            "success_result": success.get("result"),
            "breakout_result": breakout.get("result"),
        },
        "static_candidate": {
            "class": "field::EventBattleReturn",
            "vptr": f"0x{VPTR_EVENT_BATTLE_RETURN:08X}",
            "state_offset": "0x48",
            "status": "STATIC_EVIDENCE_ONLY_NOT_PRODUCTION_AUTHORITY",
        },
        "broad_diff": {
            "global": broad_window_diff(success, breakout, "global"),
            "owner": broad_window_diff(success, breakout, "owner"),
        },
        "scalar_milestone_diff": scalar_diff(success, breakout, scalar_times),
        "event_battle_return": {
            "success": find_vptr_hits(success),
            "breakout": find_vptr_hits(breakout),
        },
        "warnings": warnings,
    }

    root = app_support()
    root.mkdir(parents=True, exist_ok=True)
    stamp = now_stamp()
    jp = root / f"capture_trace_diff_{stamp}.json"
    tp = root / f"capture_trace_diff_{stamp}.txt"
    jp.write_text(json.dumps(out, indent=2), encoding="utf-8")
    tp.write_text(summary_text(out), encoding="utf-8")
    print(summary_text(out), end="")
    print("JSON:", jp)
    print("TXT :", tp)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}")
        print("Usage: python tools\\capture_trace_diff.py [SUCCESS.json BREAKOUT.json]")
        raise SystemExit(2)
