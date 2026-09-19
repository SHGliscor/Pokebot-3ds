import sys
import time
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from .appdata_store import get_profile_paths
from .shutdown_diagnostics import install as install_shutdown_diagnostics, record_event, record_exception
from .main_window import MainWindow
from .theme import APP_QSS


def _resource_root():
    # PyInstaller onedir places bundled datas under sys._MEIPASS.
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))


def main():
    profile = get_profile_paths()
    log_path = install_shutdown_diagnostics(profile.root)
    record_event(f"APPLICATION_START diagnostics={log_path}")
    try:
        app = QApplication(sys.argv)
        record_event("QT_APPLICATION_CREATED")
        app.setApplicationName("Pokebot3DS-CFW")
        icon = _resource_root() / "assets" / "pokebot_icon.ico"
        if icon.exists():
            app.setWindowIcon(QIcon(str(icon)))
        app.setStyleSheet(APP_QSS)
        window_started = time.perf_counter()
        win = MainWindow()
        record_event(
            f"MAIN_WINDOW_CONSTRUCTED elapsed_ms={(time.perf_counter() - window_started) * 1000.0:.1f}"
        )
        if icon.exists():
            win.setWindowIcon(QIcon(str(icon)))
        win.show()
        exit_code = int(app.exec())
        record_event(f"QT_EVENT_LOOP_EXIT code={exit_code}")
        return exit_code
    except Exception as exc:
        record_exception("APPLICATION_MAIN_EXCEPTION", exc)
        raise
