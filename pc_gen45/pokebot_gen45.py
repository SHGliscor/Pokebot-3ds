from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path
import sys
import time

from backend.melonds_file import MelonDSFileBackend
from backend.melonds_udp import MelonDSUDPBackend
from gen4.pk4 import PARTY_SIZE, parse_pk4, scan_pk4, newly_seen

MAIN_RAM_BASE = 0x02000000
MAIN_RAM_SIZE = 0x00400000

HG_EU_V10_SHA1 = "eb47ab4ba0326ae842135f62c7ec68cf85c9785f"
HG_STARTERS = {152, 155, 158}
HG_STARTER_ORDER = (152, 155, 158)
# Confirmed on HeartGold Europe v10 by the live three-starter RAM probe.
HG_EU_STARTER_BASE = 0x022BBE84
HG_EN_ANCHOR_PTR = 0x021D4158
HG_STARTER_FROM_ANCHOR = 0x1BC00


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
    level = ""  # Starter buffer party-status bytes are encrypted; do not show raw level.
    return (
        f"0x{mon.address:08X} {name}{level}{shiny} "
        f"PID={mon.pid:08X} SV={mon.shiny_value} "
        f"Nature={mon.nature} Ability={mon.ability} IVs={ivs} "
        f"HP={mon.hidden_power_type}/{mon.hidden_power_power}"
    )


def make_backend(args):
    if args.backend == "file":
        return MelonDSFileBackend(args.ipc, timeout=max(5.0, args.timeout))
    return MelonDSUDPBackend(args.host, args.port, timeout=max(0.25, args.timeout))


def cmd_ping(args) -> int:
    backend = make_backend(args)
    print("PING:", backend.ping())
    return 0


def cmd_input_test(args) -> int:
    backend = make_backend(args)
    key = args.key
    print(f"Sending {key} for {args.frames} frame(s).")
    print("Keep melonDS UNFOCUSED while this runs.")
    backend.pulse(key, args.frames)
    print("Input command sent.")
    return 0


def cmd_display(args) -> int:
    backend = make_backend(args)
    enabled = args.state == "on"
    backend.set_display(enabled)
    print(f"melonDS display rendering: {'ON' if enabled else 'OFF'}")
    return 0


def cmd_audio(args) -> int:
    backend = make_backend(args)
    enabled = args.state == "on"
    backend.set_audio(enabled)
    print(f"melonDS bot audio: {'ON' if enabled else 'MUTED'}")
    return 0


def cmd_scan(args) -> int:
    backend = make_backend(args)
    names = load_species_names()
    print("Reading 4 MiB ARM9 main RAM...")
    ram = backend.read_block(MAIN_RAM_BASE, MAIN_RAM_SIZE)
    print("Scanning for checksum-valid PK4 structures...")
    mons = scan_pk4(ram, base_address=MAIN_RAM_BASE)
    print(f"Found {len(mons)} valid PK4 structure(s).")
    for mon in mons:
        print(mon_line(mon, names))
    return 0



def _read_u32(backend, address: int) -> int:
    return int.from_bytes(backend.read_block(address, 4), "little")


def _hgss_live_starter_base(backend) -> int | None:
    """
    Resolve the HGSS starter buffer from the game's runtime anchor.

    Pokebot-NDS uses:
        anchor = *(0x021D4158 + language_offset)
        starter_data = anchor + 0x1BC00

    English HG/SS uses language_offset 0. This explains the small address
    movement observed between resets and avoids scanning all 4 MiB of RAM.
    """
    anchor = _read_u32(backend, HG_EN_ANCHOR_PTR)
    if not (MAIN_RAM_BASE <= anchor < MAIN_RAM_BASE + MAIN_RAM_SIZE):
        return None
    base = anchor + HG_STARTER_FROM_ANCHOR
    if not (MAIN_RAM_BASE <= base <= MAIN_RAM_BASE + MAIN_RAM_SIZE - PARTY_SIZE * 3):
        return None
    return base


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


def _read_hgss_live_starters(backend):
    base = _hgss_live_starter_base(backend)
    if base is None:
        return None, None
    return _read_hgss_starter_triplet(backend, base), base


