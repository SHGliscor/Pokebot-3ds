from __future__ import annotations

# Pokebot3DS-CFW
# Conservative Route 101 W2/W6 hardware-evidenced component slice.
#
# This exact local geometry reproduces the corridor lengths observed in the
# earlier W6 hardware results around the proven Route 101 hunting patch.
# Unknown cells are deliberately treated as unsafe.

ROUTE101_ZONE = 23
MATRIX_ID = 1
TERRAIN_CLASS = "TALL_GRASS_ORAS_PRIMARY"
COMPONENT_ID = 1

DIRECTION_DELTAS = {
    "RIGHT": (1, 0),
    "DOWN": (0, 1),
    "LEFT": (-1, 0),
    "UP": (0, -1),
}

OPPOSITE = {
    "RIGHT": "LEFT",
    "LEFT": "RIGHT",
    "UP": "DOWN",
    "DOWN": "UP",
}

# Reconstructed conservatively from W3/W4/W6 hardware logs.
# Only cells directly supported by the proven component/corridor evidence are
# included. This is intentionally not a guessed whole-route map.
CORE1 = {
    # upper stem
    (92, 146),
    (91, 147), (92, 147), (93, 147),

    # broad upper rows
    (89, 148), (90, 148), (91, 148), (92, 148), (93, 148),
    (89, 149), (90, 149), (91, 149), (92, 149), (93, 149),
    (89, 150), (90, 150), (91, 150), (92, 150), (93, 150),

    # right-shifted lower rows
    (91, 151), (92, 151), (93, 151), (94, 151),
    (92, 152), (93, 152), (94, 152),
    (92, 153), (93, 153), (94, 153),
    (92, 154), (93, 154),
}


def in_core1(grid: tuple[int, int]) -> bool:
    return tuple(grid) in CORE1


def neighbor(grid: tuple[int, int], direction: str) -> tuple[int, int]:
    dx, dz = DIRECTION_DELTAS[direction]
    return grid[0] + dx, grid[1] + dz


def degree(grid: tuple[int, int]) -> int:
    if not in_core1(grid):
        return 0
    return sum(in_core1(neighbor(grid, d)) for d in DIRECTION_DELTAS)


def in_core2(grid: tuple[int, int]) -> bool:
    # W2 core2 behavior in this patch matches one-tile erosion of core1:
    # all four orthogonal neighbors must remain inside the same component.
    return in_core1(grid) and degree(grid) == 4


def mask_record(grid: tuple[int, int]) -> dict:
    return {
        "matrix_id": MATRIX_ID,
        "terrain_class": TERRAIN_CLASS,
        "component_id": COMPONENT_ID,
        "raw": in_core1(grid),
        "core1": in_core1(grid),
        "core2": in_core2(grid),
    }


def corridor(grid: tuple[int, int], direction: str, limit: int = 16) -> list[dict]:
    out = []
    cur = tuple(grid)
    for _ in range(limit):
        cur = neighbor(cur, direction)
        if not in_core1(cur):
            break
        out.append({
            "grid": [cur[0], cur[1]],
            "core2": in_core2(cur),
        })
    return out


def corridors(grid: tuple[int, int]) -> dict[str, list[dict]]:
    return {d: corridor(grid, d) for d in DIRECTION_DELTAS}


def choose_fluid_plan(
    grid: tuple[int, int],
    previous_direction: str | None,
) -> tuple[dict | None, dict[str, list[dict]]]:
    cs = corridors(grid)
    candidates = []

    for direction, cells in cs.items():
        n = len(cells)

        # W6 hardware-evidenced profiles:
        # 600 ms: nominal 5, observed max 6, +1 reserve => corridor >= 7
        #   v0p20 hardware evidence:
        #     [92,153] --UP 600 ms--> [92,147] = 6 tiles
        #     planned corridor length was 7, so one safe reserve remained.
        # 360 ms: nominal 3, observed max 4, +1 reserve => corridor >= 5
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
        else:
            continue

        reverse = (
            previous_direction is not None
            and direction == OPPOSITE.get(previous_direction)
        )
        core2_window = sum(
            1 for c in cells[:profile["max_observed_tiles"]] if c["core2"]
        )

        candidate = {
            "direction": direction,
            "corridor": cells,
            "corridor_length": n,
            **profile,
            "boundary_reserve_tiles": 1,
            "non_reverse_preferred": not reverse,
            "core2_tiles_in_max_window": core2_window,
            "mode": "fluid",
        }

        # Match the W6 preference hierarchy:
        # longest safe corridor/profile first; avoid immediate reversal where
        # another equally capable route exists; favor more core2 occupancy.
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


def choose_reposition(
    grid: tuple[int, int],
    previous_direction: str | None,
) -> dict | None:
    candidates = []
    for direction in DIRECTION_DELTAS:
        target = neighbor(grid, direction)
        if not in_core1(target):
            continue

        plan_after, _ = choose_fluid_plan(target, direction)
        max_corr = max(
            (len(v) for v in corridors(target).values()),
            default=0,
        )
        score = (
            1 if plan_after is not None else 0,
            1 if in_core2(target) else 0,
            max_corr,
            degree(target),
            0 if (
                previous_direction is not None
                and direction == OPPOSITE.get(previous_direction)
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
        "target_core2": in_core2(target),
        "target_degree": degree(target),
        "next_fluid_available": launch_plan is not None,
    }
