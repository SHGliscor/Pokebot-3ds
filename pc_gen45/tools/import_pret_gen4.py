from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import urllib.request

HG_COMMIT = "0985e8718df4f25e64d6507d89c0c97c0d288981"
HG_SPECIES_URL = (
    "https://raw.githubusercontent.com/pret/pokeheartgold/"
    + HG_COMMIT
    + "/include/constants/species.h"
)
HG_ABILITIES_URL = (
    "https://raw.githubusercontent.com/pret/pokeheartgold/"
    + HG_COMMIT
    + "/include/constants/abilities.h"
)

DEFINE_RE = re.compile(r"^#define\s+SPECIES_([A-Z0-9_]+)\s+(\d+)\s*$")
ABILITY_RE = re.compile(r"^#define\s+ABILITY_([A-Z0-9_]+)\s+(\d+)\s*$")


def pretty(token: str) -> str:
    special = {
        "NIDORAN_F": "Nidoran♀",
        "NIDORAN_M": "Nidoran♂",
        "MR_MIME": "Mr. Mime",
        "MIME_JR": "Mime Jr.",
        "FARFETCHD": "Farfetch'd",
        "HO_OH": "Ho-Oh",
        "PORYGON_Z": "Porygon-Z",
    }
    if token in special:
        return special[token]
    return token.replace("_", " ").title()


def fetch_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read().decode("utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default="pc_gen45/data/species_gen4.json")
    ap.add_argument("--abilities-output", default="pc_gen45/data/abilities_gen4.json")
    args = ap.parse_args()

    text = fetch_text(HG_SPECIES_URL)
    species: dict[int, str] = {}
    for line in text.splitlines():
        m = DEFINE_RE.match(line)
        if not m:
            continue
        token, num = m.group(1), int(m.group(2))
        if 1 <= num <= 493:
            species[num] = pretty(token)

    if len(species) != 493:
        raise RuntimeError(f"expected 493 Gen4 species, got {len(species)}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({str(k): species[k] for k in sorted(species)}, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(species)} species to {out}")

    ability_text = fetch_text(HG_ABILITIES_URL)
    abilities: dict[int, str] = {}
    for line in ability_text.splitlines():
        m = ABILITY_RE.match(line)
        if not m:
            continue
        token, num = m.group(1), int(m.group(2))
        if 0 <= num <= 123:
            abilities[num] = pretty(token)

    if len(abilities) != 124:
        raise RuntimeError(f"expected 124 Gen4 abilities including None, got {len(abilities)}")

    ability_out = Path(args.abilities_output)
    ability_out.parent.mkdir(parents=True, exist_ok=True)
    ability_out.write_text(
        json.dumps({str(k): abilities[k] for k in sorted(abilities)}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {len(abilities)} abilities to {ability_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
