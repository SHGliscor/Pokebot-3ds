from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

# The hardware test is launched as a file under tools\.  Add the packaged
# bot root explicitly so imports work regardless of the caller's cwd.
BOT_ROOT = Path(__file__).resolve().parents[1]
if str(BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(BOT_ROOT))

from pokebot.common.bridge import Bridge
from pokebot.common.gen6_profiles import profile_from_game_info
from pokebot.common.xy_ram import read_trainer_ids, read_wild_decoded, WILD0


def main():
    ap = argparse.ArgumentParser(description='Read-only Pokémon X/Y normal-wild PK6 hardware validation.')
    ap.add_argument('host', nargs='?', default='192.168.0.28')
    ap.add_argument('--port', type=int, default=4952)
    ap.add_argument('--timeout', type=float, default=2.0)
    ap.add_argument('--wait', type=float, default=45.0, help='seconds to wait for a valid wild PK6')
    args = ap.parse_args()

    bridge = Bridge(args.host, args.port, args.timeout)
    gi = bridge.game_info()
    profile = profile_from_game_info(gi)
    print(f"GAME_INFO: {gi.get('process_name')} PID {gi.get('pid')} title={gi.get('title_id')}")
    if not profile or profile.get('family') != 'xy':
        raise SystemExit(f"REFUSED: expected Pokémon X/Y GAME_INFO, got {profile or gi}")

    tid, sid = read_trainer_ids(bridge)
    print(f"Detected {profile['name']} • trainer TID/SID {tid}/{sid}")
    print(f"Read-only wild PK6 address: 0x{WILD0:08X}")
    print("Start or enter a NORMAL single wild battle. No controller input will be sent.")
    print("Waiting for a checksum-valid wild PK6...\n")

    deadline = time.monotonic() + max(1.0, args.wait)
    last = None
    while time.monotonic() < deadline:
        try:
            p = read_wild_decoded(bridge, 0)
            last = p
            if p.get('valid') and p.get('checksum_valid'):
                trainer_match = int(p.get('tid', -1)) == tid and int(p.get('sid', -1)) == sid
                result = {
                    'timestamp': datetime.now().isoformat(timespec='seconds'),
                    'game_profile': profile,
                    'game_info': gi,
                    'wild_address': f'0x{WILD0:08X}',
                    'trainer_ids': {'tid': tid, 'sid': sid},
                    'trainer_match': trainer_match,
                    'wild_pk6': p,
                    'status': 'XY_NORMAL_WILD_READ_PASS' if trainer_match else 'XY_NORMAL_WILD_READ_TRAINER_MISMATCH',
                    'controller_inputs_sent': 0,
                    'ram_writes': 0,
                }
                outdir = Path(__file__).resolve().parents[1] / 'support'
                outdir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                out = outdir / f'xy_normal_wild_read_{stamp}.json'
                out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
                print(f"Species: {p.get('species_name')} ({p.get('species')})")
                print(f"Checksum valid: {p.get('checksum_valid')}")
                print(f"TID/SID match: {trainer_match}")
                print(f"Shiny: {p.get('is_shiny')}  shiny_xor={p.get('shiny_xor')}")
                print(f"RESULT: {result['status']}")
                print(f"Saved: {out}")
                return 0 if trainer_match else 2
        except Exception as exc:
            last = {'read_error': f'{type(exc).__name__}: {exc}'}
        time.sleep(0.20)

    print('TIMEOUT: no checksum-valid normal wild PK6 appeared before the deadline.')
    if last:
        print('Last observation:', json.dumps(last, ensure_ascii=False)[:1000])
    return 3


if __name__ == '__main__':
    raise SystemExit(main())
