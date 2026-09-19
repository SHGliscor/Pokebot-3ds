from __future__ import annotations

import argparse
import json
import math
import struct
import time
import traceback
from collections import Counter, deque
from datetime import datetime
from pathlib import Path

import causal_core_v0p14 as core
import route101_w6_mask as terrain


# ---------------------------------------------------------------------------
# Pokebot3DS-CFW W6 Causal Integration v0p22
#
# Integration scope:
#   KEEP: W6 Route101 terrain containment / one-tile reposition / fluid profiles
#   KEEP: one authoritative PK6 read + shiny absolute HOLD
#   KEEP: native acknowledged touch command 9
#   REPLACE ONLY: old flow==5 / guessed menu readiness authorization
#   WITH: v0p14 causal Run outcome loop
# ---------------------------------------------------------------------------

DEFAULT_TARGET_ENCOUNTERS = 10
ROLLING_DETAIL_ENCOUNTERS = 3
BUNNY_BATTLE_POLL_SEC = 0.05
BUNNY_POSITION_CHECK_SEC = 1.0
BUNNY_MAX_HOLD_SEC = 120.0

PRIMARY_X_ADDR = 0x08C6E7B0
PRIMARY_Z_ADDR = 0x08C6E7B8
SECONDARY_X_ADDR = 0x08DA8568
SECONDARY_Z_ADDR = 0x08DA8570

POSITION_EPSILON = 0.25
CENTER_EPSILON = 0.30

# Active-low raw HID.
HID_NEUTRAL = 0xFFF
HID_B = HID_NEUTRAL & ~(1 << 1)  # 0xFFD
HID_DIR = {
    "RIGHT": HID_NEUTRAL & ~(1 << 4),  # 0xFEF
    "LEFT":  HID_NEUTRAL & ~(1 << 5),  # 0xFDF
    "UP":    HID_NEUTRAL & ~(1 << 6),  # 0xFBF
    "DOWN":  HID_NEUTRAL & ~(1 << 7),  # 0xF7F
}
HID_B_DIR = {d: (v & HID_B) for d, v in HID_DIR.items()}

MOVEMENT_SETTLE_MS = 140
MOVEMENT_STATUS_TIMEOUT = 2.5
POST_FLUID_SETTLE_SEC = 0.10

TRANSITION_RECHECKS = 24
TRANSITION_RECHECK_SEC = 0.10

FIELD_AUTHORITY_TIMEOUT_SEC = 4.0
FIELD_AUTHORITY_POLL_SEC = 0.06
FIELD_AUTHORITY_CONFIRM_SAMPLES = 2


