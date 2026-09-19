from __future__ import annotations

import json
import math
import sqlite3
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pokebot.common.bridge import Bridge
from pokebot.common.gen6_profiles import profile_from_game_info
from pokebot.wild.world_authority import WorldMap, nearest_grid

DB_PATH = ROOT / "pokebot" / "wild" / "world" / "pokemon_y_grass_runtime.sqlite"
OUT_DIR = ROOT / "support"

FAST_START = 0x08C40000
FAST_END   = 0x08CC0000
WIDE_START = 0x08B00000
WIDE_END   = 0x08E00000
READ_CHUNK = 0x200

RETURN_EPS = 0.20
ORTHO_EPS = 0.30
MIN_MOVE = 0.50
MAX_MOVE = 4096.0


def _as_int(v):
    if isinstance(v, int):
        return v
    return int(str(v), 16)


def readable_ranges(br: Bridge, start: int, end: int):
    out = []
    addr = start
    seen = set()
    while addr < end:
        try:
            q = br.query(addr)
        except Exception:
            addr += 0x1000
            continue
        if q.get("status") != 0 or not q.get("size"):
            addr += 0x1000
            continue
        base = _as_int(q.get("base", addr))
        size = int(q["size"])
        rend = base + size
        key = (base, size)
        if key in seen:
            addr = max(addr + 0x1000, rend)
            continue
        seen.add(key)
        if int(q.get("perm", 0)) & 1:
            lo, hi = max(start, base), min(end, rend)
            if hi > lo:
                out.append((lo, hi))
        addr = max(addr + 0x1000, rend)
    return out


def capture(br: Bridge, ranges, label: str):
    chunks = []
    total = sum(hi - lo for lo, hi in ranges)
    done = 0
    last = 0.0
    print(f"\nCapturing {label}: {total/1024:.0f} KiB across {len(ranges)} readable range(s)")
    for lo, hi in ranges:
        buf = bytearray()
        addr = lo
        while addr < hi:
            n = min(READ_CHUNK, hi - addr)
            try:
                data = br.read(addr, n)
            except Exception:
                data = b"\x00" * n
            if len(data) != n:
                data = bytes(data).ljust(n, b"\x00")
            buf.extend(data)
            addr += n
            done += n
            now = time.monotonic()
            if now - last >= 0.8:
                pct = 100.0 * done / max(1, total)
                print(f"  {pct:5.1f}%", end="\r", flush=True)
                last = now
        chunks.append((lo, bytes(buf)))
    print("  100.0%")
    return chunks


def u16_u32_zone_matches(snapshot, expected):
    hits = []
    expected = set(int(v) for v in expected)
    for base, data in snapshot:
        for off in range(0, len(data) - 1, 2):
            v = struct.unpack_from("<H", data, off)[0]
            if v in expected:
                hits.append({"address": base + off, "width": 2, "value": v})
        for off in range(0, len(data) - 3, 4):
            v = struct.unpack_from("<I", data, off)[0]
            if v in expected:
                hits.append({"address": base + off, "width": 4, "value": v})
    return hits


def floats(snapshot):
    out = {}
    for base, data in snapshot:
        for off in range(0, len(data) - 3, 4):
            v = struct.unpack_from("<f", data, off)[0]
            if math.isfinite(v) and abs(v) <= 1_000_000.0:
                out[base + off] = float(v)
    return out


def analyze_axis(base_f, h_f, h0_f, v_f, v0_f):
    common = set(base_f) & set(h_f) & set(h0_f) & set(v_f) & set(v0_f)
    xs, zs = [], []
    for a in common:
        b = base_f[a]
        dh = h_f[a] - b
        rh = h0_f[a] - b
        dv = v_f[a] - h0_f[a]
        rv = v0_f[a] - h0_f[a]
        if MIN_MOVE <= abs(dh) <= MAX_MOVE and abs(rh) <= RETURN_EPS and abs(dv) <= ORTHO_EPS and abs(rv) <= RETURN_EPS:
            tileish = abs(abs(dh) / 18.0 - round(abs(dh) / 18.0))
            xs.append({
                "address_int": a, "address": f"0x{a:08X}",
                "baseline": b, "horizontal_delta": dh,
                "horizontal_return_error": rh, "vertical_cross_delta": dv,
                "final_return_error": rv, "tile18_error": tileish,
            })
        if MIN_MOVE <= abs(dv) <= MAX_MOVE and abs(h_f[a] - b) <= ORTHO_EPS and abs(rh) <= RETURN_EPS and abs(rv) <= RETURN_EPS:
            tileish = abs(abs(dv) / 18.0 - round(abs(dv) / 18.0))
            zs.append({
                "address_int": a, "address": f"0x{a:08X}",
                "baseline": b, "vertical_delta": dv,
                "horizontal_cross_delta": h_f[a] - b,
                "horizontal_return_error": rh, "final_return_error": rv,
                "tile18_error": tileish,
            })
    xs.sort(key=lambda r: (r["tile18_error"], abs(r["horizontal_return_error"]), r["address_int"]))
    zs.sort(key=lambda r: (r["tile18_error"], abs(r["final_return_error"]), r["address_int"]))
    return xs, zs