def _hgss_starter_ui_ready(backend, base: int) -> bool:
    """
    HGSS starter-carousel guard ported from Pokebot-NDS.

    It advances text while:
        *(starter_data - 8) != 0 OR *(starter_data - 4) == 0

    Therefore the safe stop condition is:
        *(starter_data - 8) == 0 AND *(starter_data - 4) != 0
    """
    raw = backend.read_block(base - 8, 8)
    before8 = int.from_bytes(raw[:4], "little")
    before4 = int.from_bytes(raw[4:8], "little")
    return before8 == 0 and before4 != 0


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
    # Preferred route: resolve the moving HGSS starter buffer from the live
    # runtime anchor. This is a tiny 4-byte pointer read + 708-byte PK4 read.
    try:
        triplet, live_base = _read_hgss_live_starters(backend)
    except (RuntimeError, TimeoutError):
        triplet, live_base = None, None
    if triplet is not None:
        return triplet, live_base, "pointer"

    # Diagnostic compatibility with a manually supplied/previously observed base.
    try:
        triplet = _read_hgss_starter_triplet(backend, base_hint)
    except (RuntimeError, TimeoutError):
        triplet = None
    if triplet is not None:
        return triplet, base_hint, "hint"

    if not allow_full_scan:
        return None, base_hint, "none"

    # Last-resort diagnostic only. Normal hunting never reaches this path.
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
    input_interval: float,
    boot_settle: float,
    jitter_boost: float = 0.0,
):
    """
    Reach the HGSS starter carousel without selecting a starter.

    Boot/title handling and in-game text are separate phases:
      1) While the HGSS runtime anchor is unavailable, send robust A presses
         through the title/continue screens.
      2) Once the save is loaded and the live starter pointer resolves, use
         HGSS' two pre-buffer state words as the exact safe stop guard.
      3) When the guard says the carousel is ready, release A, return to normal
         speed and allow 9 normal frames for all three PK4s to finish writing.

    This mirrors Pokebot-NDS' HGSS starter strategy instead of blindly tapping
    A based on wall-clock sleeps.
    """
    cycle_started = time.monotonic()
    deadline = cycle_started + timeout

    jitter = 0.0
    if after_reset:
        jitter = random.uniform(reset_delay_min, reset_delay_max) + jitter_boost
        if boot_settle > 0:
            time.sleep(boot_settle)
        if jitter > 0:
            time.sleep(jitter)
        print(
            f"Boot settle={boot_settle:.2f}s RNG jitter={jitter:.2f}s"
            + (f" (adaptive +{jitter_boost:.2f}s)" if jitter_boost else "")
        )

    fast_forward_enabled = False
    try:
        # Do not fast-forward the HGSS title/continue input phase. A 4-frame
        # pulse at 1000 FPS is only a few milliseconds in real time and proved
        # intermittent at "Touch to Start". Keep title input at normal speed
        # and reproduce a physical DS-style press/release cadence instead.
        backend.set_fast_forward(False)

        loaded_reported = False
        boot_input_cycle = 0

        while time.monotonic() < deadline:
            try:
                live_base = _hgss_live_starter_base(backend)
            except (RuntimeError, TimeoutError):
                live_base = None

            if live_base is None:
                # The uploaded retail-behaviour video confirms A alone
                # advances HGSS' "Touch to Start" screen. Reproduce a deliberate
                # physical press: ~200 ms down at 60 FPS, then ~250 ms released.
                # Do not mix Start into this phase; doing so only adds timing
                # variance between title and Continue.
                boot_input_cycle += 1
                backend.pulse("A", 12)
                time.sleep(0.25)
                continue

            if not loaded_reported:
                print(f"Save/runtime anchor available after {time.monotonic() - cycle_started:.2f}s.")
                loaded_reported = True

            # Title input ran at normal speed, so just clear any boot input
            # before the exact HGSS pre-starter guard takes over.
            backend.reset_input()
            time.sleep(0.05)

            try:
                ready = _hgss_starter_ui_ready(backend, live_base)
            except (RuntimeError, TimeoutError):
                ready = False

            if ready:
                backend.reset_input()

                # Pokebot-NDS waits 9 frames after the guard changes so all
                # three starters are fully written. At 60 FPS this is 150 ms.
                time.sleep(9.0 / 60.0)

                mons = _read_hgss_starter_triplet(backend, live_base)
                if mons is not None:
                    # One second read guards against a partially-updated trio.
                    time.sleep(0.02)
                    stable = _read_hgss_starter_triplet(backend, live_base)
                    if stable is not None and _starter_set_identity(stable) == _starter_set_identity(mons):
                        return stable, live_base, "pointer", time.monotonic() - cycle_started

                # Guard became ready but data was not stable yet; do not press A.
                # Simply allow the game a few more frames to finish the writes.
                time.sleep(0.05)
                continue

            # Pokebot-NDS progress_text() holds A for 5-20 frames then releases
            # for 5. A shorter deterministic pulse is enough here while still
            # giving the game a clean release edge before we re-check the guard.
            backend.pulse("A", 6)
            time.sleep(max(input_interval, 0.12))

        return None, base_hint, "timeout", time.monotonic() - cycle_started
    finally:
        try:
            backend.reset_input()
        except Exception:
            pass
        if fast_forward_enabled:
            try:
                backend.set_fast_forward(False)
            except Exception:
                pass


