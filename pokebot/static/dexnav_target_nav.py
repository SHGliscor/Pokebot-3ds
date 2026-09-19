from __future__ import annotations

import math
import struct

# Hardware-probed Omega Ruby 1.4 Route 101 DexNav tutorial object.
# HF89 probe 2026-09-12:
#   baseline before target: target block zero/inactive
#   target visible: X=1701.0, Y=2.0, Z=2331.0, active ptr=0x08D3B920
#   manual sneak: target coordinates remained fixed for all 25 samples
#   battle contact: player ~=1710.85266,2332.55713; active ptr cleared just
#                   before BATTLE_STATE changed to active.
DEXNAV_TARGET_BLOCK = 0x08D3B560
DEXNAV_TARGET_BLOCK_LEN = 0x18


def decode_target_block(raw: bytes) -> dict:
    if len(raw) != DEXNAV_TARGET_BLOCK_LEN:
        raise ValueError(f"DexNav target block must be {DEXNAV_TARGET_BLOCK_LEN} bytes, got {len(raw)}")
    x, y, z = struct.unpack_from('<fff', raw, 0)
    active_flag = struct.unpack_from('<I', raw, 0x0C)[0]
    object_ptr = struct.unpack_from('<I', raw, 0x10)[0]
    active_ptr = struct.unpack_from('<I', raw, 0x14)[0]
    finite = all(math.isfinite(v) for v in (x, y, z))
    route101_plausible = finite and 1600.0 <= x <= 1850.0 and 2250.0 <= z <= 2425.0
    pointer_plausible = 0x08000000 <= active_ptr < 0x0A000000
    active = bool(route101_plausible and active_flag != 0 and pointer_plausible)
    return {
        'x': float(x), 'y': float(y), 'z': float(z),
        'active_flag': int(active_flag),
        'object_ptr': int(object_ptr),
        'active_ptr': int(active_ptr),
        'route101_plausible': bool(route101_plausible),
        'active': active,
    }


def steering_command(player_x: float, player_z: float, target_x: float, target_z: float, *, vertical_stall: int = 0) -> dict:
    """Return a fast, low-gap RAM-directed CPAD correction.

    HF91 proved the target-vector steering and safe partial-stick range. HF94
    deliberately stops tapering the stick toward contact: that taper was the
    visible slowdown immediately before the tutorial encounter.  Corrections
    remain partial-stick (never full tilt) so DexNav sneaking is preserved.
    """
    px, pz, tx, tz = map(float, (player_x, player_z, target_x, target_z))
    dx = tx - px
    dz = tz - pz
    distance = math.hypot(dx, dz)

    adx = abs(dx)
    if adx <= 2.5:
        sx = 0.0
    elif adx <= 16.0:
        sx = 0.48
    elif adx <= 35.0:
        sx = 0.52
    else:
        sx = 0.55
    x = -sx if dx < 0 else sx

    adz = abs(dz)
    if adz <= 2.5:
        sy = 0.0
    elif adz <= 9.0:
        sy = 0.34
    elif adz <= 16.0:
        sy = 0.36
    else:
        sy = 0.38

    # Preserve HF91's proven dead-zone recovery.  If RAM says the Z axis did
    # not move, increase only the vertical component, never beyond the tested
    # 0.48 partial-stick ceiling.
    if adz > 4.0 and vertical_stall > 0:
        sy = max(sy, min(0.48, 0.38 + 0.05 * min(vertical_stall, 2)))
    y = sy if dz < 0 else -sy

    # Longer holds mean fewer unavoidable firmware pulse boundaries/RAM-read
    # gaps.  Near contact we still keep enough duration and stick magnitude to
    # avoid the old slow crawl, while retaining a final correction opportunity.
    if distance > 65.0:
        hold_ms = 2800
    elif distance > 38.0:
        hold_ms = 2200
    elif distance > 22.0:
        hold_ms = 1700
    elif distance > 11.0:
        hold_ms = 1250
    else:
        hold_ms = 850

    return {
        'dx': dx, 'dz': dz, 'distance': distance,
        'x': x, 'y': y, 'hold_ms': int(hold_ms),
        'vertical_stall': int(vertical_stall),
    }
