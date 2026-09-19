from __future__ import annotations

"""ORAS Cave movement authority.

Cave floors are not represented by the grass-only encounter-cell database, so
Cave modes use live RAM zone/grid authority rather than pretending raw field
permissions are globally classified collision data.

Cave Walk keeps the proven one-tile oscillation path. Cave Run uses the same
600 ms B+direction profile as validated normal Wild running (nominal 5,
observed max 6 tiles), but treats the game's own cave collision as the local
boundary. A blocked edge is normal: the runner reverses instead of demanding a
large pre-proven corridor.

Safety invariants:
- live ORAS zone is locked for the whole hunt;
- duplicate world-coordinate copies must agree;
- logical cave grid occupancy replaces grass/land tile-centre calibration;
- start/post-battle grids must be stable across repeated RAM samples;
- Cave Run long pulses stay on the selected axis and remain within the proven
  1-6 tile Wild movement range;
- blocked cave collision reverses direction rather than invalidating the start;
- movement commands are never retransmitted blindly.
"""

import time


def allowed_directions(axis_key: str):
    if axis_key == "vertical":
        return ("UP", "DOWN")
    if axis_key == "horizontal":
        return ("LEFT", "RIGHT")
    raise ValueError(f"Unknown cave movement axis: {axis_key}")


def _delta(direction: str):
    return {
        "RIGHT": (1, 0),
        "LEFT": (-1, 0),
        "DOWN": (0, 1),
        "UP": (0, -1),
    }[direction]


def require_cave_position(backend, pos: dict, expected_zone: int, label: str):
    """Return the logical Cave grid without requiring land-style centre settle.

    Fiery Path hardware evidence showed a stationary cave player can fail the
    global grass/land ``settled_tile_center`` calibration before any input is
    sent.  Cave authority therefore follows the Surf fix: exact zone and the
    duplicate world-coordinate copies remain mandatory, while logical grid
    stability is proven separately with repeated RAM samples.
    """
    if int(pos.get("zone_id", -1)) != int(expected_zone):
        raise backend.IntegrationHold(
            f"{label}: live zone changed from {expected_zone} to {pos.get('zone_id')}"
        )
    if not pos.get("duplicates_match"):
        raise backend.IntegrationHold(f"{label}: duplicate world coordinates disagree")
    grid = pos.get("grid")
    if not isinstance(grid, (list, tuple)) or len(grid) != 2:
        raise backend.IntegrationHold(f"{label}: logical Cave grid is unavailable")
    return tuple(int(v) for v in grid)


def read_stable_cave_position(
    br,
    backend,
    expected_zone: int,
    label: str,
    *,
    timeout: float = 3.00,
    required_samples: int = 2,
    poll_sec: float = 0.10,
):
    """Require repeated agreement on one logical Cave grid before movement."""
    deadline = time.monotonic() + float(timeout)
    last_grid = None
    consecutive = 0
    samples = []

    while time.monotonic() < deadline:
        pos = backend.read_position(br)
        try:
            grid = require_cave_position(
                backend, pos, expected_zone, f"{label} sample"
            )
            valid = True
        except Exception as exc:
            samples.append({
                "valid": False,
                "error": str(exc),
                "grid": pos.get("grid"),
                "world_a": pos.get("world_a"),
                "world_b": pos.get("world_b"),
                "duplicates_match": pos.get("duplicates_match"),
                "settled_tile_center": pos.get("settled_tile_center"),
            })
            last_grid = None
            consecutive = 0
            time.sleep(float(poll_sec))
            continue

        if grid == last_grid:
            consecutive += 1
        else:
            last_grid = grid
            consecutive = 1

        samples.append({
            "valid": valid,
            "grid": list(grid),
            "world_a": pos.get("world_a"),
            "world_b": pos.get("world_b"),
            "duplicates_match": pos.get("duplicates_match"),
            "settled_tile_center": pos.get("settled_tile_center"),
            "consecutive": consecutive,
        })
        if consecutive >= int(required_samples):
            return {
                "position": pos,
                "grid": grid,
                "samples": samples,
                "settled_tile_center": bool(pos.get("settled_tile_center")),
            }
        time.sleep(float(poll_sec))

    recent = [
        {
            "valid": x.get("valid"),
            "grid": x.get("grid"),
            "duplicates_match": x.get("duplicates_match"),
            "settled_tile_center": x.get("settled_tile_center"),
            "consecutive": x.get("consecutive"),
        }
        for x in samples[-10:]
    ]
    raise backend.IntegrationHold(
        f"{label}: Cave logical grid did not become stable for "
        f"{required_samples} consecutive RAM samples within {timeout:.2f}s; "
        f"recent_samples={recent}"
    )