def duplicate_groups(cands, value_maps):
    groups = []
    used = set()
    addrs = [c["address_int"] for c in cands[:250]]
    for i, a in enumerate(addrs):
        if a in used:
            continue
        va = [m.get(a) for m in value_maps]
        grp = [a]
        for b in addrs[i+1:]:
            vb = [m.get(b) for m in value_maps]
            if None in va or None in vb:
                continue
            if all(abs(x-y) <= 0.05 for x, y in zip(va, vb)):
                grp.append(b)
        if len(grp) >= 2:
            used.update(grp)
            groups.append([f"0x{x:08X}" for x in grp])
    return groups


def resolve_location(db_path: Path, text: str):
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT location_name,zone_id,map_matrix_id,parent_map FROM zones ORDER BY zone_id"
        ).fetchall()
    finally:
        con.close()
    names = {}
    for name, zid, mid, pm in rows:
        names.setdefault(str(name), []).append((int(zid), int(mid), int(pm)))
    t = text.strip().casefold()
    exact = [k for k in names if k.casefold() == t]
    if len(exact) == 1:
        return exact[0], names[exact[0]]
    partial = [k for k in names if t in k.casefold()]
    if len(partial) == 1:
        return partial[0], names[partial[0]]
    if not partial:
        raise ValueError(f"No Kalos database location matches {text!r}")
    raise ValueError("Ambiguous location: " + ", ".join(partial[:15]))


def pair_candidates(xs, zs, world: WorldMap, expected_zones, zone_hits):
    zone_addresses = {int(h["address"]): h for h in zone_hits}
    pairs = []
    for x in xs[:80]:
        for z in zs[:80]:
            xa, za = x["address_int"], z["address_int"]
            structural = 0
            if za - xa == 8:
                structural += 4
            elif abs(za - xa) <= 0x40:
                structural += 1

            nearby_zone = None
            for guess in (xa + 0xD4, za + 0xCC):
                if guess in zone_addresses:
                    nearby_zone = zone_addresses[guess]
                    structural += 5
                    break

            grid = [nearest_grid(x["baseline"]), nearest_grid(z["baseline"])]
            db_matches = []
            for zid in expected_zones:
                r = world.resolve(zid, grid)
                if r.get("resolved"):
                    db_matches.append({
                        "zone_id": zid,
                        "location_name": r.get("location_name"),
                        "matrix_id": r.get("matrix_id"),
                        "grid": grid,
                        "region_id": r.get("region_id"),
                        "local_tile": r.get("local_tile"),
                        "encounter_terrain": bool(r.get("encounter_terrain")),
                    })
            db_score = 0
            if db_matches:
                db_score += 3
                if any(r["encounter_terrain"] for r in db_matches):
                    db_score += 3

            score = (
                structural + db_score
                + max(0, 2 - min(2, x["tile18_error"] * 8))
                + max(0, 2 - min(2, z["tile18_error"] * 8))
            )
            if structural or db_matches:
                pairs.append({
                    "score": round(float(score), 4),
                    "x_address": x["address"],
                    "z_address": z["address"],
                    "x_baseline": x["baseline"],
                    "z_baseline": z["baseline"],
                    "grid_assuming_ORAS_18_unit_scale": grid,
                    "nearby_zone_candidate": (
                        {
                            "address": f"0x{nearby_zone['address']:08X}",
                            "width": nearby_zone["width"],
                            "value": nearby_zone["value"],
                        } if nearby_zone else None
                    ),
                    "database_matches": db_matches,
                })
    pairs.sort(key=lambda r: (-r["score"], r["x_address"], r["z_address"]))
    return pairs[:100]


