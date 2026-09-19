from pathlib import Path

from pokebot.static.dexnav_target_nav import steering_command

ROOT = Path(__file__).resolve().parents[1]

# Hardware-probed Route 101 geometry: keep partial-stick authority high enough
# that the approach no longer visibly slows down before contact.
far = steering_command(1791.0, 2349.0, 1701.0, 2331.0)
assert far["x"] == -0.55, far
assert far["y"] == 0.38, far
assert far["hold_ms"] == 2800, far

near = steering_command(1710.5, 2334.0, 1701.0, 2331.0)
assert near["x"] == -0.48, near
assert near["y"] == 0.34, near
assert near["hold_ms"] == 850, near

# Z dead-zone recovery still escalates only the vertical component.
stall1 = steering_command(1770.0, 2349.0, 1701.0, 2331.0, vertical_stall=1)
stall2 = steering_command(1770.0, 2349.0, 1701.0, 2331.0, vertical_stall=2)
assert stall1["y"] >= 0.43, stall1
assert stall2["y"] > stall1["y"], (stall1, stall2)
assert stall2["y"] <= 0.48, stall2

static_text = (ROOT / "qt_ui" / "static_worker.py").read_text(encoding="utf-8")
assert "settle_ms = 850 if cleanup else 650" in static_text
assert "target_active_after_press" in static_text
assert "deferred_to_next_pre_step_read" in static_text
assert "_wait_battle_active(bridge, 0.12)" not in static_text

main_text = (ROOT / "qt_ui" / "main_window.py").read_text(encoding="utf-8")
dash_text = (ROOT / "qt_ui" / "dashboard_page.py").read_text(encoding="utf-8")
assert "self.rng_refresh_timer = None" in main_text
assert "self.rng_refresh_timer = QTimer(self)" not in main_text
assert 'rng_panel = Panel("RNG tracker")' not in dash_text
assert "self.rng_next_shiny = None" in dash_text

print("HF94 DEXNAV/UI SELF TEST: PASS")
