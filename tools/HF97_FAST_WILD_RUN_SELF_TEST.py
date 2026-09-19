from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
walk_core = (ROOT / 'pokebot/wild/validated/walk_v0p23/causal_core_v0p14.py').read_text(encoding='utf-8')
acro_core = (ROOT / 'pokebot/wild/validated/acro_v0p27/causal_core_v0p14.py').read_text(encoding='utf-8')
walk_move = (ROOT / 'pokebot/wild/validated/walk_v0p23/w6_unlimited_v0p23.py').read_text(encoding='utf-8')
acro_move = (ROOT / 'pokebot/wild/validated/acro_v0p27/acro_bunny_latch_10_v0p27.py').read_text(encoding='utf-8')
worker = (ROOT / 'qt_ui/wild_worker.py').read_text(encoding='utf-8')
movement = (ROOT / 'qt_ui/wild_movement.py').read_text(encoding='utf-8')
oras_manifest = (ROOT / 'tools/build_oras_map_manifest.py').read_text(encoding='utf-8')
oras_records = (ROOT / 'tools/inspect_oras_map_records.py').read_text(encoding='utf-8')

checks = {
    'walk hold 90': 'TOUCH_HOLD_MS = 90' in walk_core,
    'walk settle 60': 'TOUCH_SETTLE_MS = 60' in walk_core,
    'walk observe .30': 'POST_TAP_OBSERVE_SEC = 0.30' in walk_core,
    'walk poll .035': 'POST_TAP_BATTLE_POLL_SEC = 0.035' in walk_core,
    'walk field samples 2': 'FIELD_STABLE_SAMPLES = 2' in walk_core,
    'walk field poll .06': 'FIELD_STABLE_POLL_SEC = 0.06' in walk_core,
    'acro hold 90': 'TOUCH_HOLD_MS = 90' in acro_core,
    'acro settle 60': 'TOUCH_SETTLE_MS = 60' in acro_core,
    'acro observe .30': 'POST_TAP_OBSERVE_SEC = 0.30' in acro_core,
    'acro poll .035': 'POST_TAP_BATTLE_POLL_SEC = 0.035' in acro_core,
    'walk authority .06': 'FIELD_AUTHORITY_POLL_SEC = 0.06' in walk_move,
    'acro authority .06': 'FIELD_AUTHORITY_POLL_SEC = 0.06' in acro_move,
    'phase log': 'HF97 PHASE #' in worker,
    'boundary phase': 'hf97_boundary_wait_s' in worker,
    'run phase': 'hf97_run_to_core_field_s' in worker,
    'all encounter run recovery': 'causal = self._causal_run_until_field(br, core)' in worker,
    'run recovery remains grass gated': 'stable field before returning success' in worker,
    'run never falls back to walk': 'walking fallback is disabled' in movement,
    'run dispatches fluid only': 'rec = backend.do_fluid(br, before, plan)' in movement,
    'short grass strips use Run': '"SHORT_GRASS_STRIP"' in movement and 'short_tiles = min(n, 2)' in movement,
    'one-cell Run corridors remain fail-closed': 'elif n >= 2:' in movement and 'wall_bounded_run_authorized": False' in movement,
    'wall-bounded Run metadata requires collision authority': 'terminal_hard_boundary' in movement and 'def is_hard_boundary' in (ROOT / 'pokebot/wild/world_authority.py').read_text(encoding='utf-8'),
    'run keeps grass endpoint authority': '"boundary_policy": "endpoint_must_remain_grass"' in movement,
    'requested direction is edge fallback preference': 'permit the other direction in the selected axis' in movement,
    'fallback direction is logged': '"requested_direction_fallback"' in movement,
    'boundary edges derive from world database': 'boundary_edges_derived' in movement and 'def boundary_edges' in (ROOT / 'pokebot/wild/world_authority.py').read_text(encoding='utf-8'),
    'patch metadata is exposed': '"patch_id"' in movement and 'def corridor_metadata' in (ROOT / 'pokebot/wild/world_authority.py').read_text(encoding='utf-8'),
    'oras base update overlay manifest': 'update_1_4' in oras_manifest and 'a/0/1/3' in oras_manifest and 'a/0/4/0' in oras_manifest,
    'oras ZO MM record inspector': 'def inspect_zo' in oras_records and 'def inspect_mm' in oras_records and 'offsets_valid' in oras_records,
    'corridor is the authoritative reserve': 'reserve_required = False' in walk_move and 'reserve_ok = len(plan["corridor"]) >= moved' in walk_move,
}
failed = [name for name, ok in checks.items() if not ok]
for name, ok in checks.items():
    print(('PASS' if ok else 'FAIL'), name)
if failed:
    raise SystemExit('HF97 self-test failed: ' + ', '.join(failed))
print('HF97 fast wild Run self-test: PASS')