def prompt(msg):
    input("\n" + msg + "\nPress ENTER when the player is completely still... ")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    wide = "--wide" in argv
    if wide:
        argv.remove("--wide")
    host = argv.pop(0) if argv else input("3DS IP address: ").strip()
    if not DB_PATH.is_file():
        print(f"FAIL: {DB_PATH} is missing")
        return 2

    location_in = " ".join(argv).strip()
    if not location_in:
        print("\nUse a simple overworld section with room to move. Route 2 or Route 3 are ideal.")
        location_in = input("Current Pokémon Y location name: ").strip()

    try:
        location_name, zone_rows = resolve_location(DB_PATH, location_in)
    except ValueError as exc:
        print(f"FAIL: {exc}")
        return 2
    expected_zones = sorted({z for z, _, _ in zone_rows})

    br = Bridge(host=host, port=4952, timeout=2.5)
    gi = br.game_info()
    profile = profile_from_game_info(gi)
    if not profile or profile.get("key") != "pokemon_y":
        print(f"FAIL: this database is certified for Pokémon Y; detected {profile}")
        return 3

    start, end = (WIDE_START, WIDE_END) if wide else (FAST_START, FAST_END)
    ranges = readable_ranges(br, start, end)
    if not ranges:
        print("FAIL: no readable RAM ranges in mapper window")
        return 4

    print("\nHF59 X/Y KALOS WORLD RAM AUTHORITY MAPPER")
    print(f"Detected: {profile['name']} / {profile['process']}")
    print(f"Location: {location_name}")
    print(f"Expected static zone ID(s): {expected_zones}")
    print(f"RAM window: 0x{start:08X}-0x{end:08X}{' WIDE' if wide else ''}")
    print("\nIMPORTANT: this mapper sends ZERO controller inputs and ZERO RAM writes.")
    print("Move manually with the 3DS controls. Stay in the SAME map/route throughout.")
    print("For the return steps, return to the exact tile you started from.")

    prompt("STEP 1/5 — stand at the starting tile.")
    s0 = capture(br, ranges, "baseline")
    prompt("STEP 2/5 — move several tiles RIGHT only, then stop.")
    sh = capture(br, ranges, "horizontal moved")
    prompt("STEP 3/5 — move LEFT back to the exact original starting tile, then stop.")
    sh0 = capture(br, ranges, "horizontal returned")
    prompt("STEP 4/5 — from the original tile, move several tiles UP only, then stop.")
    sv = capture(br, ranges, "vertical moved")
    prompt("STEP 5/5 — move DOWN back to the exact original starting tile, then stop.")
    sv0 = capture(br, ranges, "vertical returned")

    f0, fh, fh0, fv, fv0 = map(floats, (s0, sh, sh0, sv, sv0))
    xs, zs = analyze_axis(f0, fh, fh0, fv, fv0)
    zone_hits = u16_u32_zone_matches(s0, expected_zones)
    world = WorldMap(DB_PATH)
    pairs = pair_candidates(xs, zs, world, expected_zones, zone_hits)

    x_dupes = duplicate_groups(xs, [f0, fh, fh0, fv, fv0])
    z_dupes = duplicate_groups(zs, [f0, fh, fh0, fv, fv0])

    recommendation = pairs[0] if pairs else None
    confidence = "UNPROVEN"
    if recommendation:
        score = recommendation["score"]
        if score >= 12 and recommendation.get("nearby_zone_candidate") and recommendation.get("database_matches"):
            confidence = "STRONG_CANDIDATE_REQUIRES_SECOND_LOCATION_PROOF"
        elif score >= 7:
            confidence = "CANDIDATE_REQUIRES_MORE_HARDWARE_PROOF"

    report = {
        "tool": "HF59 XY Kalos World RAM Authority Mapper",
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "game_info": gi,
        "game_profile": profile,
        "database": str(DB_PATH.relative_to(ROOT)),
        "location_name": location_name,
        "expected_zone_ids": expected_zones,
        "ram_window": [f"0x{start:08X}", f"0x{end:08X}"],
        "wide": wide,
        "controller_inputs_sent": 0,
        "ram_writes": 0,
        "x_candidate_count": len(xs),
        "z_candidate_count": len(zs),
        "zone_candidate_count": len(zone_hits),
        "x_candidates": xs[:200],
        "z_candidates": zs[:200],
        "zone_candidates": [
            {"address": f"0x{h['address']:08X}", "address_int": h["address"],
             "width": h["width"], "value": h["value"]}
            for h in zone_hits[:1000]
        ],
        "x_duplicate_groups": x_dupes[:50],
        "z_duplicate_groups": z_dupes[:50],
        "ranked_position_pairs": pairs,
        "recommended_candidate": recommendation,
        "confidence": confidence,
        "status": "XY_WORLD_RAM_MAPPING_CAPTURE_COMPLETE",
        "note": (
            "No candidate is enabled as runtime authority by this tool. "
            "A candidate must be hardware-confirmed before Hunts containment is changed."
        ),
    }

    OUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = OUT_DIR / f"xy_world_authority_mapper_{stamp}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"\nX-axis candidates: {len(xs)}")
    print(f"Z-axis candidates: {len(zs)}")
    print(f"Zone candidates: {len(zone_hits)}")
    if recommendation:
        print("Top combined candidate:")
        print(json.dumps(recommendation, indent=2))
        print(f"Confidence: {confidence}")
    else:
        print("No combined candidate in this RAM window.")
        if not wide:
            print("Re-run with --wide only if instructed after reviewing this report.")
    print(f"\nREPORT: {out}")
    print("Send this JSON back for the next build.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
