from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFormLayout, QHBoxLayout, QLabel, QMainWindow,
    QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pokebot.common.framebuffer import capture_screen, SCREEN_BOTTOM
from pokebot.wild.oras_touch_profile import load_profile, save_profile
from qt_ui.appdata_store import get_profile_paths
from qt_ui.settings_store import load_settings


class ClickImage(QLabel):
    clicked = Signal(int, int)
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.pixmap() is not None:
            # Image is shown at native 320x240 with no scaling.
            self.clicked.emit(int(event.position().x()), int(event.position().y()))
        super().mousePressEvent(event)


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Pokebot3DS-CFW — ORAS Touch Profile Calibrator")
        self.profile = load_profile(ROOT)
        settings = load_settings(get_profile_paths().settings_path)
        self.host = settings.get("three_ds_ip", "192.168.0.28")
        self.port = int(settings.get("ram_bridge_port", 4952))
        self.pending = None

        root = QWidget(); self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        intro = QLabel(
            "Read-only coordinate calibration. Put the 3DS on the relevant bottom-screen menu, "
            "capture it, choose what you are mapping, then click the centre of that control. "
            "This tool sends NO controller input and writes NO game RAM."
        )
        intro.setWordWrap(True); lay.addWidget(intro)

        bar = QHBoxLayout(); lay.addLayout(bar)
        self.capture_btn = QPushButton("Capture bottom screen")
        self.capture_btn.clicked.connect(self.capture)
        bar.addWidget(self.capture_btn)
        self.kind = QComboBox()
        for n in range(1, 7): self.kind.addItem(f"Party silver field-action icon — visible slot {n}", ("party_field_action", n))
        for n in range(1, 5): self.kind.addItem(f"Battle move button — move slot {n}", ("battle_move", n))
        bar.addWidget(self.kind, 1)

        self.image = ClickImage(); self.image.setFixedSize(320, 240)
        self.image.setAlignment(Qt.AlignCenter); self.image.setText("Capture the bottom screen")
        self.image.clicked.connect(self.pick)
        lay.addWidget(self.image, alignment=Qt.AlignHCenter)

        self.status = QLabel(); self.status.setWordWrap(True); lay.addWidget(self.status)
        self.refresh_status()
        self.resize(660, 420)

    def refresh_status(self):
        party = self.profile.get("party_field_action", {})
        moves = self.profile.get("battle_move", {})
        self.status.setText(
            "Saved party slots: " + ", ".join(f"{k}={v}" for k,v in sorted(party.items(), key=lambda x:int(x[0]))) +
            "\nSaved move slots: " + ", ".join(f"{k}={v}" for k,v in sorted(moves.items(), key=lambda x:int(x[0])))
        )

    def capture(self):
        try:
            out = get_profile_paths().root / "support" / "oras_touch_calibration_bottom.png"
            rec = capture_screen(self.host, out, selector=SCREEN_BOTTOM, port=self.port, timeout=1.25)
            pix = QPixmap(str(out))
            if pix.width() != 320 or pix.height() != 240:
                raise RuntimeError(f"expected 320x240 bottom screen; got {pix.width()}x{pix.height()}")
            self.image.setPixmap(pix)
            self.image.setToolTip(str(rec))
        except Exception as exc:
            QMessageBox.critical(self, "Capture failed", f"{type(exc).__name__}: {exc}")

    def pick(self, x, y):
        group, slot = self.kind.currentData()
        label = self.kind.currentText()
        answer = QMessageBox.question(
            self, "Save coordinate?", f"Save {label} as ({x}, {y})?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.profile.setdefault(group, {})[str(slot)] = [int(x), int(y)]
        self.profile.setdefault("proven", {}).setdefault(group, [])
        if slot not in self.profile["proven"][group]:
            self.profile["proven"][group].append(slot)
        save_profile(ROOT, self.profile)
        self.refresh_status()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = Window(); w.show()
    raise SystemExit(app.exec())
