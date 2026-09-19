from __future__ import annotations

import math
import struct
import time

ALPHA_SAPPHIRE_TITLE_ID = "0x000400000011C500"

# Shared game memory layout only. No starter-specific semantics here.
PROC_ADDRESS = 0x08C69214
PROC_VPTR = 0x006FA8DC
PROVEN_VIEW_ADDRESS = 0x081FB574
PROC_MAIN_STATE_OFFSET = 0x04
PROC_VIEW_PTR_OFFSET = 0x3C
PROC_COMPLETION_FLAG_OFFSET = 0x48

LOWER_ADDRESS = 0x08229C7C
LOWER_VIEW_VPTR = 0x006FA988
LOWER_SELECTED_SLOT_OFFSET = 0xB4
LOWER_CONFIRM_PTR_OFFSET = 0xB8
LOWER_STATE_OFFSET = 0xBC

TRAINER_IDS = 0x08C81340
PARTY0 = 0x08CFB26C
PARTY_STRIDE = 484
PARTY_SLOTS = 6
PK6_SIZE = 232

BATTLE_STATE = 0x081FB478
BATTLE_ACTIVE = 0x00040001

FIELD_ACCESSOR_STUB = 0x006F4048
FIELD_ACCESSOR_STUB_BYTES = bytes.fromhex("04f01fe52ce93f00")
FIELD_GLOBAL_SLOT = 0x005F46FC
PROVEN_FIELD_ROOT = 0x08D3DC14
PROVEN_ZONE_ID_ADDRESS = 0x08C6E884
PROVEN_ZONE_ID = 23
PROVEN_POSITION_OBJECT = 0x08D249A8
PROVEN_POSITION_VPTR = 0x005DF560

EXPECTED_GRID_X = 97.0
EXPECTED_GRID_Z = 150.0
EXPECTED_WORLD_X = 1755.0
EXPECTED_WORLD_Z = 2709.0
EXPECTED_FACING_X = 0.0
EXPECTED_FACING_Z = -1.0
EXPECTED_FRONT_X = 97.0
EXPECTED_FRONT_Z = 149.0

FACING_X_OFFSETS = (0x1C, 0x28, 0x34, 0x40)
FACING_Z_OFFSETS = (0x24, 0x30, 0x3C, 0x48)
GRID_X_PRIMARY = 0x94
GRID_Z_PRIMARY = 0x9C
GRID_X_DUP = 0xC0
GRID_Z_DUP = 0xC8
WORLD_X_PRIMARY = 0xEC
WORLD_Z_PRIMARY = 0xF4
WORLD_X_DUP = 0x114
WORLD_Z_DUP = 0x11C


def u16(raw, off=0):
    return struct.unpack_from("<H", raw, off)[0]

def u32(raw, off=0):
    return struct.unpack_from("<I", raw, off)[0]

def f32(raw, off):
    return struct.unpack_from("<f", raw, off)[0]

def eqf(a, b, tol=0.0005):
    return math.isclose(a, b, rel_tol=0.0, abs_tol=tol)

def read_proc(bridge):
    raw = bridge.read(PROC_ADDRESS, 0x50)
    return {
        "vptr": f"0x{u32(raw, 0):08X}",
        "vptr_matches": u32(raw, 0) == PROC_VPTR,
        "main_state": u32(raw, PROC_MAIN_STATE_OFFSET),
        "view_pointer": f"0x{u32(raw, PROC_VIEW_PTR_OFFSET):08X}",
        "view_matches": u32(raw, PROC_VIEW_PTR_OFFSET) == PROVEN_VIEW_ADDRESS,
        "completion_flag": raw[PROC_COMPLETION_FLAG_OFFSET],
    }

def read_lower(bridge):
    raw = bridge.read(LOWER_ADDRESS, 0xC0)
    return {
        "vptr": f"0x{u32(raw, 0):08X}",
        "vptr_matches": u32(raw, 0) == LOWER_VIEW_VPTR,
        "selected_slot": u32(raw, LOWER_SELECTED_SLOT_OFFSET),
        "confirm_pointer": f"0x{u32(raw, LOWER_CONFIRM_PTR_OFFSET):08X}",
        "state": raw[LOWER_STATE_OFFSET],
    }

def snapshot(bridge):
    return {"proc": read_proc(bridge), "lower": read_lower(bridge)}

def bounded_gate(bridge, delays, predicate, log, label):
    observations = []
    for index, delay in enumerate(delays, 1):
        time.sleep(delay)
        s = snapshot(bridge)
        s["check"] = index
        observations.append(s)
        ok = predicate(s)
        log(
            label,
            check=index,
            proc_state=s["proc"]["main_state"],
            lower_state=s["lower"]["state"],
            selected_slot=s["lower"]["selected_slot"],
            confirm_pointer=s["lower"]["confirm_pointer"],
            authority=ok,
        )
        if ok:
            return True, observations
    return False, observations

