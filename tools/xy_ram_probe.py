from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pokebot.common.bridge import Bridge
from pokebot.common.gen6_profiles import profile_from_game_info
from pokebot.common.xy_ram import (
    PARTY0, PARTY_STRIDE, TRAINER_IDS, WILD0, read_party_decoded, read_trainer_ids,
)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    host = argv[0] if argv else input("3DS IP address: ").strip()
    bridge = Bridge(host=host, port=4952, timeout=2.5)
    gi = bridge.game_info()
    profile = profile_from_game_info(gi)
    print(f"GAME_INFO: {gi}")
    if not profile or profile.get("family") != "xy":
        print("FAIL: Pokémon X/Y was not detected. No XY RAM addresses were read.")
        return 2

    print(f"Detected: {profile['name']}  {profile['title_id']}  {profile['process']}")
    tid, sid = read_trainer_ids(bridge)
    print(f"Trainer IDs @ 0x{TRAINER_IDS:08X}: TID={tid} SID={sid}")
    party = read_party_decoded(bridge)
    valid = 0
    for p in party:
        slot = p.get("slot")
        if p.get("valid") and p.get("checksum_valid"):
            valid += 1
            print(
                f"Slot {slot}: {p.get('species_name')} #{p.get('species')} "
                f"PID={p.get('pid')} {p.get('nature')} "
                f"IVs={p.get('ivs')} shiny={p.get('is_shiny')} checksum=PASS"
            )
        else:
            print(
                f"Slot {slot}: empty/invalid "
                f"species={p.get('species')} checksum={p.get('checksum_valid')}"
            )

    report = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "host": host,
        "game_info": gi,
        "game_profile": profile,
        "addresses": {
            "trainer_ids": f"0x{TRAINER_IDS:08X}",
            "party": f"0x{PARTY0:08X}",
            "party_stride": PARTY_STRIDE,
            "wild": f"0x{WILD0:08X}",
        },
        "party": party,
        "valid_party_slots": valid,
    }
    out = ROOT / f"xy_ram_probe_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Report: {out.name}")
    if valid == 0:
        print("FAIL: no checksum-valid PK6 party slots. Address/revision needs investigation.")
        return 3
    print("PASS: at least one checksum-valid X/Y party PK6 was decoded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
