
from __future__ import annotations

import ipaddress
from pathlib import Path

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QLineEdit,
    QSpinBox, QDoubleSpinBox, QCheckBox, QPushButton, QMessageBox,
    QFileDialog, QRadioButton, QButtonGroup, QScrollArea, QTabWidget,
)

from .widgets import Panel
from .settings_store import DEFAULTS, normalize
from .appdata_store import get_profile_paths

class SettingsPage(QWidget):
    settings_saved = Signal(dict)
    test_connection_requested = Signal()
    test_sound_requested = Signal(str)
    reset_all_stats_requested = Signal()
    open_stats_folder_requested = Signal()
    export_support_requested = Signal()

    def __init__(self, settings, base_dir, parent=None):
        super().__init__(parent)
        self.base_dir = Path(base_dir)
        self._settings = normalize(settings)

        page_outer = QVBoxLayout(self)
        page_outer.setContentsMargins(0, 0, 0, 0)
        page_outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        outer = QVBoxLayout(content)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(7)
        scroll.setWidget(content)
        page_outer.addWidget(scroll)

        top = QHBoxLayout()
        top.setSpacing(7)

        connection = Panel("3DS CONNECTION")
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)

        grid.addWidget(QLabel("3DS IP Address"), 0, 0)
        self.ip = QLineEdit()
        self.ip.setPlaceholderText("192.168.0.28")
        grid.addWidget(self.ip, 0, 1)

        grid.addWidget(QLabel("RAM Bridge Port"), 1, 0)
        self.ram_port = QSpinBox()
        self.ram_port.setRange(1, 65535)
        grid.addWidget(self.ram_port, 1, 1)

        grid.addWidget(QLabel("Pokebot-Luma Input Port (shared)"), 2, 0)
        self.input_port = QSpinBox()
        self.input_port.setRange(1, 65535)
        self.input_port.setEnabled(False)
        self.input_port.setToolTip(
            "Pokebot-Luma unified controller commands share the same acknowledged UDP endpoint as the RAM bridge. This field mirrors RAM Bridge Port."
        )
        grid.addWidget(self.input_port, 2, 1)
        self.ram_port.valueChanged.connect(self.input_port.setValue)

        grid.addWidget(QLabel("RAM Bridge Timeout"), 3, 0)
        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(0.25, 10.0)
        self.timeout.setSingleStep(0.25)
        self.timeout.setSuffix(" s")
        grid.addWidget(self.timeout, 3, 1)

        connection.outer.addLayout(grid)

        self.auto_test = QCheckBox(
            "Test RAM connection automatically when dashboard opens"
        )
        connection.outer.addWidget(self.auto_test)

        self.test_conn = QPushButton("TEST CONNECTION")
        self.test_conn.setObjectName("SmallAction")
        connection.outer.addWidget(self.test_conn)
        top.addWidget(connection, 1)

        interface = Panel("INTERFACE / STARTUP")
        self.remember_starter = QCheckBox("Remember selected starter")
        self.always_on_top = QCheckBox("Always keep dashboard on top")
        self.download_sprites = QCheckBox(
            "Download/cache ORAS Pokémon sprites"
        )
        interface.outer.addWidget(self.remember_starter)
        interface.outer.addWidget(self.always_on_top)
        interface.outer.addWidget(self.download_sprites)

        interface_note = QLabel(
            "Connection/IP changes apply to the next connection test or hunt. Patch mode applies to the next hunt."
        )
        interface_note.setWordWrap(True)
        interface_note.setObjectName("Muted")
        interface.outer.addWidget(interface_note)
        interface.outer.addStretch(1)
        top.addWidget(interface, 1)

        outer.addLayout(top)

        patch = Panel("GAME PATCH / RESET ROUTE")
        patch_row = QHBoxLayout()
        patch_row.setSpacing(18)

        patch_row.addWidget(QLabel("Use code.ips"))
        self.code_ips_yes = QRadioButton("Yes — direct post-Continue route")
        self.code_ips_no = QRadioButton(
            "No — RAM-gated communication-error handling"
        )
        self.code_ips_group = QButtonGroup(self)
        self.code_ips_group.setExclusive(True)
        self.code_ips_group.addButton(self.code_ips_yes)
        self.code_ips_group.addButton(self.code_ips_no)
        patch_row.addWidget(self.code_ips_yes)
        patch_row.addWidget(self.code_ips_no)
        patch_row.addStretch(1)
        patch.outer.addLayout(patch_row)

        self.patch_policy = QLabel("")
        self.patch_policy.setWordWrap(True)
        self.patch_policy.setObjectName("Muted")
        patch.outer.addWidget(self.patch_policy)

        patch_note = QLabel(
            "This setting does not install or remove code.ips. It tells "
            "Pokebot3DS-CFW which reset route is expected. Pokebot-Luma uses "
            "read-only RAM and acknowledged controller commands on the shared UDP 4952 "
            "bridge; enable the Pokebot3DS bridge services in Rosalina."
        )
        patch_note.setWordWrap(True)
        patch_note.setObjectName("Muted")
        patch.outer.addWidget(patch_note)
        outer.addWidget(patch)

        lower = QHBoxLayout()
        lower.setSpacing(7)

        alerts = Panel("SHINY ALERT / SUPPORT")
        self.shiny_sound = QCheckBox("Play shiny sound notification")
        alerts.outer.addWidget(self.shiny_sound)

        sound_grid = QGridLayout()
        sound_grid.addWidget(QLabel("Sound File"), 0, 0)
        self.sound_path = QLineEdit()
        sound_grid.addWidget(self.sound_path, 0, 1)

        self.browse_sound = QPushButton("Browse")
        self.browse_sound.setObjectName("SmallAction")
        sound_grid.addWidget(self.browse_sound, 0, 2)

        self.test_sound = QPushButton("TEST SHINY SOUND")
        self.test_sound.setObjectName("SmallAction")
        sound_grid.addWidget(self.test_sound, 1, 1)

        alerts.outer.addLayout(sound_grid)

        sound_note = QLabel(
            "A built-in Gen-6-style sparkle chime is included. "
            "You can point this setting to another local WAV file."
        )
        sound_note.setWordWrap(True)
        sound_note.setObjectName("Muted")
        alerts.outer.addWidget(sound_note)

        self.auto_support = QCheckBox(
            "Automatically export support ZIP on stop / hold / shiny"
        )
        alerts.outer.addWidget(self.auto_support)

        self.export_support_btn = QPushButton("EXPORT SUPPORT ZIP")
        self.export_support_btn.setObjectName("SmallAction")
        self.export_support_btn.setToolTip(
            "Create a diagnostic ZIP containing settings, stats/history, "
            "recent logs/support evidence and build manifests. "
            "Sprite caches and build output are excluded."
        )
        alerts.outer.addWidget(self.export_support_btn)

        self.export_support_status = QLabel("")
        self.export_support_status.setObjectName("SmallGreen")
        self.export_support_status.setWordWrap(True)
        alerts.outer.addWidget(self.export_support_status)

        keep_row = QHBoxLayout()
        keep_row.addWidget(QLabel("Recent raw PK6 files to retain"))
        self.raw_keep = QSpinBox()
        self.raw_keep.setRange(1, 50)
        keep_row.addWidget(self.raw_keep)
        keep_row.addStretch(1)
        alerts.outer.addLayout(keep_row)

        lower.addWidget(alerts, 1)

        safety = Panel("SAFETY — FIXED")
        for text in (
            "Shiny = absolute HOLD",
            "Wrong species / checksum / TID-SID = HOLD",
            "RAM read or state failure = HOLD",
            "Safe Stop / Close prevents the next reset",
            "No RAM writes",
        ):
            c = QCheckBox(text)
            c.setChecked(True)
            c.setEnabled(False)
            safety.outer.addWidget(c)
        safety.outer.addStretch(1)
        lower.addWidget(safety, 1)

        outer.addLayout(lower)

        management = QHBoxLayout()
        management.setSpacing(7)

        storage = Panel("PERSISTENT HUNT DATA")
        storage_title = QLabel("Settings, statistics and history are stored in Windows AppData.")
        storage_title.setStyleSheet("font-size:11pt;font-weight:900;color:#eef6fd;")
        storage.outer.addWidget(storage_title)
        storage_desc = QLabel(
            "Lifetime encounters, species shiny totals, target phases, Last Seen, "
            "recent shiny history, support files and calibration persist across app updates."
        )
        storage_desc.setWordWrap(True)
        storage_desc.setObjectName("Muted")
        storage.outer.addWidget(storage_desc)
        profile = get_profile_paths()
        profile_label = QLabel(str(profile.root))
        profile_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        profile_label.setStyleSheet(
            "background:#151c24;border:1px solid #35576a;"
            "padding:7px;color:#bfeaff;font-family:Consolas;"
        )
        storage.outer.addWidget(profile_label)
        self.open_stats_btn = QPushButton("OPEN STATS FOLDER")
        self.open_stats_btn.setObjectName("SmallAction")
        storage.outer.addWidget(self.open_stats_btn)
        management.addWidget(storage, 1)

        danger = Panel("STATISTICS MANAGEMENT")
        warning = QLabel(
            "Reset All Stats clears ORAS starter/wild lifetime statistics, "
            "species shiny totals, phase progress, recent shinies and Last Seen "
            "history. IP/settings, logs and support ZIPs are preserved."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color:#ffd36a;font-weight:700;")
        danger.outer.addWidget(warning)
        self.reset_stats_btn = QPushButton("RESET ALL STATS")
        self.reset_stats_btn.setObjectName("StatsResetButton")
        danger.outer.addWidget(self.reset_stats_btn)
        self.stats_management_status = QLabel("")
        self.stats_management_status.setObjectName("SmallGreen")
        self.stats_management_status.setAlignment(Qt.AlignCenter)
        danger.outer.addWidget(self.stats_management_status)
        management.addWidget(danger, 1)

        outer.addLayout(management)

        # Re-home the growing settings surface into focused tabs without
        # changing any runtime behavior or signal wiring.
        top.removeWidget(connection); top.removeWidget(interface)
        outer.removeWidget(patch)
        lower.removeWidget(alerts); lower.removeWidget(safety)
        management.removeWidget(storage); management.removeWidget(danger)

        settings_tabs = QTabWidget()
        conn_page = QWidget(); conn_lay = QVBoxLayout(conn_page); conn_lay.setContentsMargins(6,6,6,6); conn_lay.setSpacing(7)
        conn_lay.addWidget(connection); conn_lay.addWidget(patch); conn_lay.addStretch(1)
        ui_page = QWidget(); ui_lay = QVBoxLayout(ui_page); ui_lay.setContentsMargins(6,6,6,6); ui_lay.addWidget(interface); ui_lay.addStretch(1)
        alerts_page = QWidget(); alerts_lay = QVBoxLayout(alerts_page); alerts_lay.setContentsMargins(6,6,6,6); alerts_lay.setSpacing(7)
        alerts_lay.addWidget(alerts); alerts_lay.addWidget(storage); alerts_lay.addStretch(1)
        safety_page = QWidget(); safety_lay = QVBoxLayout(safety_page); safety_lay.setContentsMargins(6,6,6,6); safety_lay.setSpacing(7)
        safety_lay.addWidget(safety); safety_lay.addWidget(danger); safety_lay.addStretch(1)
        settings_tabs.addTab(conn_page, "Connection & patch")
        settings_tabs.addTab(ui_page, "Interface")
        settings_tabs.addTab(alerts_page, "Alerts & data")
        settings_tabs.addTab(safety_page, "Safety & statistics")
        outer.addWidget(settings_tabs, 1)

        actions = QHBoxLayout()
        self.defaults_btn = QPushButton("RESTORE DEFAULTS")
        self.defaults_btn.setObjectName("SmallAction")
        self.save_btn = QPushButton("SAVE SETTINGS")
        self.save_btn.setObjectName("StartButton")
        actions.addWidget(self.defaults_btn)
        actions.addStretch(1)
        actions.addWidget(self.save_btn)
        outer.addLayout(actions)

        self.status = QLabel("")
        self.status.setObjectName("SmallGreen")
        self.status.setAlignment(Qt.AlignRight)
        outer.addWidget(self.status)
        outer.addStretch(1)

        self.save_btn.clicked.connect(self._save)
        self.defaults_btn.clicked.connect(self._restore_defaults)
        self.test_conn.clicked.connect(self.test_connection_requested)
        self.browse_sound.clicked.connect(self._browse_sound)
        self.test_sound.clicked.connect(self._test_sound)
        self.code_ips_yes.toggled.connect(self._update_patch_policy)
        self.code_ips_no.toggled.connect(self._update_patch_policy)
        self.open_stats_btn.clicked.connect(self.open_stats_folder_requested)
        self.reset_stats_btn.clicked.connect(self.reset_all_stats_requested)
        self.export_support_btn.clicked.connect(self.export_support_requested)

        self.set_values(self._settings)

    def set_reset_status(self, text):
        self.stats_management_status.setText(str(text))

    def set_support_status(self, text):
        self.export_support_status.setText(str(text))

    def set_values(self, settings):
        s = normalize(settings)
        self._settings = s
        self.ip.setText(s["three_ds_ip"])
        self.ram_port.setValue(s["ram_bridge_port"])
        self.input_port.setValue(s["input_port"])
        self.timeout.setValue(s["bridge_timeout_s"])
        self.code_ips_yes.setChecked(bool(s["use_code_ips"]))
        self.code_ips_no.setChecked(not bool(s["use_code_ips"]))
        self._update_patch_policy()
        self.auto_test.setChecked(s["auto_connection_test"])
        self.remember_starter.setChecked(s["remember_selected_starter"])
        self.always_on_top.setChecked(s["always_on_top"])
        self.download_sprites.setChecked(s["download_oras_sprites"])
        self.shiny_sound.setChecked(s["shiny_sound_enabled"])
        self.sound_path.setText(s["shiny_sound_path"])
        self.auto_support.setChecked(s["auto_support_zip"])
        self.raw_keep.setValue(s["raw_pk6_keep"])

    def values(self):
        values = dict(self._settings)
        values.update({
            "three_ds_ip": self.ip.text().strip(),
            "ram_bridge_port": self.ram_port.value(),
            "input_port": self.input_port.value(),
            "bridge_timeout_s": self.timeout.value(),
            "use_code_ips": self.code_ips_yes.isChecked(),
            "auto_connection_test": self.auto_test.isChecked(),
            "remember_selected_starter": self.remember_starter.isChecked(),
            "always_on_top": self.always_on_top.isChecked(),
            "download_oras_sprites": self.download_sprites.isChecked(),
            "shiny_sound_enabled": self.shiny_sound.isChecked(),
            "shiny_sound_path": self.sound_path.text().strip(),
            "auto_support_zip": self.auto_support.isChecked(),
            "raw_pk6_keep": self.raw_keep.value(),
        })
        return normalize(values)

    def _update_patch_policy(self):
        if self.code_ips_yes.isChecked():
            self.patch_policy.setText(
                "Active policy: code.ips ON → direct reset route. "
                "Communication-error dismissal is disabled; an unexpected "
                "communication-error state causes a safety HOLD."
            )
        else:
            self.patch_policy.setText(
                "Active policy: code.ips OFF → legacy Alpha Sapphire reset route. "
                "Omega Ruby automated starter resets require the hardware-proven "
                "code.ips ON route. Communication-error handling remains RAM-gated."
            )

    def _save(self):
        raw = self.ip.text().strip()
        try:
            ipaddress.ip_address(raw)
        except Exception:
            QMessageBox.warning(
                self,
                "Invalid 3DS IP",
                "Enter a valid IP address, for example 192.168.0.28.",
            )
            return

        settings = self.values()
        settings["three_ds_ip"] = raw
        self._settings = settings
        self.settings_saved.emit(settings)
        self.status.setText("Settings saved")

    def _restore_defaults(self):
        selected = self._settings.get("selected_starter", "torchic")
        values = dict(DEFAULTS)
        values["selected_starter"] = selected
        self.set_values(values)
        self.status.setText("Defaults loaded — press SAVE SETTINGS")

    def _browse_sound(self):
        start = self.sound_path.text().strip()
        if start and not Path(start).is_absolute():
            start = str(self.base_dir / start)

        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select shiny sound",
            start or str(self.base_dir),
            "Wave audio (*.wav);;All files (*.*)",
        )
        if path:
            try:
                relative = Path(path).resolve().relative_to(
                    self.base_dir.resolve()
                )
                self.sound_path.setText(relative.as_posix())
            except Exception:
                self.sound_path.setText(path)

    def _test_sound(self):
        self.test_sound_requested.emit(self.sound_path.text().strip())
