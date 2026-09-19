from pathlib import Path

root = Path(__file__).resolve().parents[1]
worker = (root / "qt_ui" / "wild_worker.py").read_text(encoding="utf-8")
dash = (root / "qt_ui" / "dashboard_page.py").read_text(encoding="utf-8")

checks = {
    "field counters": "field_to_encounter_total_s = 0.0" in worker,
    "field boundary record": "field_to_encounter_s = self._note_field_to_encounter" in worker,
    "battle return record": "battle_to_field_s = self._complete_encounter_timing" in worker,
    "timing log": "TIMING #{self.attempt}" in worker,
    "payload field avg": '"field_to_encounter_average"' in worker,
    "payload battle avg": '"battle_to_field_average"' in worker,
    "payload cycle avg": '"measured_cycle_average"' in worker,
    "dashboard field": '("Field Avg:", "—")' in dash,
    "dashboard battle": '("Battle/Return Avg:", "—")' in dash,
    "dashboard cycle": '("Cycle Avg:", "0.00s")' in dash,
    "old dashboard label removed": '"Average Time:"' not in dash,
}
failed = [name for name, ok in checks.items() if not ok]
for name, ok in checks.items():
    print(("PASS" if ok else "FAIL"), name)
if failed:
    raise SystemExit("HF95 self-test failed: " + ", ".join(failed))
print("HF95 encounter timing telemetry self-test: PASS")
