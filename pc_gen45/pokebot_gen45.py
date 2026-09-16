from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path
import sys
import time

from backend.melonds_file import MelonDSFileBackend
from gen4.pk4 import PARTY_SIZE, parse_pk4, scan_pk4, newly_seen

MAIN_RAM_BASE = 0x02000000
MAIN_RAM_SIZE = 0x00400000

HG_EU_V10_SHA1 = "eb47ab4ba0326ae842135f62c7ec68cf85c9785f"
HG_STARTERS = {152, 155, 158}
HG_STARTER_ORDER = (152, 155, 158)
# Confirmed on HeartGold Europe v10 by the live three-starter RAM probe.
HG_EU_STARTER_BASE = 0x022BBE84


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


def cmd_input_test(args) -> int:
    backend = MelonDSFileBackend(args.ipc, timeout=max(5.0, args.timeout))
    key = args.key
    print(f"Sending {key} for {args.frames} frame(s).")
    print("Keep melonDS UNFOCUSED while this runs.")
    backend.pulse(key, args.frames)
    print("Input command sent.")
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



def _read_hgss_starter_triplet(backend, base: int):
    """Read and validate the three contiguous HGSS starter PartyPokemon structs."""
    raw = backend.read_block(base, PARTY_SIZE * 3)
    mons = []
    for i, expected_species in enumerate(HG_STARTER_ORDER):
        off = i * PARTY_SIZE
        mon = parse_pk4(raw[off:off + PARTY_SIZE], address=base + off)
        if mon is None or mon.species != expected_species:
            return None
        mons.append(mon)
    return tuple(mons)


def _find_hgss_starter_triplet_in_ram(ram: bytes):
    """Fallback discovery: find 152/155/158 exactly 0xEC apart with valid checksums."""
    mons = scan_pk4(
        ram,
        base_address=MAIN_RAM_BASE,
        stride=4,
        species=set(HG_STARTER_ORDER),
    )
    by_address = {m.address: m for m in mons}
    for mon in mons:
        if mon.species != HG_STARTER_ORDER[0]:
            continue
        base = mon.address
        triplet = tuple(by_address.get(base + i * PARTY_SIZE) for i in range(3))
        if all(triplet) and tuple(m.species for m in triplet) == HG_STARTER_ORDER:
            return triplet
    return None


def _locate_hgss_starters(backend, base_hint: int, *, allow_full_scan: bool = True):
    try:
        triplet = _read_hgss_starter_triplet(backend, base_hint)
    except (RuntimeError, TimeoutError):
        triplet = None
    if triplet is not None:
        return triplet, base_hint, "fast"

    if not allow_full_scan:
        return None, base_hint, "none"

    ram = backend.read_block(MAIN_RAM_BASE, MAIN_RAM_SIZE)
    triplet = _find_hgss_starter_triplet_in_ram(ram)
    if triplet is None:
        return None, base_hint, "none"
    return triplet, triplet[0].address, "scan"


def _starter_set_identity(mons) -> tuple[tuple[int, int, int], ...]:
    return tuple(m.identity for m in mons)


def _print_starter_set(mons, names: dict[int, str], *, prefix: str = "  ") -> None:
    for mon in mons:
        print(prefix + mon_line(mon, names))


def _sound_target() -> None:
    try:
        import winsound
        for frequency in (988, 1319, 1568):
            winsound.Beep(frequency, 180)
    except Exception:
        pass


def _reach_hgss_starter_screen(
    backend,
    *,
    base_hint: int,
    timeout: float,
    after_reset: bool,
    reset_delay_min: float,
    reset_delay_max: float,
):
    """
    Reach the HGSS starter screen without ever pressing A after a valid starter
    triplet becomes readable.

    HGSS creates all three starters before the player chooses one.  On reset we
    wait a randomized boot delay (mirroring Pokebot-NDS' duplicate-seed
    mitigation), press Start once, then pulse A while validating the starter
    structures between every input.
    """
    deadline = time.monotonic() + timeout
    next_scan = time.monotonic()

    if after_reset:
        delay = random.uniform(reset_delay_min, reset_delay_max)
        print(f"Boot RNG delay: {delay:.2f}s")
        time.sleep(delay)
        # One Start is sufficient for the title screen; subsequent progression
        # uses A only so we cannot accidentally open the in-game Start menu.
        backend.pulse("Start", 2)
        time.sleep(0.45)

    while time.monotonic() < deadline:
        now = time.monotonic()
        allow_scan = now >= next_scan
        mons, resolved_base, source = _locate_hgss_starters(
            backend, base_hint, allow_full_scan=allow_scan
        )
        if allow_scan:
            next_scan = time.monotonic() + 2.0

        if mons is not None:
            # Re-read after a short settling interval.  This replaces the
            # Pokebot-NDS 9-frame wait and ensures all three PK4s are stable.
            time.sleep(0.15)
            stable = _read_hgss_starter_triplet(backend, resolved_base)
            if stable is not None and _starter_set_identity(stable) == _starter_set_identity(mons):
                return stable, resolved_base, source

        # No valid 152/155/158 triplet exists yet, so it is safe to advance.
        backend.pulse("A", 2)
        time.sleep(0.40)

    return None, base_hint, "timeout"


