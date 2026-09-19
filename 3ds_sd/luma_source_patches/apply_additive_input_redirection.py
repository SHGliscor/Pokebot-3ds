from pathlib import Path

root = Path(__file__).resolve().parents[2] / "Luma3DS"
hooks_path = root / "sysmodules" / "rosalina" / "source" / "input_redirection_hooks.s"
text = hooks_path.read_text(encoding="utf-8")

marker = "@ Pokebot additive HID: effective buttons = physical OR remote"
if marker in text:
    raise SystemExit(0)

old = r'''@ HID reg. Copy +20 => +0 if remote is not exactly 0xfff. Else, pass local through.
ldr r1, [r1]        @ Read HID reg.
ldr r2, [r0, #20]   @ Load remote HID reg.
cmp r2, r3          @ Is remote 0xfff?
movne r1, r2        @ If not, load remote.
str r1, [r0]
'''

new = r'''@ Pokebot additive HID: effective buttons = physical OR remote.
@ The HID register is active-low: 0xFFF is neutral and a cleared bit means
@ pressed. Therefore bitwise AND merges the two sources without suppressing
@ the real console buttons. Examples: physical B (FFD) & remote A (FFE) = FFC.
ldr r1, [r1]        @ Read physical HID register.
ldr r2, [r0, #20]   @ Load remote HID register (FFF when neutral).
and r1, r1, r2      @ Active-low union: preserve BOTH physical and remote presses.
str r1, [r0]
'''

if old not in text:
    raise SystemExit("upstream HID replacement block not found")

text = text.replace(old, new, 1)
hooks_path.write_text(text, encoding="utf-8")
