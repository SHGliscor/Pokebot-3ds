from pathlib import Path

root = Path(__file__).resolve().parents[1]
text = (root / "qt_ui" / "static_worker.py").read_text(encoding="utf-8")
assert "DEXNAV TUTORIAL SUSTAINED CPAD START" in text
assert "((1, 3000), (2, 1400))" in text
assert "hold_ms=hold_ms" in text
assert "sustained automatic Circle Pad sneak" in text
assert "range(1, 13)" not in text[text.index("DEXNAV TUTORIAL SUSTAINED CPAD START"):text.index("@staticmethod", text.index("DEXNAV TUTORIAL SUSTAINED CPAD START"))]
print("HF87 SUSTAINED CPAD SELF TEST: PASS")
