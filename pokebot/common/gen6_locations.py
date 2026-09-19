from __future__ import annotations

from .oras_locations import oras_location_name
from .xy_locations import XY_STARTER_LOCATION_NAME, xy_location_name
from .xy_world import xy_zone_location_name


def starter_location_for_family(family: str | None) -> str:
    return XY_STARTER_LOCATION_NAME if str(family or "").casefold() == "xy" else "Route 101"


def gen6_location_name(family: str | None, location_id: int | None, fallback: str = "Unknown Location") -> str:
    if str(family or "").casefold() == "xy":
        return xy_location_name(location_id, fallback)
    return oras_location_name(location_id, fallback)


def gen6_zone_location_name(family: str | None, zone_id: int | None, fallback: str = "Unknown Location") -> str:
    """Resolve a live runtime zone id when a family-specific world DB exists."""
    if str(family or "").casefold() == "xy":
        return xy_zone_location_name(zone_id, fallback)
    return fallback
