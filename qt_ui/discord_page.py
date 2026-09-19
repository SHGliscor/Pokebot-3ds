from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QLineEdit,
    QCheckBox, QPushButton, QSpinBox, QScrollArea, QListWidget, QListWidgetItem,
)

from .widgets import Panel
from .settings_store import normalize


class DiscordPage(QWidget):
    save_requested = Signal(dict)
    connect_requested = Signal()
    disconnect_requested = Signal()
    test_requested = Signal()
    rpc_connect_requested = Signal()
    rpc_disconnect_requested = Signal()
    rpc_test_requested = Signal()

    def __init__(self, settings, profile_root: Path, parent=None):
        super().__init__(parent)
        self.profile_root = Path(profile_root)
        self._settings = normalize(settings)

        page_outer = QVBoxLayout(self)
        page_outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        outer = QVBoxLayout(content)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(7)
        scroll.setWidget(content)
        page_outer.addWidget(scroll)

        # ------------------------------------------------------------------
        # Bot account: server notifications + normal bot presence.
        # ------------------------------------------------------------------
        top = QHBoxLayout()
        top.setSpacing(7)

        conn = Panel("DISCORD BOT CONNECTION")
        self.enabled = QCheckBox("Enable Discord bot integration")
        conn.outer.addWidget(self.enabled)
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)

        grid.addWidget(QLabel("Bot Token"), 0, 0)
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText("Discord Developer Portal bot token")
        grid.addWidget(self.token, 0, 1)

        grid.addWidget(QLabel("Server / Guild ID"), 1, 0)
        self.guild_id = QLineEdit()
        self.guild_id.setPlaceholderText("Optional")
        grid.addWidget(self.guild_id, 1, 1)

        grid.addWidget(QLabel("Channel ID"), 2, 0)
        self.channel_id = QLineEdit()
        self.channel_id.setPlaceholderText("Required notification channel ID")
        grid.addWidget(self.channel_id, 2, 1)
        conn.outer.addLayout(grid)

        self.auto_connect = QCheckBox("Connect bot automatically when Pokebot3DS-CFW opens")
        conn.outer.addWidget(self.auto_connect)

        row = QHBoxLayout()
        self.connect_btn = QPushButton("CONNECT BOT")
        self.connect_btn.setObjectName("StartButton")
        self.disconnect_btn = QPushButton("DISCONNECT")
        self.disconnect_btn.setObjectName("SmallAction")
        self.test_btn = QPushButton("SEND TEST + 2DS SCREENSHOT")
        self.test_btn.setObjectName("SmallAction")
        row.addWidget(self.connect_btn)
        row.addWidget(self.disconnect_btn)
        row.addWidget(self.test_btn)
        conn.outer.addLayout(row)

        test_note = QLabel(
            "Discord Test also pulls the current 400×240 top screen directly "
            "from Pokebot-CFW and attaches it. No capture card is required."
        )
        test_note.setWordWrap(True)
        test_note.setObjectName("Muted")
        conn.outer.addWidget(test_note)

        self.connection_status = QLabel("Bot not connected")
        self.connection_status.setWordWrap(True)
        self.connection_status.setObjectName("Muted")
        conn.outer.addWidget(self.connection_status)
        top.addWidget(conn, 3)

        notify = Panel("BOT NOTIFICATIONS")
        self.presence = QCheckBox("Live presence on the Discord bot account")
        self.notify_shiny = QCheckBox("✨ Shiny Found")
        self.notify_pokerus = QCheckBox("🦠 New Pokérus detected")
        self.notify_start = QCheckBox("Hunt Started")
        self.notify_stop = QCheckBox("Hunt Stopped")
        self.notify_hold = QCheckBox("Safety HOLD")
        self.notify_milestones = QCheckBox("Phase milestones")
        self.periodic = QCheckBox("Periodic hunt summary")
        for widget in (
            self.presence, self.notify_shiny, self.notify_pokerus, self.notify_start, self.notify_stop,
            self.notify_hold, self.notify_milestones, self.periodic,
        ):
            notify.outer.addWidget(widget)

        period_row = QHBoxLayout()
        period_row.addWidget(QLabel("Summary every"))
        self.summary_minutes = QSpinBox()
        self.summary_minutes.setRange(5, 1440)
        self.summary_minutes.setSuffix(" min")
        period_row.addWidget(self.summary_minutes)
        period_row.addStretch(1)
        notify.outer.addLayout(period_row)
        top.addWidget(notify, 2)
        outer.addLayout(top)

        # ------------------------------------------------------------------
        # Personal account Rich Presence. This uses local Discord IPC only.
        # ------------------------------------------------------------------
        rpc_row = QHBoxLayout()
        rpc_row.setSpacing(7)

        rpc = Panel("PERSONAL RICH PRESENCE")
        self.rpc_enabled = QCheckBox("Enable Rich Presence on my own Discord account")
        rpc.outer.addWidget(self.rpc_enabled)

        rpc_grid = QGridLayout()
        rpc_grid.setHorizontalSpacing(8)
        rpc_grid.setVerticalSpacing(6)
        rpc_grid.addWidget(QLabel("Discord Application ID"), 0, 0)
        self.rpc_app_id = QLineEdit()
        self.rpc_app_id.setPlaceholderText("Application / Client ID — no personal token")
        rpc_grid.addWidget(self.rpc_app_id, 0, 1)
        rpc.outer.addLayout(rpc_grid)

        self.rpc_auto_connect = QCheckBox("Connect personal Rich Presence automatically on launch")
        rpc.outer.addWidget(self.rpc_auto_connect)

        preview = QLabel(
            "Presence layout:\n"
            "Pokebot3DS-CFW\n"
            "Treecko | 518 (0✨) | 93/h\n"
            "Route 101 | Alpha Sapphire\n"
            "Elapsed hunt timer counts up automatically in Discord."
        )
        preview.setWordWrap(True)
        preview.setObjectName("Muted")
        rpc.outer.addWidget(preview)

        rpc_buttons = QHBoxLayout()
        self.rpc_connect_btn = QPushButton("CONNECT MY PRESENCE")
        self.rpc_connect_btn.setObjectName("StartButton")
        self.rpc_disconnect_btn = QPushButton("DISCONNECT")
        self.rpc_disconnect_btn.setObjectName("SmallAction")
        self.rpc_test_btn = QPushButton("TEST PRESENCE")
        self.rpc_test_btn.setObjectName("SmallAction")
        rpc_buttons.addWidget(self.rpc_connect_btn)
        rpc_buttons.addWidget(self.rpc_disconnect_btn)
        rpc_buttons.addWidget(self.rpc_test_btn)
        rpc.outer.addLayout(rpc_buttons)

        self.rpc_connection_status = QLabel("Personal Rich Presence not connected")
        self.rpc_connection_status.setWordWrap(True)
        self.rpc_connection_status.setObjectName("Muted")
        rpc.outer.addWidget(self.rpc_connection_status)
        rpc_row.addWidget(rpc, 3)

        rpc_setup = Panel("RICH PRESENCE SETUP")
        rpc_setup_text = QLabel(
            "1. Use the same Discord Developer application that owns your bot, or create a separate Pokebot3DS-CFW application.\n"
            "2. Copy its Application ID into this tab.\n"
            "3. Upload Rich Presence artwork using these exact asset keys:\n"
            "   • alpha_sapphire — Pokémon Alpha Sapphire image\n"
            "   • omega_ruby — Pokémon Omega Ruby image\n"
            "   • pokebot3ds_cfw — Pokebot3DS-CFW logo (small image)\n"
            "4. Keep the Discord desktop app running and logged into YOUR account.\n"
            "5. SAVE, then CONNECT MY PRESENCE.\n\n"
            "No Discord user token is requested or stored. The large image is always the detected game/version, never the current Pokémon."
        )
        rpc_setup_text.setWordWrap(True)
        rpc_setup_text.setObjectName("Muted")
        rpc_setup.outer.addWidget(rpc_setup_text)
        rpc_row.addWidget(rpc_setup, 2)
        outer.addLayout(rpc_row)

        middle = QHBoxLayout()
        middle.setSpacing(7)

        milestones = Panel("PHASE MILESTONES")
        milestones.outer.addWidget(QLabel("Notify when phase encounters cross:"))
        self.milestone_values = QLineEdit()
        self.milestone_values.setPlaceholderText("100,500,1000,2048,4096")
        milestones.outer.addWidget(self.milestone_values)
        note = QLabel(
            "Comma-separated encounter counts. Milestones are notification-only and never affect hunt control."
        )
        note.setWordWrap(True)
        note.setObjectName("Muted")
        milestones.outer.addWidget(note)
        middle.addWidget(milestones, 1)

        setup = Panel("BOT SETUP")
        setup_text = QLabel(
            "1. Create a Bot in the Discord Developer Portal.\n"
            "2. Invite it to your server with View Channel, Send Messages and Embed Links.\n"
            "3. Enable Discord Developer Mode, then copy the server/channel IDs.\n"
            "4. Paste the token and Channel ID here, SAVE, then CONNECT BOT.\n\n"
            "Message Content intent is not required because Pokebot3DS-CFW only sends telemetry."
        )
        setup_text.setWordWrap(True)
        setup_text.setObjectName("Muted")
        setup.outer.addWidget(setup_text)
        middle.addWidget(setup, 1)
        outer.addLayout(middle)

        events = Panel("RECENT DISCORD EVENTS")
        self.events = QListWidget()
        self.events.setMinimumHeight(145)
        events.outer.addWidget(self.events)
        event_note = QLabel(
            "Discord is telemetry only. Bot/Rich Presence failures cannot authorise a reset, alter RAM authority, or cancel a shiny HOLD. The bot token is excluded from Support Export ZIPs; personal Rich Presence never uses a user token."
        )
        event_note.setWordWrap(True)
        event_note.setObjectName("Muted")
        events.outer.addWidget(event_note)
        outer.addWidget(events)

        actions = QHBoxLayout()
        self.save_btn = QPushButton("SAVE DISCORD SETTINGS")
        self.save_btn.setObjectName("StartButton")
        self.save_status = QLabel("")
        self.save_status.setObjectName("SmallGreen")
        actions.addWidget(self.save_status)
        actions.addStretch(1)
        actions.addWidget(self.save_btn)
        outer.addLayout(actions)
        outer.addStretch(1)

        self.save_btn.clicked.connect(self._save)
        self.connect_btn.clicked.connect(self.connect_requested.emit)
        self.disconnect_btn.clicked.connect(self.disconnect_requested.emit)
        self.test_btn.clicked.connect(self.test_requested.emit)
        self.rpc_connect_btn.clicked.connect(self.rpc_connect_requested.emit)
        self.rpc_disconnect_btn.clicked.connect(self.rpc_disconnect_requested.emit)
        self.rpc_test_btn.clicked.connect(self.rpc_test_requested.emit)
        self.set_values(self._settings)

    def set_values(self, settings):
        s = normalize(settings)
        self._settings = s
        self.enabled.setChecked(bool(s.get("discord_enabled", False)))
        self.token.setText(str(s.get("discord_bot_token") or ""))
        self.guild_id.setText(str(s.get("discord_guild_id") or ""))
        self.channel_id.setText(str(s.get("discord_channel_id") or ""))
        self.auto_connect.setChecked(bool(s.get("discord_auto_connect", False)))
        self.presence.setChecked(bool(s.get("discord_presence_enabled", True)))
        self.notify_shiny.setChecked(bool(s.get("discord_notify_shiny", True)))
        self.notify_pokerus.setChecked(bool(s.get("discord_notify_pokerus", True)))
        self.notify_start.setChecked(bool(s.get("discord_notify_hunt_started", True)))
        self.notify_stop.setChecked(bool(s.get("discord_notify_hunt_stopped", True)))
        self.notify_hold.setChecked(bool(s.get("discord_notify_safety_hold", True)))
        self.notify_milestones.setChecked(bool(s.get("discord_notify_milestones", True)))
        self.periodic.setChecked(bool(s.get("discord_periodic_summary", False)))
        self.summary_minutes.setValue(int(s.get("discord_summary_minutes", 60)))
        self.milestone_values.setText(str(s.get("discord_phase_milestones") or "100,500,1000,2048,4096"))
        self.rpc_enabled.setChecked(bool(s.get("discord_rpc_enabled", False)))
        self.rpc_app_id.setText(str(s.get("discord_rpc_application_id") or ""))
        self.rpc_auto_connect.setChecked(bool(s.get("discord_rpc_auto_connect", False)))

    def values(self):
        values = dict(self._settings)
        values.update({
            "discord_enabled": self.enabled.isChecked(),
            "discord_bot_token": self.token.text().strip(),
            "discord_guild_id": self.guild_id.text().strip(),
            "discord_channel_id": self.channel_id.text().strip(),
            "discord_auto_connect": self.auto_connect.isChecked(),
            "discord_presence_enabled": self.presence.isChecked(),
            "discord_notify_shiny": self.notify_shiny.isChecked(),
            "discord_notify_pokerus": self.notify_pokerus.isChecked(),
            "discord_notify_hunt_started": self.notify_start.isChecked(),
            "discord_notify_hunt_stopped": self.notify_stop.isChecked(),
            "discord_notify_safety_hold": self.notify_hold.isChecked(),
            "discord_notify_milestones": self.notify_milestones.isChecked(),
            "discord_periodic_summary": self.periodic.isChecked(),
            "discord_summary_minutes": self.summary_minutes.value(),
            "discord_phase_milestones": self.milestone_values.text().strip(),
            "discord_rpc_enabled": self.rpc_enabled.isChecked(),
            "discord_rpc_application_id": self.rpc_app_id.text().strip(),
            "discord_rpc_auto_connect": self.rpc_auto_connect.isChecked(),
        })
        return normalize(values)

    def _save(self):
        self._settings = self.values()
        self.save_requested.emit(dict(self._settings))
        self.save_status.setText("Discord settings saved")

    def set_connection(self, payload):
        payload = dict(payload or {})
        if payload.get("connected"):
            self.connection_status.setText(f"● CONNECTED — {payload.get('detail', '')}")
            self.connection_status.setStyleSheet("color:#73ff60;font-weight:800;")
        elif payload.get("connecting"):
            self.connection_status.setText("● CONNECTING…")
            self.connection_status.setStyleSheet("color:#ffd36a;font-weight:800;")
        else:
            self.connection_status.setText(f"● OFFLINE — {payload.get('detail', 'Not connected')}")
            self.connection_status.setStyleSheet("color:#ff8b8b;font-weight:800;")

    def set_rpc_connection(self, payload):
        payload = dict(payload or {})
        if payload.get("connected"):
            self.rpc_connection_status.setText(f"● CONNECTED TO MY ACCOUNT — {payload.get('detail', '')}")
            self.rpc_connection_status.setStyleSheet("color:#73ff60;font-weight:800;")
        elif payload.get("connecting"):
            self.rpc_connection_status.setText("● CONNECTING TO DISCORD DESKTOP…")
            self.rpc_connection_status.setStyleSheet("color:#ffd36a;font-weight:800;")
        else:
            self.rpc_connection_status.setText(f"● OFFLINE — {payload.get('detail', 'Not connected')}")
            self.rpc_connection_status.setStyleSheet("color:#ff8b8b;font-weight:800;")

    def add_event(self, event):
        event = dict(event or {})
        stamp = str(event.get("time") or "")
        if "T" in stamp:
            stamp = stamp.split("T", 1)[1][:8]
        marker = "✓" if event.get("ok", True) else "✕"
        text = f"{stamp}  {marker}  {event.get('type','EVENT')} — {event.get('message','')}"
        self.events.insertItem(0, QListWidgetItem(text))
        while self.events.count() > 40:
            self.events.takeItem(self.events.count() - 1)

    def set_events(self, events):
        self.events.clear()
        for item in reversed(list(events or [])[-40:]):
            self.add_event(item)
