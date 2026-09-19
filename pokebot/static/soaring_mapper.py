from __future__ import annotations

"""Read-only ORAS Soaring trace helpers.

This module deliberately does not automate flight.  The first hardware mapper
uses a deterministic Dewford save and one acknowledged Y press, then records
shared ORAS RAM while the user manually flies into a Dimensional Rift.
"""

import math
import struct
from typing import Callable, Any

from pokebot.common.oras_profiles import (
    BATTLE_STATE,
    ZONE_ADDR,
    PRIMARY_BASE,
    SECONDARY_BASE,
)

BATTLE_ACTIVE = 0x00040001
BATTLE_TRANSITION = 0x00000000
BATTLE_INACTIVE = 0x00040000


def _u32(raw: bytes) -> int:
    return struct.unpack_from("<I", raw, 0)[0]


def _xyz(raw: bytes) -> list[float]:
    return [float(x) for x in struct.unpack_from("<fff", raw, 0)]


def _grid(world: float) -> int | None:
    if not math.isfinite(float(world)):
        return None
    return int(round((float(world) - 9.0) / 18.0))


def read_soaring_sample(read_fn: Callable[[int, int], bytes]) -> dict[str, Any]:
    """Read one diagnostic sample with battle state as the mandatory authority.

    Field coordinates can legitimately disappear or become stale during a
    module transition.  Those failures are recorded instead of hiding the
    battle transition that the mapper is trying to discover.
    """
    sample: dict[str, Any] = {"errors": []}
    battle = _u32(read_fn(BATTLE_STATE, 4))
    sample["battle_u32"] = battle
    sample["battle"] = f"0x{battle:08X}"
    sample["battle_phase"] = (
        "ACTIVE" if battle == BATTLE_ACTIVE else
        "TRANSITION" if battle == BATTLE_TRANSITION else
        "FIELD" if battle == BATTLE_INACTIVE else
        "UNKNOWN"
    )

    # Once the battle object is active, field coordinates are no longer needed
    # for the flight trace and may belong to an unloading field module.
    if battle == BATTLE_ACTIVE:
        return sample

    try:
        zone_raw = _u32(read_fn(ZONE_ADDR, 4))
        sample["zone_raw"] = f"0x{zone_raw:08X}"
        sample["zone"] = int(zone_raw & 0xFFFF)
        sample["zone_flags"] = int((zone_raw >> 16) & 0xFFFF)
    except Exception as exc:
        sample["errors"].append(f"ZONE:{type(exc).__name__}:{exc}")

    try:
        p = _xyz(read_fn(PRIMARY_BASE, 12))
        sample["primary_xyz"] = p
        sample["primary_grid_xz"] = [_grid(p[0]), _grid(p[2])]
        sample["primary_finite"] = all(math.isfinite(v) for v in p)
    except Exception as exc:
        sample["errors"].append(f"PRIMARY:{type(exc).__name__}:{exc}")

    try:
        s = _xyz(read_fn(SECONDARY_BASE, 12))
        sample["secondary_xyz"] = s
        sample["secondary_grid_xz"] = [_grid(s[0]), _grid(s[2])]
        sample["secondary_finite"] = all(math.isfinite(v) for v in s)
    except Exception as exc:
        sample["errors"].append(f"SECONDARY:{type(exc).__name__}:{exc}")

    return sample


def summarize_trace(samples: list[dict], *, anchor: dict | None = None) -> dict[str, Any]:
    zones = []
    primary = []
    transitions = 0
    active = 0
    read_errors = 0
    for sample in samples:
        if isinstance(sample.get("zone"), int):
            zones.append(int(sample["zone"]))
        p = sample.get("primary_xyz")
        if isinstance(p, list) and len(p) == 3 and all(isinstance(v, (int, float)) for v in p):
            if all(math.isfinite(float(v)) for v in p):
                primary.append([float(v) for v in p])
        if sample.get("battle_phase") == "TRANSITION":
            transitions += 1
        if sample.get("battle_phase") == "ACTIVE":
            active += 1
        read_errors += len(sample.get("errors") or [])

    summary: dict[str, Any] = {
        "samples": len(samples),
        "unique_zones": sorted(set(zones)),
        "transition_samples": transitions,
        "battle_active_samples": active,
        "read_errors": read_errors,
        "anchor": anchor,
    }
    if primary:
        summary["primary_xyz_min"] = [min(row[i] for row in primary) for i in range(3)]
        summary["primary_xyz_max"] = [max(row[i] for row in primary) for i in range(3)]
        summary["primary_first"] = primary[0]
        summary["primary_last"] = primary[-1]
    return summary
