from pathlib import Path

root = Path(__file__).resolve().parents[2] / "Luma3DS"
path = root / "sysmodules" / "rosalina" / "source" / "pokebot_ram_bridge.c"
text = path.read_text(encoding="utf-8")

if '#include "sock_util.h"' not in text:
    marker = '#include "sleep.h"\n'
    if marker not in text:
        raise SystemExit("sleep include marker not found")
    text = text.replace(marker, marker + '#include "sock_util.h"\n', 1)
    path.write_text(text, encoding="utf-8")