def _battle_after_pulse(br, backend):
    battle = br.u32(backend.core.BATTLE_ADDR)
    if battle == backend.core.BATTLE_TRANSITION:
        trans = backend.wait_transition_neutral(br)
        if trans["resolved"] == "BATTLE_ACTIVE":
            return True, trans
        if trans["resolved"] != "FIELD_INACTIVE":
            raise backend.IntegrationHold("cave movement transition did not resolve")
        return False, trans
    if battle == backend.core.BATTLE_ACTIVE:
        return True, None
    if battle != backend.core.BATTLE_INACTIVE:
        raise backend.IntegrationHold(
            f"cave movement unexpected battle state {backend.hx(battle)}"
        )
    return False, None


def cave_until_encounter(
    br,
    backend,
    *,
    axis_key: str,
    expected_zone: int,
    previous_direction: str | None,
    global_burst_start: int,
):
    """Oscillate across a RAM-proven two-tile cave corridor until battle."""
    dirs = allowed_directions(axis_key)
    bursts = []
    burst_no = int(global_burst_start)

    while True:
        battle_before = br.u32(backend.core.BATTLE_ADDR)
        if battle_before == backend.core.BATTLE_ACTIVE:
            pos = backend.read_position(br)
            grid = require_cave_position(backend, pos, expected_zone, "cave active-boundary")
            return ({
                "encounter": True,
                "bursts": bursts,
                "global_burst": burst_no,
                "movement_mode": "cave",
                "axis": axis_key,
                "safe_return_grids": [list(grid)],
                "reason": "battle already active",
            }, previous_direction, burst_no)

        if battle_before == backend.core.BATTLE_TRANSITION:
            trans = backend.wait_transition_neutral(br)
            bursts.append({"status": "PREMOVE_TRANSITION", "transition": trans})
            if trans["resolved"] == "BATTLE_ACTIVE":
                pos = backend.read_position(br)
                grid = require_cave_position(backend, pos, expected_zone, "cave transition-boundary")
                return ({
                    "encounter": True,
                    "bursts": bursts,
                    "global_burst": burst_no,
                    "movement_mode": "cave",
                    "axis": axis_key,
                    "safe_return_grids": [list(grid)],
                }, previous_direction, burst_no)
            if trans["resolved"] != "FIELD_INACTIVE":
                raise backend.IntegrationHold("pre-cave-movement transition unresolved")

        before = backend.read_position(br)
        before_grid = require_cave_position(backend, before, expected_zone, "cave movement before")

        # Once a direction has moved successfully, reverse it next time so the
        # bot remains on the same two tiles. At initial start, try the first
        # direction on the selected axis, then the opposite if it is blocked.
        if previous_direction in dirs:
            candidates = (backend.terrain.OPPOSITE[previous_direction],)
        else:
            candidates = dirs

        moved_this_cycle = False
        for direction in candidates:
            burst_no += 1
            dx, dz = _delta(direction)
            expected = (before_grid[0] + dx, before_grid[1] + dz)
            raw_hid = backend.HID_DIR[direction]

            controller = backend.run_movement_pulse(br, raw_hid, 160, 140)
            br.release_all()
            time.sleep(0.70)

            in_battle, transition = _battle_after_pulse(br, backend)
            rec = {
                "burst": burst_no,
                "direction": direction,
                "raw_hid": backend.hx(raw_hid),
                "controller": controller,
                "before_grid": list(before_grid),
                "expected_target_grid": list(expected),
                "axis": axis_key,
                "timing_profile": "W3C_1tile_160ms_PROVEN_CAVE_ENDPOINT_VERIFIED",
            }
            if transition is not None:
                rec["transition"] = transition

            if in_battle:
                rec["status"] = "ENCOUNTER_BOUNDARY"
                bursts.append(rec)
                return ({
                    "encounter": True,
                    "bursts": bursts,
                    "global_burst": burst_no,
                    "movement_mode": "cave",
                    "axis": axis_key,
                    "safe_return_grids": [list(before_grid), list(expected)],
                    "encounter_direction": direction,
                }, direction, burst_no)

            after = backend.read_position(br)
            after_grid = require_cave_position(backend, after, expected_zone, "cave movement after")
            rec["after_grid"] = list(after_grid)

            if after_grid == before_grid:
                rec["status"] = "BLOCKED_NO_MOVEMENT"
                bursts.append(rec)
                # Only the initial acquisition may try the opposite direction.
                # Once we have a proven two-tile corridor, either endpoint
                # becoming blocked is unexpected and must fail closed.
                if previous_direction in dirs:
                    raise backend.IntegrationHold(
                        f"proven cave return step {direction} became blocked at {list(before_grid)}"
                    )
                continue

            if after_grid != expected:
                raise backend.IntegrationHold(
                    "cave one-tile endpoint mismatch: "
                    f"{list(before_grid)} -> {list(after_grid)}, expected {list(expected)}"
                )

            rec["status"] = "PASS_CAVE_ONE_TILE"
            rec["delta_grid"] = [after_grid[0]-before_grid[0], after_grid[1]-before_grid[1]]
            bursts.append(rec)
            previous_direction = direction
            moved_this_cycle = True
            break

        if not moved_this_cycle:
            raise backend.IntegrationHold(
                f"no clear two-tile cave corridor on {axis_key} axis from {list(before_grid)}; "
                "move to a spot with one clear adjacent tile or change axis"
            )


