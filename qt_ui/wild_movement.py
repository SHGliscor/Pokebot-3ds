from __future__ import annotations

"""
Axis-constrained ORAS Wild movement adapter.

This module does NOT modify the frozen v0p23 W6 source. It selects only plans
that fit the user's chosen axis, then delegates actual gameplay pulses and
endpoint validation to the proven backend functions:

Walk:
  backend.do_reposition()
  - direction only
  - 160 ms hold / 140 ms settle
  - exactly one safe tile required

Run:
  backend.do_fluid()
  - B+direction
  - frozen 360 ms nominal3/max4 or 600 ms nominal5/max6 policy
  - one-tile backend.do_reposition() is used only to reach a valid launch cell

Unknown/off-mask endpoints remain HOLD authority.
"""


def allowed_directions(axis_key):
    if axis_key in ("vertical", "up", "down"):
        return ("UP", "DOWN")
    if axis_key in ("horizontal", "left", "right"):
        return ("LEFT", "RIGHT")
    raise ValueError(f"Unknown movement axis/direction: {axis_key}")


def requested_initial_direction(axis_key):
    return {
        "up": "UP",
        "down": "DOWN",
        "left": "LEFT",
        "right": "RIGHT",
    }.get(axis_key)


def _directions_for_this_choice(axis_key, previous_direction):
    requested = requested_initial_direction(axis_key)
    if previous_direction is None and requested is not None:
        # The selected direction is only the preferred opening direction.
        # At an edge-grass start it may have no legal grass destination even
        # though the opposite direction does.  Keep the preference first, but
        # permit the other direction in the selected axis before holding.
        return (requested,) + tuple(
            direction
            for direction in allowed_directions(axis_key)
            if direction != requested
        )
    return allowed_directions(axis_key)


def choose_walk_step(terrain, grid, previous_direction, axis_key):
    allowed = _directions_for_this_choice(axis_key, previous_direction)
    candidates = []
    for direction in allowed:
        target = terrain.neighbor(grid, direction)
        if not terrain.in_core1(target):
            continue
        continuing = previous_direction == direction
        reversing = (
            previous_direction is not None
            and direction == terrain.OPPOSITE.get(previous_direction)
        )
        score = (
            1 if continuing else 0,
            1 if terrain.in_core2(target) else 0,
            terrain.degree(target),
            0 if reversing else 1,
        )
        candidates.append((score, direction, target))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0], reverse=True)
    _, direction, target = candidates[0]
    return {
        "mode": "reposition",
        "direction": direction,
        "target_grid": [target[0], target[1]],
        "launch_grid": [target[0], target[1]],
        "hold_ms": 160,
        "settle_ms": 140,
        "post_settle_s": 0.70,
        "timing_profile": "W3C_1tile_160ms_PROVEN",
        "same_component_required": True,
        "target_core2": terrain.in_core2(target),
        "target_degree": terrain.degree(target),
        "axis": axis_key,
        "walk_only": True,
    }


def choose_run_fluid_plan(terrain, grid, previous_direction, axis_key):
    allowed = set(_directions_for_this_choice(axis_key, previous_direction))
    corridor_meta = {
        direction: terrain.corridor_metadata(grid, direction)
        for direction in terrain.DIRECTION_DELTAS
    }
    cs = {direction: meta["cells"] for direction, meta in corridor_meta.items()}
    candidates = []

    for direction, cells in cs.items():
        if direction not in allowed:
            continue
        n = len(cells)
        if n >= 7:
            profile = {
                "nominal_tiles": 5,
                "max_observed_tiles": 6,
                "hold_ms": 600,
                "timing_profile": "600ms_nominal5_max6_OBSERVED",
            }
        elif n >= 5:
            profile = {
                "nominal_tiles": 3,
                "max_observed_tiles": 4,
                "hold_ms": 360,
                "timing_profile": "360ms_nominal3_max4_OBSERVED",
            }
        elif n >= 2:
            # The shortest proven B+direction pulse can cover two grid tiles
            # on hardware.  A one-cell corridor is therefore not a safe Run
            # launch: even the shortest pulse can cross its grass boundary.
            # Require two confirmed destination cells.  Collision data is
            # exposed as boundary metadata, but it does not authorize a
            # one-cell Run: hardware has already been observed crossing one
            # cell into a non-grass boundary during the shortest pulse.
            short_tiles = min(n, 2)
            profile = {
                "nominal_tiles": short_tiles,
                "max_observed_tiles": short_tiles,
                "hold_ms": 160,
                "boundary_reserve_required": False,
                "timing_profile": (
                    f"160ms_nominal{short_tiles}_max{short_tiles}_"
                    "SHORT_GRASS_STRIP"
                ),
            }
        else:
            continue

        reverse = (
            previous_direction is not None
            and direction == terrain.OPPOSITE.get(previous_direction)
        )
        core2_window = sum(
            1 for c in cells[:profile["max_observed_tiles"]] if c["core2"]
        )
        candidate = {
            "direction": direction,
            "corridor": cells,
            "corridor_length": n,
            **profile,
            "boundary_reserve_tiles": max(0, n - 1),
            "terminal_boundary": (
                corridor_meta[direction]["terminal_boundary"]
            ),
            "terminal_boundary_is_grass": corridor_meta[direction][
                "terminal_boundary_is_grass"
            ],
            "patch_id": corridor_meta[direction]["patch_id"],
            "patch_size": corridor_meta[direction]["patch_size"],
            "patch_truncated": corridor_meta[direction]["patch_truncated"],
            "boundary_edges_derived": True,
            "boundary_policy": "endpoint_must_remain_grass",
            "wall_bounded": bool(corridor_meta[direction]["terminal_hard_boundary"]),
            "wall_bounded_run_authorized": False,
            "requested_direction_fallback": (
                direction != requested_initial_direction(axis_key)
                and previous_direction is None
            ),
            "non_reverse_preferred": not reverse,
            "core2_tiles_in_max_window": core2_window,
            "mode": "fluid",
            "axis": axis_key,
        }
        score = (
            profile["nominal_tiles"],
            n,
            0 if reverse else 1,
            core2_window,
        )
        candidates.append((score, candidate))

    if not candidates:
        return None, cs
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1], cs


