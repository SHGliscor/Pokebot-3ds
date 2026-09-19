from __future__ import annotations

import json
import math
import socket
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from pokebot.common.bridge import Bridge as ReadBridge
from pokebot.common.framebuffer import SCREEN_BOTTOM, capture_screen
from pokebot.wild.validated_loader import load_walk_v0p23
from qt_ui.appdata_store import ensure_profile
from qt_ui.settings_store import load_settings

X_TITLE_ID = 0x0004000000055D00
Y_TITLE_ID = 0x0004000000055E00
VALID_TITLES = {X_TITLE_ID: "Pokemon X", Y_TITLE_ID: "Pokemon Y"}

TOUCH_HOLD_MS = 180
TOUCH_SETTLE_MS = 300
LUMA_INPUT_PORT = 4950


def encode_touch_xy(x: int, y: int) -> int:
    x = max(0, min(319, int(x)))
    y = max(0, min(239, int(y)))
    xr = int(math.floor(x * 4095.0 / 320.0)) & 0xFFF
    yr = int(math.floor(y * 4095.0 / 240.0)) & 0xFFF
    return 0x01000000 | (yr << 12) | xr


class ClickImage(QLabel):
    clicked = Signal(int, int)

    def __init__(self):
        super().__init__()
        self.setAlignment(Qt.AlignCenter)
        self.setFixedSize(640, 480)
        self.setStyleSheet("background:#111; border:1px solid #555;")
        self._native_w = 320
        self._native_h = 240

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton or self.pixmap() is None:
            return
        # Image is deliberately rendered at exactly 2x native size.
        px = int(event.position().x())
        py = int(event.position().y())
        x = max(0, min(self._native_w - 1, px // 2))
        y = max(0, min(self._native_h - 1, py // 2))
        self.clicked.emit(x, y)


class ProbeWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Pokebot3DS - XY Global Touch Sanity Probe")
        self.resize(720, 760)

        profile = ensure_profile(ROOT)
        settings = load_settings(profile.settings_path)
        self.host = settings["three_ds_ip"]
        self.port = int(settings["ram_bridge_port"])
        self.timeout = float(settings["bridge_timeout_s"])
        self.out_dir = profile.root / "xy_global_touch_probe"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.out_dir / f"xy_global_touch_probe_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"

        self.read_bridge = ReadBridge(host=self.host, port=self.port, timeout=self.timeout)
        _runner, core, _terrain = load_walk_v0p23()
        self.input_bridge = core.Bridge(self.host, timeout=self.timeout)

        root = QWidget()
        layout = QVBoxLayout(root)

        title = QLabel("XY GLOBAL TOUCH SANITY PROBE")
        title.setStyleSheet("font-size:18px; font-weight:600;")
        layout.addWidget(title)

        instructions = QLabel(
            "1. Put Pokemon X/Y on a normal touch-driven bottom screen (PSS is ideal).\n"
            "2. Click Refresh to capture the live bottom screen.\n"
            "3. Click an icon in the captured image that should visibly react on the real 3DS.\n"
            "The probe sends ONE touch only, logs it, then recaptures the screen.\n"
            "It does NOT reset, press A, or advance dialogue."
        )
        instructions.setWordWrap(True)
        layout.addWidget(instructions)

        controls = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh framebuffer")
        self.refresh_btn.clicked.connect(self.refresh)
        controls.addWidget(self.refresh_btn)

        controls.addWidget(QLabel("Touch path:"))
        self.touch_path = QComboBox()
        self.touch_path.addItem("Legacy Luma UDP 4950 (only if enabled in Rosalina)", "luma4950")
        self.touch_path.addItem("Pokebot acknowledged UDP 4952 (PRIMARY TEST)", "pokebot4952")
        self.touch_path.setCurrentIndex(1)
        controls.addWidget(self.touch_path)

        controls.addWidget(QLabel("Y mapping:"))
        self.mapping = QComboBox()
        self.mapping.addItem("Direct (display Y = touch Y)", "direct")
        self.mapping.addItem("Flipped (touch Y = 239 - display Y)", "flip")
        controls.addWidget(self.mapping)
        layout.addLayout(controls)

        self.image = ClickImage()
        self.image.clicked.connect(self.send_click)
        layout.addWidget(self.image, alignment=Qt.AlignHCenter)

        self.status = QLabel("Not connected yet.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(130)
        layout.addWidget(self.log)

        self.setCentralWidget(root)
        QTimer.singleShot(100, self.refresh)

    def append_log(self, event: str, **fields):
        row = {
            "timestamp": datetime.now().isoformat(timespec="milliseconds"),
            "event": event,
            **fields,
        }
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")
        line = event + " " + " ".join(f"{k}={v}" for k, v in fields.items())
        self.log.append(line)

    def verify_xy(self):
        info = self.read_bridge.game_info()
        if int(info.get("status", -1)) != 0:
            raise RuntimeError(f"GAME_INFO failed: {info.get('status_name')}")
        title_id = int(str(info.get("title_id", "0")), 16)
        if title_id not in VALID_TITLES:
            raise RuntimeError(
                f"Expected Pokemon X/Y; connected title is 0x{title_id:016X}"
            )
        return VALID_TITLES[title_id], info

    def get_input_caps(self):
        try:
            caps = self.input_bridge.input_ping()
            return caps if isinstance(caps, dict) else {"raw": caps}
        except Exception as exc:
            return {"error": str(exc)}

    def refresh(self):
        try:
            game, info = self.verify_xy()
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            path = self.out_dir / f"bottom_{stamp}.png"
            meta = capture_screen(
                self.host,
                path,
                selector=SCREEN_BOTTOM,
                port=self.port,
                timeout=self.timeout,
            )
            if int(meta["width"]) != 320 or int(meta["height"]) != 240:
                raise RuntimeError(f"Unexpected bottom framebuffer {meta['width']}x{meta['height']}")
            pix = QPixmap(str(path)).scaled(
                640,
                480,
                Qt.IgnoreAspectRatio,
                Qt.FastTransformation,
            )
            self.image.setPixmap(pix)
            caps = self.get_input_caps()
            runtime_flags = int(caps.get("runtime_flags", caps.get("runtimeFlags", 0)) or 0) if not caps.get("error") else 0
            legacy_active = bool(runtime_flags & 0x2)
            custom_active = bool(runtime_flags & 0x1)
            self.status.setText(
                f"Connected: {game} / {self.host}:{self.port} | "
                f"Pokebot4952={'ACTIVE' if custom_active else 'UNKNOWN/OFF'} | "
                f"Legacy4950={'ACTIVE' if legacy_active else 'OFF'} | Log: {self.log_path}"
            )
            self.append_log(
                "FRAMEBUFFER_CAPTURE",
                game=game,
                process=info.get("process_name"),
                path=str(path),
                frozen=meta.get("frozen"),
                generation=meta.get("snapshot_generation"),
                input_caps=caps,
                custom_4952_active=custom_active,
                legacy_4950_active=legacy_active,
            )
        except Exception as exc:
            self.status.setText(f"ERROR: {exc}")
            self.append_log("ERROR", operation="refresh", error=str(exc))

    def _send_luma_touch_4950(self, touch_state: int):
        # Exact original Luma InputRedirection 12-byte HID frame:
        #   u32 remote HID, u32 remote touch, u32 remote circle pad.
        # Touch press and release are separate datagrams, matching the official
        # InputRedirectionClient-Qt implementation.
        pressed = struct.pack("<III", 0x00000FFF, int(touch_state) & 0xFFFFFFFF, 0x007FF7FF)
        neutral = struct.pack("<III", 0x00000FFF, 0x02000000, 0x007FF7FF)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.sendto(neutral, (self.host, LUMA_INPUT_PORT))
            time.sleep(0.05)
            sock.sendto(pressed, (self.host, LUMA_INPUT_PORT))
            time.sleep(TOUCH_HOLD_MS / 1000.0)
            sock.sendto(neutral, (self.host, LUMA_INPUT_PORT))
            time.sleep(TOUCH_SETTLE_MS / 1000.0)
        finally:
            sock.close()

    def send_click(self, display_x: int, display_y: int):
        try:
            game, _ = self.verify_xy()
            mode = self.mapping.currentData()
            touch_x = int(display_x)
            touch_y = int(display_y) if mode == "direct" else 239 - int(display_y)
            touch_state = encode_touch_xy(touch_x, touch_y)

            path_mode = self.touch_path.currentData()
            sequence_id = None
            completed = True
            if path_mode == "luma4950":
                caps = self.get_input_caps()
                runtime_flags = int(caps.get("runtime_flags", caps.get("runtimeFlags", 0)) or 0) if not caps.get("error") else 0
                if not (runtime_flags & 0x2):
                    raise RuntimeError(
                        "Legacy UDP 4950 is not active. Enable Luma InputRedirection in Rosalina "
                        "before testing 4950, or use the PRIMARY Pokebot UDP 4952 path."
                    )
                self._send_luma_touch_4950(touch_state)
            else:
                self.input_bridge.release_all()
                rec = self.input_bridge.touch_pulse_no_retransmit(
                    touch_state,
                    TOUCH_HOLD_MS,
                    TOUCH_SETTLE_MS,
                )
                self.input_bridge.release_all()
                sequence_id = rec.get("sequence_id")
                completed = bool(rec.get("completed"))

            self.append_log(
                "TOUCH_SENT",
                game=game,
                path=path_mode,
                mapping=mode,
                display_xy=[display_x, display_y],
                touch_xy=[touch_x, touch_y],
                touch_state=f"0x{touch_state:08X}",
                sequence_id=sequence_id,
                completed=completed,
            )
            self.status.setText(
                f"Sent ONE touch: display ({display_x},{display_y}) -> "
                f"touch ({touch_x},{touch_y}) [{mode}] via {path_mode}. Recapturing..."
            )
            # Refresh twice after a completed touch: an early capture for responsiveness and\n            # a later capture to replace any stale framebuffer frame without requiring\n            # the user to press Refresh manually.\n            QTimer.singleShot(250, self.refresh)\n            QTimer.singleShot(900, self.refresh)
        except Exception as exc:
            self.status.setText(f"TOUCH ERROR: {exc}")
            self.append_log("ERROR", operation="touch", error=str(exc))
            QMessageBox.critical(self, "XY Global Touch Sanity Probe", str(exc))

    def closeEvent(self, event):
        try:
            self.input_bridge.release_all()
        except Exception:
            pass
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    win = ProbeWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
