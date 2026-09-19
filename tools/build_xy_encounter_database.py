from __future__ import annotations

import argparse
import json
import struct
import sys
from collections import defaultdict
from pathlib import Path

# Reuse the validated X/Y GARC/Mini/ZoneData parsing from HF53.
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from build_xy_world_database import (  # noqa: E402
    ZONE_SIZE,
    decompress_lz11,
    garc_files,
    load_location_names,
    unpack_mini,
)

SLOT_SIZE = 4
ENCOUNTER_SIZE = 0x178
TOTAL_SLOTS = ENCOUNTER_SIZE // SLOT_SIZE  # 94

# pk3DS XYWE.cs All_Species order, totaling 94 fixed slots / 0x178 bytes.
RAW_GROUPS = (
    ("grass", "Grass", 12),
    ("yellow_flowers", "Yellow Flowers", 12),
    ("purple_flowers", "Purple Flowers", 12),
    ("red_flowers", "Red Flowers", 12),
    ("rough_terrain", "Rough Terrain", 12),
    ("surf", "Surf", 5),
    ("rock_smash", "Rock Smash", 5),
    ("old_rod", "Old Rod", 3),
    ("good_rod", "Good Rod", 3),
    ("super_rod", "Super Rod", 3),
    ("horde_a", "Horde A", 5),
    ("horde_b", "Horde B", 5),
    ("horde_c", "Horde C", 5),
)

DISPLAY_GROUPS = (
    ("grass", "Grass", ("grass",)),
    ("yellow_flowers", "Yellow Flowers", ("yellow_flowers",)),
    ("purple_flowers", "Purple Flowers", ("purple_flowers",)),
    ("red_flowers", "Red Flowers", ("red_flowers",)),
    ("rough_terrain", "Rough Terrain", ("rough_terrain",)),
    ("surf", "Surf", ("surf",)),
    ("rock_smash", "Rock Smash", ("rock_smash",)),
    ("old_rod", "Old Rod", ("old_rod",)),
    ("good_rod", "Good Rod", ("good_rod",)),
    ("super_rod", "Super Rod", ("super_rod",)),
    ("horde", "Horde", ("horde_a", "horde_b", "horde_c")),
)


def load_species_names(path: Path) -> dict[int, str]:
    ns: dict[str, object] = {}
    exec(path.read_text(encoding="utf-8"), ns)
    names = ns.get("SPECIES_NAMES")
    if not isinstance(names, dict):
        raise ValueError(f"SPECIES_NAMES missing from {path}")
    return {int(k): str(v) for k, v in names.items()}


def master_zone_table(enc_files: list[bytes]) -> bytes:
    masters = [x for x in enc_files if x and x[0] != 0x11 and len(x) % ZONE_SIZE == 0]
    if len(masters) != 1:
        raise ValueError(f"Expected one 360-record ZoneData master table; found {len(masters)}")
    master = masters[0]
    if len(master) // ZONE_SIZE != 360:
        raise ValueError(f"Expected 360 X/Y zones; found {len(master) // ZONE_SIZE}")
    return master


def map_zone_packages(enc_files: list[bytes], master: bytes) -> dict[int, list[bytes]]:
    # Do not assume GARC entry order. Match each ZO package's embedded ZoneData
    # record to the authoritative master table. This proved one-to-one for all
    # 360 records in the user's Pokémon Y archive.
    lookup: dict[bytes, int] = {
        master[i * ZONE_SIZE:(i + 1) * ZONE_SIZE]: i for i in range(360)
    }
    result: dict[int, list[bytes]] = {}
    for raw in enc_files:
        try:
            dec = decompress_lz11(raw)
            parts = unpack_mini(dec, b"ZO")
        except Exception:
            continue
        if len(parts) < 4:
            continue
        zid = lookup.get(parts[0])
        if zid is None:
            raise ValueError("Found ZO package whose ZoneData does not match master table")
        if zid in result:
            raise ValueError(f"Duplicate ZO package for zone {zid}")
        result[zid] = parts
    if len(result) != 360:
        missing = sorted(set(range(360)) - set(result))
        raise ValueError(f"Mapped {len(result)}/360 zone packages; missing {missing[:20]}")
    return result