def choose_run_reposition(terrain, grid, previous_direction, axis_key):
    candidates = []
    for direction in _directions_for_this_choice(axis_key, previous_direction):
        target = terrain.neighbor(grid, direction)
        if not terrain.in_core1(target):
            continue

        plan_after, _ = choose_run_fluid_plan(
            terrain, target, direction, axis_key
        )
        axis_corridors = [
            len(terrain.corridor(target, d))
            for d in allowed_directions(axis_key)
        ]
        score = (
            1 if plan_after is not None else 0,
            max(axis_corridors, default=0),
            1 if terrain.in_core2(target) else 0,
            terrain.degree(target),
            0 if (
                previous_direction is not None
                and direction == terrain.OPPOSITE.get(previous_direction)
            ) else 1,
        )
        candidates.append((score, direction, target, plan_after))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    _, direction, target, launch_plan = candidates[0]
    return {
        "mode": "reposition",
        "direction": direction,
        "target_grid": [target[0], target[1]],
        "launch_grid": [target[0], target[1]],
        "hold_ms": 160,
        "settle_ms": 140,
        "post_settle_s": 0.70,
        "timing_profile": "W3C_1tile_160ms_PROVEN",
        "same_component_required": True,
        "target_core2": terrain.in_core2(target),
        "target_degree": terrain.degree(target),
        "next_fluid_available": launch_plan is not None,
        "axis": axis_key,
    }


def movement_until_encounter_axis(
    br,
    backend,
    *,
    movement_mode,
    axis_key,
    previous_direction,
    global_burst_start,
):
    terrain = backend.terrain
    bursts = []
    burst_no = global_burst_start

    while True:
        burst_no += 1

        battle_before = br.u32(backend.core.BATTLE_ADDR)
        if battle_before == backend.core.BATTLE_ACTIVE:
            return {
                "encounter": True,
                "bursts": bursts,
                "global_burst": burst_no - 1,
                "reason": "battle already active",
                "movement_mode": movement_mode,
                "axis": axis_key,
                "requested_initial_direction": requested_initial_direction(axis_key),
            }, previous_direction, burst_no - 1

        if battle_before == backend.core.BATTLE_TRANSITION:
            trans = backend.wait_transition_neutral(br)
            bursts.append({
                "burst": burst_no,
                "status": "PREMOVE_TRANSITION",
                "transition": trans,
                "axis": axis_key,
            })
            if trans["resolved"] == "BATTLE_ACTIVE":
                return {
                    "encounter": True,
                    "bursts": bursts,
                    "global_burst": burst_no,
                    "reason": "transition resolved active",
                    "movement_mode": movement_mode,
                    "axis": axis_key,
                    "requested_initial_direction": requested_initial_direction(axis_key),
                }, previous_direction, burst_no
            if trans["resolved"] != "FIELD_INACTIVE":
                raise backend.IntegrationHold("pre-movement transition unresolved")

        before = backend.read_position(br)
        grid = backend.require_safe_position(before, "movement before")

        if movement_mode == "walk":
            plan = choose_walk_step(
                terrain, grid, previous_direction, axis_key
            )
            if plan is None:
                raise backend.IntegrationHold(
                    f"no safe {axis_key} one-tile Walk step from {list(grid)}"
                )
            rec = backend.do_reposition(br, before, plan)
            corridor_counts = {
                d: len(v) for d, v in terrain.corridors(grid).items()
            }
        elif movement_mode == "run":
            plan, cs = choose_run_fluid_plan(
                terrain, grid, previous_direction, axis_key
            )
            corridor_counts = {d: len(v) for d, v in cs.items()}
            if plan is None:
                # Run is a strict input mode.  A one-tile reposition uses the
                # plain directional HID pulse and is therefore walking, even
                # when the user selected Run.  Never silently downgrade Run to
                # Walk at narrow grass boundaries; hold and report the exact
                # corridor state instead.
                raise backend.IntegrationHold(
                    f"no safe {axis_key} Run corridor from {list(grid)}; "
                    "walking fallback is disabled"
                )
            rec = backend.do_fluid(br, before, plan)
        else:
            raise ValueError(f"Unknown movement mode: {movement_mode}")

        rec["burst"] = burst_no
        rec["before"] = before
        rec["corridors"] = corridor_counts
        rec["selected_axis"] = axis_key
        rec["requested_initial_direction"] = requested_initial_direction(axis_key)
        rec["selected_movement_mode"] = movement_mode
        bursts.append(rec)
        previous_direction = plan["direction"]

        if rec["status"] == "ENCOUNTER_BOUNDARY":
            return {
                "encounter": True,
                "bursts": bursts,
                "global_burst": burst_no,
                "movement_mode": movement_mode,
                "axis": axis_key,
                "requested_initial_direction": requested_initial_direction(axis_key),
            }, previous_direction, burst_no
