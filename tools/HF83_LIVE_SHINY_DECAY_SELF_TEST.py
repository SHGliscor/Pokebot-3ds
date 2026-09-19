from pokebot.common.gen6_rng_tracker import (
    LiveMT, find_next_raw_shiny_frame, gen6_tsv,
    read_live_locked_shiny_countdown,
)
import struct

ORAS_INDEX = 0x08C59E44
ORAS_TABLE = 0x08C59E48
ORAS_IDS = 0x08C81340
TID, SID = 12345, 54321
TSV = gen6_tsv(TID, SID)


def snap_from_rng(rng):
    return {
        'available': True,
        'family': 'oras',
        'index': rng.index,
        'normalized_index': rng.index,
        'state': rng.state.copy(),
        'tid': TID,
        'sid': SID,
        'tsv': TSV,
    }


class CursorBridge:
    def __init__(self, rng):
        self.rng = rng
    def read(self, address, length):
        if address == ORAS_INDEX and length == 2:
            return struct.pack('<H', self.rng.index)
        if address == ORAS_IDS and length == 4:
            return struct.pack('<HH', TID, SID)
        if address == ORAS_TABLE + self.rng.index * 4 and length == 4:
            return struct.pack('<I', self.rng.current_raw_state())
        raise AssertionError((hex(address), length, self.rng.index))

rng = LiveMT.from_seed(0xAABBCCDD)
first = find_next_raw_shiny_frame(snap_from_rng(rng), 250000)
assert first['available'], first
start_distance = first['frames_away']
tracking = {
    'family': 'oras',
    'state': rng.state.copy(),
    'index': rng.index,
    'remaining': start_distance,
    'pid': first['pid'],
    'tid': TID,
    'sid': SID,
    'tsv': TSV,
}

advance = min(173, start_distance - 1)
for _ in range(advance):
    rng.next_uint()
result = read_live_locked_shiny_countdown(CursorBridge(rng), 'oras', tracking_state=tracking)
assert result['available'], result
assert result['pid'] == first['pid']
assert result['frames_away'] == start_distance - advance, (start_distance, advance, result)
assert result['countdown_delta'] == advance
assert result['cursor_mode'] == 'fast_cursor'

# Repeat from the returned local state to prove consecutive ticks keep decaying.
tracking2 = result['tracking_state']
advance2 = min(91, result['frames_away'] - 1)
for _ in range(advance2):
    rng.next_uint()
result2 = read_live_locked_shiny_countdown(CursorBridge(rng), 'oras', tracking_state=tracking2)
assert result2['frames_away'] == result['frames_away'] - advance2
assert result2['pid'] == first['pid']

print('HF83 LIVE SHINY DECAY SELF TEST: PASS')
print('target:', first['pid_hex'])
print('start:', start_distance)
print('tick1 advanced:', advance, 'remaining:', result['frames_away'])
print('tick2 advanced:', advance2, 'remaining:', result2['frames_away'])
