from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pokebot.static.dexnav_target_nav import decode_target_block, steering_command


def main():
    baseline = bytes(0x18)
    b = decode_target_block(baseline)
    assert not b['active']

    raw = bytearray(0x18)
    struct.pack_into('<fff', raw, 0, 1701.0, 2.0, 2331.0)
    struct.pack_into('<I', raw, 0x0C, 1)
    struct.pack_into('<I', raw, 0x10, 0x08D3B84C)
    struct.pack_into('<I', raw, 0x14, 0x08D3B920)
    t = decode_target_block(bytes(raw))
    assert t['active'] and t['x'] == 1701.0 and t['z'] == 2331.0

    c = steering_command(1791.0, 2349.0, t['x'], t['z'])
    assert c['x'] == -0.40 and c['y'] == 0.16 and c['hold_ms'] == 2500
    assert 91.0 < c['distance'] < 92.0

    near = steering_command(1710.85266, 2332.55713, t['x'], t['z'])
    assert 9.0 < near['distance'] < 11.0
    assert near['x'] < 0

    struct.pack_into('<I', raw, 0x14, 0)
    gone = decode_target_block(bytes(raw))
    assert not gone['active']
    print('HF90 DEXNAV TARGET STEERING SELF TEST: PASS')

if __name__ == '__main__':
    main()
