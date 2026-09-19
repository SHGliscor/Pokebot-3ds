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

DB = ROOT / "pokebot" / "wild" / "world" / "pokemon_y_grass_runtime.sqlite"
OUT = ROOT / "support"

# HF59 Route 3 hardware candidates.
X_PRIMARY = 0x08C670BC
Z_PRIMARY = X_PRIMARY + 0x08
ZONE_PRIMARY = X_PRIMARY + 0xD4

# HF59 also found this exact duplicate X value nearby. Probe its +8 pair too.
X_NEAR_DUP = 0x08C671A0
Z_NEAR_DUP = X_NEAR_DUP + 0x08

# Other raw Route 3 zone matches retained as diagnostics only.
ZONE_ALT_1 = 0x08C670AE
ZONE_ALT_2 = 0x08C67156

EPS_RETURN = 0.30
EPS_CROSS = 0.35
TILE = 18.0


def f32(br, addr):
    return struct.unpack("<f", br.read(addr, 4))[0]


def u32(br, addr):
    return struct.unpack("<I", br.read(addr, 4))[0]


def sample(br, label):
    r = {
        "label": label,
        "time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "x_primary": f32(br, X_PRIMARY),
        "z_primary": f32(br, Z_PRIMARY),
        "x_near_dup": f32(br, X_NEAR_DUP),
        "z_near_dup": f32(br, Z_NEAR_DUP),
        "zone_primary_raw": u32(br, ZONE_PRIMARY),
        "zone_alt_1_raw": u32(br, ZONE_ALT_1),
        "zone_alt_2_raw": u32(br, ZONE_ALT_2),
    }
    r["zone_primary"] = r["zone_primary_raw"] & 0xFFFF
    r["zone_alt_1"] = r["zone_alt_1_raw"] & 0xFFFF
    r["zone_alt_2"] = r["zone_alt_2_raw"] & 0xFFFF
    return r


def multiple18(delta):
    if not math.isfinite(delta) or abs(delta) < 9.0:
        return False, None, None
    tiles = delta / TILE
    nearest = round(tiles)
    err = abs(tiles - nearest)
    return err <= 0.08, nearest, err


def resolve_location(text):
    con = sqlite3.connect(DB)
    try:
        rows = con.execute(
            "SELECT location_name,zone_id FROM zones ORDER BY zone_id"
        ).fetchall()
    finally:
        con.close()
    names = {}
    for name, zid in rows:
        names.setdefault(str(name), []).append(int(zid))
    t = text.strip().casefold()
    exact = [k for k in names if k.casefold() == t]
    if len(exact) == 1:
        return exact[0], sorted(set(names[exact[0]]))
    partial = [k for k in names if t in k.casefold()]
    if len(partial) == 1:
        return partial[0], sorted(set(names[partial[0]]))
    raise ValueError("Location not uniquely resolved: " + ", ".join(partial[:20]))


