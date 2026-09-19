from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from pokebot.common.luma_input import encode_circle_pad, CPAD_NEUTRAL
worker=(ROOT/'qt_ui'/'static_worker.py').read_text(encoding='utf-8')
assert 'strength_x=-0.40' in worker
assert 'strength_y=0.08' in worker
assert 'circle_pad_pulse(\n                -0.40, 0.08,' in worker
state=encode_circle_pad(-0.40,0.08)
assert state != CPAD_NEUTRAL
x=state & 0xFFF
y=(state>>12)&0xFFF
assert x < 0x7FF, (hex(state),x,y)
assert y > 0x7FF, (hex(state),x,y)
print(f'HF86 DIAGONAL CPAD SELF TEST: PASS state=0x{state:08X} x=0x{x:03X} y=0x{y:03X}')
