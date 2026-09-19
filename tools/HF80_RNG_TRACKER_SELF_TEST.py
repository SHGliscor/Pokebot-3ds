from pokebot.common.gen6_rng_tracker import LiveMT, gen6_psv, find_next_raw_shiny_frame

seed=0xAABBCCDD
base=LiveMT.from_seed(seed)
for _ in range(1379):
    base.next_uint()
state=base.state.copy(); idx=base.index
expected=base.clone().next_uint()
clone=LiveMT(state.copy(), idx)
assert clone.next_uint()==expected, 'direct MT snapshot did not preserve next output'

snap={
    'available': True,
    'family': 'oras',
    'state': state,
    'index': idx,
    'normalized_index': idx,
    'tid': 12345,
    'sid': 54321,
    'tsv': ((12345^54321)>>4)&0xFFF,
}
out=find_next_raw_shiny_frame(snap, max_advances=250000)
assert out['available']
assert out['frames_away'] > 0
assert gen6_psv(out['pid']) == snap['tsv']
print('HF80 RNG TRACKER SELF TEST: PASS', out['frames_away'], out['pid_hex'])