def parse_slot(raw: bytes, slot_index: int, group_key: str) -> dict | None:
    packed, level_min, level_max = struct.unpack_from("<HBB", raw, 0)
    species = packed & 0x7FF
    form = packed >> 11
    # pk3DS treats species index 0 as an unused slot. Some unused X/Y slots
    # retain non-zero level bytes, so species 0 is authoritative emptiness.
    if species == 0:
        return None
    if species > 721:
        raise ValueError(f"Invalid species {species} in {group_key} slot {slot_index}")
    if level_min <= 0 or level_max <= 0 or level_min > level_max:
        raise ValueError(
            f"Invalid levels {level_min}-{level_max} for species {species} "
            f"in {group_key} slot {slot_index}"
        )
    return {
        "species": species,
        "form": form,
        "min_level": level_min,
        "max_level": level_max,
        "slot_index": slot_index,
        "source_group": group_key,
    }


def encounter_payload(parts: list[bytes]) -> bytes | None:
    blob = parts[3]
    if not blob:
        return None
    # XYWE reads a 0x178 encounter payload appended after the encounter-file
    # header. In unpacked ZO part 3 this is the final 0x178 bytes (0x10 header
    # + 0x178 payload for populated tables in the user's Y archive).
    if len(blob) < ENCOUNTER_SIZE:
        return None
    payload = blob[-ENCOUNTER_SIZE:]
    if len(payload) != ENCOUNTER_SIZE:
        raise ValueError("Failed to isolate 0x178 X/Y encounter table")
    return payload


def parse_zone_slots(parts: list[bytes]) -> dict[str, list[dict]]:
    payload = encounter_payload(parts)
    if payload is None:
        return {key: [] for key, _, _ in RAW_GROUPS}
    groups: dict[str, list[dict]] = {}
    cursor = 0
    for key, _title, count in RAW_GROUPS:
        rows = []
        for local_index in range(count):
            off = (cursor + local_index) * SLOT_SIZE
            row = parse_slot(payload[off:off + SLOT_SIZE], local_index, key)
            if row is not None:
                rows.append(row)
        groups[key] = rows
        cursor += count
    if cursor != TOTAL_SLOTS:
        raise AssertionError(f"Encounter layout totals {cursor}, expected {TOTAL_SLOTS}")
    return groups


def aggregate_section(
    zones: list[dict],
    raw_group_keys: tuple[str, ...],
    title: str,
    species_names: dict[int, str],
) -> dict | None:
    # Aggregate duplicate slots by species+form while preserving level envelope,
    # contributing zones and source group(s). Slot count is literal number of
    # fixed game-file slots, not an inferred percentage.
    agg: dict[tuple[int, int], dict] = {}
    slot_records = 0
    for zone in zones:
        zid = int(zone["zone_id"])
        groups = zone["groups"]
        for raw_key in raw_group_keys:
            for row in groups.get(raw_key, []):
                slot_records += 1
                k = (int(row["species"]), int(row["form"]))
                out = agg.get(k)
                if out is None:
                    out = {
                        "species": k[0],
                        "species_name": species_names.get(k[0], f"Species #{k[0]}"),
                        "form": k[1],
                        "min_level": int(row["min_level"]),
                        "max_level": int(row["max_level"]),
                        "slot_count": 0,
                        "zone_ids": [],
                        "source_groups": [],
                        "automation_ready": False,
                    }
                    agg[k] = out
                out["min_level"] = min(out["min_level"], int(row["min_level"]))
                out["max_level"] = max(out["max_level"], int(row["max_level"]))
                out["slot_count"] += 1
                if zid not in out["zone_ids"]:
                    out["zone_ids"].append(zid)
                if raw_key not in out["source_groups"]:
                    out["source_groups"].append(raw_key)
    if not agg:
        return None
    pokemon = sorted(agg.values(), key=lambda r: (r["species"], r["form"]))
    for row in pokemon:
        row["zone_ids"].sort()
    return {
        "title": title,
        "pokemon": pokemon,
        "slot_records": slot_records,
        "note": "Parsed from Pokémon Y a/0/1/2 encounter slots • browser data only; X/Y wild automation is not yet hardware-wired",
        "automation_ready": False,
    }