def wait_enter(msg):
    input("\n" + msg + "\nPress ENTER only after the player is completely still... ")


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else input("3DS IP address: ").strip()
    print("\nHF60 - XY TARGETED FIELD AUTHORITY PROOF")
    print("========================================")
    print("Zero automated controller inputs. Zero RAM writes.")
    print("This probes only the field block identified by your HF59 Route 3 capture.")
    print(f"X candidate:    0x{X_PRIMARY:08X}")
    print(f"Z candidate:    0x{Z_PRIMARY:08X} (X + 8)")
    print(f"Zone candidate: 0x{ZONE_PRIMARY:08X} (X + 0xD4)")
    print("\nUse a spot where you can move both left/right AND up/down without changing maps.")

    loc_in = input("Current location [Route 3]: ").strip() or "Route 3"
    try:
        loc_name, expected_zones = resolve_location(loc_in)
    except ValueError as e:
        print("FAIL:", e)
        return 2

    br = Bridge(host=host, port=4952, timeout=2.5)
    gi = br.game_info()
    profile = profile_from_game_info(gi)
    if not profile or profile.get("key") != "pokemon_y":
        print("FAIL: Pokémon Y required; detected:", profile)
        return 3

    wait_enter("STEP 1/5 — stand on your starting tile.")
    s0 = sample(br, "baseline")
    print(json.dumps(s0, indent=2))

    wait_enter("STEP 2/5 — move at least 3 tiles RIGHT, then stop.")
    sr = sample(br, "right")

    wait_enter("STEP 3/5 — move LEFT back to the exact original tile, then stop.")
    sr0 = sample(br, "right_return")

    vertical = (input("\nFor the vertical test, which direction is clear from the start tile? [UP/DOWN]: ").strip().upper() or "UP")
    if vertical not in ("UP", "DOWN"):
        print("FAIL: choose UP or DOWN")
        return 4
    opposite = "DOWN" if vertical == "UP" else "UP"

    wait_enter(f"STEP 4/5 — from the original tile, move at least 3 tiles {vertical}, then stop.")
    sv = sample(br, "vertical")

    wait_enter(f"STEP 5/5 — move {opposite} back to the exact original tile, then stop.")
    sv0 = sample(br, "vertical_return")

    samples = [s0, sr, sr0, sv, sv0]

    dx = sr["x_primary"] - s0["x_primary"]
    xret = sr0["x_primary"] - s0["x_primary"]
    xcross = sv["x_primary"] - sr0["x_primary"]
    xfinal = sv0["x_primary"] - s0["x_primary"]

    dz = sv["z_primary"] - sr0["z_primary"]
    zret_h = sr0["z_primary"] - s0["z_primary"]
    zcross_h = sr["z_primary"] - s0["z_primary"]
    zfinal = sv0["z_primary"] - s0["z_primary"]

    x18, xtiles, xerr = multiple18(dx)
    z18, ztiles, zerr = multiple18(dz)

    zones_primary = [s["zone_primary"] for s in samples]
    zone_stable = len(set(zones_primary)) == 1
    zone_expected = zone_stable and zones_primary[0] in expected_zones

    x_proven = (
        x18 and abs(xret) <= EPS_RETURN and abs(xcross) <= EPS_CROSS
        and abs(xfinal) <= EPS_RETURN
    )
    z_proven = (
        z18 and abs(zret_h) <= EPS_CROSS and abs(zcross_h) <= EPS_CROSS
        and abs(zfinal) <= EPS_RETURN
    )

    near_dup_x_tracks = all(
        math.isfinite(s["x_near_dup"]) and abs(s["x_near_dup"] - s["x_primary"]) <= 0.05
        for s in samples
    )
    near_dup_z_tracks = all(
        math.isfinite(s["z_near_dup"]) and abs(s["z_near_dup"] - s["z_primary"]) <= 0.05
        for s in samples
    )

    world = WorldMap(DB)
    grid0 = [nearest_grid(s0["x_primary"]), nearest_grid(s0["z_primary"])]
    resolved0 = (
        world.resolve(zones_primary[0], grid0)
        if zone_stable else {"resolved": False, "reason": "ZONE_NOT_STABLE"}
    )

    checks = {
        "x_primary_tracks_horizontal_only": x_proven,
        "z_primary_tracks_vertical_only": z_proven,
        "zone_primary_stable": zone_stable,
        "zone_primary_matches_database_location": zone_expected,
        "zone_structural_offset_is_x_plus_0xD4": ZONE_PRIMARY - X_PRIMARY == 0xD4,
        "z_structural_offset_is_x_plus_8": Z_PRIMARY - X_PRIMARY == 8,
        "baseline_world_grid_resolves": bool(resolved0.get("resolved")),
        "baseline_resolves_expected_location": (
            bool(resolved0.get("resolved"))
            and str(resolved0.get("location_name", "")).casefold() == loc_name.casefold()
        ),
    }

    # Duplicate pair is extra corroboration, not mandatory authority.
    extras = {
        "near_duplicate_x_tracks_primary": near_dup_x_tracks,
        "near_duplicate_z_tracks_primary": near_dup_z_tracks,
    }

    authority = all(checks.values())
    status = (
        "XY_FIELD_AUTHORITY_HARDWARE_PROOF_PASS"
        if authority
        else "XY_FIELD_AUTHORITY_PROOF_INCOMPLETE"
    )

    report = {
        "tool": "HF60 XY Targeted Field Authority Proof",
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "game_info": gi,
        "game_profile": profile,
        "location_name": loc_name,
        "expected_zone_ids": expected_zones,
        "addresses": {
            "x_primary": f"0x{X_PRIMARY:08X}",
            "z_primary": f"0x{Z_PRIMARY:08X}",
            "zone_primary": f"0x{ZONE_PRIMARY:08X}",
            "x_near_duplicate": f"0x{X_NEAR_DUP:08X}",
            "z_near_duplicate": f"0x{Z_NEAR_DUP:08X}",
            "zone_alt_1": f"0x{ZONE_ALT_1:08X}",
            "zone_alt_2": f"0x{ZONE_ALT_2:08X}",
        },
        "controller_inputs_sent": 0,
        "ram_writes": 0,
        "vertical_direction": vertical,
        "samples": samples,
        "deltas": {
            "x_horizontal": dx,
            "x_horizontal_tiles": xtiles,
            "x_multiple18_error": xerr,
            "x_return_error": xret,
            "x_vertical_cross_delta": xcross,
            "x_final_return_error": xfinal,
            "z_vertical": dz,
            "z_vertical_tiles": ztiles,
            "z_multiple18_error": zerr,
            "z_horizontal_cross_delta": zcross_h,
            "z_horizontal_return_error": zret_h,
            "z_final_return_error": zfinal,
        },
        "baseline_grid": grid0,
        "baseline_world_resolution": resolved0,
        "checks": checks,
        "extra_corroboration": extras,
        "authority": authority,
        "status": status,
    }

    OUT.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = OUT / f"xy_targeted_field_authority_proof_{stamp}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\nRESULT")
    print("======")
    for k, v in checks.items():
        print(f"{'PASS' if v else 'FAIL'}  {k}")
    print(f"\nX horizontal delta: {dx:.3f} ({xtiles} tiles if valid)")
    print(f"Z vertical delta:   {dz:.3f} ({ztiles} tiles if valid)")
    print(f"Zone samples:       {zones_primary}")
    print(f"Baseline grid:      {grid0}")
    print(f"World resolve:      {resolved0}")
    print(f"\nSTATUS: {status}")
    print(f"REPORT: {path}")
    print("\nSend the JSON back.")
    return 0 if authority else 5


if __name__ == "__main__":
    raise SystemExit(main())