def wait_for_cave_field(br, backend, expected_zone: int, allowed_grids, timeout=6.0):
    """Require stable post-Run field authority on the encounter's two tiles."""
    allowed = {tuple(int(v) for v in g) for g in (allowed_grids or [])}
    deadline = time.monotonic() + float(timeout)
    samples = []
    consecutive = 0
    last_grid = None

    while time.monotonic() < deadline:
        battle = br.u32(backend.core.BATTLE_ADDR)
        pos = backend.read_position(br)
        try:
            grid = require_cave_position(backend, pos, expected_zone, "cave post-escape sample")
            position_ok = not allowed or grid in allowed
        except Exception:
            grid = tuple(pos.get("grid") or ())
            position_ok = False

        safe = battle == backend.core.BATTLE_INACTIVE and position_ok
        if safe and grid == last_grid:
            consecutive += 1
        elif safe:
            last_grid, consecutive = grid, 1
        else:
            last_grid, consecutive = None, 0

        samples.append({
            "battle": backend.hx(battle),
            "grid": list(grid) if grid else None,
            "position_ok": position_ok,
            "consecutive": consecutive,
        })
        if consecutive >= 2:
            return {"ready": True, "final_position": pos, "samples": samples}
        time.sleep(0.10)

    return {"ready": False, "status": "CAVE_FIELD_TIMEOUT", "samples": samples}