def build(source_dir: Path, xy_locations_py: Path, species_names_py: Path) -> dict:
    enc_path = source_dir / "a" / "0" / "1" / "2"
    if not enc_path.is_file():
        raise FileNotFoundError(enc_path)
    enc_files = garc_files(enc_path)
    master = master_zone_table(enc_files)
    packages = map_zone_packages(enc_files, master)
    locations = load_location_names(xy_locations_py)
    species_names = load_species_names(species_names_py)

    zones: list[dict] = []
    populated_zones = 0
    raw_slot_total = 0
    for zid in range(360):
        zd = master[zid * ZONE_SIZE:(zid + 1) * ZONE_SIZE]
        parent_raw = struct.unpack_from("<H", zd, 0x1C)[0]
        parent_map = parent_raw & 0x03FF
        groups = parse_zone_slots(packages[zid])
        count = sum(len(v) for v in groups.values())
        if count:
            populated_zones += 1
            raw_slot_total += count
        zones.append({
            "zone_id": zid,
            "parent_map": parent_map,
            "location_name": locations.get(parent_map, f"Location {parent_map}"),
            "map_matrix_id": struct.unpack_from("<H", zd, 0x04)[0],
            "map_area": struct.unpack_from("<H", zd, 0x02)[0],
            "groups": groups,
            "slot_records": count,
        })

    by_parent: dict[int, list[dict]] = defaultdict(list)
    for zone in zones:
        by_parent[zone["parent_map"]].append(zone)

    out_locations = []
    for parent_map in sorted(by_parent):
        loc_zones = by_parent[parent_map]
        sections = []
        for display_key, title, raw_keys in DISPLAY_GROUPS:
            section = aggregate_section(loc_zones, raw_keys, title, species_names)
            if section is None:
                continue
            section["key"] = display_key
            sections.append(section)
        zone_ids = sorted(int(z["zone_id"]) for z in loc_zones)
        matrix_ids = sorted({int(z["map_matrix_id"]) for z in loc_zones})
        out_locations.append({
            "parent_map": parent_map,
            "name": locations.get(parent_map, f"Location {parent_map}"),
            "zone_ids": zone_ids,
            "map_matrix_ids": matrix_ids,
            "has_encounters": bool(sections),
            "environment_hint": "water" if sections and all(s["key"] in {"surf", "old_rod", "good_rod", "super_rod"} for s in sections) else "land",
            "sections": sections,
        })

    encounter_locations = [x for x in out_locations if x["has_encounters"]]
    return {
        "format": 1,
        "source": "User-extracted Pokémon Y RomFS a/0/1/2; parsed using pk3DS XYWE fixed 0x178 / 94-slot layout and HF53 ZoneData parent-map mapping",
        "games": {
            "pokemon_y": {
                "source_archive": "a/0/1/2",
                "source_authority": "game_file",
                "zones_total": 360,
                "zones_with_encounters": populated_zones,
                "locations_with_encounters": len(encounter_locations),
                "raw_slot_records": raw_slot_total,
                "locations": out_locations,
            }
        },
        "section_order": [x[0] for x in DISPLAY_GROUPS],
        "browser_notes": [
            "Pokémon Y encounter rosters are parsed directly from the user's decrypted a/0/1/2 game archive.",
            "Repeated fixed slots are counted literally; slot_count is not presented as an encounter percentage.",
            "Pokémon X is intentionally not synthesized from Y because X/Y version-exclusive encounter slots can differ.",
            "X/Y wild hunt buttons remain disabled until the corresponding automation is hardware-validated.",
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Build Pokémon Y Hunts encounter database from X/Y RomFS a/0/1/2")
    ap.add_argument("source_dir", type=Path, help="Folder containing a/0/1/2")
    ap.add_argument("--output", type=Path, default=Path("xy_encounters.json"))
    ap.add_argument("--xy-locations", type=Path, default=HERE.parent / "pokebot" / "common" / "xy_locations.py")
    ap.add_argument("--species-names", type=Path, default=HERE.parent / "pokebot" / "common" / "species_names.py")
    args = ap.parse_args()
    result = build(args.source_dir, args.xy_locations, args.species_names)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    game = result["games"]["pokemon_y"]
    print(f"Wrote {args.output}")
    print(f"Zones with encounters: {game['zones_with_encounters']}/360")
    print(f"Locations with encounters: {game['locations_with_encounters']}")
    print(f"Raw populated slots: {game['raw_slot_records']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
