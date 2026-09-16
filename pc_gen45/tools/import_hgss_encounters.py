from __future__ import annotations

import argparse
import json
from pathlib import Path
import urllib.request

HG_COMMIT = "0985e8718df4f25e64d6507d89c0c97c0d288981"
ENCOUNTER_URL = (
    "https://raw.githubusercontent.com/pret/pokeheartgold/"
    + HG_COMMIT
    + "/files/fielddata/encountdata/gs_enc_data.json"
)

LAND_WEIGHTS = [20, 20, 10, 10, 10, 10, 5, 5, 4, 4, 1, 1]
SURF_WEIGHTS = [60, 30, 5, 4, 1]
FISH_WEIGHTS = [40, 30, 15, 10, 5]
ROCK_SMASH_WEIGHTS = [80, 20]
HEADBUTT_WEIGHTS = [50, 15, 15, 10, 5, 5]


def fetch_json(url: str) -> object:
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def load_species(path: Path) -> dict[str, int]:
    by_id = json.loads(path.read_text(encoding="utf-8"))
    result: dict[str, int] = {}
    for raw_id, name in by_id.items():
        species_id = int(raw_id)
        token = (
            str(name)
            .upper()
            .replace("♀", "_F")
            .replace("♂", "_M")
            .replace(".", "")
            .replace("'", "")
            .replace("-", "_")
            .replace(" ", "_")
        )
        # A few Game Freak constant spellings differ from display-name spelling.
        special = {
            "MR_MIME": "MR_MIME",
            "MIME_JR": "MIME_JR",
            "FARFETCHD": "FARFETCHD",
            "HO_OH": "HO_OH",
            "PORYGON_Z": "PORYGON_Z",
            "NIDORAN_F": "NIDORAN_F",
            "NIDORAN_M": "NIDORAN_M",
        }
        result["SPECIES_" + special.get(token, token)] = species_id
    return result


def resolve(value, *, version: str, species: dict[str, int]):
    if isinstance(value, list):
        return [resolve(v, version=version, species=species) for v in value]

    if isinstance(value, dict):
        if version in value:
            return resolve(value[version], version=version, species=species)
        return {
            k: resolve(v, version=version, species=species)
            for k, v in value.items()
        }

    if isinstance(value, str) and value.startswith("SPECIES_"):
        if value not in species:
            raise KeyError(f"unknown species constant: {value}")
        return species[value]

    return value


def annotate_weights(encounter: dict) -> None:
    land = encounter.get("land")
    if isinstance(land, dict) and land.get("mons"):
        for i, slot in enumerate(land["mons"]):
            if i < len(LAND_WEIGHTS):
                slot["slot"] = i
                slot["percent"] = LAND_WEIGHTS[i]

    surf = encounter.get("surf")
    if isinstance(surf, dict) and surf.get("mons"):
        for i, slot in enumerate(surf["mons"]):
            if i < len(SURF_WEIGHTS):
                slot["slot"] = i
                slot["percent"] = SURF_WEIGHTS[i]

    rock = encounter.get("rock_smash")
    if isinstance(rock, dict) and rock.get("mons"):
        for i, slot in enumerate(rock["mons"]):
            if i < len(ROCK_SMASH_WEIGHTS):
                slot["slot"] = i
                slot["percent"] = ROCK_SMASH_WEIGHTS[i]

    fishing = encounter.get("fishing")
    if isinstance(fishing, dict):
        for rod in ("old_rod", "good_rod", "super_rod"):
            table = fishing.get(rod)
            if isinstance(table, dict) and table.get("mons"):
                for i, slot in enumerate(table["mons"]):
                    if i < len(FISH_WEIGHTS):
                        slot["slot"] = i
                        slot["percent"] = FISH_WEIGHTS[i]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--species", default="pc_gen45/data/species_gen4.json")
    ap.add_argument("--output", default="pc_gen45/data/heartgold_encounters.json")
    ap.add_argument("--version", choices=("HEARTGOLD", "SOULSILVER"), default="HEARTGOLD")
    args = ap.parse_args()

    species = load_species(Path(args.species))
    raw = fetch_json(ENCOUNTER_URL)
    if not isinstance(raw, dict) or not isinstance(raw.get("encounters"), list):
        raise RuntimeError("unexpected pret encounter JSON shape")

    resolved = resolve(raw["encounters"], version=args.version, species=species)
    assert isinstance(resolved, list)

    for encounter in resolved:
        if isinstance(encounter, dict):
            annotate_weights(encounter)

    payload = {
        "source": "pret/pokeheartgold",
        "source_commit": HG_COMMIT,
        "game": args.version,
        "slot_percentages": {
            "land": LAND_WEIGHTS,
            "surf": SURF_WEIGHTS,
            "fishing": FISH_WEIGHTS,
            "rock_smash": ROCK_SMASH_WEIGHTS,
            "headbutt": HEADBUTT_WEIGHTS,
        },
        "encounters": resolved,
    }

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(resolved)} HGSS encounter tables to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
