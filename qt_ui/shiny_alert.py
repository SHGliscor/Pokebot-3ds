
from __future__ import annotations

import sys
from pathlib import Path

def resolve_sound_path(base_dir, configured):
    path = Path(str(configured))
    if not path.is_absolute():
        path = Path(base_dir) / path
    return path

def play_shiny_sound(base_dir, configured_path):
    """Play asynchronously on Windows; fall back to Qt beep elsewhere."""
    path = resolve_sound_path(base_dir, configured_path)

    if sys.platform.startswith("win"):
        try:
            import winsound
            if path.exists():
                winsound.PlaySound(
                    str(path),
                    winsound.SND_FILENAME
                    | winsound.SND_ASYNC
                    | winsound.SND_NODEFAULT,
                )
            else:
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            return True
        except Exception:
            pass

    try:
        from PySide6.QtWidgets import QApplication
        QApplication.beep()
        return True
    except Exception:
        return False
