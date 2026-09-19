from pokebot.static.oras_static import get_static_profile, StaticEncounterStateMachine, StaticState

p = get_static_profile("poochyena_dexnav_tutorial")
sm = StaticEncounterStateMachine(p, "omega_ruby")
sm.field_ready(); sm.trigger_sent(); sm.battle_ready()
e = sm.accidental_encounter({"species": 263, "pid": "0x12345678", "ec": "0x87654321"}, reason="Zigzagoon")
assert e["event"] == "ACCIDENTAL_ENCOUNTER_RESET"
assert sm.state == StaticState.NONSHINY_RESET_REQUIRED
sm.field_ready()

from pathlib import Path
root = Path(__file__).resolve().parents[1]
worker = (root / "qt_ui" / "static_worker.py").read_text(encoding="utf-8")
dash = (root / "qt_ui" / "dashboard_page.py").read_text(encoding="utf-8")
for token in [
    '"tutorial_fang_name": fang_name',
    '"special_move": fang_name',
    'DEXNAV TUTORIAL ACCIDENTAL ENCOUNTER',
    'Accidental shiny encountered during DexNav tutorial approach',
    'self.state_machine.accidental_encounter',
]:
    assert token in worker, token
for token in ['tutorial_fang_name', 'Special move:', 'ability_display']:
    assert token in dash, token
print("HF93 DEXNAV IDENTITY FAILSAFE SELF TEST: PASS")
