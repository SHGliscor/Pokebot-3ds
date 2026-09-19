from __future__ import annotations

import sqlite3
from functools import lru_cache
from pathlib import Path

_DEFAULT_DB = Path(__file__).resolve().parents[1] / "wild" / "world" / "pokemon_xy_world_runtime.sqlite"


@lru_cache(maxsize=1)
def _zones() -> dict[int, dict]:
    if not _DEFAULT_DB.is_file():
        return {}
    db = sqlite3.connect(_DEFAULT_DB)
    db.row_factory = sqlite3.Row
    try:
        return {
            int(r["zone_id"]): {
                "zone_id": int(r["zone_id"]),
                "parent_map": int(r["parent_map"]),
                "location_name": str(r["location_name"]),
                "map_matrix_id": int(r["map_matrix_id"]),
                "map_area": int(r["map_area"]),
                "enable_roller_skates": bool(r["enable_roller_skates"]),
                "enable_bicycle": bool(r["enable_bicycle"]),
                "enable_running": bool(r["enable_running"]),
                "enable_fly": bool(r["enable_fly"]),
            }
            for r in db.execute(
                "SELECT zone_id,parent_map,location_name,map_matrix_id,map_area,"
                "enable_roller_skates,enable_bicycle,enable_running,enable_fly FROM zones"
            )
        }
    finally:
        db.close()


def xy_zone_record(zone_id: int | None) -> dict | None:
    if zone_id is None:
        return None
    return _zones().get(int(zone_id))


def xy_zone_location_name(zone_id: int | None, fallback: str = "Unknown Location") -> str:
    row = xy_zone_record(zone_id)
    return str(row["location_name"]) if row else fallback
