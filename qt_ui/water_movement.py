from __future__ import annotations

"""ORAS Surf / Ocean first hardware-test movement adapter.

This module intentionally does NOT guess water collision/permission semantics
from the currently grass-focused runtime world DB.

Manual precondition for v0p40a:
    The player must already be Surfing in open encounter water before Start.

Movement:
    direction-only 160 ms
    140 ms controller settle
    700 ms field settle
    exact one-tile RAM endpoint

The bot acquires one adjacent tile on the selected axis, then reverses every
successful pulse so it remains on the same two coordinates:

    A <-> B <-> A <-> B

Safety:
- live ORAS zone is locked;
- duplicate coordinate copies must agree;
- player must be tile-centred;
- non-battle input must move exactly one tile in the commanded direction;
- initial blocked side may try the opposite side once;
- once a two-tile corridor is acquired, a blocked return HOLDs;
- after battle escape, field must recover to one of the encounter pulse's
  two possible grids;
- a 120-second no-encounter timeout HOLDs with a reminder to verify the player
  is actually Surfing in encounter water;
- no B/run input is used.
"""

import time


SURF_NO_ENCOUNTER_TIMEOUT = 120.0
SURF_SWEEP_HOLD_MS = 600
SURF_SWEEP_SETTLE_MS = 160
SURF_SWEEP_POST_SEC = 0.45
SURF_SWEEP_MAX_TILES = 6
SURF_ANCHOR_ENVELOPE_TILES = 6


def allowed_directions(axis_key: str):
    if axis_key == "vertical":
        return ("UP", "DOWN")
    if axis_key == "horizontal":
        return ("LEFT", "RIGHT")
    raise ValueError(f"Unknown Surf movement axis: {axis_key}")


def _delta(direction: str):
    return {
        "RIGHT": (1, 0),
        "LEFT": (-1, 0),
        "DOWN": (0, 1),
        "UP": (0, -1),
    }[direction]


def require_water_position(backend, pos: dict, expected_zone: int, label: str):
    """Return the logical Surf grid without requiring exact tile-centre settle.

    Surfing has a mounted/idle animation. Hardware evidence from v0p40a showed
    three independent starts (both axes) consistently failing the land-style
    `settled_tile_center` test before any input was sent.

    For Surf, authority is therefore:
      * exact live zone;
      * duplicate world-coordinate copies agree;
      * a valid logical nearest-grid coordinate exists.

    Exact centre settle remains intentionally *diagnostic only* here. Stable
    occupancy is established separately by observing the same logical grid in
    repeated RAM samples.
    """
    if int(pos.get("zone_id", -1)) != int(expected_zone):
        raise backend.IntegrationHold(
            f"{label}: live zone changed from {expected_zone} to {pos.get('zone_id')}"
        )
    if not pos.get("duplicates_match"):
        raise backend.IntegrationHold(
            f"{label}: duplicate world coordinates disagree"
        )
    grid = pos.get("grid")
    if not isinstance(grid, (list, tuple)) or len(grid) != 2:
        raise backend.IntegrationHold(
            f"{label}: logical Surf grid is unavailable"
        )
    return tuple(int(v) for v in grid)