def cmd_hgss_starter_check(args) -> int:
    backend = MelonDSFileBackend(args.ipc, timeout=max(5.0, args.timeout))
    names = load_species_names()
    mons, base, source = _locate_hgss_starters(
        backend, int(args.base, 0), allow_full_scan=True
    )
    if mons is None:
        print("No valid HGSS starter triplet is currently visible in RAM.")
        print("Open Elm's starter selection screen and run this command again.")
        return 2

    print(f"HGSS starter set @ 0x{base:08X} ({source} path):")
    _print_starter_set(mons, names)
    if any(m.shiny for m in mons):
        print("SHINY STARTER PRESENT - do not reset.")
        _sound_target()
        return 10
    print("No shiny among the three starters.")
    return 0


def cmd_hgss_starter_hunt(args) -> int:
    backend = MelonDSFileBackend(args.ipc, timeout=max(5.0, args.timeout))
    names = load_species_names()
    base_hint = int(args.base, 0)

    target_species = set(HG_STARTERS)
    if args.target:
        lookup = {
            "chikorita": 152,
            "cyndaquil": 155,
            "totodile": 158,
        }
        target_species = {lookup[x.lower()] for x in args.target}

    print("HGSS three-starter hunter")
    print("Checks Chikorita, Cyndaquil and Totodile before selection.")
    print("Start from your save at/near Elm's starter machine.")
    print("Targets:", ", ".join(names.get(x, str(x)) for x in sorted(target_species)))
    print("Press Ctrl+C to stop.")
    print()

    seen_sets: set[tuple[tuple[int, int, int], ...]] = set()
    sets_seen = 0
    duplicates = 0
    resets = 0
    after_reset = False

    try:
        while True:
            mons, resolved_base, source = _reach_hgss_starter_screen(
                backend,
                base_hint=base_hint,
                timeout=args.navigation_timeout,
                after_reset=after_reset,
                reset_delay_min=args.reset_delay_min,
                reset_delay_max=args.reset_delay_max,
            )
            if mons is None:
                backend.reset_input()
                print("SAFETY HOLD: starter screen was not reached before timeout.")
                print("No further input or resets will be sent.")
                return 3

            base_hint = resolved_base
            identity = _starter_set_identity(mons)
            if identity in seen_sets:
                duplicates += 1
                print(f"Duplicate starter set detected (duplicate #{duplicates}).")
            else:
                seen_sets.add(identity)
                sets_seen += 1
                print(
                    f"Set {sets_seen} @ 0x{base_hint:08X} [{source}] "
                    f"({sets_seen * 3} starters checked)"
                )
                _print_starter_set(mons, names)

            targets = [m for m in mons if m.species in target_species and m.shiny]
            if targets:
                backend.reset_input()
                print()
                print("=" * 68)
                print("TARGET FOUND - STOPPED BEFORE STARTER SELECTION")
                _print_starter_set(targets, names)
                print(
                    f"Sets={sets_seen}  Starters={sets_seen * 3}  "
                    f"Resets={resets}  Duplicates={duplicates}"
                )
                print("=" * 68)
                _sound_target()
                return 10

            if args.max_resets and resets >= args.max_resets:
                backend.reset_input()
                print(
                    f"Reached max resets ({args.max_resets}). "
                    f"Sets={sets_seen}, starters={sets_seen * 3}, duplicates={duplicates}."
                )
                return 0

            print("No target in this set; resetting...")
            backend.reset_input()
            backend.reset_game()
            resets += 1
            after_reset = True

    except KeyboardInterrupt:
        try:
            backend.reset_input()
        except Exception:
            pass
        print()
        print(
            f"Stopped. Sets={sets_seen}, starters={sets_seen * 3}, "
            f"resets={resets}, duplicates={duplicates}."
        )
        return 130



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

    s = sub.add_parser("input-test")
    s.add_argument("--key", default="A")
    s.add_argument("--frames", type=int, default=6)
    s.set_defaults(func=cmd_input_test)

    s = sub.add_parser("scan")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("starter-probe")
    s.add_argument("--output", default="heartgold_starter_probe.json")
    s.set_defaults(func=cmd_starter_probe)

    s = sub.add_parser("hgss-starter-check")
    s.add_argument("--base", default=f"0x{HG_EU_STARTER_BASE:08X}")
    s.set_defaults(func=cmd_hgss_starter_check)

    s = sub.add_parser("hgss-starter-hunt")
    s.add_argument("--base", default=f"0x{HG_EU_STARTER_BASE:08X}")
    s.add_argument(
        "--target",
        action="append",
        choices=["chikorita", "cyndaquil", "totodile"],
        help="Limit shiny targets; repeat for multiple. Default: all three.",
    )
    s.add_argument("--max-resets", type=int, default=0, help="0 = unlimited")
    s.add_argument("--navigation-timeout", type=float, default=45.0)
    s.add_argument("--reset-delay-min", type=float, default=5.0)
    s.add_argument("--reset-delay-max", type=float, default=10.0)
    s.set_defaults(func=cmd_hgss_starter_hunt)

    s = sub.add_parser("hash-rom")
    s.add_argument("rom")
    s.set_defaults(func=cmd_hash)

    return p


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
