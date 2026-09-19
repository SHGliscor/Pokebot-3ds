from pathlib import Path
from pokebot.common.luma_input import (
    CAPABILITIES, CAP_CPAD_PULSE, CPAD_NEUTRAL, encode_circle_pad,
)
from pokebot.common.evolution_prediction import predict_split_evolution

ROOT = Path(__file__).resolve().parents[1]

def must(cond, msg):
    if not cond:
        raise AssertionError(msg)

must(encode_circle_pad(0.0, 0.0) == CPAD_NEUTRAL, "CPAD neutral encoding")
must(encode_circle_pad(-0.40, 0.0) != CPAD_NEUTRAL, "partial-left CPAD encoding")
must(CAPABILITIES & CAP_CPAD_PULSE, "CPAD capability advertised")

w = predict_split_evolution(265, 0x5A1F8FB4)
must(w and w.get("line") == "Silcoon → Beautifly", "support-ZIP Wurmple prediction")

static_worker = (ROOT / "qt_ui" / "static_worker.py").read_text(encoding="utf-8")
must("DEXNAV_TUTORIAL_A_PRESSES = 14" in static_worker, "14 A tutorial sequence")
must('self._pulse(inputs, ("A",), 120, 1300)' in static_worker, "faster dialogue cadence")
must("inputs.circle_pad_pulse(" in static_worker, "automatic CPAD sneak")

dashboard = (ROOT / "qt_ui" / "dashboard_page.py").read_text(encoding="utf-8")
must('"Evolution"' in dashboard and "predict_split_evolution" in dashboard, "history evolution UI")

patch = (ROOT / "3ds_sd" / "luma_source_patches" / "apply_ack_controller_v0p5.py").read_text(encoding="utf-8")
must("POKEBOT_CMD_CPAD_PULSE  13" in patch, "firmware command 13")
must("POKEBOT_INPUT_CAPS     0x000001CFUL" in patch, "firmware CPAD capability")
must("PokebotInput_SetRemoteCircle" in patch, "firmware remote CPAD accessor")

print("HF77 focused regression: PASS")
print(f"CPAD partial-left packed state: 0x{encode_circle_pad(-0.40, 0.0):06X}")
print(f"Wurmple 0x5A1F8FB4: {w['line']}")