def bounded_battle_gate(bridge, delays, log):
    observations = []
    transition_seen = False

    def _check(index, delay, grace=False):
        nonlocal transition_seen
        time.sleep(delay)
        value = u32(bridge.read(BATTLE_STATE, 4))
        transition_seen = transition_seen or value == 0x00040000
        rec = {
            "check": index,
            "value": f"0x{value:08X}",
            "authority": value == BATTLE_ACTIVE,
        }
        if grace:
            rec["grace"] = True
        observations.append(rec)
        log("BATTLE_GATE", **rec)
        return rec["authority"]

    # Preserve every existing starter's normal battle-gate timing exactly.
    for index, delay in enumerate(delays, 1):
        if _check(index, delay):
            return True, observations

    # N3DS XL hardware evidence (HF72 random-starter run, 588 consecutive
    # passes) showed a rare valid battle transition that remained at
    # 0x00040000 through the entire normal 6.25 s window.  Only when that
    # transitional state has actually been observed do we grant a short,
    # read-only grace period.  No input is repeated and PK6 authority still
    # requires BATTLE_ACTIVE (0x00040001).
    if transition_seen:
        for grace_index, delay in enumerate((0.50, 0.50, 1.00, 1.00), len(delays) + 1):
            if _check(grace_index, delay, grace=True):
                return True, observations

    return False, observations

def read_trainer_ids(bridge):
    return struct.unpack("<HH", bridge.read(TRAINER_IDS, 4))

def validate_bag(bridge):
    stub = bridge.read(FIELD_ACCESSOR_STUB, len(FIELD_ACCESSOR_STUB_BYTES))
    root = u32(bridge.read(FIELD_GLOBAL_SLOT, 4))
    zone = u16(bridge.read(PROVEN_ZONE_ID_ADDRESS, 2))
    pos = bridge.read(PROVEN_POSITION_OBJECT, 0x120)

    vptr = u32(pos, 0)
    facing_x = [f32(pos, off) for off in FACING_X_OFFSETS]
    facing_z = [f32(pos, off) for off in FACING_Z_OFFSETS]
    grid_x = f32(pos, GRID_X_PRIMARY)
    grid_z = f32(pos, GRID_Z_PRIMARY)
    grid_x_dup = f32(pos, GRID_X_DUP)
    grid_z_dup = f32(pos, GRID_Z_DUP)
    world_x = f32(pos, WORLD_X_PRIMARY)
    world_z = f32(pos, WORLD_Z_PRIMARY)
    world_x_dup = f32(pos, WORLD_X_DUP)
    world_z_dup = f32(pos, WORLD_Z_DUP)
    front_x = grid_x + facing_x[0]
    front_z = grid_z + facing_z[0]

    checks = {
        "field_stub": stub == FIELD_ACCESSOR_STUB_BYTES,
        "field_root": root == PROVEN_FIELD_ROOT,
        "zone": zone == PROVEN_ZONE_ID,
        "position_vptr": vptr == PROVEN_POSITION_VPTR,
        "grid_x": eqf(grid_x, EXPECTED_GRID_X),
        "grid_z": eqf(grid_z, EXPECTED_GRID_Z),
        "grid_x_dup": eqf(grid_x_dup, EXPECTED_GRID_X),
        "grid_z_dup": eqf(grid_z_dup, EXPECTED_GRID_Z),
        "world_x": eqf(world_x, EXPECTED_WORLD_X),
        "world_z": eqf(world_z, EXPECTED_WORLD_Z),
        "world_x_dup": eqf(world_x_dup, EXPECTED_WORLD_X),
        "world_z_dup": eqf(world_z_dup, EXPECTED_WORLD_Z),
        "facing_x": all(eqf(v, EXPECTED_FACING_X) for v in facing_x),
        "facing_z": all(eqf(v, EXPECTED_FACING_Z) for v in facing_z),
        "front_x": eqf(front_x, EXPECTED_FRONT_X),
        "front_z": eqf(front_z, EXPECTED_FRONT_Z),
    }
    return {
        "authority": all(checks.values()),
        "checks": checks,
        "root": f"0x{root:08X}",
        "zone": zone,
        "vptr": f"0x{vptr:08X}",
        "grid": [grid_x, grid_z],
        "world": [world_x, world_z],
        "facing": [facing_x[0], facing_z[0]],
        "front_grid": [front_x, front_z],
    }


def read_party_snapshot(bridge):
    """Finite one-shot party snapshot.

    Reads each of the six party slots once. This is UI telemetry only and is
    called after the authoritative encounter decision; it is not a polling loop.
    """
    return [
        bridge.read(PARTY0 + slot * PARTY_STRIDE, PK6_SIZE)
        for slot in range(PARTY_SLOTS)
    ]
