from pokebot.common.gen6_rng_tracker import LiveMT, read_live_main_rng_snapshot, find_next_raw_shiny_frame, MT_N
import struct

ORAS_INDEX = 0x08C59E44
ORAS_TABLE = 0x08C59E48
ORAS_IDS = 0x08C81340

class MovingBridge:
    def __init__(self, seed=0xAABBCCDD, index=49, advance_after=2):
        rng = LiveMT.from_seed(seed)
        for _ in range(index):
            rng.next_uint()
        self.rng = rng
        self.advance_after = advance_after
        self.index_read_count = 0
        self.snapshot_state = None
        self.snapshot_index = None

    def _advance(self):
        for _ in range(self.advance_after):
            self.rng.next_uint()

    def read(self, address, length):
        if address == ORAS_INDEX and length == 2:
            raw = struct.pack('<H', self.rng.index)
            self.index_read_count += 1
            if self.index_read_count == 2:
                # State/index at the designated HF81 snapshot instant.
                self.snapshot_state = self.rng.state.copy()
                self.snapshot_index = self.rng.index
            self._advance()
            return raw
        if address == ORAS_IDS and length == 4:
            raw = struct.pack('<HH', 12345, 54321)
            self._advance()
            return raw
        if ORAS_TABLE <= address < ORAS_TABLE + MT_N * 4:
            off = address - ORAS_TABLE
            blob = struct.pack('<624I', *self.rng.state)
            raw = blob[off:off+length]
            self._advance()
            return raw
        raise AssertionError((hex(address), length))

b = MovingBridge()
snap = read_live_main_rng_snapshot(b, 'oras')
assert snap['available'], snap
assert snap['normalized_index'] == b.snapshot_index, (snap['normalized_index'], b.snapshot_index)
assert snap['state'] == b.snapshot_state, 'rolling repair did not reconstruct designated snapshot'
assert snap['snapshot_advanced'] > 0
pred = find_next_raw_shiny_frame(snap, 250000)
assert pred['available'], pred
print('HF81 rolling MT snapshot self-test PASS')
print('snapshot index:', snap['index'])
print('advanced during acquisition:', snap['snapshot_advanced'])
print('next shiny distance:', pred['frames_away'])
print('next shiny PID:', pred['pid_hex'])