def cmd_hgss_starter_check(args) -> int:
    backend = make_backend(args)
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
    backend = make_backend(args)
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
    duplicate_streak = 0
    resets = 0
    after_reset = False

    display_disabled = False
    audio_disabled = False

    try:
        if not args.show_display:
            backend.set_display(False)
            display_disabled = True
            print("melonDS display rendering: OFF (bot headless mode)")
        if not args.keep_audio:
            backend.set_audio(False)
            audio_disabled = True
            print("melonDS sound: MUTED for hunt")
        print()

        try:
            while True:
                jitter_boost = min(duplicate_streak * args.duplicate_jitter_step, args.duplicate_jitter_max)
                mons, resolved_base, source, cycle_seconds = _reach_hgss_starter_screen(
                    backend,
                    base_hint=base_hint,
                    timeout=args.navigation_timeout,
                    after_reset=after_reset,
                    reset_delay_min=args.reset_delay_min,
                    reset_delay_max=args.reset_delay_max,
                    input_interval=args.input_interval,
                    boot_settle=args.boot_settle,
                    jitter_boost=jitter_boost,
                )
                if mons is None:
                    backend.reset_input()
                    print("SAFETY HOLD: starter screen was not reached before timeout.")
                    print("No further input or resets will be sent.")
                    return 3

                print(f"Starter screen reacquired in {cycle_seconds:.2f}s.")
                base_hint = resolved_base
                identity = _starter_set_identity(mons)
                if identity in seen_sets:
                    duplicates += 1
                    duplicate_streak += 1
                    print(
                        f"Duplicate starter set detected (duplicate #{duplicates}, "
                        f"streak {duplicate_streak})."
                    )
                else:
                    duplicate_streak = 0
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
                    if display_disabled:
                        backend.set_display(True)
                        display_disabled = False
                    if audio_disabled:
                        backend.set_audio(True)
                        audio_disabled = False
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
    finally:
        # Always restore the emulator presentation on target, safety hold,
        # Ctrl+C, normal max-reset exit, or an unexpected exception.
        if display_disabled:
            try:
                backend.set_display(True)
            except Exception:
                pass
        if audio_disabled:
            try:
                backend.set_audio(True)
            except Exception:
                pass



def cmd_starter_probe(args) -> int:
    backend = make_backend(args)
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
    p.add_argument("--backend", choices=["native", "file"], default="native")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4953)
    p.add_argument("--ipc", default="pokebot_ipc", help="Legacy file/Lua IPC directory")
    p.add_argument("--timeout", type=float, default=2.0)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("ping")
    s.set_defaults(func=cmd_ping)

    s = sub.add_parser("input-test")
    s.add_argument("--key", default="A")
    s.add_argument("--frames", type=int, default=6)
    s.set_defaults(func=cmd_input_test)

    s = sub.add_parser("display")
    s.add_argument("state", choices=["on", "off"])
    s.set_defaults(func=cmd_display)

    s = sub.add_parser("audio")
    s.add_argument("state", choices=["on", "off"])
    s.set_defaults(func=cmd_audio)

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
    s.add_argument("--show-display", action="store_true", help="Keep melonDS screen rendering during the hunt")
    s.add_argument("--keep-audio", action="store_true", help="Keep melonDS game audio enabled during the hunt")
    s.add_argument("--navigation-timeout", type=float, default=45.0)
    s.add_argument("--boot-settle", type=float, default=0.35)
    s.add_argument("--reset-delay-min", type=float, default=0.00)
    s.add_argument("--reset-delay-max", type=float, default=0.20)
    s.add_argument("--duplicate-jitter-step", type=float, default=0.40)
    s.add_argument("--duplicate-jitter-max", type=float, default=2.00)
    s.add_argument(
        "--input-interval",
        type=float,
        default=0.035,
        help="Minimum wall delay between boot/title input checks (default: 0.035)",
    )
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
