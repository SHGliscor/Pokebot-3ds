from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

from backend.melonds_file import MelonDSFileBackend
from gen4.pk4 import scan_pk4, newly_seen

MAIN_RAM_BASE = 0x02000000
MAIN_RAM_SIZE = 0x00400000

HG_EU_V10_SHA1 = "eb47ab4ba0326ae842135f62c7ec68cf85c9785f"
HG_STARTERS = {152, 155, 158}


def load_species_names() -> dict[int, str]:
    path = Path(__file__).with_name("data") / "species_gen4.json"
    if not path.exists():
        return {}
    obj = json.loads(path.read_text(encoding="utf-8"))
    return {int(k): str(v) for k, v in obj.items()}


def mon_line(mon, names: dict[int, str]) -> str:
    name = names.get(mon.species, f"Species {mon.species}")
    ivs = "/".join(map(str, mon.ivs))
    shiny = " SHINY" if mon.shiny else ""
    level = f" Lv{mon.level}" if mon.level else ""
    return (
        f"0x{mon.address:08X} {name}{level}{shiny} "
        f"PID={mon.pid:08X} SV={mon.shiny_value} "
        f"Nature={mon.nature} Ability={mon.ability} IVs={ivs} "
        f"HP={mon.hidden_power_type}/{mon.hidden_power_power}"
    )


def cmd_ping(args) -> int:
    backend = MelonDSFileBackend(args.ipc)
    print("PING:", backend.ping())
    return 0


def cmd_scan(args) -> int:
    backend = MelonDSFileBackend(args.ipc, timeout=max(5.0, args.timeout))
    names = load_species_names()
    print("Reading 4 MiB ARM9 main RAM...")
    ram = backend.read_block(MAIN_RAM_BASE, MAIN_RAM_SIZE)
    print("Scanning for checksum-valid PK4 structures...")
    mons = scan_pk4(ram, base_address=MAIN_RAM_BASE)
    print(f"Found {len(mons)} valid PK4 structure(s).")
    for mon in mons:
        print(mon_line(mon, names))
    return 0


def cmd_starter_probe(args) -> int:
    backend = MelonDSFileBackend(args.ipc, timeout=max(5.0, args.timeout))
    names = load_species_names()

    print("Taking BEFORE snapshot...")
    before_ram = backend.read_block(MAIN_RAM_BASE, MAIN_RAM_SIZE)
    before = scan_pk4(before_ram, base_address=MAIN_RAM_BASE)

    print()
    print("Now choose Chikorita, Cyndaquil, or Totodile in HeartGold.")
    print("When the game has returned control to you, press Enter here.")
    input()

    print("Taking AFTER snapshot...")
    after_ram = backend.read_block(MAIN_RAM_BASE, MAIN_RAM_SIZE)
    after = scan_pk4(after_ram, base_address=MAIN_RAM_BASE)
    new = newly_seen(before, after)
    starters = [m for m in new if m.species in HG_STARTERS]

    print(f"Before: {len(before)} valid PK4; after: {len(after)}; new identities: {len(new)}")
    if starters:
        print("Starter candidate(s):")
        for mon in starters:
            print("  " + mon_line(mon, names))
        out = Path(args.output)
        out.write_text(json.dumps([
            {
                "address": f"0x{m.address:08X}",
                "species": m.species,
                "pid": f"0x{m.pid:08X}",
                "ot_id": f"0x{m.ot_id:08X}",
                "shiny": m.shiny,
                "shiny_value": m.shiny_value,
                "nature": m.nature,
                "ability": m.ability,
                "ivs": list(m.ivs),
            }
            for m in starters
        ], indent=2), encoding="utf-8")
        print(f"Saved: {out}")
        return 0

    print("No newly-created Johto starter was found.")
    print("New checksum-valid candidates:")
    for mon in new[:50]:
        print("  " + mon_line(mon, names))
    return 2


def cmd_hash(args) -> int:
    path = Path(args.rom)
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    digest = h.hexdigest()
    print(digest)
    if digest == HG_EU_V10_SHA1:
        print("MATCH: Pokemon HeartGold Europe v10 target")
        return 0
    print("Different ROM revision; data tables still apply, but runtime probing must be treated separately.")
    return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Pokebot Gen4/5 PC melonDS proof")
    p.add_argument("--ipc", default="pokebot_ipc", help="IPC directory shared with melonDS Lua")
    p.add_argument("--timeout", type=float, default=10.0)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("ping")
    s.set_defaults(func=cmd_ping)

    s = sub.add_parser("scan")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("starter-probe")
    s.add_argument("--output", default="heartgold_starter_probe.json")
    s.set_defaults(func=cmd_starter_probe)

    s = sub.add_parser("hash-rom")
    s.add_argument("rom")
    s.set_defaults(func=cmd_hash)

    return p


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
