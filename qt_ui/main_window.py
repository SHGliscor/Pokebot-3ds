
from __future__ import annotations

import json
from pokebot.common.ability_names import ability_name
import os
import platform
import sys
import zipfile
import hashlib
import shutil
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFrame, QLabel,
    QPushButton, QStackedWidget, QMessageBox, QFileDialog
)

from .backend_worker import HuntWorker, ProbeWorker, PartyRefreshWorker, RngRefreshWorker, ControllerToolWorker
from pokebot.common.live_party import get_runtime_party_snapshot
from pokebot.common.bridge import Bridge
from pokebot.common.gen6_locations import starter_location_for_family
from pokebot.static.oras_static import get_static_profile
from pokebot.gift.oras_gifts import get_gift_profile
from .dashboard_page import DashboardPage
from .hunt_page import HuntPage
from .oak_challenge_page import OakChallengePage
from .stats_page import StatsPage
from .tools_page import ToolsPage
from .appdata_store import ensure_profile, reset_all_stats
from .settings_page import SettingsPage
from .discord_page import DiscordPage
from .discord_service import DiscordManager
from .discord_rich_presence import DiscordRichPresenceManager
from .settings_store import load_settings, save_settings
from .shiny_alert import play_shiny_sound
from .shutdown_diagnostics import record_event, record_exception