class IntegrationHold(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def hx(v: int) -> str:
    return f"0x{v:08X}"


def read_f32(br: core.Bridge, address: int) -> float:
    return struct.unpack("<f", br.read(address, 4))[0]


def grid_float(world: float) -> float:
    return (world - 9.0) / 18.0


def nearest_grid(world: float) -> int:
    return int(round(grid_float(world)))


def grid_center(grid: int) -> float:
    return grid * 18.0 + 9.0


def read_position(br: core.Bridge) -> dict:
    # Same duplicate-world-coordinate authority used by W3/W6.
    #
    # 0x08C6E884 is a packed 32-bit field on this hardware path:
    #   low 16 bits  = map/zone id
    #   high 16 bits = runtime/status flags
    #
    # Hardware evidence from v0p15:
    #   0x00000017 -> Route 101
    #   0x00010017 -> still Route 101, upper flag set
    #
    # Therefore terrain authority must compare the low 16-bit zone id while
    # retaining the entire raw word for diagnostics.
    zone_raw = br.u32(core.ZONE_ADDR)
    zone_id = zone_raw & 0xFFFF
    zone_flags = (zone_raw >> 16) & 0xFFFF

    ax = read_f32(br, PRIMARY_X_ADDR)
    az = read_f32(br, PRIMARY_Z_ADDR)
    bx = read_f32(br, SECONDARY_X_ADDR)
    bz = read_f32(br, SECONDARY_Z_ADDR)

    duplicates_match = (
        abs(ax - bx) <= POSITION_EPSILON
        and abs(az - bz) <= POSITION_EPSILON
    )

    gx = nearest_grid(ax)
    gz = nearest_grid(az)
    settled = (
        abs(ax - grid_center(gx)) <= CENTER_EPSILON
        and abs(az - grid_center(gz)) <= CENTER_EPSILON
        and abs(bx - grid_center(gx)) <= CENTER_EPSILON
        and abs(bz - grid_center(gz)) <= CENTER_EPSILON
    )

    g = (gx, gz)
    return {
        "zone": zone_id,  # backward-compatible normalized zone authority
        "zone_id": zone_id,
        "zone_raw": zone_raw,
        "zone_raw_hex": hx(zone_raw),
        "zone_flags": zone_flags,
        "zone_flags_hex": f"0x{zone_flags:04X}",
        "world_a": [ax, az],
        "world_b": [bx, bz],
        "duplicates_match": duplicates_match,
        "settled_tile_center": settled,
        "grid": [gx, gz],
        "mask": terrain.mask_record(g),
    }


def require_safe_position(pos: dict, label: str) -> tuple[int, int]:
    if pos["zone_id"] != terrain.ROUTE101_ZONE:
        raise IntegrationHold(
            f"{label}: expected Route101 zone id 23, got "
            f"{pos['zone_id']} (raw {pos['zone_raw_hex']})"
        )
    if not pos["duplicates_match"]:
        raise IntegrationHold(f"{label}: duplicate world coordinates disagree")
    if not pos["settled_tile_center"]:
        raise IntegrationHold(f"{label}: player is not settled on a tile center")
    if not pos["mask"]["core1"]:
        raise IntegrationHold(
            f"{label}: grid {pos['grid']} is outside the proven W2/W6 Route101 core1 slice"
        )
    return tuple(pos["grid"])



def require_bunny_interior_position(pos: dict, label: str) -> tuple[int, int]:
    """
    Acro Bike stationary authority.

    Unlike walking W6, a bunny-hop start does NOT need an exact tile-centre
    settle because the player can already be in Acro animation/offset while
    still belonging to the same logical grass tile.

    "Not an edge" is represented by Route101 core2:
      - current grid is known grass/core1
      - all four orthogonal neighbouring cells are also inside the same
        proven grass component

    While B is latched, the logical grid must remain exactly the original
    anchor grid. Any grid change is a HOLD.
    """
    if pos["zone_id"] != terrain.ROUTE101_ZONE:
        raise IntegrationHold(
            f"{label}: expected Route101 zone id 23, got "
            f"{pos['zone_id']} (raw {pos['zone_raw_hex']})"
        )
    if not pos["duplicates_match"]:
        raise IntegrationHold(f"{label}: duplicate world coordinates disagree")
    if not pos["mask"]["core1"]:
        raise IntegrationHold(
            f"{label}: grid {pos['grid']} is outside the proven Route101 grass component"
        )
    if not pos["mask"]["core2"]:
        raise IntegrationHold(
            f"{label}: grid {pos['grid']} is a grass edge/boundary tile; "
            "start one tile further inside the grass"
        )
    return tuple(pos["grid"])


def wait_transition_neutral(br: core.Bridge) -> dict:
    states = []
    br.release_all()
    for i in range(1, TRANSITION_RECHECKS + 1):
        b = br.u32(core.BATTLE_ADDR)
        states.append({
            "index": i,
            "time": now_iso(),
            "battle": hx(b),
        })
        if b == core.BATTLE_ACTIVE:
            return {"resolved": "BATTLE_ACTIVE", "states": states}
        if b == core.BATTLE_INACTIVE:
            return {"resolved": "FIELD_INACTIVE", "states": states}
        time.sleep(TRANSITION_RECHECK_SEC)
    return {"resolved": "UNRESOLVED", "states": states}



def wait_transition_while_latched(br: core.Bridge) -> dict:
    """
    Observe battle TRANSITION while preserving the current command-10 HID latch.

    This is Acro-specific. Unlike the generic neutral transition resolver,
    this function NEVER sends RELEASE_ALL while the transition is being
    classified. That prevents a harmless field transient from introducing a
    B-release gap.

    Outcomes:
      BATTLE_ACTIVE   -> caller immediately RELEASE_ALL and enters encounter
      FIELD_INACTIVE  -> caller keeps the SAME latch active and continues
      UNRESOLVED      -> caller RELEASE_ALL and safety-HOLDs
    """
    states = []
    for i in range(1, TRANSITION_RECHECKS + 1):
        b = br.u32(core.BATTLE_ADDR)
        states.append({
            "index": i,
            "time": now_iso(),
            "battle": hx(b),
        })
        if b == core.BATTLE_ACTIVE:
            return {"resolved": "BATTLE_ACTIVE", "states": states}
        if b == core.BATTLE_INACTIVE:
            return {"resolved": "FIELD_INACTIVE", "states": states}
        time.sleep(TRANSITION_RECHECK_SEC)
    return {"resolved": "UNRESOLVED", "states": states}


def run_movement_pulse(
    br: core.Bridge,
    raw_hid: int,
    hold_ms: int,
    settle_ms: int,
) -> dict:
    # core method explicitly sends the gameplay pulse with retries=1.
    rec = br.hid_pulse_no_retransmit(raw_hid, hold_ms, settle_ms)
    if not rec["completed"]:
        raise IntegrationHold(
            f"movement pulse {hx(raw_hid)} did not reach COMPLETED"
        )
    return rec


def validate_fluid_endpoint(
    before_grid: tuple[int, int],
    after_grid: tuple[int, int],
    plan: dict,
) -> dict:
    dx = after_grid[0] - before_grid[0]
    dz = after_grid[1] - before_grid[1]

    expected_axis = {
        "RIGHT": (1, 0),
        "LEFT": (-1, 0),
        "DOWN": (0, 1),
        "UP": (0, -1),
    }[plan["direction"]]

    if expected_axis[0] != 0:
        axis_ok = dz == 0 and dx * expected_axis[0] > 0
        moved = abs(dx)
    else:
        axis_ok = dx == 0 and dz * expected_axis[1] > 0
        moved = abs(dz)

    corridor_grids = [tuple(c["grid"]) for c in plan["corridor"]]
    endpoint_in_corridor = after_grid in corridor_grids

    # Reserve is checked against the plan, not by interpolating observed speed.
    reserve_ok = (
        len(plan["corridor"])
        >= plan["max_observed_tiles"] + plan["boundary_reserve_tiles"]
    )
    movement_short_of_nominal = moved < plan["nominal_tiles"]
    movement_overshoot = moved > plan["max_observed_tiles"]
    positive_progress = moved >= 1

    # IMPORTANT:
    # "nominal" is a planner target, not a minimum safe displacement.
    # Hardware can legitimately cover fewer tiles during a timed B+direction
    # pulse. Safety is defined by:
    #   - movement on the commanded axis/direction
    #   - endpoint remaining inside the pre-authorized core1 corridor
    #   - planner preserving the +1 reserve against the proven MAX displacement
    #   - no movement beyond the proven max
    #
    # A short-of-nominal pulse is logged but is NOT a safety failure.
    return {
        "delta_grid": [dx, dz],
        "tiles_moved": moved,
        "nominal_tiles": plan["nominal_tiles"],
        "max_observed_tiles": plan["max_observed_tiles"],
        "axis_ok": axis_ok,
        "endpoint_in_corridor": endpoint_in_corridor,
        "boundary_reserve_preserved": reserve_ok,
        "positive_progress": positive_progress,
        "movement_short_of_nominal": movement_short_of_nominal,
        "movement_overshoot": movement_overshoot,
        "pass": (
            axis_ok
            and endpoint_in_corridor
            and reserve_ok
            and positive_progress
            and not movement_overshoot
        ),
    }


def do_reposition(
    br: core.Bridge,
    before: dict,
    plan: dict,
) -> dict:
    before_grid = tuple(before["grid"])
    direction = plan["direction"]
    raw_hid = HID_DIR[direction]
    expected = tuple(plan["target_grid"])

    controllers = []

    def run_attempt(attempt: int):
        controller = run_movement_pulse(
            br,
            raw_hid,
            160,
            140,
        )
        controllers.append(controller)
        br.release_all()
        time.sleep(0.70)

        battle = br.u32(core.BATTLE_ADDR)
        if battle == core.BATTLE_TRANSITION:
            trans = wait_transition_neutral(br)
            if trans["resolved"] == "BATTLE_ACTIVE":
                return {
                    "boundary": {
                        "mode": "reposition",
                        "plan": {**plan, "raw_hid": hx(raw_hid)},
                        "controller": controller,
                        "controllers": controllers,
                        "reposition_attempt": attempt,
                        "battle": hx(core.BATTLE_ACTIVE),
                        "transition": trans,
                        "status": "ENCOUNTER_BOUNDARY",
                    }
                }
            if trans["resolved"] != "FIELD_INACTIVE":
                raise IntegrationHold("reposition battle transition did not resolve")
            battle = core.BATTLE_INACTIVE

        if battle == core.BATTLE_ACTIVE:
            return {
                "boundary": {
                    "mode": "reposition",
                    "plan": {**plan, "raw_hid": hx(raw_hid)},
                    "controller": controller,
                    "controllers": controllers,
                    "reposition_attempt": attempt,
                    "battle": hx(battle),
                    "status": "ENCOUNTER_BOUNDARY",
                }
            }

        if battle != core.BATTLE_INACTIVE:
            raise IntegrationHold(
                f"reposition unexpected battle state {hx(battle)}"
            )

        after = read_position(br)
        after_grid = require_safe_position(
            after, f"reposition after attempt {attempt}"
        )
        return {
            "battle": battle,
            "after": after,
            "after_grid": after_grid,
            "controller": controller,
        }

    first = run_attempt(1)
    if "boundary" in first:
        return first["boundary"]

    after = first["after"]
    after_grid = first["after_grid"]
    battle = first["battle"]
    retry_used = False

    exact_target = after_grid == expected
    same_component = terrain.in_core1(after_grid)

    if not exact_target:
        # Hardware has shown that an otherwise valid one-tile reposition can
        # occasionally be completely ignored.  Permit ONE retry only when
        # RAM proves zero displacement and the original tile remains safe.
        # Re-prove the unchanged field state immediately before retrying so
        # this can never become a blind retransmit after delayed movement.
        if after_grid != before_grid or not same_component:
            raise IntegrationHold(
                f"reposition did not land exactly one safe tile: "
                f"{list(before_grid)} -> {list(after_grid)}, expected {list(expected)}"
            )

        time.sleep(0.35)
        retry_battle = br.u32(core.BATTLE_ADDR)
        if retry_battle != core.BATTLE_INACTIVE:
            raise IntegrationHold(
                f"reposition zero-move retry lost field authority: "
                f"battle={hx(retry_battle)}"
            )

        retry_before = read_position(br)
        retry_before_grid = require_safe_position(
            retry_before, "reposition zero-move retry proof"
        )
        if retry_before_grid != before_grid or not terrain.in_core1(retry_before_grid):
            raise IntegrationHold(
                f"reposition zero-move retry proof changed position: "
                f"{list(before_grid)} -> {list(retry_before_grid)}"
            )

        retry_used = True
        second = run_attempt(2)
        if "boundary" in second:
            return second["boundary"]
        after = second["after"]
        after_grid = second["after_grid"]
        battle = second["battle"]
        exact_target = after_grid == expected
        same_component = terrain.in_core1(after_grid)

        if not exact_target or not same_component:
            raise IntegrationHold(
                f"reposition retry did not land exactly one safe tile: "
                f"{list(before_grid)} -> {list(after_grid)}, expected {list(expected)}"
            )

    if not same_component:
        raise IntegrationHold(
            f"reposition left safe component: {list(after_grid)}"
        )

    return {
        "mode": "reposition",
        "plan": {**plan, "raw_hid": hx(raw_hid)},
        "controller": controllers[-1],
        "controllers": controllers,
        "reposition_attempts": len(controllers),
        "zero_move_retry_used": retry_used,
        "battle": hx(battle),
        "after": after,
        "validation": {
            "delta_grid": [
                after_grid[0] - before_grid[0],
                after_grid[1] - before_grid[1],
            ],
            "tiles_moved": 1,
            "expected_target_grid": list(expected),
            "exact_target": exact_target,
            "same_core1_component": same_component,
            "zero_move_retry_used": retry_used,
        },
        "status": (
            "PASS_REPOSITION_ONE_TILE_AFTER_ZERO_MOVE_RETRY"
            if retry_used else
            "PASS_REPOSITION_ONE_TILE"
        ),
    }


def do_fluid(
    br: core.Bridge,
    before: dict,
    plan: dict,
) -> dict:
    before_grid = tuple(before["grid"])
    direction = plan["direction"]
    raw_hid = HID_B_DIR[direction]
    plan_out = {**plan, "raw_hid": hx(raw_hid)}
    controllers = []
    zero_move_retry = False

    for attempt in (1, 2):
        controller = run_movement_pulse(
            br,
            raw_hid,
            plan["hold_ms"],
            MOVEMENT_SETTLE_MS,
        )
        controllers.append(controller)
        br.release_all()
        time.sleep(POST_FLUID_SETTLE_SEC)

        battle = br.u32(core.BATTLE_ADDR)

        if battle == core.BATTLE_TRANSITION:
            trans = wait_transition_neutral(br)
            if trans["resolved"] == "BATTLE_ACTIVE":
                return {
                    "mode": "fluid",
                    "plan": plan_out,
                    "controller": controller,
                    "controllers": controllers,
                    "fluid_attempt": attempt,
                    "zero_move_retry_used": zero_move_retry,
                    "battle": hx(core.BATTLE_ACTIVE),
                    "transition": trans,
                    "status": "ENCOUNTER_BOUNDARY",
                }
            if trans["resolved"] != "FIELD_INACTIVE":
                raise IntegrationHold("fluid battle transition did not resolve")
            battle = core.BATTLE_INACTIVE

        if battle == core.BATTLE_ACTIVE:
            return {
                "mode": "fluid",
                "plan": plan_out,
                "controller": controller,
                "controllers": controllers,
                "fluid_attempt": attempt,
                "zero_move_retry_used": zero_move_retry,
                "battle": hx(battle),
                "status": "ENCOUNTER_BOUNDARY",
            }

        if battle != core.BATTLE_INACTIVE:
            raise IntegrationHold(f"fluid unexpected battle state {hx(battle)}")

        after = read_position(br)
        after_grid = require_safe_position(after, f"fluid after attempt {attempt}")
        validation = validate_fluid_endpoint(before_grid, after_grid, plan)

        if validation["pass"]:
            return {
                "mode": "fluid",
                "plan": plan_out,
                "controller": controller,
                "controllers": controllers,
                "fluid_attempt": attempt,
                "zero_move_retry_used": zero_move_retry,
                "battle": hx(battle),
                "after": after,
                "validation": validation,
                "status": (
                    "PASS_NO_BATTLE_AFTER_ZERO_MOVE_RETRY"
                    if zero_move_retry else "PASS_NO_BATTLE"
                ),
            }

        # Hardware-proven recovery: an otherwise valid movement command can
        # occasionally produce exactly zero displacement. Retry ONCE only if
        # the game is still provably on the same safe field tile. Partial or
        # wrong-axis movement is never retried.
        exact_zero_move = (
            validation["delta_grid"] == [0, 0]
            and validation["tiles_moved"] == 0
            and after_grid == before_grid
        )
        if attempt == 1 and exact_zero_move:
            # Re-prove field authority immediately before the second pulse.
            # require_safe_position already proved zone/duplicate coordinates,
            # tile settling and terrain safety; battle was re-read as inactive.
            reproved = read_position(br)
            reproved_grid = require_safe_position(
                reproved, "fluid zero-move retry reproof"
            )
            reproved_battle = br.u32(core.BATTLE_ADDR)
            if (
                reproved_battle == core.BATTLE_INACTIVE
                and reproved_grid == before_grid
            ):
                zero_move_retry = True
                continue

        raise IntegrationHold(
            f"fluid endpoint validation failed: {validation}"
        )

    raise IntegrationHold("fluid zero-move retry exhausted")



def wait_for_field_authority(
    br: core.Bridge,
    timeout: float = FIELD_AUTHORITY_TIMEOUT_SEC,
) -> dict:
    """
    After causal Run has already proven battle inactive, wait until the field
    position becomes authoritative again.

    No gameplay input is sent here.

    Authority requires, simultaneously:
      - battle remains INACTIVE
      - normalized low-16-bit zone id == Route 101 (23)
      - the two duplicate world-coordinate copies agree
      - both coordinate copies are settled on the same tile center
      - the resolved tile is inside the proven Route101 core1 component

    Two consecutive identical safe samples are required before movement can
    resume. A transient mismatch/unsettled sample is logged and waited out,
    never treated as permission to move.
    """
    deadline = time.monotonic() + timeout
    samples = []
    consecutive = 0
    last_safe_grid = None

    while time.monotonic() < deadline:
        battle = br.u32(core.BATTLE_ADDR)
        pos = read_position(br)

        safe_now = (
            battle == core.BATTLE_INACTIVE
            and pos["zone_id"] == terrain.ROUTE101_ZONE
            and pos["duplicates_match"]
            and pos["settled_tile_center"]
            and pos["mask"]["core1"]
        )

        grid_tuple = tuple(pos["grid"])
        if safe_now:
            if last_safe_grid == grid_tuple:
                consecutive += 1
            else:
                last_safe_grid = grid_tuple
                consecutive = 1
        else:
            consecutive = 0
            last_safe_grid = None

        sample = {
            "time": now_iso(),
            "battle": hx(battle),
            "safe_now": safe_now,
            "consecutive_safe_samples": consecutive,
            "position": pos,
        }
        samples.append(sample)

        if safe_now and consecutive >= FIELD_AUTHORITY_CONFIRM_SAMPLES:
            return {
                "ready": True,
                "status": "FIELD_AUTHORITY_CONFIRMED",
                "confirmed_grid": list(grid_tuple),
                "confirm_samples": consecutive,
                "samples": samples,
                "final_position": pos,
            }

        # If battle unexpectedly becomes active again, do not wait through it:
        # no movement is safe under that condition.
        if battle == core.BATTLE_ACTIVE:
            return {
                "ready": False,
                "status": "UNEXPECTED_BATTLE_REACTIVATION",
                "samples": samples,
                "final_position": pos,
            }

        time.sleep(FIELD_AUTHORITY_POLL_SEC)

    final = samples[-1]["position"] if samples else None
    return {
        "ready": False,
        "status": "FIELD_AUTHORITY_TIMEOUT",
        "samples": samples,
        "final_position": final,
    }


def movement_until_encounter(
    br: core.Bridge,
    previous_direction: str | None,
    global_burst_start: int,
) -> tuple[dict, str | None, int]:
    bursts = []
    burst_no = global_burst_start

    while True:
        burst_no += 1

        battle_before = br.u32(core.BATTLE_ADDR)
        if battle_before == core.BATTLE_ACTIVE:
            return {
                "encounter": True,
                "bursts": bursts,
                "global_burst": burst_no - 1,
                "reason": "battle already active",
            }, previous_direction, burst_no - 1

        if battle_before == core.BATTLE_TRANSITION:
            trans = wait_transition_neutral(br)
            bursts.append({
                "burst": burst_no,
                "status": "PREMOVE_TRANSITION",
                "transition": trans,
            })
            if trans["resolved"] == "BATTLE_ACTIVE":
                return {
                    "encounter": True,
                    "bursts": bursts,
                    "global_burst": burst_no,
                    "reason": "transition resolved active",
                }, previous_direction, burst_no
            if trans["resolved"] != "FIELD_INACTIVE":
                raise IntegrationHold("pre-movement transition unresolved")

        before = read_position(br)
        grid = require_safe_position(before, "movement before")

        fluid, cs = terrain.choose_fluid_plan(grid, previous_direction)
        corridor_counts = {d: len(v) for d, v in cs.items()}

        if fluid is None:
            reposition = terrain.choose_reposition(grid, previous_direction)
            if reposition is None:
                raise IntegrationHold(
                    f"no safe W6 fluid plan or one-tile reposition from {list(grid)}"
                )
            rec = do_reposition(br, before, reposition)
            rec["burst"] = burst_no
            rec["before"] = before
            rec["corridors"] = corridor_counts
            bursts.append(rec)
            previous_direction = reposition["direction"]
        else:
            rec = do_fluid(br, before, fluid)
            rec["burst"] = burst_no
            rec["before"] = before
            rec["corridors"] = corridor_counts
            bursts.append(rec)
            previous_direction = fluid["direction"]

        if rec["status"] == "ENCOUNTER_BOUNDARY":
            return {
                "encounter": True,
                "bursts": bursts,
                "global_burst": burst_no,
            }, previous_direction, burst_no


def bunny_until_encounter(
    br: core.Bridge,
    anchor_grid: tuple[int, int],
    global_burst_start: int,
) -> tuple[dict, int]:
    """
    True Acro Bike stationary encounter trigger.

    Command 10 latches B ONCE. Firmware then keeps B continuously asserted;
    there are no neutral gaps and no repeated gameplay pulses. Battle RAM is
    polled while B remains held. The first ACTIVE/TRANSITION battle boundary
    immediately authorizes RELEASE_ALL before PK6 processing.

    Position is revalidated periodically while still in field. Any tile drift
    or a 120-second no-encounter timeout releases all input and HOLDs.
    """
    cycle_no = global_burst_start + 1

    battle_before = br.u32(core.BATTLE_ADDR)
    if battle_before == core.BATTLE_ACTIVE:
        rel = br.release_all()
        return {
            "encounter": True,
            "mode": "ACRO_BUNNY_LATCH",
            "bursts": [],
            "global_burst": global_burst_start,
            "anchor_grid": list(anchor_grid),
            "reason": "battle already active",
            "release_all": rel,
        }, global_burst_start

    if battle_before == core.BATTLE_TRANSITION:
        rel = br.release_all()
        trans = wait_transition_neutral(br)
        if trans["resolved"] == "BATTLE_ACTIVE":
            return {
                "encounter": True,
                "mode": "ACRO_BUNNY_LATCH",
                "bursts": [{
                    "burst": cycle_no,
                    "mode": "acro_bunny_latch",
                    "status": "PRE_LATCH_TRANSITION",
                    "transition": trans,
                    "release_all": rel,
                }],
                "global_burst": cycle_no,
                "anchor_grid": list(anchor_grid),
                "reason": "transition resolved active",
            }, cycle_no
        if trans["resolved"] != "FIELD_INACTIVE":
            raise IntegrationHold("pre-latch transition unresolved")

    before = read_position(br)
    before_grid = require_bunny_interior_position(before, "bunny latch before")
    if before_grid != anchor_grid:
        raise IntegrationHold(
            f"Acro bunny anchor changed before latch: "
            f"expected {list(anchor_grid)}, got {list(before_grid)}"
        )

    latch = br.hid_latch_no_retransmit(HID_B)
    if not latch.get("active"):
        raise IntegrationHold(f"Acro bunny B latch was not proven active: {latch}")

    started_mono = time.monotonic()
    last_position_check = started_mono
    checks = []

    rec = {
        "burst": cycle_no,
        "mode": "acro_bunny_latch",
        "raw_hid": hx(HID_B),
        "command": core.CMD_INPUT_HID_LATCH,
        "continuous_hold": True,
        "controller": latch,
        "before": before,
        "anchor_grid": list(anchor_grid),
        "battle_poll_seconds": BUNNY_BATTLE_POLL_SEC,
        "position_check_seconds": BUNNY_POSITION_CHECK_SEC,
        "max_hold_seconds": BUNNY_MAX_HOLD_SEC,
        "position_checks": checks,
    }

    while True:
        battle = br.u32(core.BATTLE_ADDR)
        elapsed = time.monotonic() - started_mono

        if battle == core.BATTLE_ACTIVE:
            rel = br.release_all()
            rec["battle"] = hx(battle)
            rec["held_seconds"] = elapsed
            rec["release_all"] = rel
            rec["status"] = "ENCOUNTER_BOUNDARY_RELEASED"
            return {
                "encounter": True,
                "mode": "ACRO_BUNNY_LATCH",
                "bursts": [rec],
                "global_burst": cycle_no,
                "anchor_grid": list(anchor_grid),
            }, cycle_no

        if battle == core.BATTLE_TRANSITION:
            # Acro Bike can expose a brief battle-state TRANSITION that returns
            # to normal field while B is continuously held. Do NOT release B
            # merely because the transient appeared; classify it first while
            # preserving the same command-10 latch.
            trans = wait_transition_while_latched(br)
            rec.setdefault("latched_transitions", []).append({
                "time": now_iso(),
                "held_seconds": elapsed,
                "transition": trans,
            })

            if trans["resolved"] == "BATTLE_ACTIVE":
                rel = br.release_all()
                rec["battle"] = hx(battle)
                rec["held_seconds"] = elapsed
                rec["release_all"] = rel
                rec["transition"] = trans
                rec["status"] = "ENCOUNTER_TRANSITION_RELEASED"
                return {
                    "encounter": True,
                    "mode": "ACRO_BUNNY_LATCH",
                    "bursts": [rec],
                    "global_burst": cycle_no,
                    "anchor_grid": list(anchor_grid),
                }, cycle_no

            if trans["resolved"] == "FIELD_INACTIVE":
                # Same latch remains physically active. No second command-10
                # send and no neutral gap are introduced here.
                continue

            rel = br.release_all()
            rec["battle"] = hx(battle)
            rec["held_seconds"] = elapsed
            rec["release_all"] = rel
            rec["transition"] = trans
            rec["status"] = "HOLD_TRANSITION_UNRESOLVED"
            raise IntegrationHold(
                "latched B transition did not resolve to field or active battle"
            )

        if battle != core.BATTLE_INACTIVE:
            br.release_all()
            raise IntegrationHold(f"Acro bunny unexpected battle state {hx(battle)}")

        now_mono = time.monotonic()
        if now_mono - last_position_check >= BUNNY_POSITION_CHECK_SEC:
            pos = read_position(br)
            grid = require_bunny_interior_position(pos, "bunny latch held")
            check = {
                "time": now_iso(),
                "elapsed_seconds": now_mono - started_mono,
                "grid": list(grid),
                "same_anchor": grid == anchor_grid,
                "position": pos,
            }
            checks.append(check)
            last_position_check = now_mono
            if grid != anchor_grid:
                br.release_all()
                rec["status"] = "HOLD_TILE_DRIFT"
                raise IntegrationHold(
                    f"Acro bunny unexpectedly moved player while B latched: "
                    f"{list(anchor_grid)} -> {list(grid)}"
                )

        if elapsed >= BUNNY_MAX_HOLD_SEC:
            rel = br.release_all()
            rec["held_seconds"] = elapsed
            rec["release_all"] = rel
            rec["status"] = "HOLD_NO_ENCOUNTER_TIMEOUT"
            raise IntegrationHold(
                f"Acro bunny B was continuously held for {BUNNY_MAX_HOLD_SEC:.0f}s "
                "without an encounter"
            )

        time.sleep(BUNNY_BATTLE_POLL_SEC)


def atomic_write_json(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    tmp.replace(path)


def append_jsonl(path: Path, obj: dict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, separators=(",", ":")) + "\n")
        fh.flush()


def compact_encounter_summary(enc: dict) -> dict:
    pk6 = enc.get("wild_pk6") or {}
    causal = enc.get("causal_run") or {}
    movement = enc.get("movement") or {}
    field = enc.get("post_escape_position") or {}

    timeout_recoveries = 0
    for attempt in causal.get("attempts", []):
        touch = attempt.get("touch") or {}
        if touch.get("response_timeout_recovery"):
            timeout_recoveries += 1

    bursts = movement.get("bursts", [])
    return {
        "encounter": enc.get("encounter"),
        "started": enc.get("started"),
        "finished": enc.get("finished"),
        "species": pk6.get("species"),
        "pid": pk6.get("pid"),
        "ec": pk6.get("ec"),
        "shiny_xor": pk6.get("shiny_xor"),
        "is_shiny": pk6.get("is_shiny"),
        "ivs": pk6.get("ivs"),
        "iv_sum": pk6.get("iv_sum"),
        "trigger_latch_cycles_this_encounter": len(bursts),
        "trigger_mode": movement.get("mode", "ACRO_BUNNY"),
        "global_burst": movement.get("global_burst"),
        "accepted_run_attempt": causal.get("accepted_attempt"),
        "touch_timeout_recoveries": timeout_recoveries,
        "post_escape_grid": field.get("grid"),
        "post_escape_core1": (field.get("mask") or {}).get("core1"),
        "result": enc.get("result"),
    }


def build_session_state(
    *,
    started: str,
    host: str,
    target_encounters: int,
    encounters_completed: int,
    global_burst: int,
    species_counts: Counter,
    run_attempt_counts: Counter,
    timeout_recoveries: int,
    last_summary: dict | None,
    status: str,
) -> dict:
    started_dt = datetime.fromisoformat(started)
    now_dt = datetime.now().astimezone()
    elapsed = max(0.0, (now_dt - started_dt).total_seconds())
    rate = (encounters_completed / elapsed * 3600.0) if elapsed > 0 else 0.0
    return {
        "tool": "Pokebot3DS-CFW Acro Bunny Latch 10 v0p27",
        "baseline": "v0p23 PASS_W6_CAUSAL_15_OF_15",
        "started": started,
        "updated": now_dt.isoformat(timespec="seconds"),
        "host": host,
        "mode": "UNLIMITED" if target_encounters == 0 else "FINITE",
        "target_encounters": target_encounters,
        "status": status,
        "encounters_completed": encounters_completed,
        "elapsed_seconds": round(elapsed, 3),
        "encounters_per_hour": round(rate, 3),
        "global_latch_cycles": global_burst,
        "species_counts": {str(k): v for k, v in sorted(species_counts.items())},
        "run_attempt_counts": {str(k): v for k, v in sorted(run_attempt_counts.items())},
        "touch_timeout_recoveries": timeout_recoveries,
        "last_encounter": last_summary,
    }


def save_support_report(
    *,
    prefix: str,
    metadata: dict,
    rolling_details,
    current_encounter: dict | None,
    result: dict,
) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(f"AcroBunnyLatch_v0p27_{stamp}_{prefix}.json")
    payload = {
        **metadata,
        "finished": datetime.now().astimezone().isoformat(timespec="seconds"),
        "result": result,
        "rolling_detailed_encounters": list(rolling_details),
    }
    if current_encounter is not None:
        payload["current_encounter"] = current_encounter
    atomic_write_json(out, payload)
    return out


def main() -> int:
    p = argparse.ArgumentParser(
        description="Pokebot3DS-CFW Acro Bike bunny-hop wild proof v0p27"
    )
    p.add_argument("host", nargs="?", default="192.168.0.28")
    p.add_argument(
        "--encounters",
        type=int,
        default=DEFAULT_TARGET_ENCOUNTERS,
        help="finite Acro Bunny proof count; default 10",
    )
    args = p.parse_args()

    if args.encounters <= 0:
        p.error("--encounters must be a positive finite proof count")

    target_encounters = args.encounters
    br = core.Bridge(args.host)

    started = datetime.now().astimezone().isoformat(timespec="seconds")
    session_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary_log = Path(f"AcroBunnyLatch_v0p27_{session_stamp}_encounters.jsonl")
    state_file = Path(f"AcroBunnyLatch_v0p27_{session_stamp}_state.json")

    mode_label = f"ACRO_BUNNY_LATCH_FINITE {target_encounters}"

    print("Pokebot3DS-CFW — Acro Bunny Latch v0p27")
    print()
    print(f"Mode: {mode_label}")
    print("Baseline: v0p23 PASS_W6_CAUSAL_15_OF_15")
    print("Acro Bike trigger proof: true continuous B latch + proven PK6/causal Run path.")
    print()
    print("Start already mounted on the ACRO BIKE on ANY proven interior Route 101 grass tile.")
    print("Do not press a direction. The bot presses B once and keeps it held until battle RAM changes.")
    print("There are no programmed B-release gaps before the encounter; field-returning battle transients keep the same latch active.")
    print()
    print("Session files:")
    print(" ", summary_log.resolve())
    print(" ", state_file.resolve())
    print()

    policy = {
        "baseline": {
            "source": "v0p23 PASS_W6_CAUSAL_15_OF_15",
            "validation": "30/30 complete pass",
            "hunt_state_machine_changes": "NONE",
        },
        "mode": "ACRO_BUNNY_LATCH_FINITE",
        "target_encounters": target_encounters,
        "encounter_trigger": {
            "type": "ACRO_BIKE_CONTINUOUS_B_LATCH",
            "command": core.CMD_INPUT_HID_LATCH,
            "already_mounted_required": True,
            "raw_hid": hx(HID_B),
            "presses_per_encounter": 1,
            "continuous_until_battle_boundary": True,
            "neutral_gap_before_encounter": False,
            "battle_poll_seconds": BUNNY_BATTLE_POLL_SEC,
            "position_check_seconds": BUNNY_POSITION_CHECK_SEC,
            "max_hold_seconds": BUNNY_MAX_HOLD_SEC,
            "direction_input": False,
            "start_tile": "ANY CORE2 / NON-EDGE GRASS TILE",
            "exact_tile_center_required": False,
            "tile_drift": "ABSOLUTE HOLD + RELEASE_ALL",
            "gameplay_input_retransmission": False,
            "release_authority": "BATTLE ACTIVE or transition-resolved ACTIVE; field transients keep latch held",
            "field_transition_behavior": "KEEP SAME COMMAND-10 LATCH ACTIVE",
        },
        "reporting": {
            "compact_jsonl_streaming": True,
            "state_json_overwritten_each_encounter": True,
            "rolling_detailed_encounters_in_memory": ROLLING_DETAIL_ENCOUNTERS,
            "unbounded_full_session_json_in_memory": False,
        },
        "terrain": {
            "zone": terrain.ROUTE101_ZONE,
            "matrix_id": terrain.MATRIX_ID,
            "terrain_class": terrain.TERRAIN_CLASS,
            "component_id": terrain.COMPONENT_ID,
            "unknown_cells": "UNSAFE/HOLD",
        },
        "wild_pk6_logical_reads_per_encounter": 1,
        "shiny_or_invalid": "ABSOLUTE HOLD",
        "post_escape_field_authority": {
            "fixed_delay": False,
            "timeout_seconds": FIELD_AUTHORITY_TIMEOUT_SEC,
            "poll_seconds": FIELD_AUTHORITY_POLL_SEC,
            "confirm_samples": FIELD_AUTHORITY_CONFIRM_SAMPLES,
            "requires_battle_inactive": True,
            "requires_zone23_low16": True,
            "requires_duplicate_coordinates_match": True,
            "requires_tile_center_settle": True,
            "requires_core1": True,
            "gameplay_input_during_wait": False,
        },
        "new_escape_authority": (
            "after valid non-shiny PK6, native Run pulse -> observe battle outcome; "
            "repeat only while battle remains ACTIVE"
        ),
        "native_touch": {
            "command": core.CMD_INPUT_TOUCH_PULSE,
            "screen": list(core.RUN_TOUCH_XY),
            "encoded": hx(core.RUN_TOUCH_STATE),
            "hold_ms": core.TOUCH_HOLD_MS,
            "settle_ms": core.TOUCH_SETTLE_MS,
            "max_taps_per_encounter": core.MAX_RUN_TAPS_PER_ENCOUNTER,
            "udp_response_timeout_recovery": {
                "command9_retransmit": False,
                "recovery_command": core.CMD_INPUT_STATUS,
                "same_sequence_id_required": True,
                "continue_only_if_firmware_status_completed": True,
                "not_found_or_aborted": "SAFETY_HOLD",
            },
        },
    }

    metadata = {
        "tool": "Pokebot3DS-CFW Acro Bunny Latch 10 v0p27",
        "started": started,
        "host": args.host,
        "policy": policy,
    }

    rolling_details = deque(maxlen=ROLLING_DETAIL_ENCOUNTERS)
    species_counts = Counter()
    run_attempt_counts = Counter()
    timeout_recoveries_total = 0
    encounters_completed = 0
    global_burst = 0
    previous_identity = None
    current_enc = None
    last_summary = None

    def write_state(status: str) -> None:
        state = build_session_state(
            started=started,
            host=args.host,
            target_encounters=target_encounters,
            encounters_completed=encounters_completed,
            global_burst=global_burst,
            species_counts=species_counts,
            run_attempt_counts=run_attempt_counts,
            timeout_recoveries=timeout_recoveries_total,
            last_summary=last_summary,
            status=status,
        )
        atomic_write_json(state_file, state)

    try:
        print("PING:", br.ping())
        gi = br.game_info()
        metadata["game_info"] = gi
        print("GAME:", gi["title_id_hex"], "PID", gi["pid"], gi["process"])

        if gi["title_id"] != core.AS_TITLE_ID:
            raise IntegrationHold("Alpha Sapphire 1.4 only")

        caps = br.input_ping()
        metadata["input_capabilities"] = caps
        if not caps["hid_pulse"]:
            raise IntegrationHold("CFW bridge lacks acknowledged HID pulse")
        if not caps["touch_pulse"]:
            raise IntegrationHold("CFW bridge lacks native touch pulse")
        if not caps.get("hid_latch"):
            raise IntegrationHold(
                "CFW bridge lacks command-10 latched HID; install the v0p6 boot.firm"
            )
        if caps["neutral_hid"] != HID_NEUTRAL:
            raise IntegrationHold(
                f"unexpected neutral HID {caps['neutral_hid_hex']}"
            )

        metadata["initial_release_all"] = br.release_all()

        if br.u32(core.BATTLE_ADDR) != core.BATTLE_INACTIVE:
            raise IntegrationHold("start in overworld, not in battle")

        ids = br.read(core.TRAINER_IDS_ADDR, 4)
        save_tid, save_sid = struct.unpack("<HH", ids)
        metadata["save_ids"] = {"tid": save_tid, "sid": save_sid}

        start_pos = read_position(br)
        anchor_grid = require_bunny_interior_position(start_pos, "preflight")
        metadata["start_position"] = start_pos
        metadata["acro_bunny_anchor_grid"] = list(anchor_grid)

        print(
            "START:",
            "zone", start_pos["zone_id"],
            "raw", start_pos["zone_raw_hex"],
            "flags", start_pos["zone_flags_hex"],
            "grid", start_pos["grid"],
            "core2", start_pos["mask"]["core2"],
        )
        print()
        print("Automatic W6 loop starting. Do not touch the 3DS.")
        print("Ctrl+C = RELEASE_ALL + clean session stop.")
        print()

        write_state("RUNNING")

        enc_index = 0
        while encounters_completed < target_encounters:
            enc_index += 1
            current_enc = {
                "encounter": enc_index,
                "started": now_iso(),
            }

            print("=" * 78)
            print(f"ENCOUNTER {enc_index}/{target_encounters}")
            print("=" * 78)

            movement, global_burst = bunny_until_encounter(
                br,
                anchor_grid,
                global_burst,
            )
            current_enc["movement"] = movement

            if not movement["encounter"]:
                raise IntegrationHold(
                    f"movement loop returned without encounter: {movement.get('reason')}"
                )

            br.release_all()
            print(
                f"Battle boundary at global latch cycle {movement['global_burst']}."
            )
            print("Waiting for established PK6 safety boundary...")

            boundary = core.wait_for_state2_for_pk6(br)
            current_enc["pk6_boundary"] = boundary
            if not boundary["ready_for_pk6"]:
                raise IntegrationHold(
                    f"PK6 safety boundary failed: {boundary['status']}"
                )

            # EXACTLY ONE authoritative logical wild PK6 read.
            raw_pk6 = br.read(core.WILD_PK6_ADDR, core.PK6_STORED_SIZE)
            pk6 = core.decode_stored_pk6(raw_pk6, save_tid, save_sid)
            current_enc["wild_pk6"] = pk6

            if not pk6["valid"]:
                raise IntegrationHold(
                    f"invalid wild PK6: {pk6['reason']}"
                )

            if previous_identity is not None and pk6["identity"] == previous_identity:
                raise IntegrationHold("duplicate/stale wild PK6 identity")

            previous_identity = pk6["identity"]

            print(
                f"PK6: species #{pk6['species']} PID {pk6['pid']} "
                f"XOR {pk6['shiny_xor']} shiny={pk6['is_shiny']}"
            )

            if pk6["is_shiny"]:
                raise IntegrationHold(
                    f"SHINY FOUND species #{pk6['species']} PID {pk6['pid']}"
                )

            print("Valid non-shiny -> causal native Run.")
            causal = core.causal_run_until_field(br)
            current_enc["causal_run"] = causal

            if not causal["success"]:
                raise IntegrationHold(
                    "causal Run failed: "
                    + causal.get("reason", "field did not become stable")
                )

            field_authority = wait_for_field_authority(br)
            current_enc["field_authority"] = field_authority

            if not field_authority["ready"]:
                raise IntegrationHold(
                    "post-escape field authority failed: "
                    + field_authority["status"]
                )

            field_pos = field_authority["final_position"]
            field_grid = require_safe_position(field_pos, "post-escape field")
            if not field_pos["mask"]["core2"]:
                raise IntegrationHold(
                    f"post-escape field: anchor grid {field_pos['grid']} is no longer "
                    "an interior grass/core2 tile"
                )
            if field_grid != anchor_grid:
                raise IntegrationHold(
                    f"post-escape Acro bunny anchor changed: "
                    f"expected {list(anchor_grid)}, got {list(field_grid)}"
                )
            current_enc["post_escape_position"] = field_pos
            current_enc["result"] = {
                "status": "PASS",
                "pass": True,
                "accepted_run_attempt": causal.get("accepted_attempt"),
            }
            current_enc["finished"] = now_iso()

            # Update compact, bounded session telemetry.
            last_summary = compact_encounter_summary(current_enc)
            append_jsonl(summary_log, last_summary)
            rolling_details.append(current_enc)

            encounters_completed += 1
            species_counts[pk6["species"]] += 1
            run_attempt_counts[causal.get("accepted_attempt")] += 1

            for attempt in causal.get("attempts", []):
                touch = attempt.get("touch") or {}
                if touch.get("response_timeout_recovery"):
                    timeout_recoveries_total += 1

            write_state("RUNNING")

            rate_state = build_session_state(
                started=started,
                host=args.host,
                target_encounters=target_encounters,
                encounters_completed=encounters_completed,
                global_burst=global_burst,
                species_counts=species_counts,
                run_attempt_counts=run_attempt_counts,
                timeout_recoveries=timeout_recoveries_total,
                last_summary=last_summary,
                status="RUNNING",
            )

            print(
                f"PASS {encounters_completed}/{target_encounters}: causal Run tap "
                f"{causal.get('accepted_attempt')} -> anchor grid "
                f"{field_pos['grid']} | "
                f"{rate_state['encounters_per_hour']:.1f}/hr"
            )
            print()

            current_enc = None

        # Finite diagnostic completion only.
        br.release_all()
        write_state("PASS_FINITE")
        result = {
            "status": f"PASS_ACRO_BUNNY_LATCH_{target_encounters}_OF_{target_encounters}",
            "pass": True,
            "detail": (
                f"{target_encounters} encounters completed using true command-10 "
                "continuous B latch plus the frozen PK6/causal Run backend."
            ),
        }
        out = save_support_report(
            prefix="PASS",
            metadata=metadata,
            rolling_details=rolling_details,
            current_encounter=None,
            result=result,
        )

        print("=" * 80)
        print(result["status"])
        print("Saved:", out.resolve())
        print("=" * 80)
        return 0

    except KeyboardInterrupt:
        try:
            rel = br.release_all()
        except Exception as exc:
            rel = {"error": f"{type(exc).__name__}: {exc}"}

        write_state("MANUAL_STOP")
        result = {
            "status": "MANUAL_STOP",
            "pass": False,
            "detail": (
                "Ctrl+C received; RELEASE_ALL sent; no further gameplay input. "
                f"Completed encounters: {encounters_completed}."
            ),
            "release_all": rel,
        }
        out = save_support_report(
            prefix="MANUAL_STOP",
            metadata=metadata,
            rolling_details=rolling_details,
            current_encounter=current_enc,
            result=result,
        )
        print()
        print("MANUAL_STOP — inputs released.")
        print("Completed encounters:", encounters_completed)
        print("Saved:", out.resolve())
        return 130

    except (IntegrationHold, core.SafetyHold, core.BridgeError) as exc:
        try:
            rel = br.release_all()
        except Exception as release_exc:
            rel = {"error": f"{type(release_exc).__name__}: {release_exc}"}

        is_shiny = str(exc).startswith("SHINY FOUND")
        status = "SHINY_HOLD" if is_shiny else "SAFETY_HOLD"
        write_state(status)

        result = {
            "status": status,
            "pass": False,
            "reason": str(exc),
            "detail": (
                "Hunt stopped immediately; RELEASE_ALL requested; "
                "no further movement or Run input authorized."
            ),
            "encounters_completed_before_hold": encounters_completed,
            "release_all": rel,
        }
        out = save_support_report(
            prefix=status,
            metadata=metadata,
            rolling_details=rolling_details,
            current_encounter=current_enc,
            result=result,
        )

        print()
        print("=" * 80)
        print(status)
        print(str(exc))
        print("Inputs released.")
        print("Completed encounters before hold:", encounters_completed)
        print("Saved:", out.resolve())
        print("=" * 80)
        return 2 if is_shiny else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as exc:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        crash_path = Path(f"AcroBunnyLatch_v0p27_{stamp}_CRASH.json")
        crash = {
            "tool": "Pokebot3DS-CFW Acro Bunny Latch 10 v0p27",
            "status": "PYTHON_CRASH",
            "exception_type": type(exc).__name__,
            "exception": str(exc),
            "traceback": traceback.format_exc(),
        }
        try:
            crash_path.write_text(json.dumps(crash, indent=2), encoding="utf-8")
        except Exception:
            pass
        print()
        print("=" * 80)
        print("PYTHON_CRASH")
        print(type(exc).__name__ + ":", exc)
        print(traceback.format_exc())
        print("Crash report:", crash_path.resolve())
        print("=" * 80)
        raise