def cave_run_until_encounter(
    br,
    backend,
    *,
    axis_key: str,
    expected_zone: int,
    anchor_grid,
    corridor_state: dict,
    previous_direction: str | None,
    global_burst_start: int,
):
    """Wall-bounded Cave Run using the proven 600 ms Wild run pulse.

    Cave encounter floors are physically bounded by ORAS collision and do not
    need the outdoor grass/core1/core2 containment model.  Earlier builds
    required a +/-6 clear corridor around the starting tile before Run could
    begin; that unnecessarily rejected perfectly valid edge tiles and narrow
    cave sections.

    CW policy:
    - any stable Cave grid may be the start tile;
    - send the normal 600 ms B+direction pulse on the chosen axis;
    - alternate direction after every successful pulse so movement stays local;
    - if the chosen direction is blocked, reverse once and continue;
    - zone lock + duplicate coordinate authority remain mandatory;
    - any perpendicular movement or >6-tile endpoint still HOLDs;
    - a zone transition (for example accidentally reaching an exit) HOLDs via
      ``require_cave_position`` rather than being treated as valid movement.

    No terrain database or corridor pre-proof is consulted here.
    """
    dirs = allowed_directions(axis_key)
    anchor_grid = tuple(int(v) for v in anchor_grid)
    bursts = []
    burst_no = int(global_burst_start)

    if axis_key == "horizontal":
        axis_idx, perp_idx = 0, 1
    elif axis_key == "vertical":
        axis_idx, perp_idx = 1, 0
    else:
        raise backend.IntegrationHold(f"unsupported Cave Run axis {axis_key!r}")

    # Legacy state object is retained for support/report compatibility, but no
    # corridor must be proven before edge movement is authorized.
    if isinstance(corridor_state, dict):
        corridor_state["proven"] = True
        corridor_state["policy"] = "WALL_BOUNDED_NO_CORRIDOR_PREFLIGHT"
        corridor_state["anchor_grid"] = list(anchor_grid)
        corridor_state["axis"] = axis_key
        corridor_state.pop("pending_from_offset", None)
        corridor_state.pop("pending_target_offset", None)
        corridor_state.pop("zero_move_retry", None)

    while True:
        battle_before = br.u32(backend.core.BATTLE_ADDR)
        if battle_before == backend.core.BATTLE_ACTIVE:
            return ({
                "encounter": True,
                "bursts": bursts,
                "global_burst": burst_no,
                "movement_mode": "cave_run",
                "axis": axis_key,
                "anchor_grid": list(anchor_grid),
                "safe_return_grids": [],
                "corridor_proven": False,
                "wall_bounded": True,
                "reason": "battle already active",
            }, previous_direction, burst_no)

        if battle_before == backend.core.BATTLE_TRANSITION:
            trans = backend.wait_transition_neutral(br)
            bursts.append({"status": "PREMOVE_TRANSITION", "transition": trans})
            if trans["resolved"] == "BATTLE_ACTIVE":
                return ({
                    "encounter": True,
                    "bursts": bursts,
                    "global_burst": burst_no,
                    "movement_mode": "cave_run",
                    "axis": axis_key,
                    "anchor_grid": list(anchor_grid),
                    "safe_return_grids": [],
                    "corridor_proven": False,
                    "wall_bounded": True,
                }, previous_direction, burst_no)
            if trans["resolved"] != "FIELD_INACTIVE":
                raise backend.IntegrationHold("pre-Cave-Run transition unresolved")

        before = backend.read_position(br)
        before_grid = require_cave_position(
            backend, before, expected_zone, "Cave Run before"
        )

        # Alternate after a successful pulse.  On the initial pulse use the
        # first direction for the selected axis.  If that side is a wall, the
        # opposite direction gets one immediate attempt.
        if previous_direction in dirs:
            first_direction = backend.terrain.OPPOSITE[previous_direction]
        else:
            first_direction = dirs[0]
        candidates = (
            first_direction,
            backend.terrain.OPPOSITE[first_direction],
        )

        moved = False
        for candidate_index, direction in enumerate(candidates):
            burst_no += 1
            raw_hid = backend.HID_B_DIR[direction]
            controller = backend.run_movement_pulse(br, raw_hid, 600, 140)
            br.release_all()
            time.sleep(0.20)

            in_battle, transition = _battle_after_pulse(br, backend)
            rec = {
                "burst": burst_no,
                "phase": "CAVE_RUN_WALL_BOUNDED",
                "direction": direction,
                "raw_hid": backend.hx(raw_hid),
                "controller": controller,
                "before_grid": list(before_grid),
                "anchor_grid": list(anchor_grid),
                "axis": axis_key,
                "timing_profile": "CAVE_RUN_BDIR_600ms_NOMINAL5_MAX6_WALL_BOUNDED",
                "edge_start_allowed": True,
                "reverse_after_block": bool(candidate_index),
            }
            if transition is not None:
                rec["transition"] = transition

            if in_battle:
                rec["status"] = "ENCOUNTER_BOUNDARY"
                bursts.append(rec)
                return ({
                    "encounter": True,
                    "bursts": bursts,
                    "global_burst": burst_no,
                    "movement_mode": "cave_run",
                    "axis": axis_key,
                    "anchor_grid": list(anchor_grid),
                    # No artificial corridor: after battle any stable grid in
                    # the same Cave zone is a valid return point.
                    "safe_return_grids": [],
                    "corridor_proven": False,
                    "wall_bounded": True,
                    "encounter_direction": direction,
                }, direction, burst_no)

            after = backend.read_position(br)
            after_grid = require_cave_position(
                backend, after, expected_zone, "Cave Run after"
            )
            rec["after_grid"] = list(after_grid)

            if after_grid == before_grid:
                rec["status"] = "BLOCKED_BY_CAVE_COLLISION"
                bursts.append(rec)
                # First blocked side is normal at an edge. Try the opposite
                # side once. If both sides are blocked there is no movement
                # possible on this selected axis.
                continue

            axis_move = after_grid[axis_idx] - before_grid[axis_idx]
            perp_move = after_grid[perp_idx] - before_grid[perp_idx]
            step = abs(axis_move)
            rec["delta_grid"] = [
                after_grid[0] - before_grid[0],
                after_grid[1] - before_grid[1],
            ]
            rec["step_tiles"] = step

            if perp_move != 0:
                rec["status"] = "HOLD_PERPENDICULAR_MOVEMENT"
                bursts.append(rec)
                raise backend.IntegrationHold(
                    "Cave Run moved perpendicular to the selected axis"
                )
            if step < 1 or step > 6:
                rec["status"] = "HOLD_RUN_STEP_OUTSIDE_W6_RANGE"
                bursts.append(rec)
                raise backend.IntegrationHold(
                    "Cave Run 600 ms movement was outside the validated Wild "
                    f"1-6 tile range: {list(before_grid)} -> {list(after_grid)}"
                )

            dx, dz = _delta(direction)
            commanded_sign = dx if axis_idx == 0 else dz
            observed_sign = 1 if axis_move > 0 else -1
            if observed_sign != (1 if commanded_sign > 0 else -1):
                rec["status"] = "HOLD_WRONG_DIRECTION"
                bursts.append(rec)
                raise backend.IntegrationHold(
                    "Cave Run moved opposite the commanded direction"
                )

            rec["status"] = "PASS_CAVE_RUN_WALL_BOUNDED"
            bursts.append(rec)
            previous_direction = direction
            moved = True
            break

        if not moved:
            raise backend.IntegrationHold(
                f"Cave Run cannot move on the selected {axis_key} axis from "
                f"{list(before_grid)}; both directions are blocked by cave collision. "
                "Change axis or move one tile manually."
            )


