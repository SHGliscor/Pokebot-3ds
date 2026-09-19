from __future__ import annotations

"""Recover a caught ORAS Pokémon from a mapped post-capture screen.

This is a narrow operator tool for cases where the main worker already entered
SHINY_HOLD after the catch. It never writes game RAM. Controller input is sent
only when the live Alpha Sapphire flow word is one of the hardware-mapped
post-capture states (Pokédex/nickname), and the shared production recovery state
machine rechecks the hardware-proven screen object/phase before every input.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pokebot.wild.battle_bag_throw import (
    POST_CAPTURE_CAPTURE_FLOWS,
    POST_CAPTURE_FLOW_POKEDEX,
    POST_CAPTURE_FLOW_POKEDEX_PHASES,
    POST_CAPTURE_FLOW_NICKNAME_REGISTERED,
    POST_CAPTURE_FLOW_NICKNAME_UNREGISTERED,
    clear_post_capture_screens,
)
from pokebot.wild.post_capture_validator import clear_post_capture_validator
from pokebot.wild.validated_loader import load_walk_v0p23
from qt_ui.appdata_store import get_profile_paths
from qt_ui.settings_store import load_settings

AS_TITLE_ID = 0x000400000011C500
FLOW_ADDR = 0x081FB390
BATTLE_ADDR = 0x081FB478
OUTER_PTR_ADDR = 0x081FB384
ENTRY_FLOWS = {
    *POST_CAPTURE_FLOW_POKEDEX_PHASES,
    POST_CAPTURE_FLOW_NICKNAME_REGISTERED,
    POST_CAPTURE_FLOW_NICKNAME_UNREGISTERED,
}


def hx(v: int) -> str:
    return f"0x{int(v) & 0xFFFFFFFF:08X}"


def main() -> int:
    profile = get_profile_paths()
    settings = load_settings(profile.settings_path)
    host = settings["three_ds_ip"]
    timeout = min(float(settings.get("bridge_timeout_s", 1.5)), 1.5)
    _, core, _ = load_walk_v0p23()
    br = core.Bridge(host, timeout=timeout)

    report = {
        "tool": "Pokebot3DS-CFW Post-Capture Rescue v0p43AU",
        "started": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "ram_writes": False,
        "host": host,
    }

    gi = br.game_info()
    report["game_info"] = gi
    if int(gi.get("title_id", 0)) != AS_TITLE_ID:
        raise SystemExit("Alpha Sapphire is not the active bridge process: " + str(gi))
    caps = br.input_ping()
    report["input_caps"] = caps
    if not caps.get("hid_pulse"):
        raise SystemExit("Acknowledged HID pulse capability unavailable: " + str(caps))
    br.release_all()

    battle = br.u32(BATTLE_ADDR)
    flow = br.u32(FLOW_ADDR)
    outer = br.u32(OUTER_PTR_ADDR)
    report["initial"] = {"battle": hx(battle), "flow": hx(flow), "outer_ptr": hx(outer)}

    print("=" * 72)
    print("Pokebot3DS-CFW POST-CAPTURE RESCUE v0p43AU")
    print("Alpha Sapphire 1.4 | NO RAM WRITES")
    print(f"Live state: battle={hx(battle)} flow={hx(flow)} outer={hx(outer)}")
    print("=" * 72)

    if flow not in ENTRY_FLOWS:
        report["result"] = "REFUSED_UNMAPPED_ENTRY_FLOW"
        print("REFUSED: the game is not on a mapped Pokédex/nickname entry flow.")
        return save(profile, report, 2)

    print("Mapped post-capture flow confirmed. Running phase-tolerant recovery sequence...")
    try:
        result = clear_post_capture_validator(
            br,
            core,
            check_stop=lambda: None,
            log=lambda msg: print(msg, flush=True),
        )
        report["recovery"] = result
        report["result"] = result.get("result")
        return save(profile, report, 0)
    except Exception as exc:
        report["result"] = "RECOVERY_FAILED"
        report["error"] = f"{type(exc).__name__}: {exc}"
        print(report["error"])
        return save(profile, report, 2)


def save(profile, report: dict, code: int) -> int:
    report["finished"] = datetime.now().astimezone().isoformat(timespec="milliseconds")
    out_dir = profile.root / "support"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = out_dir / f"post_capture_rescue_{stamp}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    txt = out.with_suffix(".txt")
    txt.write_text(
        "Pokebot3DS-CFW Post-Capture Rescue v0p43AU\n"
        f"Result: {report.get('result')}\n"
        f"Initial: {report.get('initial')}\n"
        f"Error: {report.get('error')}\n"
        f"RAM writes: False\n"
        f"JSON: {out}\n",
        encoding="utf-8",
    )
    print(f"Report: {out}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