TAB_NAMES = [
    "DASHBOARD", "HUNTS", "OAK CHALLENGE", "STATISTICS", "TOOLS", "DISCORD", "SETTINGS"
]

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        # Use the native operating-system window frame. This provides the
        # standard movable/resizable Windows title bar with minimise,
        # maximise/restore and close controls.

        self.base_dir = Path(__file__).resolve().parents[1]

        self.profile = ensure_profile(self.base_dir)
        self.settings_path = self.profile.settings_path
        self.settings = load_settings(self.settings_path)

        self.host = self.settings["three_ds_ip"]
        self.bridge_port = int(self.settings["ram_bridge_port"])
        self.input_port = int(self.settings["input_port"])
        self.bridge_timeout = float(self.settings["bridge_timeout_s"])
        if self.settings.get("always_on_top"):
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)

        self.hunt_thread = None
        self.hunt_worker = None
        self.probe_thread = None
        self.probe_worker = None
        self.party_refresh_thread = None
        self.party_refresh_worker = None
        self.rng_refresh_thread = None
        self.rng_refresh_worker = None
        # HF83: retain the locally reconstructed live MT cursor between short
        # refresh workers. A full 624-word snapshot is only needed to acquire
        # or reacquire a target; normal ticks read a tiny coherent cursor.
        self.rng_tracking_state = None
        self._closing = False
        self._shutdown_wait_timer = None
        self._shutdown_started = False
        self.ram_read_count = 0
        self._shiny_sound_seen = set()
        self.current_game_profile = None

        instance_name = str(os.environ.get("POKEBOT_INSTANCE_NAME") or "").strip()
        title = "Pokebot3DS-CFW — v0p43EJ HF100 ORAS Professor Oak Challenge — Pokémon Tracker"
        if instance_name:
            title += f" — {instance_name}"
        self.setWindowTitle(title)
        # Closely matches the window footprint in the supplied 1280x800 photo.
        self.resize(1120, 720)
        self.setMinimumSize(1000, 660)

        central = QWidget()
        central.setObjectName("MainFrame")
        self.setCentralWidget(central)

        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        tabs_frame = QFrame()
        tabs_frame.setObjectName("TopTabs")
        tabs_layout = QHBoxLayout(tabs_frame)
        tabs_layout.setContentsMargins(0, 0, 0, 0)
        tabs_layout.setSpacing(0)

        self.stack = QStackedWidget()
        self.dashboard = DashboardPage()
        self._load_last_seen_history()
        self._load_recent_shiny_history()
        self.discord_manager = DiscordManager(self.profile.root, self)
        self.discord_manager.apply_settings(self.settings)
        self.discord_rpc = DiscordRichPresenceManager(self.profile.root, self)
        self.discord_rpc.apply_settings(self.settings)

        self.pages = [
            self.dashboard,
            HuntPage(
                self.profile.root,
                self.base_dir,
                download_sprites=self.settings.get("download_oras_sprites", True),
            ),
            OakChallengePage(self.profile.root),
            StatsPage(self.profile, self.base_dir),
            ToolsPage(self.profile, self.base_dir),
            DiscordPage(self.settings, self.profile.root),
            SettingsPage(self.settings, self.base_dir),
        ]

        self.hunt_page = self.pages[1]
        self.oak_page = self.pages[2]
        self.stats_page = self.pages[3]
        self.tools_page = self.pages[4]
        self.discord_page = self.pages[5]
        self.settings_page = self.pages[6]
        self.tool_thread = None
        self.tool_worker = None

        self.tab_buttons = []
        for i, name in enumerate(TAB_NAMES):
            btn = QPushButton(name)
            btn.setObjectName("TabButton")
            btn.setProperty("active", i == 0)
            btn.clicked.connect(
                lambda checked=False, idx=i: self.switch_tab(idx)
            )
            tabs_layout.addWidget(btn)
            self.tab_buttons.append(btn)

        # Native Windows title-bar controls are used; do not add custom
        # window buttons to the tab row.
        tabs_layout.addStretch(1)

        outer.addWidget(tabs_frame)

        for page in self.pages:
            self.stack.addWidget(page)
        outer.addWidget(self.stack, 1)

        status = QFrame()
        status.setObjectName("StatusBar")
        srow = QHBoxLayout(status)
        srow.setContentsMargins(8, 4, 8, 4)

        self.left_status = QLabel(
            f"3DS {self.host}  |  RAM :{self.bridge_port}  |  "
            f"Pokebot-Luma Input/ACK :{self.input_port}   |   code.ips "
            f"{'ON' if self.settings.get('use_code_ips', False) else 'OFF'}"
        )
        self.left_status.setStyleSheet(
            "color:#9bcf96;font-weight:600;"
        )
        self.center_status = QLabel(
            "ORAS  |  Starters  |  Idle"
        )
        self.center_status.setAlignment(Qt.AlignCenter)
        self.right_status = QLabel("RAM Reads: 0")

        srow.addWidget(self.left_status)
        srow.addStretch(1)
        srow.addWidget(self.center_status)
        srow.addStretch(1)
        srow.addWidget(self.right_status)
        outer.addWidget(status)

        self.dashboard.start_requested.connect(self.start_hunt)
        self.dashboard.wild_start_requested.connect(self.start_wild_hunt)
        self.dashboard.static_start_requested.connect(self.start_static_hunt)
        self.dashboard.gift_start_requested.connect(self.start_gift_hunt)
        self.dashboard.stop_requested.connect(self.stop_hunt)
        self.dashboard.probe_requested.connect(self.run_probe)
        self.dashboard.starter_changed.connect(self._starter_changed)
        self.dashboard.hunt_type_changed.connect(self._hunt_type_changed)
        self.dashboard.target_settings_changed.connect(self._target_settings_changed)
        self.dashboard.auto_throw_shiny_changed.connect(
            self._auto_throw_shiny_changed
        )
        self.dashboard.capture_ball_override_changed.connect(
            self._capture_ball_override_changed
        )
        self.settings_page.settings_saved.connect(self._settings_saved)
        self.settings_page.test_connection_requested.connect(self.run_probe)
        self.settings_page.test_sound_requested.connect(self._test_shiny_sound)
        self.settings_page.reset_all_stats_requested.connect(self._reset_all_stats)
        self.settings_page.open_stats_folder_requested.connect(self._open_stats_folder)
        self.settings_page.export_support_requested.connect(self._export_support_zip)
        self.hunt_page.hunt_selected.connect(self._browser_hunt_selected)
        self.hunt_page.shiny_block_changed.connect(
            self._shiny_blocklist_changed
        )
        self.tools_page.connection_test_requested.connect(self.run_probe)
        self.tools_page.self_test_requested.connect(self.run_probe)
        self.tools_page.support_export_requested.connect(self._export_support_zip)
        self.tools_page.open_folder_requested.connect(self._open_tool_folder)
        self.tools_page.controller_test_requested.connect(self._tool_controller_button)
        self.tools_page.touch_test_requested.connect(self._tool_touch_test)
        self.tools_page.patch_validate_requested.connect(self._tool_validate_patch)
        self.tools_page.cache_clear_requested.connect(self._tool_clear_sprite_cache)
        self.discord_page.save_requested.connect(self._discord_settings_saved)
        self.discord_page.connect_requested.connect(self._discord_connect)
        self.discord_page.disconnect_requested.connect(self.discord_manager.disconnect_bot)
        self.discord_page.test_requested.connect(self.discord_manager.send_test)
        self.discord_manager.connection_changed.connect(self.discord_page.set_connection)
        self.discord_manager.event_emitted.connect(self.discord_page.add_event)
        self.discord_page.rpc_connect_requested.connect(self._discord_rpc_connect)
        self.discord_page.rpc_disconnect_requested.connect(self.discord_rpc.disconnect_presence)
        self.discord_page.rpc_test_requested.connect(self.discord_rpc.send_test_presence)
        self.discord_rpc.connection_changed.connect(self.discord_page.set_rpc_connection)
        self.discord_rpc.event_emitted.connect(self.discord_page.add_event)
        self.discord_page.set_events(self.discord_manager.recent_events())

        self.discord_timer = QTimer(self)
        self.discord_timer.setInterval(60_000)
        self.discord_timer.timeout.connect(self.discord_manager.maybe_periodic_summary)
        self.discord_timer.start()

        # Personal Rich Presence does not need per-second updates: Discord renders
        # the elapsed timer from the hunt start timestamp. A 15s refresh matches
        # the cadence used by Pokébot Gen3 without touching hunt authority.
        self.discord_rpc_timer = QTimer(self)
        self.discord_rpc_timer.setInterval(15_000)
        self.discord_rpc_timer.timeout.connect(self.discord_rpc.refresh)
        self.discord_rpc_timer.start()

        # D25 Party Viewer: hunt-independent field runtime telemetry.
        # It follows the game's live owner -> party -> PokemonParam -> PK6
        # pointer chain every 1 second. Hunt workers still own RAM access while
        # a hunt is running.
        self.party_refresh_timer = QTimer(self)
        # One refresh per second keeps the idle party viewer responsive while
        # halving short-lived QThread creation and RAM traffic on low-end PCs.
        self.party_refresh_timer.setInterval(1_000)
        self.party_refresh_timer.timeout.connect(self._refresh_idle_party)
        self.party_refresh_timer.start()
        QTimer.singleShot(2_500, self._refresh_idle_party)

        # HF94: raw main-MT telemetry is hidden/disabled for normal hunting.
        # This removes the redundant dashboard panel and, more importantly,
        # stops its 0.5 s RAM polling from competing with DexNav steering and
        # battle-state reads on UDP/4952.  Tracker implementation is retained
        # in the package for future diagnostics, but no worker is launched.
        self.rng_refresh_timer = None

        self.dashboard.apply_ui_settings(self.settings)

        if self.settings.get("remember_selected_starter"):
            self.dashboard.select_starter(
                self.settings.get("selected_starter", "torchic")
            )

        # Load each starter's persistent shiny-phase state so the Phase section
        # can show all target phases before a hunt is started.
        self._load_target_phases()

        # Let the first window paint before starting network/background work.
        # Starting these QThreads/services inside MainWindow construction can
        # make launch look hung on slower laptops even though they are async.
        if self.settings.get("auto_connection_test", True):
            QTimer.singleShot(350, self.run_probe)

        if self.settings.get("discord_enabled", False) and self.settings.get("discord_auto_connect", False):
            QTimer.singleShot(800, self.discord_manager.connect_bot)
        if self.settings.get("discord_rpc_enabled", False) and self.settings.get("discord_rpc_auto_connect", False):
            QTimer.singleShot(1_100, self.discord_rpc.connect_presence)

    def _load_target_phases(self):
        phase_data = {}
        stats_dir = self.profile.stats_dir

        for key in ("treecko", "torchic", "mudkip"):
            path = stats_dir / f"{key}.json"
            payload = {}
            if path.exists():
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    payload = {}

            lifetime_shinies = int(payload.get("lifetime_shinies", 0))
            phase_data[key] = {
                "phase_index": lifetime_shinies + 1,
                "phase_seen": int(payload.get("phase_seen", 0)),
                "phase_log_miss": payload.get("phase_log_miss"),
                "phase_cumulative_probability": payload.get("phase_cumulative_probability"),
            }

        for method in ("walk", "run", "acro_bunny", "horde", "cave", "surf", "fishing"):
            path = stats_dir / f"wild_{method}.json"
            payload = {}
            if path.exists():
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    payload = {}
            phase_data[f"wild_{method}"] = {
                "phase_index": int(payload.get("lifetime_shinies", 0)) + 1,
                "phase_seen": int(payload.get("phase_seen", 0)),
                "phase_log_miss": payload.get("phase_log_miss"),
                "phase_cumulative_probability": payload.get("phase_cumulative_probability"),
            }

        self.dashboard.set_target_phase_data(phase_data)

    def _load_last_seen_history(self):
        path = self.profile.last_seen_path
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self.dashboard.set_last_seen_history(data)
        except Exception:
            pass


    def _load_recent_shiny_history(self):
        """Restore shiny history and backfill legacy records from encounter ledgers."""
        path = self.profile.recent_shinies_path
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return

            # HF62 began persisting IVs, but older shiny history only stored the
            # sprite/SV fields.  Recover whatever is still present in the durable
            # encounter JSONL ledgers so upgrades do not leave permanent dashes.
            missing = {}
            for item in data:
                pid = str(item.get("pokemon_pid") or item.get("pid") or "").strip().lower()
                if pid and (not item.get("ivs") or item.get("nature") in (None, "", "—") or item.get("ability") in (None, "", "—")):
                    missing.setdefault(pid, []).append(item)
            if missing and self.profile.encounters_dir.exists():
                for ledger in self.profile.encounters_dir.glob("*.jsonl"):
                    try:
                        with ledger.open("r", encoding="utf-8", errors="ignore") as fh:
                            for line in fh:
                                try:
                                    rec = json.loads(line)
                                except Exception:
                                    continue
                                pid = str(rec.get("pokemon_pid") or rec.get("pid") or "").strip().lower()
                                if pid not in missing:
                                    continue
                                for item in missing[pid]:
                                    if not item.get("ivs") and isinstance(rec.get("ivs"), dict):
                                        item["ivs"] = dict(rec["ivs"])
                                    if item.get("nature") in (None, "", "—") and rec.get("nature"):
                                        item["nature"] = rec.get("nature")
                                    if item.get("gender") in (None, "", "—") and rec.get("gender"):
                                        item["gender"] = rec.get("gender")
                                    aid = rec.get("ability_id")
                                    if aid is not None and item.get("ability") in (None, "", "—"):
                                        item["ability_id"] = aid
                                        item["ability"] = ability_name(aid)
                                    elif item.get("ability") in (None, "", "—") and rec.get("ability") not in (None, "", "—"):
                                        item["ability"] = str(rec.get("ability"))
                    except Exception:
                        continue

            # Ability ID was already in PK6 authority even when its display name
            # was not persisted. Resolve it locally when available.
            for item in data:
                if item.get("ability") in (None, "", "—") and item.get("ability_id") is not None:
                    item["ability"] = ability_name(item.get("ability_id"))
            try:
                path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            except Exception:
                pass
            self.dashboard.set_recent_shinies(data)
        except Exception:
            pass

    def switch_tab(self, idx):
        self.stack.setCurrentIndex(idx)
        if idx == 3:
            self.stats_page.refresh()
        for i, btn in enumerate(self.tab_buttons):
            btn.setProperty("active", i == idx)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _shiny_blocklist_changed(self, payload):
        payload = dict(payload or {})
        species_name = str(payload.get("species_name") or "Pokémon")
        enabled = bool(payload.get("enabled"))
        action = "RUN FROM SHINY" if enabled else "SHINY HOLD"
        live = (
            " • applies to the active Wild hunt immediately"
            if self.hunt_thread and self.hunt_thread.isRunning()
            else ""
        )
        self.dashboard.append_log(
            f"SHINY BLOCKLIST: {species_name} -> {action}{live}"
        )

    def _browser_hunt_selected(self, selection):
        selection = dict(selection or {})
        if self.hunt_thread and self.hunt_thread.isRunning():
            QMessageBox.warning(
                self,
                "Hunt is running",
                "Stop the current hunt before choosing another HUNTS target.",
            )
            return

        if not self.dashboard.set_browser_wild_target(selection):
            QMessageBox.warning(
                self,
                "Target not available",
                "That encounter table is browser-only or is not enabled for the "
                "currently detected game.",
            )
            return

        # Grass, Cave and Surf all live under the top-level Wild hunt type.
        self.dashboard.select_hunt_type("wild")
        self.dashboard.set_browser_wild_target(selection)
        target_type = str(selection.get("hunt_type"))
        if target_type == "cave":
            self.dashboard.selected_wild_environment = "cave"
            self.dashboard.selected_wild_movement = "walk"
            self.dashboard.selected_wild_method = "cave"
        elif target_type == "surf":
            self.dashboard.selected_wild_environment = "water"
            self.dashboard.selected_wild_method = "surf"

        if target_type in {"cave", "surf"}:
            self.dashboard._populate_wild_profiles()
            self.dashboard._refresh_wild_type_buttons()
            self.dashboard._refresh_wild_movement_panel()
            self.dashboard._refresh_axis_panel()
            self.dashboard._refresh_wild_method_display()
        self.switch_tab(0)

        self.dashboard.append_log(
            "HUNTS target selected: "
            f"{selection.get('species_name')} • "
            f"{selection.get('location_name')} • "
            f"{selection.get('section_title')}. "
            "Live game/location/terrain will be RAM-validated at Start."
        )

    def _starter_changed(self, key):
        if self.settings.get("remember_selected_starter", True):
            self.settings["selected_starter"] = key
            self.settings = save_settings(
                self.settings_path,
                self.settings,
            )

    def _target_settings_changed(self, criteria):
        merged = dict(self.settings)
        merged["look_for_target"] = dict(criteria or {})
        self.settings = save_settings(self.settings_path, merged)
        # Keep both settings surfaces on the same normalized backing copy.
        self.settings_page._settings = dict(self.settings)

    def _auto_throw_shiny_changed(self, enabled):
        merged = dict(self.settings)
        merged["auto_throw_one_poke_ball_on_shiny"] = bool(enabled)
        self.settings = save_settings(self.settings_path, merged)
        self.settings_page._settings = dict(self.settings)

    def _capture_ball_override_changed(self, ball_key):
        merged = dict(self.settings)
        merged["capture_ball_override"] = str(ball_key or "best")
        self.settings = save_settings(self.settings_path, merged)
        self.settings_page._settings = dict(self.settings)

    def _hunt_type_changed(self, hunt_type):
        mode = str(hunt_type).lower()
        if mode == "fishing":
            game_name = (self.current_game_profile or {}).get("name", "ORAS")
            self.center_status.setText(f"{game_name}  |  Fishing")
        elif mode == "wild":
            self.center_status.setText(
                "ORAS  |  Wild"
            )
        elif mode == "static":
            game_name = (self.current_game_profile or {}).get("name", "ORAS")
            self.center_status.setText(f"{game_name}  |  Static")
        elif mode == "gift":
            game_name = (self.current_game_profile or {}).get("name", "ORAS")
            self.center_status.setText(f"{game_name}  |  Gift Pokémon")
        else:
            game_name = (self.current_game_profile or {}).get(
                "name", "RAM-detected ORAS game"
            )
            self.center_status.setText(
                f"{game_name}  |  Starters"
            )

    def _settings_saved(self, settings):
        self.settings = save_settings(
            self.settings_path,
            settings,
        )
        self.host = self.settings["three_ds_ip"]
        self.bridge_port = int(self.settings["ram_bridge_port"])
        self.input_port = int(self.settings["input_port"])
        self.bridge_timeout = float(self.settings["bridge_timeout_s"])

        if self.settings.get("remember_selected_starter", True):
            self.settings["selected_starter"] = self.dashboard.selected_starter
            self.settings = save_settings(
                self.settings_path,
                self.settings,
            )

        self.dashboard.apply_ui_settings(self.settings)
        self.hunt_page.set_download_sprites(
            self.settings.get("download_oras_sprites", True)
        )
        self.discord_manager.apply_settings(self.settings)
        self.discord_rpc.apply_settings(self.settings)
        self.discord_page.set_values(self.settings)

        self.left_status.setText(
            f"3DS {self.host}  |  RAM :{self.bridge_port}  |  "
            f"Pokebot-Luma Input/ACK :{self.input_port}   |   code.ips "
            f"{'ON' if self.settings.get('use_code_ips', False) else 'OFF'}"
        )

        self.setWindowFlag(
            Qt.WindowStaysOnTopHint,
            bool(self.settings.get("always_on_top")),
        )
        # Changing a window flag re-creates the native window handle.
        self.show()

    def _discord_settings_saved(self, settings):
        # Merge with the current settings so Discord never resets unrelated bot options.
        merged = dict(self.settings)
        merged.update(dict(settings or {}))
        self.settings = save_settings(self.settings_path, merged)
        self.discord_manager.apply_settings(self.settings)
        self.discord_rpc.apply_settings(self.settings)
        self.discord_page.set_values(self.settings)
        # Keep the general Settings page's backing copy current as well.
        self.settings_page._settings = dict(self.settings)

    def _discord_connect(self):
        # Save current fields first so Connect always uses what is visible in the tab.
        self._discord_settings_saved(self.discord_page.values())
        self.discord_manager.connect_bot()

    def _discord_rpc_connect(self):
        # Personal Rich Presence uses the local Discord desktop IPC connection.
        # It never asks for or stores a Discord user token.
        self._discord_settings_saved(self.discord_page.values())
        self.discord_rpc.connect_presence()

    def _discord_context(self):
        stats = dict(getattr(self, "_discord_last_stats", {}) or {})
        mode = self.dashboard.selected_hunt_type
        if mode == "wild":
            selected_method = self.dashboard.selected_wild_method
            fallback_method = (
                "Fishing" if selected_method == "fishing"
                else ("Horde" if selected_method == "horde"
                      else ("Cave" if selected_method in {"cave", "cave_run", "cave_bunny"} else "Wild"))
            )
            fallback_target = (
                "Fishing" if selected_method == "fishing"
                else ("Hordes" if selected_method == "horde" else "Wild Pokémon")
            )
            fallback_location = None
        elif mode == "static":
            try:
                static_profile = get_static_profile(self.dashboard.selected_static_profile)
                fallback_target = static_profile.name
                fallback_location = static_profile.location
            except Exception:
                fallback_target = "Static Pokémon"
                fallback_location = None
            fallback_method = "Static"
        elif mode == "gift":
            try:
                gift_profile = get_gift_profile(self.dashboard.selected_gift_profile)
                fallback_target = gift_profile.name
                fallback_location = gift_profile.location
            except Exception:
                fallback_target = "Gift Pokémon"
                fallback_location = None
            fallback_method = "Gift"
        else:
            fallback_method = "Starter"
            fallback_target = (
                "Random Starters" if self.dashboard.selected_starter == "random"
                else str(self.dashboard.selected_starter).title()
            )
            fallback_location = starter_location_for_family((self.current_game_profile or {}).get("family") or getattr(self.dashboard, "selected_game_family", None))
        method = stats.get("method_name") or stats.get("method") or fallback_method
        return {
            "game": (self.current_game_profile or {}).get("name") or stats.get("game") or "ORAS",
            "target": stats.get("target") or stats.get("starter") or fallback_target,
            "method": method,
            "location_name": stats.get("location_name") or fallback_location,
        }

    def _worker_status_update(self, status, detail):
        self.dashboard.set_status(status, detail)
        context = self._discord_context()
        self.discord_manager.status_update(status, detail, context)
        self.discord_rpc.status_update(status, detail, context)

    def _encounter_update(self, payload):
        self.dashboard.update_encounter(payload)

        # Play the shiny alert at the RAM-confirmed encounter boundary, not at
        # hunt shutdown. Auto-Capture intentionally keeps the hunt running, so
        # tying audio to _hunt_finished() silently skipped Wild/Horde/Fishing
        # shinies that were caught successfully.
        event = dict(payload or {})
        if bool(event.get("is_shiny")):
            sound_key = (
                str(event.get("pokemon_pid") or event.get("pid") or ""),
                int(event.get("species") or event.get("species_id") or 0),
                int(event.get("horde_slot") or 0),
            )
            if sound_key not in self._shiny_sound_seen:
                self._shiny_sound_seen.add(sound_key)
                # Keep the dedupe cache bounded for very long-running sessions.
                if len(self._shiny_sound_seen) > 64:
                    self._shiny_sound_seen = set(list(self._shiny_sound_seen)[-32:])
                if self.settings.get("shiny_sound_enabled", True):
                    play_shiny_sound(
                        self.base_dir,
                        self.settings.get(
                            "shiny_sound_path",
                            "assets/gen6_shiny_notification.wav",
                        ),
                    )

        context = self._discord_context()
        self.discord_manager.encounter_update(payload, context)
        self.discord_rpc.encounter_update(payload, context)

    def _pokerus_detected(self, payload):
        event = dict(payload or {})
        infections = list(event.get("new_infections") or [])
        names = ", ".join(
            f"slot {rec.get('slot')} {rec.get('species_name', 'Pokémon')}"
            for rec in infections
        ) or "party Pokémon"
        self.dashboard.append_log(
            f"🦠 POKÉRUS DETECTED • {names} • "
            f"{event.get('location_name') or 'ORAS'}"
        )
        self.discord_manager.pokerus_detected(
            event,
            self._discord_context(),
        )

    def _test_shiny_sound(self, configured_path):
        play_shiny_sound(
            self.base_dir,
            configured_path or "assets/gen6_shiny_notification.wav",
        )

    def _open_stats_folder(self):
        try:
            self.profile.root.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(str(self.profile.root))
            else:
                self.settings_page.set_reset_status(str(self.profile.root))
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Could not open stats folder",
                f"{type(exc).__name__}: {exc}",
            )

    def _open_tool_folder(self, key):
        mapping = {
            "stats": self.profile.stats_dir,
            "logs": self.profile.root / "logs",
            "cache": self.profile.root / "cache" / "oras_sprites",
            "support": self.profile.root / "support",
            "appdata": self.profile.root,
        }
        path = Path(mapping.get(str(key), self.profile.root))
        try:
            path.mkdir(parents=True, exist_ok=True)
            if os.name == "nt": os.startfile(str(path))
            else: self.tools_page.set_controller_result(str(path), True)
        except Exception as exc:
            self.tools_page.set_controller_result(f"Open folder failed: {type(exc).__name__}: {exc}", False)

    def _start_tool_worker(self, *, button=None, touch=False):
        if self.tool_thread and self.tool_thread.isRunning():
            return
        thread=QThread(self)
        thread.setObjectName("ControllerToolThread")
        worker=ControllerToolWorker(self.host, self.input_port, button=button, touch=touch, timeout=min(self.bridge_timeout,1.5))
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.result.connect(self._tool_controller_result)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._tool_worker_cleanup)
        self.tool_thread=thread; self.tool_worker=worker; thread.start()

    def _tool_controller_button(self, button):
        self._start_tool_worker(button=button, touch=False)

    def _tool_touch_test(self):
        self._start_tool_worker(touch=True)

    def _tool_controller_result(self, result):
        ok=bool((result or {}).get("ok"))
        if ok:
            if result.get("mode") == "touch":
                text=f"Touch PASS • sequence {result.get('sequence_id')} • {result.get('latency_ms')} ms"
            else:
                text=f"{result.get('button')} PASS • {result.get('state')} • {result.get('latency_ms')} ms"
        else:
            text=f"Controller HOLD • {result.get('error','unknown error')}"
        self.tools_page.set_controller_result(text, ok)

    def _tool_worker_cleanup(self):
        self.tool_thread=None; self.tool_worker=None

    def _tool_validate_patch(self):
        gp = self.current_game_profile or {}
        if gp.get("family") == "xy":
            self.tools_page.set_patch_result("Pokémon X/Y: code.ips is not required by Pokebot.", True)
            return
        selected, _ = QFileDialog.getOpenFileName(self, "Select local code.ips", str(self.base_dir), "IPS patch (code.ips *.ips);;All files (*)")
        if not selected:
            return
        title = "000400000011C400" if gp.get("key") == "omega_ruby" else "000400000011C500" if gp.get("key") == "alpha_sapphire" else None
        if not title:
            self.tools_page.set_patch_result("Patch validation HOLD: detect Omega Ruby or Alpha Sapphire first.", False); return
        expected_path = self.base_dir / "3ds_sd" / "luma" / "titles" / title / "code.ips"
        try:
            actual_hash=hashlib.sha256(Path(selected).read_bytes()).hexdigest()
            expected_hash=hashlib.sha256(expected_path.read_bytes()).hexdigest()
            ok=actual_hash == expected_hash
            self.tools_page.set_patch_result(
                f"{'PASS' if ok else 'MISMATCH'} • {gp.get('name')} • expected path luma/titles/{title}/code.ips • expected SHA256 {expected_hash} • selected SHA256 {actual_hash}", ok
            )
        except Exception as exc:
            self.tools_page.set_patch_result(f"Patch validation failed: {type(exc).__name__}: {exc}", False)

    def _tool_clear_sprite_cache(self):
        cache=self.profile.root / "cache" / "oras_sprites"
        answer=QMessageBox.question(self,"Clear sprite cache?","Cached normal/shiny artwork will be deleted and rebuilt on demand. Hunt/RAM data is not affected.",QMessageBox.Yes|QMessageBox.No,QMessageBox.No)
        if answer != QMessageBox.Yes: return
        try:
            if cache.exists():
                shutil.rmtree(cache)
            cache.mkdir(parents=True,exist_ok=True)
            self.tools_page.set_cache_result("Sprite cache cleared. Artwork will rebuild on demand.", True)
        except Exception as exc:
            self.tools_page.set_cache_result(f"Cache clear failed: {type(exc).__name__}: {exc}", False)

    def _reset_all_stats(self):
        if self.hunt_thread and self.hunt_thread.isRunning():
            QMessageBox.warning(
                self,
                "Hunt is running",
                "Stop the hunt and wait for the safe stop to complete before "
                "resetting statistics.",
            )
            return

        answer = QMessageBox.question(
            self,
            "Reset ALL Pokebot3DS-CFW statistics?",
            "This will permanently reset:\n\n"
            "• Treecko / Torchic / Mudkip lifetime encounters\n"
            "• Walk + Run + Acro Bike wild encounters\n"
            "• per-species shiny totals and target phases\n"
            "• recent shiny history\n"
            "• Last Seen Pokémon history\n"
            "• current dashboard session counters\n\n"
            "It will NOT delete your IP/settings, logs, raw PK6 evidence "
            "or support ZIPs.\n\n"
            "Continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        try:
            reset_all_stats(self.profile)
            self.dashboard.reset_all_statistics_ui()
            self._load_target_phases()
            self.dashboard.set_last_seen_history([])
            self.dashboard.set_recent_shinies([])
            self.hunt_page.refresh_shiny_totals()
            self.settings_page.set_reset_status(
                "All persistent hunt statistics reset."
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Stats reset failed",
                f"{type(exc).__name__}: {exc}",
            )

    def _support_zip_sources(self):
        """Return conservative diagnostic sources for a manual support export."""
        items = []

        def add_tree(path, arc_prefix, *, max_file_bytes=8 * 1024 * 1024):
            path = Path(path)
            if not path.exists():
                return
            if path.is_file():
                try:
                    if path.stat().st_size <= max_file_bytes:
                        items.append((path, Path(arc_prefix)))
                except Exception:
                    pass
                return

            skip_dirs = {
                "__pycache__", ".git", ".venv-build", "build", "dist",
                "sprite_cache", "sprites", "cache",
            }
            for child in path.rglob("*"):
                if not child.is_file():
                    continue
                try:
                    rel = child.relative_to(path)
                except Exception:
                    continue
                if any(part.lower() in skip_dirs for part in rel.parts):
                    continue
                # settings.json can contain the Discord bot token. It is never
                # copied raw into a support ZIP; a redacted copy is written separately.
                try:
                    if child.resolve() == self.settings_path.resolve():
                        continue
                except Exception:
                    pass
                try:
                    if child.stat().st_size > max_file_bytes:
                        continue
                except Exception:
                    continue
                items.append((child, Path(arc_prefix) / rel))

        # Persistent AppData: settings, stats, history and prior support evidence.
        add_tree(self.profile.root, "appdata")

        # Current source/runtime diagnostics, excluding caches/build output.
        runtime = self.base_dir / "runtime"
        add_tree(runtime / "logs", "runtime/logs")
        add_tree(runtime / "support", "runtime/support")
        add_tree(runtime / "results", "runtime/results")

        # Small build/version evidence from the exact running package.
        for pattern in (
            "README*.md",
            "README*.txt",
            "HOW_TO_USE.txt",
            "MANIFEST*.json",
            "*VALIDATION*.txt",
            "*VALIDATION*.json",
            "ORAS_UNIFIED*.json",
            "ICON_BUILDER_FIX*.txt",
        ):
            for path in self.base_dir.glob(pattern):
                if path.is_file():
                    items.append((path, Path("build") / path.name))

        # De-duplicate by destination, keeping the first occurrence.
        unique = {}
        for source, arc in items:
            unique.setdefault(str(arc).replace("\\", "/"), source)
        return [(source, Path(arc)) for arc, source in unique.items()]

    def _export_support_zip(self):
        """Create a manual support ZIP from current local diagnostic state."""
        if self.hunt_thread and self.hunt_thread.isRunning():
            # Exporting while running is safe, but be explicit that this is a
            # live snapshot and does not stop/control the hunt.
            live_note = "Live hunt snapshot"
        else:
            live_note = "Idle snapshot"

        try:
            # Canonical user-visible support folder. This is the SAME folder
            # used by automatic Wild support ZIPs, so manual Export Support ZIP
            # can no longer appear to "go missing".
            support_dir = self.profile.root / "support"
            support_dir.mkdir(parents=True, exist_ok=True)

            # Public release support is already persistent in AppData.
            persistent_support_dir = support_dir

            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"Pokebot3DS-CFW_support_{stamp}.zip"
            out = support_dir / filename
            persistent_out = persistent_support_dir / filename

            # D25 support authority: refresh the same live runtime-party source
            # used by the dashboard so SUPPORT_INFO cannot preserve a stale
            # fixed-copy Grovyle party from an earlier connection probe.
            latest_probe = dict(getattr(self.tools_page, "last_probe", {}) or {})
            try:
                support_bridge = Bridge(
                    host=self.host,
                    port=self.bridge_port,
                    timeout=min(max(0.35, float(self.bridge_timeout)), 1.25),
                )
                live_party = get_runtime_party_snapshot(self.host, self.bridge_port, support_bridge)
                latest_probe["party"] = list(live_party.get("payload") or [])
                latest_probe["party_diagnostic"] = str(live_party.get("diagnostic") or "PARTY D25 LIVE runtime snapshot")
                latest_probe["party_source"] = live_party.get("source")
                latest_probe["party_live_source"] = live_party.get("live_source")
            except Exception as party_exc:
                latest_probe["party_refresh_error"] = f"{type(party_exc).__name__}: {party_exc}"

            metadata = {
                "tool": "Pokebot3DS-CFW manual support export",
                "created": datetime.now().isoformat(timespec="seconds"),
                "snapshot": live_note,
                "platform": platform.platform(),
                "python": sys.version,
                "app_base_dir": str(self.base_dir),
                "appdata_root": str(self.profile.root),
                "current_game_profile": self.current_game_profile,
                "three_ds_ip": self.host,
                "ram_bridge_port": self.bridge_port,
                "input_port": self.input_port,
                "use_code_ips": bool(self.settings.get("use_code_ips", False)),
                "ram_read_count": int(self.ram_read_count),
                "latest_diagnostic_probe": latest_probe,
                "note": (
                    "Manual support export is read-only. It does not stop a hunt, "
                    "send controller input, write RAM or reset statistics."
                ),
            }

            sources = self._support_zip_sources()
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
                zf.writestr(
                    "SUPPORT_INFO.json",
                    json.dumps(metadata, indent=2, default=str),
                )
                redacted_settings = dict(self.settings)
                if "discord_bot_token" in redacted_settings:
                    redacted_settings["discord_bot_token"] = "<REDACTED>"
                zf.writestr(
                    "appdata/settings.redacted.json",
                    json.dumps(redacted_settings, indent=2, default=str),
                )
                for source, arc in sources:
                    try:
                        zf.write(source, arcname=str(arc).replace("\\", "/"))
                    except Exception:
                        # A log can rotate/change while exporting. Keep the
                        # remainder of the support package rather than fail all.
                        continue

            # The canonical public-release support location is AppData itself.
            mirror_note = ""

            size = out.stat().st_size
            message = (
                f"Support ZIP exported: {out.name} ({size / 1024:.1f} KiB)\n"
                f"Folder: {support_dir}"
            )
            self.settings_page.set_support_status(message)
            self.dashboard.append_log(f"Manual support ZIP: {out}")

            QMessageBox.information(
                self,
                "Support ZIP exported",
                f"{message}\n\nSaved to:\n{out}{mirror_note}",
            )
        except Exception as exc:
            self.settings_page.set_support_status(
                f"Support export failed: {type(exc).__name__}"
            )
            QMessageBox.critical(
                self,
                "Support export failed",
                f"{type(exc).__name__}: {exc}",
            )

    def run_probe(self):
        if self.probe_thread and self.probe_thread.isRunning():
            return

        self.dashboard.ram_ready.setText("Testing…")
        self.dashboard.ram_ready.setObjectName("AmberText")
        self.dashboard.ram_ready.style().unpolish(self.dashboard.ram_ready)
        self.dashboard.ram_ready.style().polish(self.dashboard.ram_ready)

        thread = QThread(self)
        thread.setObjectName("ConnectionProbeThread")
        worker = ProbeWorker(
            self.host,
            bridge_port=self.bridge_port,
            input_port=self.input_port,
            timeout=self.bridge_timeout,
            use_code_ips=self.settings.get("use_code_ips", False),
        )
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.result.connect(self._probe_result)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._probe_cleanup)

        self.probe_thread = thread
        self.probe_worker = worker
        thread.start()

    def _probe_result(self, payload):
        self._connection_update(payload)
        self.tools_page.set_probe_result(payload)

        # Startup/connection probe now includes a bounded six-slot party
        # snapshot so an existing party appears immediately while idle.
        if payload.get("party") is not None:
            self.dashboard.update_party(payload.get("party") or [])

        if payload.get("ram_ready"):
            gi = payload.get("game_info") or {}
            self.dashboard.append_log(
                f"RAM bridge ready: {gi.get('process_name')} PID {gi.get('pid')}"
            )
        else:
            self.dashboard.append_log(
                f"RAM bridge not ready: {payload.get('error', 'GAME_INFO failed')}"
            )

    def _connection_update(self, payload):
        self.dashboard.set_connection(payload)
        profile = (payload or {}).get("game_profile")
        if isinstance(profile, dict):
            previous_family = (self.current_game_profile or {}).get("family") if isinstance(self.current_game_profile, dict) else None
            previous_name = (self.current_game_profile or {}).get("name") if isinstance(self.current_game_profile, dict) else None
            self.current_game_profile = dict(profile)
            if previous_family != profile.get("family") or previous_name != profile.get("name"):
                self.rng_tracking_state = None
                if self.rng_refresh_worker is not None and self.rng_refresh_thread is not None and self.rng_refresh_thread.isRunning():
                    try:
                        self.rng_refresh_worker.request_stop()
                    except Exception:
                        pass
            self.hunt_page.set_detected_game(profile)
            patch_status = (
                "NOT REQUIRED (XY)" if profile.get("family") == "xy"
                else ("ON" if self.settings.get("use_code_ips", False) else "OFF")
            )
            self.left_status.setText(
                f"3DS {self.host}  |  RAM :{self.bridge_port}  |  "
                f"Pokebot-Luma Input/ACK :{self.input_port}   |   code.ips {patch_status}"
            )
            mode = (
                "Wild" if self.dashboard.selected_hunt_type == "wild"
                else ("Static" if self.dashboard.selected_hunt_type == "static" else "Starters")
            )
            self.center_status.setText(
                f"{profile.get('name', 'ORAS')}  |  {mode}"
            )
            self.stats_page.set_current_context({
                "game": profile.get("name"),
                "shiny_charm": (payload or {}).get("shiny_charm"),
                "location_name": ((payload or {}).get("world_location") or {}).get("location_name"),
            })
            context = self._discord_context()
            current_status = getattr(self.dashboard, "current_status", "IDLE")
            self.discord_manager.status_update(
                current_status,
                "Game profile updated",
                context,
            )
            self.discord_rpc.status_update(
                current_status,
                "Game profile updated",
                context,
            )
        elif (payload or {}).get("ram_ready") is False:
            self.current_game_profile = None
            self.rng_tracking_state = None
            self.hunt_page.set_detected_game(None)

    def _stats_update(self, data):
        self.dashboard.update_stats(data)
        payload = dict(data or {})
        if self.current_game_profile:
            payload.setdefault("game", self.current_game_profile.get("name"))
        self._discord_last_stats = dict(payload)
        self.stats_page.set_current_context(payload)
        context = self._discord_context()
        self.discord_manager.stats_update(payload, context)
        self.discord_rpc.stats_update(payload, context)

    def _recent_shinies_update(self, items):
        self.dashboard.set_recent_shinies(items)
        self.hunt_page.refresh_shiny_totals()
        self.discord_manager.recent_shinies_update(items)

    def _probe_cleanup(self):
        self.probe_thread = None
        self.probe_worker = None

    def _refresh_idle_party(self):
        if self._closing:
            return
        # Never compete with a hunt worker or a full connection probe. D25 follows
        # the live field runtime party without starting a hunt or sending input.
        if self.hunt_thread and self.hunt_thread.isRunning():
            return
        if self.probe_thread and self.probe_thread.isRunning():
            return
        if self.party_refresh_thread and self.party_refresh_thread.isRunning():
            return

        # Avoid a GAME_INFO request on every display refresh. The normal startup
        # or manual connection probe establishes current_game_profile first.
        # Both ORAS and the now-verified XY fixed party profile are supported.
        if not isinstance(self.current_game_profile, dict):
            return
        if self.current_game_profile.get("family") not in {"oras", "xy"}:
            return

        thread = QThread(self)
        thread.setObjectName("IdlePartyRefreshThread")
        worker = PartyRefreshWorker(
            self.host,
            bridge_port=self.bridge_port,
            timeout=min(self.bridge_timeout, 1.25),
            game_profile=self.current_game_profile,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.party.connect(self.dashboard.update_party)
        worker.diagnostic.connect(self.dashboard.append_log)
        worker.ram_reads.connect(self._idle_ram_reads)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._party_refresh_cleanup)
        self.party_refresh_thread = thread
        self.party_refresh_worker = worker
        thread.start()

    def _party_refresh_cleanup(self):
        self.party_refresh_thread = None
        self.party_refresh_worker = None

    def _refresh_rng_tracker(self):
        """HF85 watchdog/start method for the persistent RNG telemetry thread."""
        if self._closing:
            return
        if self.probe_thread and self.probe_thread.isRunning():
            return
        if not isinstance(self.current_game_profile, dict):
            self.dashboard.update_rng_tracker({
                "available": False,
                "reason": "waiting for game detection",
            })
            return
        family = str(self.current_game_profile.get("family") or "").lower()
        if family not in {"oras", "xy"}:
            return

        # A live persistent worker owns all normal ticks.  The 2 s timer is
        # only a watchdog so a transient worker exit can never freeze the UI.
        if self.rng_refresh_thread and self.rng_refresh_thread.isRunning():
            return

        thread = QThread(self)
        thread.setObjectName("PersistentRngTelemetryThread")
        worker = RngRefreshWorker(
            self.host,
            bridge_port=self.bridge_port,
            timeout=min(self.bridge_timeout, 0.75),
            game_profile=self.current_game_profile,
            tracking_state=self.rng_tracking_state,
            refresh_interval_s=0.50,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.rng.connect(self._rng_tracker_update)
        worker.diagnostic.connect(self.dashboard.append_log)
        worker.ram_reads.connect(self._idle_ram_reads)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._rng_refresh_cleanup)
        self.rng_refresh_thread = thread
        self.rng_refresh_worker = worker
        thread.start()

    def _rng_tracker_update(self, payload):
        payload = dict(payload or {})
        if payload.get("available"):
            tracking_state = payload.get("tracking_state")
            if isinstance(tracking_state, dict):
                # Keep a private copy: dashboard rendering must never mutate
                # the state used for the next cursor alignment.
                self.rng_tracking_state = dict(tracking_state)
                if isinstance(tracking_state.get("state"), list):
                    self.rng_tracking_state["state"] = list(tracking_state["state"])
        self.dashboard.update_rng_tracker(payload)

    def _rng_refresh_cleanup(self):
        self.rng_refresh_thread = None
        self.rng_refresh_worker = None

    def start_hunt(self, starter_key):
        if self.hunt_thread and self.hunt_thread.isRunning():
            return

        thread = QThread(self)
        thread.setObjectName("StarterHuntThread")
        worker = HuntWorker(
            starter_key=starter_key,
            host=self.host,
            base_dir=self.base_dir,
            bridge_port=self.bridge_port,
            input_port=self.input_port,
            timeout=self.bridge_timeout,
            auto_support_zip=self.settings.get("auto_support_zip", True),
            raw_pk6_keep=self.settings.get("raw_pk6_keep", 10),
            use_code_ips=self.settings.get("use_code_ips", False),
            target_criteria=self.dashboard.target_criteria_snapshot(),
        )
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.log_line.connect(self.dashboard.append_log)
        worker.status.connect(self._worker_status_update)
        worker.connection.connect(self._connection_update)
        worker.encounter.connect(self._encounter_update)
        worker.last_seen_entry.connect(self.dashboard.add_last_seen)
        worker.party.connect(self.dashboard.update_party)
        worker.stats.connect(self._stats_update)
        worker.recent_shinies.connect(self._recent_shinies_update)
        worker.ram_reads.connect(self._ram_reads)
        worker.support_ready.connect(self.dashboard.support_ready)
        worker.session_finished.connect(self._hunt_finished)
        worker.session_finished.connect(thread.quit)
        worker.session_finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._hunt_cleanup)

        self.hunt_thread = thread
        self.hunt_worker = worker
        self._discord_last_stats = {}
        discord_context = {
            "game": (self.current_game_profile or {}).get("name") or "RAM-detected ORAS",
            "target": "Random Starters" if starter_key == "random" else str(starter_key).title(),
            "method": "Starter",
            "location_name": starter_location_for_family((self.current_game_profile or {}).get("family") or getattr(self.dashboard, "selected_game_family", None)),
        }
        self.discord_manager.hunt_started(discord_context)
        self.discord_rpc.hunt_started(discord_context)

        self.dashboard.set_status("STARTING", "Running RAM preflight")
        thread.start()

    def start_wild_hunt(self, method_key, movement_axis):
        if self.hunt_thread and self.hunt_thread.isRunning():
            return

        # Pokebot-Luma uses the hardware-proven split transport:
        # read-only RAM + acknowledged controller commands on shared UDP/4952.
        if int(self.bridge_port) != 4952 or int(self.input_port) != int(self.bridge_port):
            self.dashboard.set_running(False)
            QMessageBox.warning(
                self,
                "Pokebot-Luma ports",
                "The unified Pokebot-Luma ORAS backend uses one acknowledged bridge on UDP 4952 "
                "for both RAM and controller commands.\n\n"
                "Restore RAM Bridge Port to 4952; the Input Port mirrors it automatically.",
            )
            return

        # Heavy hunt worker modules are imported only when that hunt starts,
        # keeping normal dashboard startup lighter on slower PCs.
        from .wild_worker import WildHuntWorker

        thread = QThread(self)
        thread.setObjectName("WildHuntThread")
        worker = WildHuntWorker(
            method_key=method_key,
            movement_axis=movement_axis,
            host=self.host,
            base_dir=self.base_dir,
            timeout=min(self.bridge_timeout, 1.5),
            auto_support_zip=self.settings.get("auto_support_zip", True),
            auto_throw_one_poke_ball_on_shiny=(
                self.dashboard.auto_throw_shiny_snapshot()
            ),
            capture_ball_override=(
                self.dashboard.capture_ball_override_snapshot()
            ),
            auto_capture_test_next_encounter=(
                self.dashboard.take_auto_capture_test_snapshot()
            ),
            horde_auto_attack_test_next_encounter=(
                self.dashboard.take_horde_auto_attack_test_snapshot()
                if method_key == "horde" else False
            ),
            target_selection=(
                dict(self.dashboard.selected_wild_target)
                if (method_key != "horde" and self.dashboard.selected_wild_target)
                else None
            ),
            target_criteria=self.dashboard.target_criteria_snapshot(),
            horde_trigger=(
                self.dashboard.horde_trigger_snapshot()
                if method_key == "horde" else "auto"
            ),
        )
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.log_line.connect(self.dashboard.append_log)
        worker.status.connect(self._worker_status_update)
        worker.connection.connect(self._connection_update)
        worker.encounter.connect(self._encounter_update)
        worker.last_seen_entry.connect(self.dashboard.add_last_seen)
        worker.party.connect(self.dashboard.update_party)
        worker.stats.connect(self._stats_update)
        worker.recent_shinies.connect(self._recent_shinies_update)
        worker.ram_reads.connect(self._ram_reads)
        worker.support_ready.connect(self.dashboard.support_ready)
        worker.shiny_charm.connect(self.dashboard.set_shiny_charm_state)
        worker.pokerus_detected.connect(self._pokerus_detected)
        worker.session_finished.connect(self._hunt_finished)
        worker.session_finished.connect(thread.quit)
        worker.session_finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._hunt_cleanup)

        self.hunt_thread = thread
        self.hunt_worker = worker
        self._discord_last_stats = {}
        selected = (
            {} if method_key == "horde"
            else dict(self.dashboard.selected_wild_target or {})
        )
        discord_context = {
            "game": (self.current_game_profile or {}).get("name") or selected.get("game_name") or "RAM-detected ORAS",
            "target": ("Fishing" if method_key == "fishing" else ("Hordes" if method_key == "horde" else (selected.get("species_name") or ("Cave Pokémon" if method_key in {"cave", "cave_run", "cave_bunny"} else "Wild Pokémon")))),
            "method": ("Fishing" if method_key == "fishing" else ("Horde" if method_key == "horde" else ("Cave" if method_key in {"cave", "cave_run", "cave_bunny"} else str(method_key).replace("_", " ").title()))),
            "location_name": selected.get("location_name"),
        }
        self.discord_manager.hunt_started(discord_context)
        self.discord_rpc.hunt_started(discord_context)

        self.dashboard.set_status(
            "STARTING",
            ("RAM game detection + automatic Sweet Scent Horde preflight"
             if method_key == "horde"
             else ("RAM game detection + v0p11 Fishing state preflight"
                   if method_key == "fishing"
                   else ("RAM game detection + Cave zone/endpoint preflight"
                   if method_key in {"cave", "cave_run", "cave_bunny"}
                   else "RAM game detection + validated wild preflight"))),
        )
        thread.start()

    def start_static_hunt(self, profile_key):
        if self.hunt_thread and self.hunt_thread.isRunning():
            return
        if int(self.bridge_port) != 4952 or int(self.input_port) != int(self.bridge_port):
            self.dashboard.set_running(False)
            QMessageBox.warning(
                self,
                "Pokebot-Luma ports",
                "Static hunts require the unified acknowledged Pokebot-Luma bridge on UDP 4952.",
            )
            return
        try:
            profile = get_static_profile(profile_key)
        except Exception as exc:
            self.dashboard.set_running(False)
            QMessageBox.warning(self, "Static profile", f"Invalid Static profile: {exc}")
            return

        if profile.trigger_kind == "DEXNAV_TUTORIAL_POOCHYENA":
            reply = QMessageBox.question(
                self,
                "DexNav Tutorial Poochyena",
                "HF94 automates the Route 101 tutorial with adaptive fast dialogue and "
                "RAM-targeted hidden-Pokémon steering using acknowledged partial Circle Pad input.\n\n"
                "Required save: the exact pre-tutorial mapper position at world 1917,2349.\n"
                "Each attempt: Pokebot resets → LEFT → adaptive A dialogue until RAM sees the target → "
                "reads the live DexNav overworld target from RAM → continuously corrects CPAD toward its coordinates → RAM-validates battle → checks the Lv. 5 elemental-Fang PK6.\n\n"
                "Install the bundled CPAD-capable boot.firm first and keep standard Rosalina InputRedirection OFF. "
                "Do not use D-pad during the sneak.\n\n"
                "Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                self.dashboard.set_running(False)
                return

        from .static_worker import StaticHuntWorker

        thread = QThread(self)
        thread.setObjectName("StaticHuntThread")
        worker = StaticHuntWorker(
            profile_key=profile.key,
            host=self.host,
            base_dir=self.base_dir,
            bridge_port=self.bridge_port,
            input_port=self.input_port,
            timeout=min(self.bridge_timeout, 1.5),
            auto_support_zip=self.settings.get("auto_support_zip", True),
            use_code_ips=self.settings.get("use_code_ips", False),
        )
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.log_line.connect(self.dashboard.append_log)
        worker.status.connect(self._worker_status_update)
        worker.connection.connect(self._connection_update)
        worker.encounter.connect(self._encounter_update)
        worker.last_seen_entry.connect(self.dashboard.add_last_seen)
        worker.party.connect(self.dashboard.update_party)
        worker.stats.connect(self._stats_update)
        worker.recent_shinies.connect(self._recent_shinies_update)
        worker.ram_reads.connect(self._ram_reads)
        worker.support_ready.connect(self.dashboard.support_ready)
        worker.session_finished.connect(self._hunt_finished)
        worker.session_finished.connect(thread.quit)
        worker.session_finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._hunt_cleanup)

        self.hunt_thread = thread
        self.hunt_worker = worker
        self._discord_last_stats = {}
        context = {
            "game": (self.current_game_profile or {}).get("name") or "RAM-detected ORAS",
            "target": profile.name,
            "method": "Static",
            "location_name": profile.location,
        }
        self.discord_manager.hunt_started(context)
        self.discord_rpc.hunt_started(context)
        if profile.trigger_kind == "DEXNAV_TUTORIAL_POOCHYENA":
            start_text = "DexNav Tutorial Poochyena: reset → LEFT → adaptive fast dialogue → RAM-targeted CPAD steering"
        else:
            start_text = f"Static {profile.name}: reset-first bootstrap to saved trigger • starting"
        self.dashboard.set_status("STARTING", start_text)
        thread.start()

    def start_gift_hunt(self, profile_key):
        if self.hunt_thread and self.hunt_thread.isRunning():
            return
        if int(self.bridge_port) != 4952 or int(self.input_port) != int(self.bridge_port):
            self.dashboard.set_running(False)
            QMessageBox.warning(self, "Pokebot-Luma ports", "Gift hunts require the unified acknowledged Pokebot-Luma bridge on UDP 4952.")
            return
        try:
            profile = get_gift_profile(profile_key)
        except Exception as exc:
            self.dashboard.set_running(False)
            QMessageBox.warning(self, "Gift profile", f"Invalid Gift profile: {exc}")
            return
        if not profile.automation_ready or profile.shiny_locked:
            self.dashboard.set_running(False)
            QMessageBox.warning(self, "Gift profile", f"{profile.name} is listed but is not an automated shiny-reset gift yet.")
            return
        from .gift_worker import GiftHuntWorker

        thread = QThread(self); thread.setObjectName("GiftHuntThread")
        worker = GiftHuntWorker(
            profile_key=profile.key, host=self.host, base_dir=self.base_dir,
            bridge_port=self.bridge_port, input_port=self.input_port,
            timeout=min(self.bridge_timeout, 1.5),
            auto_support_zip=self.settings.get("auto_support_zip", True),
            use_code_ips=self.settings.get("use_code_ips", False),
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.log_line.connect(self.dashboard.append_log)
        worker.status.connect(self._worker_status_update)
        worker.connection.connect(self._connection_update)
        worker.encounter.connect(self._encounter_update)
        worker.last_seen_entry.connect(self.dashboard.add_last_seen)
        worker.party.connect(self.dashboard.update_party)
        worker.stats.connect(self._stats_update)
        worker.recent_shinies.connect(self._recent_shinies_update)
        worker.ram_reads.connect(self._ram_reads)
        worker.support_ready.connect(self.dashboard.support_ready)
        worker.session_finished.connect(self._hunt_finished)
        worker.session_finished.connect(thread.quit)
        worker.session_finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._hunt_cleanup)
        self.hunt_thread = thread; self.hunt_worker = worker; self._discord_last_stats = {}
        context = {
            "game": (self.current_game_profile or {}).get("name") or "RAM-detected ORAS",
            "target": profile.name, "method": "Gift", "location_name": profile.location,
        }
        self.discord_manager.hunt_started(context); self.discord_rpc.hunt_started(context)
        if profile.trigger_kind == "FOSSIL_BATCH_5":
            self.dashboard.set_status("STARTING", "Fossil batch: revive any 5 valid fossils into party slots 2-6 before reset")
        else:
            self.dashboard.set_status("STARTING", f"Gift {profile.name}: reset to saved giver → party slot 2-6 PK6")
        thread.start()

    def stop_hunt(self):
        if self.hunt_worker:
            self.dashboard.append_log(
                "Stop requested. No new reset will be authorized after the next safe RAM boundary."
            )
            self.hunt_worker.request_stop()

    def _hunt_finished(self, status):
        context = self._discord_context()
        self.discord_manager.hunt_finished(status, context)
        self.discord_rpc.hunt_finished(status, context)
        # If the hunt ended by shiny/safety rather than a user Stop click,
        # freeze the stopwatch at the actual end. Stop already freezes it
        # immediately at button press, so calling this again is harmless.
        self.dashboard.freeze_session_clock()
        self.dashboard.set_running(False)
        if status == "IDLE":
            self.dashboard.set_status("IDLE", "Stopped safely")
        elif "SHINY" in status:
            self.dashboard.set_status(
                "SHINY HOLD",
                "Shiny detected — no further reset"
            )
        elif "TARGET" in status:
            self.dashboard.set_status(
                "TARGET HOLD",
                "Requested IV/Nature/Shiny/Gender/Hidden Power target found — no further input"
            )
        elif "HOLD" in status:
            self.dashboard.set_status("SAFETY HOLD", "Authority failure — no further reset")

    def _hunt_cleanup(self):
        self.hunt_thread = None
        self.hunt_worker = None

    def _idle_ram_reads(self, delta):
        # PartyRefreshWorker instances are short-lived and restart their own
        # CountingBridge count at zero every tick, so idle telemetry arrives as
        # deltas and is accumulated here. This makes background party polling
        # visible instead of incorrectly leaving the footer at RAM Reads: 0.
        self.ram_read_count += max(0, int(delta))
        self.right_status.setText(
            f"RAM Reads: {self.ram_read_count} • background telemetry"
        )

    def _ram_reads(self, count):
        self.ram_read_count = int(count)
        self.right_status.setText(f"RAM Reads: {self.ram_read_count}")

    def _running_background_threads(self):
        running = []
        for attr in ("party_refresh_thread", "rng_refresh_thread", "probe_thread", "tool_thread"):
            thread = getattr(self, attr, None)
            if thread is not None and thread.isRunning():
                running.append((attr, thread))
        return running

    def _request_auxiliary_shutdown(self):
        """Stop read-only/background Qt workers without destroying live QThreads.

        v0p43DD deliberately does not block the GUI thread with QThread.wait().
        Some idle-party RAM reads may still be inside a bounded socket operation
        when Windows asks the main window to close.  Keeping the MainWindow alive
        until those workers emit finished prevents Qt from destroying a running
        child QThread and removes the `QThread: Destroyed while thread ... is
        still running` shutdown race.
        """
        for timer_name in (
            "party_refresh_timer", "rng_refresh_timer", "discord_timer", "discord_rpc_timer"
        ):
            timer = getattr(self, timer_name, None)
            if timer is not None:
                timer.stop()

        for worker_name in (
            "party_refresh_worker", "rng_refresh_worker", "probe_worker", "tool_worker"
        ):
            worker = getattr(self, worker_name, None)
            if worker is not None and hasattr(worker, "request_stop"):
                try:
                    worker.request_stop()
                except Exception:
                    pass

        for _attr, thread in self._running_background_threads():
            try:
                thread.requestInterruption()
            except Exception:
                pass
            # quit() is cooperative.  If a worker slot is currently executing,
            # the thread remains alive until that slot returns; we keep the
            # window/QThread object alive for exactly that reason.
            try:
                thread.quit()
            except Exception:
                pass

    def _finish_deferred_close_when_idle(self):
        if not self._closing:
            if self._shutdown_wait_timer is not None:
                self._shutdown_wait_timer.stop()
            return
        if self.hunt_thread and self.hunt_thread.isRunning():
            return
        if self._running_background_threads():
            return
        if self._shutdown_wait_timer is not None:
            self._shutdown_wait_timer.stop()
        # Re-enter closeEvent on the normal Qt event loop.  At this point no
        # auxiliary QThread is alive, so parent destruction is safe.
        QTimer.singleShot(0, self.close)

    def closeEvent(self, event):
        record_event("MAIN_WINDOW_CLOSE_REQUESTED")
        if self.hunt_thread and self.hunt_thread.isRunning():
            if self.hunt_worker:
                self.hunt_worker.request_stop()
            QMessageBox.information(
                self,
                "Hunt still running",
                "A safe stop has been requested.\n\n"
                "The window will remain open until the bot reaches a safe RAM boundary "
                "and guarantees that no further reset will be sent."
            )
            event.ignore()
            return

        if not self._closing:
            self._closing = True
            self._request_auxiliary_shutdown()

        running = self._running_background_threads()
        if running:
            # Do not destroy, terminate, or wait-block on a live child QThread.
            # Keep the main window alive and poll from the Qt event loop until
            # every bounded background operation returns naturally.
            if self._shutdown_wait_timer is None:
                self._shutdown_wait_timer = QTimer(self)
                self._shutdown_wait_timer.setInterval(100)
                self._shutdown_wait_timer.timeout.connect(
                    self._finish_deferred_close_when_idle
                )
            if not self._shutdown_wait_timer.isActive():
                self._shutdown_wait_timer.start()
            names = ",".join(
                thread.objectName() or attr for attr, thread in running
            )
            record_event(f"MAIN_WINDOW_CLOSE_DEFERRED threads={names}")
            event.ignore()
            return

        # No Qt worker threads remain.  Shut down Discord services once, then
        # accept the window close normally.
        if not self._shutdown_started:
            self._shutdown_started = True
            try:
                record_event("DISCORD_BOT_SHUTDOWN_BEGIN")
                self.discord_manager.shutdown()
                record_event("DISCORD_BOT_SHUTDOWN_RETURNED")
            except Exception as exc:
                record_exception("DISCORD_BOT_SHUTDOWN_EXCEPTION", exc)
            try:
                record_event("DISCORD_RPC_SHUTDOWN_BEGIN")
                self.discord_rpc.shutdown()
                record_event("DISCORD_RPC_SHUTDOWN_RETURNED")
            except Exception as exc:
                record_exception("DISCORD_RPC_SHUTDOWN_EXCEPTION", exc)
        record_event("MAIN_WINDOW_CLOSE_ACCEPTED")
        event.accept()
