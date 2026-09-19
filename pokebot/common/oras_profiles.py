from __future__ import annotations

import math
import struct

ALPHA_SAPPHIRE_TITLE_ID = "0x000400000011C500"
OMEGA_RUBY_TITLE_ID = "0x000400000011C400"

ALPHA_SAPPHIRE_TITLE_INT = 0x000400000011C500
OMEGA_RUBY_TITLE_INT = 0x000400000011C400

PROFILES = {
    ALPHA_SAPPHIRE_TITLE_ID.lower(): {
        "key": "alpha_sapphire",
        "name": "Alpha Sapphire",
        "title_id": ALPHA_SAPPHIRE_TITLE_ID,
        "title_id_int": ALPHA_SAPPHIRE_TITLE_INT,
        "process": "sango-2",
        "starter_reset_profile": "alpha_sapphire_locked",
        "wild_profile": "oras_shared",
    },
    OMEGA_RUBY_TITLE_ID.lower(): {
        "key": "omega_ruby",
        "name": "Omega Ruby",
        "title_id": OMEGA_RUBY_TITLE_ID,
        "title_id_int": OMEGA_RUBY_TITLE_INT,
        "process": "sango-1",
        "starter_reset_profile": "omega_ruby_s2_proven",
        "wild_profile": "oras_shared",
    },
}

# Hardware-proven shared ORAS RAM authority.
BATTLE_STATE = 0x081FB478
BATTLE_INACTIVE = 0x00040000
ZONE_ADDR = 0x08C6E884
PRIMARY_BASE = 0x08C6E7B0
SECONDARY_BASE = 0x08DA8568

BIRCH_ZONE = 23
BIRCH_GRID = (97, 150)
BIRCH_WORLD = (1755.0, 2709.0)
POSITION_EPSILON = 0.01
CENTER_EPSILON = 0.30


def normalize_title_id(value):
    if isinstance(value, int):
        return f"0x{value:016x}"
    s = str(value or "").strip().lower()
    if not s:
        return ""
    if s.startswith("0x"):
        try:
            return f"0x{int(s, 16):016x}"
        except ValueError:
            return s
    try:
        return f"0x{int(s):016x}"
    except ValueError:
        return s


def profile_from_game_info(info):
    info = info or {}
    title_id = normalize_title_id(
        info.get("title_id", info.get("title_id_hex", ""))
    )
    profile = PROFILES.get(title_id)
    if profile is None:
        return None
    process = info.get("process_name", info.get("process"))
    if process != profile["process"]:
        return None
    try:
        pid = int(info.get("pid", 0))
    except Exception:
        pid = 0
    if pid <= 0:
        return None
    if "status" in info and int(info.get("status", 0)) != 0:
        return None
    return dict(profile)


def is_supported_oras(info):
    return profile_from_game_info(info) is not None


def _u32(raw, off=0):
    return struct.unpack_from("<I", raw, off)[0]


def _f32(raw, off=0):
    return struct.unpack_from("<f", raw, off)[0]


def _grid(world):
    return int(round((world - 9.0) / 18.0))


def validate_shared_birch_bag(bridge):
    """Cross-version Birch-bag authority proven on AS and OR hardware.

    Uses only the RAM locations independently proven shared between both 1.4
    games. It intentionally does not depend on the older AS-only field-object
    signature.
    """
    battle = _u32(bridge.read(BATTLE_STATE, 4))
    zone_raw = _u32(bridge.read(ZONE_ADDR, 4))
    zone = zone_raw & 0xFFFF

    p = bridge.read(PRIMARY_BASE, 12)
    s = bridge.read(SECONDARY_BASE, 12)
    ax, az = _f32(p, 0), _f32(p, 8)
    bx, bz = _f32(s, 0), _f32(s, 8)

    finite = all(math.isfinite(v) for v in (ax, az, bx, bz))
    duplicates = (
        finite
        and abs(ax - bx) <= POSITION_EPSILON
        and abs(az - bz) <= POSITION_EPSILON
    )
    gx = _grid(ax) if finite else None
    gz = _grid(az) if finite else None

    # Hardware support from 2026-08-23 showed a valid Omega Ruby Birch-bag
    # return with the primary world coordinate already exact while the older
    # secondary duplicate remained all-zero after soft reset.  Treat an
    # explicitly zero secondary copy as "not initialised yet", not as a
    # contradictory position.  Any NON-zero disagreement still fails closed.
    primary_exact = (
        finite
        and abs(ax - BIRCH_WORLD[0]) <= CENTER_EPSILON
        and abs(az - BIRCH_WORLD[1]) <= CENTER_EPSILON
        and (gx, gz) == BIRCH_GRID
    )
    secondary_exact = (
        finite
        and abs(bx - BIRCH_WORLD[0]) <= CENTER_EPSILON
        and abs(bz - BIRCH_WORLD[1]) <= CENTER_EPSILON
    )
    secondary_uninitialized_zero = (
        finite
        and abs(bx) <= POSITION_EPSILON
        and abs(bz) <= POSITION_EPSILON
    )
    secondary_safe = secondary_exact or secondary_uninitialized_zero

    checks = {
        "battle_inactive": battle == BATTLE_INACTIVE,
        "zone_23": zone == BIRCH_ZONE,
        "coordinates_finite": finite,
        "primary_exact_birch_bag_position": primary_exact,
        "secondary_exact_or_uninitialized_zero": secondary_safe,
    }
    authority = all(checks.values())
    if authority and secondary_exact:
        authority_mode = "dual_coordinate_exact"
    elif authority and secondary_uninitialized_zero:
        authority_mode = "primary_exact_secondary_uninitialized_zero"
    else:
        authority_mode = "failed"

    return {
        "authority": authority,
        "authority_mode": authority_mode,
        "checks": checks,
        "battle": f"0x{battle:08X}",
        "zone_raw": f"0x{zone_raw:08X}",
        "zone": zone,
        "grid": [gx, gz] if finite else None,
        "world_primary": [ax, az],
        "world_secondary": [bx, bz],
        "duplicates_match": duplicates,
        "secondary_uninitialized_zero": secondary_uninitialized_zero,
    }
