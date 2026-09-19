from __future__ import annotations

import sqlite3
import struct
import time
from collections import deque
from pathlib import Path

DIRECTION_DELTAS = {
    "RIGHT": (1, 0),
    "DOWN": (0, 1),
    "LEFT": (-1, 0),
    "UP": (0, -1),
}
OPPOSITE = {"RIGHT": "LEFT", "LEFT": "RIGHT", "UP": "DOWN", "DOWN": "UP"}

POSITION_EPSILON = 0.25
CENTER_EPSILON = 0.30


def grid_float(world: float) -> float:
    return (world - 9.0) / 18.0


def nearest_grid(world: float) -> int:
    return int(round(grid_float(world)))


def grid_center(grid: int) -> float:
    return grid * 18.0 + 9.0


class WorldMap:
    """Small in-memory runtime view of a compiled Gen 6 world database."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(f"World terrain database missing: {self.path}")

        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            zone_columns = {
                str(r["name"]) for r in db.execute("PRAGMA table_info(zones)")
            }
            zone_number_col = (
                "oa_zone_number" if "oa_zone_number" in zone_columns
                else "xy_zone_number" if "xy_zone_number" in zone_columns
                else "zone_id"
            )
            cycling_col = (
                "enable_cycling" if "enable_cycling" in zone_columns
                else "enable_bicycle" if "enable_bicycle" in zone_columns
                else None
            )
            zone_rows = db.execute("SELECT * FROM zones").fetchall()
            self.zones = {}
            for r in zone_rows:
                zone_number = int(r[zone_number_col])
                cycling = bool(r[cycling_col]) if cycling_col else False
                self.zones[int(r["zone_id"])] = {
                    "zone_id": int(r["zone_id"]),
                    "oa_zone_number": zone_number,
                    "game_zone_number": zone_number,
                    "parent_map": int(r["parent_map"]),
                    "location_name": str(r["location_name"]),
                    "map_matrix_id": int(r["map_matrix_id"]),
                    "enable_cycling": cycling,
                    "enable_running": bool(r["enable_running"]),
                }
            self.matrices = {
                int(r["matrix_id"]): {
                    "matrix_id": int(r["matrix_id"]),
                    "has_lod": int(r["has_lod"]),
                    "width": int(r["width"]),
                    "height": int(r["height"]),
                }
                for r in db.execute("SELECT * FROM map_matrices")
            }
            self.regions = {
                (int(r["matrix_id"]), int(r["matrix_x"]), int(r["matrix_y"])): int(r["region_id"])
                for r in db.execute("SELECT * FROM matrix_regions")
            }
            self.zone_switches = {
                (int(r["matrix_id"]), int(r["sub_x"]), int(r["sub_y"])): int(r["zone_id"])
                for r in db.execute("SELECT * FROM matrix_zone_switches")
            }
            self.encounter_cells = {
                (int(r["region_id"]), int(r["tile_x"]), int(r["tile_y"])): {
                    "raw_hex": str(r["raw_hex"]),
                    "semantic_code": int(r["semantic_code"]),
                    "terrain_name": str(r["terrain_name"]),
                    "evidence": str(r["evidence"]),
                }
                for r in db.execute("SELECT * FROM encounter_cells")
            }
            self.collision_cells = {}
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='collision_cells'"
            ).fetchone():
                columns = {
                    str(r["name"])
                    for r in db.execute("PRAGMA table_info(collision_cells)")
                }
                required = {"zone_id", "grid_x", "grid_y", "blocked"}
                if required.issubset(columns):
                    self.collision_cells = {
                        (int(r["zone_id"]), int(r["grid_x"]), int(r["grid_y"])): bool(r["blocked"])
                        for r in db.execute(
                            "SELECT zone_id, grid_x, grid_y, blocked FROM collision_cells"
                        )
                    }
        finally:
            db.close()

    def resolve(self, zone_id: int, grid: tuple[int, int] | list[int]) -> dict:
        zone_id = int(zone_id)
        gx, gy = int(grid[0]), int(grid[1])
        zone = self.zones.get(zone_id)
        if zone is None:
            return {"resolved": False, "reason": "ZONE_NOT_IN_DATABASE", "zone_id": zone_id, "grid": [gx, gy]}

        matrix = self.matrices.get(zone["map_matrix_id"])
        if matrix is None:
            return {"resolved": False, "reason": "MATRIX_NOT_IN_DATABASE", "zone_id": zone_id, "grid": [gx, gy]}

        width_tiles = matrix["width"] * 40
        height_tiles = matrix["height"] * 40
        if not (0 <= gx < width_tiles and 0 <= gy < height_tiles):
            return {
                "resolved": False,
                "reason": "GRID_OUTSIDE_MATRIX",
                "zone_id": zone_id,
                "location_name": zone["location_name"],
                "matrix_id": matrix["matrix_id"],
                "grid": [gx, gy],
                "matrix_size_tiles": [width_tiles, height_tiles],
            }

        mx, my = gx // 40, gy // 40
        tx, ty = gx % 40, gy % 40
        rid = self.regions.get((matrix["matrix_id"], mx, my), -1)
        if rid < 0:
            return {
                "resolved": False,
                "reason": "NO_FIELD_REGION",
                "zone_id": zone_id,
                "location_name": zone["location_name"],
                "matrix_id": matrix["matrix_id"],
                "matrix_cell": [mx, my],
                "grid": [gx, gy],
            }

        switch_zone = self.zone_switches.get((matrix["matrix_id"], gx // 10, gy // 10))
        switch_matches = switch_zone is None or switch_zone == zone_id
        cell = self.encounter_cells.get((rid, tx, ty))
        encounter = cell is not None and switch_matches

        return {
            "resolved": True,
            "zone_id": zone_id,
            "oa_zone_number": zone["oa_zone_number"],
            "parent_map": zone["parent_map"],
            "location_name": zone["location_name"],
            "enable_cycling": zone["enable_cycling"],
            "enable_running": zone["enable_running"],
            "matrix_id": matrix["matrix_id"],
            "matrix_has_zone_switches": bool(matrix["has_lod"]),
            "matrix_size_regions": [matrix["width"], matrix["height"]],
            "matrix_cell": [mx, my],
            "region_id": rid,
            "local_tile": [tx, ty],
            "grid": [gx, gy],
            "zone_switch_at_tile": switch_zone,
            "zone_switch_matches_live": None if switch_zone is None else switch_matches,
            "encounter_terrain": encounter,
            "terrain": dict(cell) if encounter else None,
        }

    def is_hard_boundary(self, zone_id: int, grid: tuple[int, int]) -> bool:
        """Return collision authority for a non-grass terminal cell.

        Older databases do not contain collision_cells and must remain
        conservative: an unknown non-grass cell is not a wall.
        """
        return bool(self.collision_cells.get((int(zone_id), int(grid[0]), int(grid[1])), False))


class DynamicTerrain:
    """Route-independent terrain API compatible with the frozen W6 planners."""

    ROUTE101_ZONE = 23  # compatibility only; no longer used as authority.
    MATRIX_ID = -1
    TERRAIN_CLASS = "ORAS_WORLD_DATABASE_ENCOUNTER_GRASS"
    COMPONENT_ID = -1
    DIRECTION_DELTAS = DIRECTION_DELTAS
    OPPOSITE = OPPOSITE

    def __init__(self, world: WorldMap):
        self.world = world
        self.current_zone: int | None = None
        self.expected_zone: int | None = None
        self._patch_cache: dict[tuple[int, int], dict] = {}

    def set_zone(self, zone_id: int):
        self.current_zone = int(zone_id)
        self._patch_cache.clear()

    def lock_zone(self, zone_id: int):
        self.current_zone = int(zone_id)
        self.expected_zone = int(zone_id)
        self._patch_cache.clear()

    def _zone(self) -> int:
        if self.current_zone is None:
            raise RuntimeError("dynamic terrain zone has not been set")
        return self.current_zone

    def resolve(self, grid):
        return self.world.resolve(self._zone(), tuple(grid))

    def in_core1(self, grid) -> bool:
        r = self.resolve(grid)
        return bool(r.get("resolved") and r.get("encounter_terrain"))

    def neighbor(self, grid, direction):
        dx, dy = DIRECTION_DELTAS[direction]
        return int(grid[0]) + dx, int(grid[1]) + dy

    def degree(self, grid) -> int:
        if not self.in_core1(grid):
            return 0
        return sum(self.in_core1(self.neighbor(grid, d)) for d in DIRECTION_DELTAS)

    def in_core2(self, grid) -> bool:
        return self.in_core1(grid) and self.degree(grid) == 4

    def mask_record(self, grid) -> dict:
        r = self.resolve(grid)
        core1 = bool(r.get("resolved") and r.get("encounter_terrain"))
        core2 = core1 and self.degree(grid) == 4
        terrain = r.get("terrain") or {}
        return {
            "matrix_id": r.get("matrix_id"),
            "terrain_class": terrain.get("terrain_name", self.TERRAIN_CLASS),
            "component_id": self.current_zone,
            "raw": core1,
            "core1": core1,
            "core2": core2,
            "raw_hex": terrain.get("raw_hex"),
            "location_name": r.get("location_name"),
            "zone_switch_matches_live": r.get("zone_switch_matches_live"),
        }

    def corridor(self, grid, direction, limit=16):
        out = []
        cur = tuple(grid)
        for _ in range(limit):
            cur = self.neighbor(cur, direction)
            if not self.in_core1(cur):
                break
            out.append({"grid": [cur[0], cur[1]], "core2": self.in_core2(cur)})
        return out

    def boundary_edges(self, grid, limit=4096):
        """Return the grass component and every edge leaving it.

        The database is the authority for encounter grass.  A boundary is
        therefore generated whenever a cardinal neighbour is outside that
        component, including map bounds and non-grass/path cells.
        """
        start = (int(grid[0]), int(grid[1]))
        if not self.in_core1(start):
            return {
                "patch_id": None,
                "cells": [],
                "edges": [],
                "truncated": False,
            }
        cached = self._patch_cache.get(start)
        if cached is not None:
            return cached

        cells = set()
        queue = deque([start])
        while queue and len(cells) < limit:
            cell = queue.popleft()
            if cell in cells or not self.in_core1(cell):
                continue
            cells.add(cell)
            for direction in DIRECTION_DELTAS:
                neighbour = self.neighbor(cell, direction)
                if neighbour not in cells:
                    queue.append(neighbour)

        edges = []
        for cell in sorted(cells):
            for direction in DIRECTION_DELTAS:
                neighbour = self.neighbor(cell, direction)
                if neighbour not in cells:
                    edges.append({
                        "grass_grid": [cell[0], cell[1]],
                        "direction": direction,
                        "outside_grid": [neighbour[0], neighbour[1]],
                        "outside_is_grass": self.in_core1(neighbour),
                    })
        ordered = sorted(cells)
        patch_id = (
            f"zone{self._zone()}:{len(cells)}:"
            f"{ordered[0][0]},{ordered[0][1]}"
            if ordered else None
        )
        result = {
            "patch_id": patch_id,
            "cells": [[x, y] for x, y in ordered],
            "edges": edges,
            "truncated": len(cells) >= limit and bool(queue),
        }
        self._patch_cache[start] = result
        return result

    def corridor_metadata(self, grid, direction, limit=16):
        cells = self.corridor(grid, direction, limit)
        terminal = None
        if cells:
            terminal = self.neighbor(cells[-1]["grid"], direction)
        else:
            terminal = self.neighbor(grid, direction)
        patch = self.boundary_edges(grid)
        terminal_hard_boundary = (
            self.world.is_hard_boundary(self._zone(), tuple(terminal))
        )
        return {
            "cells": cells,
            "available_grass_distance": len(cells),
            "terminal_boundary": [terminal[0], terminal[1]],
            "terminal_boundary_is_grass": self.in_core1(terminal),
            "terminal_hard_boundary": terminal_hard_boundary,
            "patch_id": patch["patch_id"],
            "patch_size": len(patch["cells"]),
            "patch_truncated": patch["truncated"],
        }

    def corridors(self, grid):
        return {d: self.corridor(grid, d) for d in DIRECTION_DELTAS}

    # Kept for compatibility with frozen source paths not used by the Qt axis
    # adapter. Profiles remain identical to v0p23.
    def choose_fluid_plan(self, grid, previous_direction):
        cs = self.corridors(grid)
        candidates = []
        for direction, cells in cs.items():
            n = len(cells)
            if n >= 7:
                profile = {"nominal_tiles": 5, "max_observed_tiles": 6, "hold_ms": 600, "timing_profile": "600ms_nominal5_max6_OBSERVED"}
            elif n >= 5:
                profile = {"nominal_tiles": 3, "max_observed_tiles": 4, "hold_ms": 360, "timing_profile": "360ms_nominal3_max4_OBSERVED"}
            else:
                continue
            reverse = previous_direction is not None and direction == OPPOSITE.get(previous_direction)
            core2_window = sum(1 for c in cells[:profile["max_observed_tiles"]] if c["core2"])
            candidate = {
                "direction": direction, "corridor": cells, "corridor_length": n, **profile,
                "boundary_reserve_tiles": 1, "non_reverse_preferred": not reverse,
                "core2_tiles_in_max_window": core2_window, "mode": "fluid",
            }
            candidates.append(((profile["nominal_tiles"], n, 0 if reverse else 1, core2_window), candidate))
        if not candidates:
            return None, cs
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1], cs

    def choose_reposition(self, grid, previous_direction):
        candidates = []
        for direction in DIRECTION_DELTAS:
            target = self.neighbor(grid, direction)
            if not self.in_core1(target):
                continue
            plan_after, _ = self.choose_fluid_plan(target, direction)
            max_corr = max((len(v) for v in self.corridors(target).values()), default=0)
            reverse = previous_direction is not None and direction == OPPOSITE.get(previous_direction)
            score = (1 if plan_after else 0, 1 if self.in_core2(target) else 0, max_corr, self.degree(target), 0 if reverse else 1)
            candidates.append((score, direction, target, plan_after))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        _, direction, target, launch_plan = candidates[0]
        return {
            "mode": "reposition", "direction": direction,
            "target_grid": [target[0], target[1]], "launch_grid": [target[0], target[1]],
            "hold_ms": 160, "settle_ms": 140, "post_settle_s": 0.70,
            "timing_profile": "W3C_1tile_160ms_PROVEN", "same_component_required": True,
            "target_core2": self.in_core2(target), "target_degree": self.degree(target),
            "next_fluid_available": launch_plan is not None,
        }


def _read_f32(br, address: int) -> float:
    return struct.unpack("<f", br.read(address, 4))[0]


def install_world_authority(backend, db_path: str | Path):
    """
    Attach whole-game terrain authority to a loaded frozen v0p23/v0p27 module.

    The validated movement timings/input/encounter logic are not edited. Only
    the Route101-specific position/terrain globals are replaced at runtime.
    """
    world = WorldMap(db_path)
    terrain = DynamicTerrain(world)
    backend.terrain = terrain

    def read_position(br):
        zone_raw = br.u32(backend.core.ZONE_ADDR)
        zone_id = zone_raw & 0xFFFF
        zone_flags = (zone_raw >> 16) & 0xFFFF
        terrain.set_zone(zone_id)

        ax = _read_f32(br, backend.PRIMARY_X_ADDR)
        az = _read_f32(br, backend.PRIMARY_Z_ADDR)
        bx = _read_f32(br, backend.SECONDARY_X_ADDR)
        bz = _read_f32(br, backend.SECONDARY_Z_ADDR)

        duplicates_match = abs(ax - bx) <= POSITION_EPSILON and abs(az - bz) <= POSITION_EPSILON
        gx, gz = nearest_grid(ax), nearest_grid(az)
        settled = (
            abs(ax - grid_center(gx)) <= CENTER_EPSILON and abs(az - grid_center(gz)) <= CENTER_EPSILON
            and abs(bx - grid_center(gx)) <= CENTER_EPSILON and abs(bz - grid_center(gz)) <= CENTER_EPSILON
        )
        g = (gx, gz)
        resolved = world.resolve(zone_id, g)
        return {
            "zone": zone_id, "zone_id": zone_id, "zone_raw": zone_raw,
            "zone_raw_hex": backend.hx(zone_raw), "zone_flags": zone_flags,
            "zone_flags_hex": f"0x{zone_flags:04X}", "world_a": [ax, az], "world_b": [bx, bz],
            "duplicates_match": duplicates_match, "settled_tile_center": settled,
            "grid": [gx, gz], "mask": terrain.mask_record(g),
            "world_resolve": resolved, "location_name": resolved.get("location_name", f"Zone {zone_id}"),
            "parent_map": resolved.get("parent_map"), "matrix_id": resolved.get("matrix_id"),
        }

    def require_safe_position(pos, label):
        terrain.set_zone(pos["zone_id"])
        if terrain.expected_zone is not None and pos["zone_id"] != terrain.expected_zone:
            raise backend.IntegrationHold(
                f"{label}: live zone changed from {terrain.expected_zone} to {pos['zone_id']}"
            )
        if not pos["duplicates_match"]:
            raise backend.IntegrationHold(f"{label}: duplicate world coordinates disagree")
        if not pos["settled_tile_center"]:
            raise backend.IntegrationHold(f"{label}: player is not settled on a tile center")
        if not pos["mask"]["core1"]:
            raise backend.IntegrationHold(
                f"{label}: {pos.get('location_name', 'zone '+str(pos['zone_id']))} grid {pos['grid']} "
                "is not classified as encounter grass by the whole-game database"
            )
        return tuple(pos["grid"])

    def require_bunny_interior_position(pos, label):
        terrain.set_zone(pos["zone_id"])
        if terrain.expected_zone is not None and pos["zone_id"] != terrain.expected_zone:
            raise backend.IntegrationHold(
                f"{label}: live zone changed from {terrain.expected_zone} to {pos['zone_id']}"
            )
        if not pos["duplicates_match"]:
            raise backend.IntegrationHold(f"{label}: duplicate world coordinates disagree")
        if not pos["mask"]["core1"]:
            raise backend.IntegrationHold(
                f"{label}: grid {pos['grid']} is not encounter grass in {pos.get('location_name', 'current location')}"
            )
        if not pos["mask"]["core2"]:
            raise backend.IntegrationHold(
                f"{label}: grid {pos['grid']} is a grass edge/boundary tile; start one tile further inside"
            )
        return tuple(pos["grid"])

    def wait_for_field_authority(br, timeout=None):
        if timeout is None:
            timeout = backend.FIELD_AUTHORITY_TIMEOUT_SEC
        deadline = time.monotonic() + timeout
        samples = []
        consecutive = 0
        last_safe_grid = None
        while time.monotonic() < deadline:
            battle = br.u32(backend.core.BATTLE_ADDR)
            pos = read_position(br)
            expected_ok = terrain.expected_zone is None or pos["zone_id"] == terrain.expected_zone
            safe_now = (
                battle == backend.core.BATTLE_INACTIVE and expected_ok and pos["duplicates_match"]
                and pos["settled_tile_center"] and pos["mask"]["core1"]
            )
            grid_tuple = tuple(pos["grid"])
            if safe_now:
                if last_safe_grid == grid_tuple:
                    consecutive += 1
                else:
                    last_safe_grid, consecutive = grid_tuple, 1
            else:
                consecutive, last_safe_grid = 0, None
            samples.append({
                "time": backend.now_iso(), "battle": backend.hx(battle), "safe_now": safe_now,
                "consecutive_safe_samples": consecutive, "position": pos,
            })
            if safe_now and consecutive >= backend.FIELD_AUTHORITY_CONFIRM_SAMPLES:
                return {
                    "ready": True, "status": "FIELD_AUTHORITY_CONFIRMED",
                    "confirmed_grid": list(grid_tuple), "confirm_samples": consecutive,
                    "samples": samples, "final_position": pos,
                }
            if battle == backend.core.BATTLE_ACTIVE:
                return {"ready": False, "status": "UNEXPECTED_BATTLE_REACTIVATION", "samples": samples, "final_position": pos}
            # Zone/coordinate fields can be transient while the overworld is
            # being restored after Run. Preserve the proven v0p23 behavior:
            # treat a transient mismatch as non-authoritative and keep waiting;
            # never convert it into permission to move.
            time.sleep(backend.FIELD_AUTHORITY_POLL_SEC)
        return {
            "ready": False, "status": "FIELD_AUTHORITY_TIMEOUT", "samples": samples,
            "final_position": samples[-1]["position"] if samples else None,
        }

    backend.read_position = read_position
    backend.require_safe_position = require_safe_position
    backend.wait_for_field_authority = wait_for_field_authority
    if hasattr(backend, "require_bunny_interior_position"):
        backend.require_bunny_interior_position = require_bunny_interior_position

    return terrain


def probe_world_position(bridge, db_path: str | Path, *, zone_addr=0x08C6E884,
                         primary_base=0x08C6E7B0, secondary_base=0x08DA8568) -> dict:
    """Three-read, no-input dashboard location/terrain probe."""
    world = WorldMap(db_path)
    zone_raw = struct.unpack("<I", bridge.read(zone_addr, 4))[0]
    zone_id = zone_raw & 0xFFFF
    p = bridge.read(primary_base, 12)
    s = bridge.read(secondary_base, 12)
    ax, az = struct.unpack_from("<f", p, 0)[0], struct.unpack_from("<f", p, 8)[0]
    bx, bz = struct.unpack_from("<f", s, 0)[0], struct.unpack_from("<f", s, 8)[0]
    gx, gy = nearest_grid(ax), nearest_grid(az)
    duplicates = abs(ax-bx) <= POSITION_EPSILON and abs(az-bz) <= POSITION_EPSILON
    settled = (
        abs(ax-grid_center(gx)) <= CENTER_EPSILON and abs(az-grid_center(gy)) <= CENTER_EPSILON
        and abs(bx-grid_center(gx)) <= CENTER_EPSILON and abs(bz-grid_center(gy)) <= CENTER_EPSILON
    )
    current = world.resolve(zone_id, (gx, gy))
    terrain = DynamicTerrain(world); terrain.set_zone(zone_id)
    corridors = {d: terrain.corridor((gx,gy), d) for d in DIRECTION_DELTAS}
    return {
        "zone_raw": f"0x{zone_raw:08X}", "zone_id": zone_id,
        "world_primary": [ax, az], "world_secondary": [bx, bz],
        "duplicates_match": duplicates, "settled_tile_center": settled,
        "grid": [gx, gy], "resolved": bool(current.get("resolved")),
        "location_name": current.get("location_name", f"Zone {zone_id}"),
        "parent_map": current.get("parent_map"), "matrix_id": current.get("matrix_id"),
        "enable_cycling": current.get("enable_cycling"),
        "enable_running": current.get("enable_running"),
        "region_id": current.get("region_id"), "local_tile": current.get("local_tile"),
        "encounter_terrain": bool(current.get("encounter_terrain")),
        "terrain": current.get("terrain"), "core2_interior": terrain.in_core2((gx,gy)),
        "corridor_lengths": {d: len(v) for d,v in corridors.items()},
    }
