from pokebot.common.gen6_rng_tracker import (
    LiveMT, find_next_raw_shiny_frame, gen6_tsv,
    read_live_locked_shiny_countdown,
)
import struct

ORAS_INDEX = 0x08C59E44
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


class AtomicCursorBridge:
    def __init__(self, rng):
        self.rng = rng
    def read(self, address, length):
        if address == ORAS_INDEX and length == 8:
            # Layout: u16 index, u16 padding, u32 MT[0] period anchor.
            return struct.pack('<HHI', self.rng.index, 0, self.rng.state[0])
        if address == ORAS_IDS and length == 4:
            return struct.pack('<HH', TID, SID)
        raise AssertionError((hex(address), length, self.rng.index))

rng = LiveMT.from_seed(0xAABBCCDD)
# Put the cursor near the real-hardware failure area to prove a moving live RNG
# no longer requires before/after index equality.
for _ in range(597):
    rng.next_uint()

first = find_next_raw_shiny_frame(snap_from_rng(rng), 250000)
assert first['available'], first
tracking = {
    'family': 'oras',
    'state': rng.state.copy(),
    'index': rng.index,
    'remaining': first['frames_away'],
    'pid': first['pid'],
    'tid': TID,
    'sid': SID,
    'tsv': TSV,
}

# Simulate the exact user-observed 597 -> 598 advance before the next refresh.
rng.next_uint()
result = read_live_locked_shiny_countdown(
    AtomicCursorBridge(rng), 'oras', tracking_state=tracking
)
assert result['available'], result
assert result['pid'] == first['pid']
assert result['frames_away'] == first['frames_away'] - 1
assert result['countdown_delta'] == 1
assert result['cursor_mode'] == 'atomic_cursor_anchor'

# Cross the MT ring boundary and verify the MT[0] anchor distinguishes periods.
tracking2 = result['tracking_state']
advance2 = min(40, result['frames_away'] - 1)
for _ in range(advance2):
    rng.next_uint()
result2 = read_live_locked_shiny_countdown(
    AtomicCursorBridge(rng), 'oras', tracking_state=tracking2
)
assert result2['available'], result2
assert result2['pid'] == first['pid']
assert result2['frames_away'] == result['frames_away'] - advance2
assert result2['countdown_delta'] == advance2

print('HF84 ATOMIC CURSOR SELF TEST: PASS')
print('start index: 597')
print('tick1 index:', result['current_index'], 'remaining:', result['frames_away'])
print('tick2 index:', result2['current_index'], 'remaining:', result2['frames_away'])
print('target:', first['pid_hex'])