def read_stable_water_position(
    br,
    backend,
    expected_zone: int,
    label: str,
    *,
    timeout: float = 3.00,
    required_samples: int = 2,
    poll_sec: float = 0.10,
):
    """Require repeated agreement on one logical Surf grid.

    This is the Surf equivalent of land tile-centre settle. It tolerates the
    mounted animation/offset but will not authorize movement while the logical
    tile is changing.
    """
    deadline = time.monotonic() + float(timeout)
    last_grid = None
    consecutive = 0
    samples = []

    while time.monotonic() < deadline:
        pos = backend.read_position(br)
        try:
            grid = require_water_position(
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
            "valid": True,
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

    recent = []
    for sample in samples[-10:]:
        recent.append({
            "valid": sample.get("valid"),
            "grid": sample.get("grid"),
            "duplicates_match": sample.get("duplicates_match"),
            "settled_tile_center": sample.get("settled_tile_center"),
            "consecutive": sample.get("consecutive"),
        })
    raise backend.IntegrationHold(
        f"{label}: Surf logical grid did not become stable for "
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
            raise backend.IntegrationHold(
                "Surf movement transition did not resolve"
            )
        return False, trans
    if battle == backend.core.BATTLE_ACTIVE:
        return True, None
    if battle != backend.core.BATTLE_INACTIVE:
        raise backend.IntegrationHold(
            f"Surf movement unexpected battle state {backend.hx(battle)}"
        )
    return False, None


def surf_until_encounter(
    br,
    backend,
    *,
    axis_key: str,
    expected_zone: int,
    anchor_grid,
    previous_direction: str | None,
    global_burst_start: int,
):
    """Fast Surf/Ocean sweep using long direction-only RAM-bounded bursts.

    Route 104 v0p40b hardware evidence:
      - 160 ms LEFT moved one logical tile.
      - the immediate 160 ms RIGHT return moved zero logical tiles.

    Surf therefore uses the same bounded-long-burst philosophy as the proven
    Grass Run path, but WITHOUT B:
      - direction only, 600 ms;
      - completed non-battle endpoint may be 1..6 logical tiles;
      - selected axis only;
      - movement must be in the commanded direction;
      - fixed start anchor with +/-6 tile RAM envelope;
      - whenever off-anchor, next sweep points toward the anchor.

    Start in open encounter water, well away from shore transitions.
    """
    dirs = allowed_directions(axis_key)
    anchor_grid = tuple(int(v) for v in anchor_grid)
    bursts = []
    burst_no = int(global_burst_start)
    started = time.monotonic()

    if axis_key == "horizontal":
        axis_idx, perp_idx = 0, 1
    elif axis_key == "vertical":
        axis_idx, perp_idx = 1, 0
    else:
        raise backend.IntegrationHold(
            f"unsupported Surf movement axis {axis_key!r}"
        )

    def axis_delta(direction):
        dx, dz = _delta(direction)
        return dx if axis_idx == 0 else dz

    positive_dir = next(d for d in dirs if axis_delta(d) > 0)
    negative_dir = next(d for d in dirs if axis_delta(d) < 0)

    def offset_of(grid, label):
        if grid[perp_idx] != anchor_grid[perp_idx]:
            raise backend.IntegrationHold(
                f"{label}: Surf moved perpendicular to selected {axis_key} axis: "
                f"anchor {list(anchor_grid)}, current {list(grid)}"
            )
        offset = grid[axis_idx] - anchor_grid[axis_idx]
        if abs(offset) > SURF_ANCHOR_ENVELOPE_TILES:
            raise backend.IntegrationHold(
                f"{label}: Surf left the +/-{SURF_ANCHOR_ENVELOPE_TILES} tile "
                f"anchor envelope: anchor {list(anchor_grid)}, current {list(grid)}"
            )
        return offset

    def possible_boundary_grids(before_grid, direction):
        dx, dz = _delta(direction)
        return [
            [before_grid[0] + dx * step, before_grid[1] + dz * step]
            for step in range(0, SURF_SWEEP_MAX_TILES + 1)
        ]

    while True:
        if time.monotonic() - started >= SURF_NO_ENCOUNTER_TIMEOUT:
            raise backend.IntegrationHold(
                "Fast Surf/Ocean produced no encounter for 120 seconds. "
                "Verify the player is still Surfing in encounter water and "
                "start farther from shoreline/land transitions."
            )

        battle_before = br.u32(backend.core.BATTLE_ADDR)
        if battle_before == backend.core.BATTLE_ACTIVE:
            stable = read_stable_water_position(
                br, backend, expected_zone, "Surf active-boundary"
            )
            grid = stable["grid"]
            offset_of(grid, "Surf active-boundary")
            return ({
                "encounter": True,
                "bursts": bursts,
                "global_burst": burst_no,
                "movement_mode": "surf_fast_sweep",
                "axis": axis_key,
                "anchor_grid": list(anchor_grid),
                "safe_return_grids": [list(grid)],
                "reason": "battle already active",
            }, previous_direction, burst_no)

        if battle_before == backend.core.BATTLE_TRANSITION:
            trans = backend.wait_transition_neutral(br)
            bursts.append({"status": "PREMOVE_TRANSITION", "transition": trans})
            if trans["resolved"] == "BATTLE_ACTIVE":
                stable = read_stable_water_position(
                    br, backend, expected_zone, "Surf transition-boundary"
                )
                grid = stable["grid"]
                offset_of(grid, "Surf transition-boundary")
                return ({
                    "encounter": True,
                    "bursts": bursts,
                    "global_burst": burst_no,
                    "movement_mode": "surf_fast_sweep",
                    "axis": axis_key,
                    "anchor_grid": list(anchor_grid),
                    "safe_return_grids": [list(grid)],
                }, previous_direction, burst_no)
            if trans["resolved"] != "FIELD_INACTIVE":
                raise backend.IntegrationHold(
                    "pre-Surf movement transition unresolved"
                )

        before_stable = read_stable_water_position(
            br, backend, expected_zone, "Surf movement before"
        )
        before_grid = before_stable["grid"]
        before_offset = offset_of(before_grid, "Surf movement before")

        if before_offset > 0:
            candidates = (negative_dir,)
        elif before_offset < 0:
            candidates = (positive_dir,)
        elif previous_direction in dirs:
            candidates = (backend.terrain.OPPOSITE[previous_direction],)
        else:
            candidates = dirs

        moved = False
        for direction in candidates:
            burst_no += 1
            raw_hid = backend.HID_DIR[direction]
            safe_boundary = possible_boundary_grids(before_grid, direction)

            controller = backend.run_movement_pulse(
                br,
                raw_hid,
                SURF_SWEEP_HOLD_MS,
                SURF_SWEEP_SETTLE_MS,
            )
            br.release_all()
            time.sleep(SURF_SWEEP_POST_SEC)

            in_battle, transition = _battle_after_pulse(br, backend)
            rec = {
                "burst": burst_no,
                "direction": direction,
                "raw_hid": backend.hx(raw_hid),
                "controller": controller,
                "before_grid": list(before_grid),
                "before_offset": before_offset,
                "anchor_grid": list(anchor_grid),
                "axis": axis_key,
                "timing_profile": "SURF_FAST_SWEEP_DIR_600ms_MAX6",
                "hold_ms": SURF_SWEEP_HOLD_MS,
                "max_tiles": SURF_SWEEP_MAX_TILES,
                "anchor_envelope_tiles": SURF_ANCHOR_ENVELOPE_TILES,
                "manual_precondition": "ALREADY_SURFING_OPEN_ENCOUNTER_WATER",
                "before_stability_samples": before_stable.get("samples", []),
            }
            if transition is not None:
                rec["transition"] = transition

            if in_battle:
                rec["status"] = "ENCOUNTER_BOUNDARY"
                rec["safe_return_grids"] = safe_boundary
                bursts.append(rec)
                return ({
                    "encounter": True,
                    "bursts": bursts,
                    "global_burst": burst_no,
                    "movement_mode": "surf_fast_sweep",
                    "axis": axis_key,
                    "anchor_grid": list(anchor_grid),
                    "safe_return_grids": safe_boundary,
                    "encounter_direction": direction,
                }, direction, burst_no)

            after_stable = read_stable_water_position(
                br, backend, expected_zone, "Surf movement after"
            )
            after_grid = after_stable["grid"]
            rec["after_grid"] = list(after_grid)
            rec["after_stability_samples"] = after_stable.get("samples", [])

            dx_obs = after_grid[0] - before_grid[0]
            dz_obs = after_grid[1] - before_grid[1]
            axis_move = dx_obs if axis_idx == 0 else dz_obs
            perp_move = dz_obs if axis_idx == 0 else dx_obs
            step = abs(axis_move)
            rec["delta_grid"] = [dx_obs, dz_obs]
            rec["step_tiles"] = step

            if after_grid == before_grid:
                rec["status"] = "BLOCKED_NO_MOVEMENT"
                bursts.append(rec)
                if before_offset != 0:
                    raise backend.IntegrationHold(
                        f"Fast Surf return toward anchor became blocked at "
                        f"{list(before_grid)}. No further movement authorized."
                    )
                previous_direction = direction
                continue

            if perp_move != 0:
                rec["status"] = "HOLD_PERPENDICULAR_MOVEMENT"
                bursts.append(rec)
                raise backend.IntegrationHold(
                    "Fast Surf moved perpendicular to selected axis: "
                    f"{list(before_grid)} -> {list(after_grid)}"
                )

            if step < 1 or step > SURF_SWEEP_MAX_TILES:
                rec["status"] = "HOLD_SWEEP_DISTANCE"
                bursts.append(rec)
                raise backend.IntegrationHold(
                    "Fast Surf sweep moved outside the 1-6 tile hardware-test "
                    f"range: {list(before_grid)} -> {list(after_grid)} "
                    f"({step} tiles)"
                )

            expected_sign = 1 if axis_delta(direction) > 0 else -1
            observed_sign = 1 if axis_move > 0 else -1
            if expected_sign != observed_sign:
                rec["status"] = "HOLD_WRONG_DIRECTION"
                bursts.append(rec)
                raise backend.IntegrationHold(
                    "Fast Surf moved opposite the commanded direction"
                )

            after_offset = offset_of(after_grid, "Surf movement after")
            rec["after_offset"] = after_offset
            rec["status"] = "PASS_SURF_FAST_SWEEP"
            bursts.append(rec)

            previous_direction = direction
            moved = True
            break

        if not moved:
            raise backend.IntegrationHold(
                f"Fast Surf could not move on either {axis_key} direction from "
                f"{list(before_grid)}. Start farther from shoreline/obstacles "
                "or choose the other axis."
            )



def wait_for_surf_field(
    br,
    backend,
    expected_zone: int,
    allowed_grids,
    timeout=6.0,
):
    """Require repeated post-Run logical-grid authority.

    Exact tile-centre settle is not required while mounted on Surf.
    """
    allowed = {
        tuple(int(v) for v in g)
        for g in (allowed_grids or [])
    }
    deadline = time.monotonic() + float(timeout)
    samples = []
    consecutive = 0
    last_grid = None
    final_position = None

    while time.monotonic() < deadline:
        battle = br.u32(backend.core.BATTLE_ADDR)
        pos = backend.read_position(br)
        final_position = pos
        try:
            grid = require_water_position(
                backend, pos, expected_zone, "Surf post-escape sample"
            )
            position_ok = not allowed or grid in allowed
        except Exception as exc:
            grid = tuple(pos.get("grid") or ())
            position_ok = False
            err = str(exc)
        else:
            err = None

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
            "duplicates_match": pos.get("duplicates_match"),
            "settled_tile_center": pos.get("settled_tile_center"),
            "world_a": pos.get("world_a"),
            "world_b": pos.get("world_b"),
            "error": err,
            "consecutive": consecutive,
        })
        if consecutive >= 2:
            return {
                "ready": True,
                "final_position": final_position,
                "samples": samples,
            }
        time.sleep(0.10)

    return {
        "ready": False,
        "status": "SURF_FIELD_TIMEOUT",
        "samples": samples,
    }
