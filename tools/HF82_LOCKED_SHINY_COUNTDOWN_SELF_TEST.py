from pokebot.common.gen6_rng_tracker import (
    LiveMT, find_next_raw_shiny_frame, find_locked_raw_pid_frame,
    gen6_tsv,
)

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

rng = LiveMT.from_seed(0xAABBCCDD)
first = find_next_raw_shiny_frame(snap_from_rng(rng), 250000)
assert first['available'], first
assert first['frames_away'] > 200, first
locked_pid = first['pid']
start_distance = first['frames_away']

# Move part-way toward the selected shiny frame. The exact same PID must stay
# locked and its distance must decay by the exact number of MT outputs consumed.
advance = min(173, start_distance - 1)
for _ in range(advance):
    rng.next_uint()
tracked = find_locked_raw_pid_frame(snap_from_rng(rng), locked_pid, start_distance)
assert tracked is not None
assert tracked['pid'] == locked_pid
assert tracked['frames_away'] == start_distance - advance, (start_distance, advance, tracked)

# Consume through the target without using it as an encounter PID. It must no
# longer exist in the future search window, which is the signal to jump to the
# next shiny frame.
remaining = tracked['frames_away']
for _ in range(remaining):
    rng.next_uint()
missed = find_locked_raw_pid_frame(snap_from_rng(rng), locked_pid, remaining)
assert missed is None, missed
next_target = find_next_raw_shiny_frame(snap_from_rng(rng), 250000)
assert next_target['available'], next_target
assert next_target['pid'] != locked_pid
assert next_target['frames_away'] > 0

print('HF82 LOCKED SHINY COUNTDOWN SELF TEST: PASS')
print('initial target:', first['pid_hex'], 'distance', start_distance)
print('after advancing:', advance, 'distance', tracked['frames_away'])
print('passed target -> next:', next_target['pid_hex'], 'distance', next_target['frames_away'])