def cave_bunny_until_encounter(
    br,
    backend,
    *,
    expected_zone: int,
    anchor_grid,
    global_burst_start: int,
):
    """Stationary Acro Bunny adapted to cave zone/grid authority.

    The proven command-10 continuous B latch is reused.  Unlike grass Acro,
    this path never requires core1/core2 terrain.  It requires the live cave
    zone to remain unchanged and the player grid to remain exactly at the
    start anchor whenever position is sampled.
    """
    anchor_grid = tuple(int(v) for v in anchor_grid)
    cycle_no = int(global_burst_start) + 1

    battle_before = br.u32(backend.core.BATTLE_ADDR)
    if battle_before == backend.core.BATTLE_ACTIVE:
        rel = br.release_all()
        return ({
            "encounter": True,
            "movement_mode": "cave_bunny",
            "mode": "CAVE_ACRO_BUNNY_LATCH",
            "bursts": [],
            "global_burst": int(global_burst_start),
            "anchor_grid": list(anchor_grid),
            "safe_return_grids": [list(anchor_grid)],
            "reason": "battle already active",
            "release_all": rel,
        }, int(global_burst_start))

    if battle_before == backend.core.BATTLE_TRANSITION:
        rel = br.release_all()
        trans = backend.wait_transition_neutral(br)
        if trans["resolved"] == "BATTLE_ACTIVE":
            return ({
                "encounter": True,
                "movement_mode": "cave_bunny",
                "mode": "CAVE_ACRO_BUNNY_LATCH",
                "bursts": [{
                    "burst": cycle_no,
                    "status": "PRE_LATCH_TRANSITION",
                    "transition": trans,
                    "release_all": rel,
                }],
                "global_burst": cycle_no,
                "anchor_grid": list(anchor_grid),
                "safe_return_grids": [list(anchor_grid)],
            }, cycle_no)
        if trans["resolved"] != "FIELD_INACTIVE":
            raise backend.IntegrationHold(
                "pre-Cave-Acro transition unresolved"
            )

    before = backend.read_position(br)
    before_grid = require_cave_position(
        backend, before, expected_zone, "Cave Acro before"
    )
    if before_grid != anchor_grid:
        raise backend.IntegrationHold(
            f"Cave Acro anchor changed before latch: expected "
            f"{list(anchor_grid)}, got {list(before_grid)}"
        )

    latch = br.hid_latch_no_retransmit(backend.HID_B)
    if not latch.get("active"):
        raise backend.IntegrationHold(
            f"Cave Acro B latch was not proven active: {latch}"
        )

    started_mono = time.monotonic()
    last_position_check = started_mono
    checks = []
    rec = {
        "burst": cycle_no,
        "mode": "cave_acro_bunny_latch",
        "raw_hid": backend.hx(backend.HID_B),
        "command": backend.core.CMD_INPUT_HID_LATCH,
        "continuous_hold": True,
        "controller": latch,
        "before": before,
        "anchor_grid": list(anchor_grid),
        "battle_poll_seconds": backend.BUNNY_BATTLE_POLL_SEC,
        "position_check_seconds": backend.BUNNY_POSITION_CHECK_SEC,
        "max_hold_seconds": backend.BUNNY_MAX_HOLD_SEC,
        "position_checks": checks,
    }

    while True:
        battle = br.u32(backend.core.BATTLE_ADDR)
        elapsed = time.monotonic() - started_mono

        if battle == backend.core.BATTLE_ACTIVE:
            rel = br.release_all()
            rec["battle"] = backend.hx(battle)
            rec["held_seconds"] = elapsed
            rec["release_all"] = rel
            rec["status"] = "ENCOUNTER_BOUNDARY_RELEASED"
            return ({
                "encounter": True,
                "movement_mode": "cave_bunny",
                "mode": "CAVE_ACRO_BUNNY_LATCH",
                "bursts": [rec],
                "global_burst": cycle_no,
                "anchor_grid": list(anchor_grid),
                "safe_return_grids": [list(anchor_grid)],
            }, cycle_no)

        if battle == backend.core.BATTLE_TRANSITION:
            trans = backend.wait_transition_while_latched(br)
            rec.setdefault("latched_transitions", []).append({
                "held_seconds": elapsed,
                "transition": trans,
            })
            if trans["resolved"] == "BATTLE_ACTIVE":
                rel = br.release_all()
                rec["held_seconds"] = elapsed
                rec["release_all"] = rel
                rec["transition"] = trans
                rec["status"] = "ENCOUNTER_TRANSITION_RELEASED"
                return ({
                    "encounter": True,
                    "movement_mode": "cave_bunny",
                    "mode": "CAVE_ACRO_BUNNY_LATCH",
                    "bursts": [rec],
                    "global_burst": cycle_no,
                    "anchor_grid": list(anchor_grid),
                    "safe_return_grids": [list(anchor_grid)],
                }, cycle_no)
            if trans["resolved"] == "FIELD_INACTIVE":
                continue
            br.release_all()
            raise backend.IntegrationHold(
                "Cave Acro latched transition did not resolve"
            )

        if battle != backend.core.BATTLE_INACTIVE:
            br.release_all()
            raise backend.IntegrationHold(
                f"Cave Acro unexpected battle state {backend.hx(battle)}"
            )

        now_mono = time.monotonic()
        if now_mono - last_position_check >= backend.BUNNY_POSITION_CHECK_SEC:
            pos = backend.read_position(br)
            grid = require_cave_position(
                backend, pos, expected_zone, "Cave Acro latch held"
            )
            checks.append({
                "elapsed_seconds": now_mono - started_mono,
                "grid": list(grid),
                "same_anchor": grid == anchor_grid,
            })
            last_position_check = now_mono
            if grid != anchor_grid:
                br.release_all()
                rec["status"] = "HOLD_TILE_DRIFT"
                raise backend.IntegrationHold(
                    f"Cave Acro moved from anchor {list(anchor_grid)} "
                    f"to {list(grid)}"
                )

        if elapsed >= backend.BUNNY_MAX_HOLD_SEC:
            rel = br.release_all()
            rec["held_seconds"] = elapsed
            rec["release_all"] = rel
            rec["status"] = "HOLD_NO_ENCOUNTER_TIMEOUT"
            raise backend.IntegrationHold(
                f"Cave Acro B held for "
                f"{backend.BUNNY_MAX_HOLD_SEC:.0f}s without an encounter"
            )

        time.sleep(backend.BUNNY_BATTLE_POLL_SEC)

