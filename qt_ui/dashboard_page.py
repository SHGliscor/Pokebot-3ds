
from __future__ import annotations

from pathlib import Path

import time

from PySide6.QtCore import Qt, Signal, QSize, QTimer
from PySide6.QtGui import QColor, QIcon, QBrush
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QComboBox, QCheckBox, QMessageBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QProgressBar, QDialog, QTabWidget
)

from .widgets import Panel, DataPair, StarterRow, PartyCard, SpriteLoader
from .target_dialog import TargetDialog
from pokebot.common.shiny_odds import (
    resolve_shiny_odds, cumulative_probability_from_log_miss,
    format_cumulative_percent, phase_progress_bar_value,
)
from pokebot.common.ability_names import ability_name
from pokebot.common.evolution_prediction import predict_split_evolution
from pokebot.common.target_filter import (
    normalize_target, has_active_constraints, target_probability, target_summary,
    format_one_in, format_duration,
)
from pokebot.wild.registry import (
    GAMES, WILD_METHODS, AXES, game_from_probe, method_available,
)
from pokebot.static.oras_static import static_profiles_for_game, get_static_profile, hardware_validation_label
from pokebot.gift.oras_gifts import gift_profiles_for_game, get_gift_profile, gift_validation_label

STARTER_GROUPS = {
    "oras": ("random", "treecko", "torchic", "mudkip"),
    "xy": ("chespin", "fennekin", "froakie"),
}
STARTER_NAMES = {
    "random": "Random",
    "treecko": "Treecko", "torchic": "Torchic", "mudkip": "Mudkip",
    "chespin": "Chespin", "fennekin": "Fennekin", "froakie": "Froakie",
}
STARTER_SPECIES = {
    "treecko": 252, "torchic": 255, "mudkip": 258,
    "chespin": 650, "fennekin": 653, "froakie": 656,
}

# Keep History intentionally compact and non-scrollable. Older entries remain
# durable in the profile ledgers; the dashboard is a newest-first glance view.
HISTORY_VISIBLE_ROWS = 5
STARTER_ABILITY_BY_SPECIES = {
    252: "Overgrow", 255: "Blaze", 258: "Torrent",
    650: "Overgrow", 653: "Blaze", 656: "Torrent",
}

class DashboardPage(QWidget):
    start_requested = Signal(str)
    wild_start_requested = Signal(str, str)
    static_start_requested = Signal(str)
    gift_start_requested = Signal(str)
    stop_requested = Signal()
    probe_requested = Signal()
    starter_changed = Signal(str)
    hunt_type_changed = Signal(str)
    wild_method_changed = Signal(str)
    wild_axis_changed = Signal(str)
    target_settings_changed = Signal(dict)
    auto_throw_shiny_changed = Signal(bool)
    capture_ball_override_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.selected_starter = "torchic"
        self.selected_game_family = "oras"
        self.starter_selection_by_family = {"oras": "torchic", "xy": "fennekin"}
        self.selected_hunt_type = "starter"
        self.selected_static_profile = "kecleon"
        self.selected_gift_profile = "beldum"
        # Backend method key remains the authority passed to WildHuntWorker.
        # The dashboard now separates Wild environment/type from movement UI.
        self.selected_wild_environment = "grass"
        self.selected_wild_movement = "run"
        self.selected_wild_method = "run"
        self.selected_horde_trigger = "auto"
        self.fishing_mode = False
        self.selected_wild_axis = "up"
        self.wild_backend_available = {
            "walk": False,
            "run": False,
            "acro_bunny": False,
            "horde": False,
            "cave": False,
            "cave_run": False,
            "cave_bunny": False,
            "surf": False,
            "fishing": False,
        }
        self.detected_game = None
        self.last_controller_capabilities = 0
        self.wild_methods_ready = False
        self.shiny_charm_present = None
        self.shiny_charm_status = "UNKNOWN"
        self.shiny_charm_hit_address = None
        self.current_wild_location = "Unknown Location"
        self.selected_wild_target = None
        self.current_wild_zone = None
        self.current_world_location = {}
        self.running = False
        self.last_support = None
        self.target_criteria = normalize_target(None)
        self.target_search_seen = 0
        self.target_found = False
        self.auto_throw_one_poke_ball_on_shiny = False
        self.capture_ball_override = "best"
        self.auto_capture_test_next_encounter = False
        self.horde_auto_attack_test_next_encounter = False

        # UI-owned session telemetry. It is intentionally independent from a
        # single HuntWorker instance so Stop -> Start resumes without clearing.
        self.session_elapsed_accum = 0.0
        self.session_active_started = None
        self.session_encounters = 0
        self.session_shinies = 0
        self.session_resets = 0
        self.worker_resets_seen = 0
        self.session_high_sv = None
        self.session_low_sv = None
        self.session_high_iv_sum = None
        self.session_low_iv_sum = None
        self.fishing_chain_telemetry = None
        # HF96: field-only pace is intentionally separate from real completed
        # encounters/hour. The latter includes battle/run/return overhead.
        self.latest_field_pace_per_hour = None

        # Each target owns its own persistent shiny phase.
        self.target_phases = {
            "treecko": {"phase_index": 1, "phase_seen": 0},
            "torchic": {"phase_index": 1, "phase_seen": 0},
            "mudkip": {"phase_index": 1, "phase_seen": 0},
            "chespin": {"phase_index": 1, "phase_seen": 0},
            "fennekin": {"phase_index": 1, "phase_seen": 0},
            "froakie": {"phase_index": 1, "phase_seen": 0},
            "wild_walk": {"phase_index": 1, "phase_seen": 0},
            "wild_run": {"phase_index": 1, "phase_seen": 0},
            "wild_acro_bunny": {"phase_index": 1, "phase_seen": 0},
            "wild_horde": {"phase_index": 1, "phase_seen": 0},
            "wild_cave": {"phase_index": 1, "phase_seen": 0},
            "wild_surf": {"phase_index": 1, "phase_seen": 0},
            "wild_fishing": {"phase_index": 1, "phase_seen": 0},
            "static": {"phase_index": 1, "phase_seen": 0},
            "gift": {"phase_index": 1, "phase_seen": 0},
        }

        self.session_timer = QTimer(self)
        self.session_timer.setInterval(500)
        self.session_timer.timeout.connect(self._tick_session_telemetry)
        self.session_timer.start()

        main = QVBoxLayout(self)
        main.setContentsMargins(8, 8, 8, 7)
        main.setSpacing(7)

        top = QHBoxLayout()
        top.setSpacing(7)

        # GAME
        game_panel = Panel("Game")
        grow = QHBoxLayout()
        self.oras_btn = QPushButton("ORAS")
        self.oras_btn.setObjectName("GameButtonActive")
        self.xy_btn = QPushButton("XY")
        self.xy_btn.setObjectName("GameButtonInactive")
        self.xy_btn.setToolTip("Pokémon X/Y backend: initial RAM validation build.")
        self.usum_btn = QPushButton("USUM")
        self.usum_btn.setObjectName("GameButtonInactive")
        self.usum_btn.setEnabled(False)
        self.usum_btn.setToolTip("USUM backend is not wired in this build.")
        grow.addWidget(self.oras_btn)
        grow.addWidget(self.xy_btn)
        grow.addWidget(self.usum_btn)
        game_panel.outer.addLayout(grow)
        top.addWidget(game_panel, 2)

        # HUNT TYPE
        hunt_panel = Panel("Hunt type")
        hrow = QHBoxLayout()
        self.starter_btn = QPushButton("Starters")
        self.starter_btn.setObjectName("HuntButtonActive")
        self.wild_btn = QPushButton("Wild")
        self.wild_btn.setObjectName("HuntButtonInactive")
        self.wild_btn.setToolTip(
            "All ORAS wild encounter methods: Walk, Run, Acro Bike, Hordes and Caves."
        )
        hrow.addWidget(self.starter_btn)
        hrow.addWidget(self.wild_btn)

        self.fishing_btn = QPushButton("Fishing")
        self.fishing_btn.setObjectName("HuntButtonInactive")
        self.fishing_btn.setToolTip(
            "Alpha Sapphire 1.4 hardware-proven RAM fishing: "
            "state 5 immediate reel, state 10 no-bite recovery."
        )
        hrow.addWidget(self.fishing_btn)

        self.static_btn = QPushButton("Static")
        self.static_btn.setObjectName("HuntButtonInactive")
        self.static_btn.setEnabled(True)
        self.static_btn.setToolTip("Static / legendary reset hunts. Save at the documented trigger position, then Start may be pressed from anywhere: Pokebot resets to the save before hunting.")
        hrow.addWidget(self.static_btn)
        self.gift_btn = QPushButton("Gifts")
        self.gift_btn.setObjectName("HuntButtonInactive")
        self.gift_btn.setEnabled(True)
        self.gift_btn.setToolTip(
            "ORAS Gift Pokémon reset hunts. Direct gifts are read from the newly populated live-party PK6 in slots 2-6."
        )
        hrow.addWidget(self.gift_btn)
        hunt_panel.outer.addLayout(hrow)
        top.addWidget(hunt_panel, 4)

        # CONNECTION STATUS
        conn_panel = Panel("Connection")
        c1 = QHBoxLayout()
        l1 = QLabel("RAM Bridge:")
        l1.setObjectName("FieldLabel")
        self.ram_ready = QLabel("Not tested")
        self.ram_ready.setObjectName("AmberText")
        c1.addWidget(l1)
        c1.addWidget(self.ram_ready)
        c1.addStretch(1)
        conn_panel.outer.addLayout(c1)

        c2 = QHBoxLayout()
        l2 = QLabel("Input:")
        l2.setObjectName("FieldLabel")
        self.input_ready = QLabel("Pokebot-Luma RAM + Input 4952")
        self.input_ready.setObjectName("GreenText")
        self.probe_btn = QPushButton("Test")
        self.probe_btn.setObjectName("SmallAction")
        c2.addWidget(l2)
        c2.addWidget(self.input_ready)
        c2.addStretch(1)
        c2.addWidget(self.probe_btn)
        conn_panel.outer.addLayout(c2)
        top.addWidget(conn_panel, 2)

        # BOT STATUS
        bot_panel = Panel("Status")
        self.bot_status = QLabel("IDLE")
        self.bot_status.setObjectName("IdleText")
        self.bot_status.setAlignment(Qt.AlignCenter)
        self.bot_sub = QLabel("Ready to Start")
        self.bot_sub.setObjectName("SmallGreen")
        self.bot_sub.setAlignment(Qt.AlignCenter)
        bot_panel.outer.addWidget(self.bot_status)
        bot_panel.outer.addWidget(self.bot_sub)
        top.addWidget(bot_panel, 2)

        main.addLayout(top)

        body = QHBoxLayout()
        body.setSpacing(6)

        # LEFT COLUMN -----------------------------------------------------
        left = QVBoxLayout()
        left.setSpacing(6)
        self.left_layout = left

        self.selection_panel = Panel("Starter")

        # Starter selection remains a fixed list.
        self.starter_container = QWidget()
        starter_layout = QVBoxLayout(self.starter_container)
        starter_layout.setContentsMargins(0, 0, 0, 0)
        starter_layout.setSpacing(2)

        self.starter_rows = {}
        colors = {
            "random": "#d7b8ff",
            "treecko": "#82e9a1", "torchic": "#ffb873", "mudkip": "#85dfff",
            "chespin": "#82e9a1", "fennekin": "#ffb873", "froakie": "#85dfff",
        }
        all_starter_keys = tuple(dict.fromkeys(STARTER_GROUPS["oras"] + STARTER_GROUPS["xy"]))
        for key in all_starter_keys:
            row = StarterRow(key, STARTER_NAMES[key], colors[key])
            row.button.clicked.connect(
                lambda checked=False, k=key: self.select_starter(k)
            )
            starter_layout.addWidget(row)
            self.starter_rows[key] = row
        self.selection_panel.outer.addWidget(self.starter_container)

        # Wild is split into a compact environment/type selector. This avoids
        # a growing vertical list as additional movement variants are added.
        self.wild_type_container = QWidget()
        wild_type_layout = QHBoxLayout(self.wild_type_container)
        wild_type_layout.setContentsMargins(0, 0, 0, 0)
        wild_type_layout.setSpacing(5)

        self.wild_type_buttons = {}
        for key, label in (
            ("grass", "Grass"),
            ("horde", "Horde"),
            ("cave", "Cave"),
            ("water", "Surf / Ocean"),
        ):
            btn = QPushButton(label)
            btn.setObjectName("SmallAction")
            btn.setMinimumHeight(30)
            btn.clicked.connect(
                lambda checked=False, k=key: self.select_wild_environment(k)
            )
            wild_type_layout.addWidget(btn)
            self.wild_type_buttons[key] = btn
        self.wild_type_container.setVisible(False)
        self.selection_panel.outer.addWidget(self.wild_type_container)

        left.addWidget(self.selection_panel)

        # Movement/method choice is a separate compact panel. Grass and Cave
        # share Walk / Run / Bunny slots. Cave Run/Bunny are intentionally
        # present but disabled until their own hardware paths are implemented.
        self.movement_panel = Panel("Method")
        self.movement_button_row = QWidget()
        movement_layout = QHBoxLayout(self.movement_button_row)
        movement_layout.setContentsMargins(0, 0, 0, 0)
        movement_layout.setSpacing(5)

        self.wild_movement_buttons = {}
        for key, label in (
            ("walk", "Walk"),
            ("run", "Run"),
            ("acro_bunny", "Acro Bunny"),
        ):
            btn = QPushButton(label)
            btn.setObjectName("SmallAction")
            btn.setMinimumHeight(30)
            btn.clicked.connect(
                lambda checked=False, k=key: self.select_wild_movement(k)
            )
            movement_layout.addWidget(btn)
            self.wild_movement_buttons[key] = btn
        self.movement_panel.outer.addWidget(self.movement_button_row)

        self.horde_trigger_combo = QComboBox()
        self.horde_trigger_combo.setObjectName("Input")
        self.horde_trigger_combo.setMinimumHeight(30)
        self.horde_trigger_combo.addItem("Auto — Sweet Scent, then Honey (slot 1)", "auto")
        self.horde_trigger_combo.addItem("Sweet Scent", "sweet_scent")
        self.horde_trigger_combo.addItem("Honey — slot 1 fast path", "honey")
        self.horde_trigger_combo.setVisible(False)
        self.horde_trigger_combo.currentIndexChanged.connect(self._horde_trigger_changed)
        self.movement_panel.outer.addWidget(self.horde_trigger_combo)

        self.surf_method_btn = QPushButton("Fast Surf Sweep")
        self.surf_method_btn.setObjectName("SelectedStarter")
        self.surf_method_btn.setMinimumHeight(30)
        self.surf_method_btn.setEnabled(False)
        self.surf_method_btn.setVisible(False)
        self.movement_panel.outer.addWidget(self.surf_method_btn)

        self.movement_note = QLabel("")
        self.movement_note.setObjectName("Muted")
        self.movement_note.setWordWrap(True)
        self.movement_panel.outer.addWidget(self.movement_note)
        self.movement_panel.setVisible(False)
        left.addWidget(self.movement_panel)

        self.axis_panel = Panel("Direction")
        axis_row = QHBoxLayout()
        self.axis_buttons = {}
        for key in ("up", "down", "left", "right", "vertical", "horizontal"):
            btn = QPushButton(AXES[key]["name"])
            btn.setObjectName("SmallAction")
            btn.clicked.connect(
                lambda checked=False, k=key: self.select_wild_axis(k)
            )
            axis_row.addWidget(btn)
            self.axis_buttons[key] = btn
        self.axis_panel.outer.addLayout(axis_row)
        self.axis_note = QLabel("")
        self.axis_note.setObjectName("Muted")
        self.axis_note.setWordWrap(True)
        self.axis_panel.outer.addWidget(self.axis_note)
        self.axis_panel.setVisible(False)
        left.addWidget(self.axis_panel)

        self.profile_panel = Panel("Profile")
        self.profile_combo = QComboBox()
        self._populate_starter_profiles()
        self.profile_combo.setCurrentIndex(1)
        self.profile_combo.currentIndexChanged.connect(self._combo_changed)
        self.profile_panel.outer.addWidget(self.profile_combo)

        prow = QHBoxLayout()
        self.verify_btn = QPushButton("Verify")
        self.verify_btn.setObjectName("SmallAction")
        self.module_btn = QPushButton("Module")
        self.module_btn.setObjectName("SmallAction")
        self.locked_btn = QPushButton("Locked")
        self.locked_btn.setObjectName("SmallAction")
        self.locked_btn.setEnabled(False)
        prow.addWidget(self.verify_btn)
        prow.addWidget(self.module_btn)
        prow.addWidget(self.locked_btn)
        self.profile_panel.outer.addLayout(prow)
        # Keep the old auxiliary Profile panel hidden, but DO NOT hide the actual
        # profile selector. Static/Gift (and the single Fishing profile) need this
        # dropdown in the visible left-hand selection panel. The combo remains the
        # single source of truth used by the existing selection/state code.
        self.profile_panel.setVisible(False)
        self.profile_combo.setParent(self.selection_panel)
        self.profile_combo.setMinimumHeight(30)
        self.profile_combo.setMinimumContentsLength(18)
        self.profile_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.selection_panel.outer.addWidget(self.profile_combo)
        self.profile_combo.setVisible(False)

        # Optional target-search overlay. This does not replace Starter/Wild; it
        # filters each already-authoritative PK6 and HOLDs on a complete match.
        self.target_panel = Panel("LOOK FOR TARGET / CAPTURE")
        # Wild Run can show both Movement and Direction panels. Reserve enough
        # height that target/capture controls never collapse into one another.
        self.target_panel.setMinimumHeight(184)
        target_row = QHBoxLayout()
        self.target_enabled = QCheckBox("Enable")
        self.auto_throw_shiny = QCheckBox("Auto-catch shiny")
        self.auto_throw_shiny.setToolTip(
            "Auto-catch RAM-confirmed shinies for every non-starter hunt method. "
            "Best Ball is the default; use Capture Ball Override below to force "
            "one exact Ball type. Horde mode first isolates the single shiny."
        )
        self.target_configure = QPushButton("Configure")
        self.target_configure.setObjectName("SmallAction")
        target_row.addWidget(self.target_enabled)
        target_row.addWidget(self.auto_throw_shiny)
        target_row.addStretch(1)
        target_row.addWidget(self.target_configure)
        self.target_panel.outer.addLayout(target_row)

        self.capture_ball_row = QWidget()
        capture_ball_layout = QHBoxLayout(self.capture_ball_row)
        capture_ball_layout.setContentsMargins(0, 0, 0, 0)
        capture_ball_layout.setSpacing(6)
        self.capture_ball_label = QLabel("Capture Ball:")
        self.capture_ball_label.setObjectName("FieldLabel")
        self.capture_ball_combo = QComboBox()
        for label, key in (
            ("Best Ball (automatic)", "best"),
            ("Poké Ball", "poke ball"),
            ("Great Ball", "great ball"),
            ("Ultra Ball", "ultra ball"),
            ("Net Ball", "net ball"),
            ("Dive Ball", "dive ball"),
            ("Nest Ball", "nest ball"),
            ("Repeat Ball", "repeat ball"),
            ("Timer Ball", "timer ball"),
            ("Luxury Ball", "luxury ball"),
            ("Premier Ball", "premier ball"),
            ("Dusk Ball", "dusk ball"),
            ("Heal Ball", "heal ball"),
            ("Quick Ball", "quick ball"),
            ("Master Ball (explicit override)", "master ball"),
        ):
            self.capture_ball_combo.addItem(label, key)
        self.capture_ball_combo.setMinimumWidth(190)
        self.capture_ball_combo.setToolTip(
            "Best Ball keeps automatic scoring. An override forces that exact Ball "
            "on every throw/rethrow. If it is missing or depleted, the bot Safety "
            "HOLDs instead of silently using another Ball. Master Ball is used only "
            "when explicitly selected here."
        )
        capture_ball_layout.addWidget(self.capture_ball_label)
        capture_ball_layout.addWidget(self.capture_ball_combo, 1)
        self.target_panel.outer.addWidget(self.capture_ball_row)

        self.auto_capture_test = QCheckBox("Test Auto-Capture on next normal encounter")
        self.auto_capture_test.setToolTip(
            "One-shot hardware test. The next checksum-valid NON-SHINY single encounter "
            "is sent through the normal BAG / Ball / retry / capture / Pokédex recovery "
            "state machine. The Pokémon remains non-shiny in all records and this cannot "
            "create a shiny or increment shiny counters. Hordes and target HOLDs are skipped."
        )
        self.auto_capture_test.toggled.connect(self._auto_capture_test_toggle)
        self.target_panel.outer.addWidget(self.auto_capture_test)

        self.horde_auto_attack_test = QCheckBox("Test Auto-Attack on next non-shiny Horde")
        self.horde_auto_attack_test.setToolTip(
            "One-shot hardware test for Horde move selection. The next checksum-valid "
            "five-Pokémon Horde must contain ZERO shinies. The bot reads the live lead "
            "moves and PP from RAM, chooses one authorized damaging move, executes exactly "
            "one attack on a non-shiny target, proves the target/turn state, then returns "
            "to the normal Horde escape path. Any real shiny blocks all test attack input."
        )
        self.horde_auto_attack_test.toggled.connect(self._horde_auto_attack_test_toggle)
        self.target_panel.outer.addWidget(self.horde_auto_attack_test)

        # Always-visible target status. This deliberately duplicates the most
        # important part of Configure so the operator can prove at a glance
        # which species are armed before starting a hunt.
        self.target_species_label = QLabel("Targets: Not configured")
        self.target_species_label.setObjectName("Value")
        self.target_species_label.setWordWrap(True)
        self.target_panel.outer.addWidget(self.target_species_label)
        self.target_mode_label = QLabel("Target mode: OFF")
        self.target_mode_label.setObjectName("FieldLabel")
        self.target_mode_label.setWordWrap(True)
        self.target_panel.outer.addWidget(self.target_mode_label)

        self.target_summary_label = QLabel("Not configured — press Configure")
        self.target_summary_label.setObjectName("Muted")
        self.target_summary_label.setWordWrap(True)
        self.target_panel.outer.addWidget(self.target_summary_label)
        self.target_odds_label = QLabel("Target odds: —")
        self.target_odds_label.setObjectName("FieldLabel")
        self.target_odds_label.setWordWrap(True)
        self.target_panel.outer.addWidget(self.target_odds_label)
        self.target_eta_label = QLabel("Until odds: —")
        self.target_eta_label.setObjectName("Muted")
        self.target_eta_label.setWordWrap(True)
        self.target_panel.outer.addWidget(self.target_eta_label)
        self.target_enabled.toggled.connect(self._target_toggle)
        self.auto_throw_shiny.toggled.connect(self._auto_throw_shiny_toggle)
        self.capture_ball_combo.currentIndexChanged.connect(
            self._capture_ball_override_changed
        )
        self.target_configure.clicked.connect(self._configure_target)
        # Target panel is placed in the right column above Session so its
        # multi-line status remains readable during Wild Run and other hunts.
        # Initial criteria are disabled; avoid reserving empty odds/ETA rows.
        self.target_odds_label.setVisible(False)
        self.target_eta_label.setVisible(False)

        start_stop = Panel("Hunt control")
        self.start_stop_panel = start_stop
        srow = QHBoxLayout()
        self.start_btn = QPushButton("Start hunt")
        self.start_btn.setObjectName("StartButton")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("StopButton")
        self.stop_btn.setEnabled(False)
        srow.addWidget(self.start_btn)
        srow.addWidget(self.stop_btn)
        start_stop.outer.addLayout(srow)

        self.reset_btn = QPushButton("Reset")
        self.reset_btn.setObjectName("ResetButton")
        self.reset_btn.setEnabled(False)
        self.reset_btn.setToolTip(
            "Manual reset is intentionally disabled. The RAM-authorized hunt loop owns resets."
        )
        start_stop.outer.addWidget(self.reset_btn)

        self.soft_reset = QCheckBox("Soft Reset (L+R+Start+Select)")
        self.soft_reset.setChecked(True)
        self.soft_reset.setEnabled(False)
        start_stop.outer.addWidget(self.soft_reset)

        self.connection_btn = QPushButton("Run Connection Test")
        self.connection_btn.setObjectName("SmallAction")
        start_stop.outer.addWidget(self.connection_btn)
        left.addWidget(start_stop)
        left.addStretch(1)

        body.addLayout(left, 3)

        # CENTER COLUMN ---------------------------------------------------
        center = QVBoxLayout()
        center.setSpacing(6)

        hunt_info = Panel("Current hunt")
        g = QGridLayout()
        g.setHorizontalSpacing(12)
        g.setVerticalSpacing(3)

        self.hunt_values = {}
        pairs = [
            ("Game:", "Alpha Sapphire"),
            ("Encounters:", "0"),
            ("Type:", "Starter Hunt"),
            ("Last Shiny:", "—"),
            ("Target:", "Torchic"),
            ("Encounters/Hour:", "0.0"),
            ("Field Pace/Hour:", "—"),
            ("Profile:", "Torchic — Unlimited — LOCKED 10/10"),
            ("Total Time:", "00:00:00"),
            ("Target filter:", "OFF — not configured"),
            ("Target Search:", "—"),
            ("Method:", "Pokebot-Luma RAM + Input 4952"),
            ("Cycle Avg:", "0.00s"),
            ("Field Avg:", "—"),
            ("Battle/Return Avg:", "—"),
            ("Mode:", "Capture-Free / RAM"),
            ("Run Limit:", "Unlimited"),
            ("Phase:", "Phase 1 • 0 seen"),
        ]
        for i, (key, value) in enumerate(pairs):
            lab = QLabel(key)
            lab.setObjectName("FieldLabel")
            val = QLabel(value)
            val.setObjectName("Value")
            g.addWidget(lab, i // 2, (i % 2) * 2)
            g.addWidget(val, i // 2, (i % 2) * 2 + 1)
            self.hunt_values[key] = val
        self.hunt_values["Encounters/Hour:"].setToolTip(
            "Actual completed encounter throughput for this UI session."
        )
        self.hunt_values["Field Pace/Hour:"].setToolTip(
            "Field-only trigger pace from movement start to proven encounter boundary."
        )
        hunt_info.outer.addLayout(g)
        center.addWidget(hunt_info)

        party_panel = Panel("Party")
        self.party_cards = []
        party_grid = QGridLayout()
        party_grid.setContentsMargins(0, 0, 0, 0)
        party_grid.setHorizontalSpacing(4)
        party_grid.setVerticalSpacing(4)

        for slot in range(1, 7):
            card = PartyCard(slot)
            self.party_cards.append(card)
            party_grid.addWidget(card, (slot - 1) // 2, (slot - 1) % 2)

        self.sprite_loader = SpriteLoader(
            Path(__file__).resolve().parents[1] / "runtime" / "sprites",
            self,
        )
        self.sprite_loader.sprite_ready.connect(self._party_sprite_ready)

        party_panel.outer.addLayout(party_grid)
        center.addWidget(party_panel)

        history_panel = Panel("History")
        self.history_tabs = QTabWidget()

        def _history_table():
            table = QTableWidget(0, 12)
            table.setObjectName("LastSeenTable")
            table.setHorizontalHeaderLabels([
                "", "Nature", "Ability", "Evolution", "HP", "ATK", "DEF", "SPA", "SPD", "SPE", "SUM", "SV"
            ])
            table.verticalHeader().setVisible(False)
            table.setAlternatingRowColors(False)
            table.setSelectionMode(QAbstractItemView.NoSelection)
            table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            table.setShowGrid(False)
            table.setIconSize(QSize(27, 27))

            # History is a fixed newest-first dashboard feed, not a spreadsheet.
            # Never expose Qt scrollbars or allow keyboard/wheel navigation into
            # clipped rows. The durable ledgers still retain the full history.
            table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            table.setFocusPolicy(Qt.NoFocus)
            table.setTextElideMode(Qt.ElideRight)
            table.setFixedHeight(181)

            header = table.horizontalHeader()
            header.setStretchLastSection(False)
            header.setFixedHeight(24)
            header.setMinimumSectionSize(22)
            header.setSectionResizeMode(QHeaderView.Fixed)

            # Reserve predictable space for the IV ledger and let Ability take
            # the remaining width. This keeps Nature/Ability readable at the
            # normal 1280x800 dashboard size without horizontal scrolling.
            table.setColumnWidth(0, 34)
            table.setColumnWidth(1, 68)
            header.setSectionResizeMode(2, QHeaderView.Stretch)
            table.setColumnWidth(3, 110)
            for col in range(4, 10):
                table.setColumnWidth(col, 28)
            table.setColumnWidth(10, 39)
            table.setColumnWidth(11, 45)
            return table

        self._recent_shiny_items = []
        self.last_seen_table = _history_table()
        self.recent_oras_table = _history_table()
        self.recent_target_table = _history_table()
        self.history_tabs.addTab(self.last_seen_table, "Recent encounters")
        self.history_tabs.addTab(self.recent_oras_table, "ORAS shinies")
        self.history_tabs.addTab(self.recent_target_table, "Target shinies")
        history_panel.outer.addWidget(self.history_tabs)

        self.last_seen_sprite_loader = SpriteLoader(
            Path(__file__).resolve().parents[1] / "runtime" / "sprites",
            self,
        )
        self.last_seen_sprite_loader.sprite_ready.connect(
            self._last_seen_sprite_ready
        )
        self._last_seen_sprite_token = 100000

        center.addWidget(history_panel, 1)

        # Backend logs continue to be written to runtime/logs and support ZIPs.
        # Only the visible dashboard log box has been removed.
        self.last_backend_message = ""

        body.addLayout(center, 5)

        # RIGHT COLUMN ----------------------------------------------------
        right = QVBoxLayout()
        right.setSpacing(6)

        # Keep the active target/capture controls at the top-right. This panel
        # contains several status lines and was too cramped beneath Wild movement
        # controls in the left column.
        self.target_panel.setMinimumHeight(210)
        right.addWidget(self.target_panel)

        # HF94: the passive raw-MT tracker was useful while validating RAM RNG
        # reads, but it is redundant for normal hunting and consumed background
        # UDP/RAM bandwidth.  It is intentionally absent from the dashboard and
        # its worker is not started by MainWindow.  The underlying tracker code
        # remains packaged for future diagnostics.
        self.rng_next_shiny = None
        self.rng_pairs = {}
        self.rng_note = None

        # The old right-side Shiny diagnostics panel was removed from the UI.
        # Do not construct hidden Qt widgets for it: an unparented hidden panel
        # can be garbage-collected while encounter callbacks still hold Python
        # wrappers for its child labels, causing a libshiboken "already deleted"
        # RuntimeError during live hunts.

        session = Panel("Session")
        # This panel contains more than twenty vertical items.  Panel's normal
        # 5px spacing wastes over 100px here and made Qt squash the label glyphs
        # at the supported 1280x800 laptop size.  Keep every row readable and
        # reclaim the space from gaps instead.
        session.outer.setContentsMargins(9, 4, 9, 5)
        session.outer.setSpacing(1)
        session.setMinimumHeight(316)
        session.title_label.setMinimumHeight(13)
        self.session_pairs = {}
        for key, value in (
            ("Resets:", "0"),
            ("Encounters:", "0"),
            ("Shinies:", "0"),
            ("Encounters/Hour:", "0.0"),
            ("Field Pace/Hour:", "—"),
            ("Session Time:", "00:00:00"),
            ("Phase:", "Phase 1 • 0 seen"),
            ("Highest SV:", "—"),
            ("Lowest SV:", "—"),
            ("Highest IV Sum:", "—"),
            ("Lowest IV Sum:", "—"),
        ):
            pair = DataPair(key, value, key_width=112)
            pair.setMinimumHeight(13)
            session.outer.addWidget(pair)
            self.session_pairs[key] = pair.v

        self.session_pairs["Encounters/Hour:"].setToolTip(
            "Actual completed encounter throughput: session encounters divided "
            "by active session time. Includes battle, Run and field-return time."
        )
        self.session_pairs["Field Pace/Hour:"].setToolTip(
            "Field-only encounter-trigger pace derived from Field Avg. Excludes "
            "battle, Run and field-return overhead; useful for Encounter Power tests."
        )

        self.phase_progress_header = QLabel("CUMULATIVE SHINY CHANCE  0.00%  •  ODDS 1/4,096")
        self.phase_progress_header.setObjectName("PhaseProgressHeader")
        self.phase_progress_header.setAlignment(Qt.AlignCenter)
        self.phase_progress_header.setMinimumHeight(13)
        session.outer.addWidget(self.phase_progress_header)

        self.phase_progress = QProgressBar()
        self.phase_progress.setObjectName("PhaseProgress")
        self.phase_progress.setRange(0, 10000)
        self.phase_progress.setValue(0)
        self.phase_progress.setTextVisible(False)
        self.phase_progress.setFixedHeight(14)
        session.outer.addWidget(self.phase_progress)

        self.phase_odds_detail = QLabel("Starter • Shiny Charm does not alter starter odds")
        self.phase_odds_detail.setObjectName("Muted")
        self.phase_odds_detail.setAlignment(Qt.AlignCenter)
        self.phase_odds_detail.setMinimumHeight(13)
        session.outer.addWidget(self.phase_odds_detail)

        self.phase_title = QLabel("Target phases")
        self.phase_title.setObjectName("PanelTitle")
        self.phase_title.setAlignment(Qt.AlignCenter)
        self.phase_title.setMinimumHeight(13)
        session.outer.addWidget(self.phase_title)

        self.phase_labels = {}
        for key, label in (
            ("treecko", "Treecko:"), ("torchic", "Torchic:"), ("mudkip", "Mudkip:"),
            ("chespin", "Chespin:"), ("fennekin", "Fennekin:"), ("froakie", "Froakie:"),
        ):
            pair = DataPair(label, "Phase 1 • 0 seen", key_width=70)
            pair.setMinimumHeight(13)
            session.outer.addWidget(pair)
            self.phase_labels[key] = pair.v

        self.wild_phase_labels = {}
        for key, label in (
            ("wild_walk", "Walk:"),
            ("wild_run", "Run:"),
            ("wild_acro_bunny", "Acro Bike:"),
            ("wild_horde", "Horde:"),
            ("wild_cave", "Cave:"),
            ("wild_surf", "Surf/Ocean:"),
            ("wild_fishing", "Fishing:"),
        ):
            pair = DataPair(label, "Phase 1 • 0 seen", key_width=70)
            pair.setMinimumHeight(13)
            pair.setVisible(False)
            session.outer.addWidget(pair)
            self.wild_phase_labels[key] = pair

        self.session_auto = QLabel("Session stats persist when stopped")
        self.session_auto.setObjectName("SmallGreen")
        self.session_auto.setAlignment(Qt.AlignCenter)
        self.session_auto.setMinimumHeight(13)
        session.outer.addWidget(self.session_auto)
        right.addWidget(session)

        body.addLayout(right, 4)
        main.addLayout(body, 1)

        self.oras_btn.clicked.connect(lambda checked=False: self.select_game_family("oras"))
        self.xy_btn.clicked.connect(lambda checked=False: self.select_game_family("xy"))
        self.starter_btn.clicked.connect(
            lambda checked=False: self.select_hunt_type("starter")
        )
        self.wild_btn.clicked.connect(
            lambda checked=False: self.select_hunt_type("wild")
        )
        self.fishing_btn.clicked.connect(
            lambda checked=False: self.select_fishing()
        )
        self.static_btn.clicked.connect(
            lambda checked=False: self.select_static()
        )
        self.gift_btn.clicked.connect(
            lambda checked=False: self.select_gift()
        )
        self.start_btn.clicked.connect(self._start_clicked)
        self.stop_btn.clicked.connect(self._stop_clicked)
        self.probe_btn.clicked.connect(self.probe_requested)
        self.connection_btn.clicked.connect(self.probe_requested)
        self.verify_btn.clicked.connect(self._verify_clicked)
        self.module_btn.clicked.connect(self._module_clicked)

        self.select_starter("torchic")
        self.select_hunt_type("starter", emit_signal=False)

    def update_rng_tracker(self, payload):
        payload = dict(payload or {})
        if getattr(self, "rng_next_shiny", None) is None:
            return
        if not payload.get("available"):
            reason = str(payload.get("reason") or "waiting for RAM authority")
            self.rng_next_shiny.setText("NEXT SHINY: —")
            for label in ("MT Index:", "Snapshot:", "TSV:", "Shiny PID:", "XOR:"):
                self.rng_pairs[label].setText("—")
            self.rng_note.setText(f"Passive • {reason}")
            return

        distance = int(payload.get("frames_away", 0))
        current_index = int(payload.get("current_index", 0))
        tsv = int(payload.get("tsv", 0))
        pid_hex = str(payload.get("pid_hex") or "—")
        shiny_xor = payload.get("shiny_xor")

        self.rng_next_shiny.setText(
            f"NEXT SHINY: +{distance:,} MT frame{'s' if distance != 1 else ''}"
        )
        self.rng_pairs["MT Index:"].setText(str(current_index))
        self.rng_pairs["Snapshot:"].setText("LIVE")
        self.rng_pairs["TSV:"].setText(str(tsv))
        self.rng_pairs["Shiny PID:"].setText(pid_hex)
        self.rng_pairs["XOR:"].setText("—" if shiny_xor is None else str(int(shiny_xor)))
        target_status = str(payload.get("target_status") or "tracking")
        if target_status == "acquired":
            note = "Passive • shiny frame locked • countdown active"
        elif target_status == "passed_reacquired":
            note = "Passive • previous shiny frame passed/skipped • next frame locked"
        elif target_status == "reset_reacquired":
            note = "Passive • RNG stream changed/reset • new shiny frame locked"
        else:
            delta = int(payload.get("countdown_delta", 0) or 0)
            note = (
                f"Passive • persistent tracker • locked shiny frame • {delta:,} MT advances"
                if delta > 0 else
                "Passive • persistent tracker • locked shiny frame • countdown active"
            )
        self.rng_note.setText(note)

    def apply_ui_settings(self, settings):
        allow = bool(settings.get("download_oras_sprites", True))
        self.sprite_loader.set_download_enabled(allow)
        self.last_seen_sprite_loader.set_download_enabled(allow)
        self.target_criteria = normalize_target(settings.get("look_for_target"))
        self.target_enabled.blockSignals(True)
        self.target_enabled.setChecked(bool(self.target_criteria.get("enabled")))
        self.target_enabled.blockSignals(False)
        self.auto_throw_one_poke_ball_on_shiny = bool(
            settings.get("auto_throw_one_poke_ball_on_shiny", False)
        )
        self.auto_throw_shiny.blockSignals(True)
        self.auto_throw_shiny.setChecked(
            self.auto_throw_one_poke_ball_on_shiny
        )
        self.auto_throw_shiny.blockSignals(False)
        self.capture_ball_override = str(
            settings.get("capture_ball_override", "best") or "best"
        ).strip().lower()
        idx = self.capture_ball_combo.findData(self.capture_ball_override)
        if idx < 0:
            idx = self.capture_ball_combo.findData("best")
            self.capture_ball_override = "best"
        self.capture_ball_combo.blockSignals(True)
        self.capture_ball_combo.setCurrentIndex(idx)
        self.capture_ball_combo.blockSignals(False)
        self._refresh_target_panel()
        self._refresh_auto_throw_control()

    def _auto_throw_shiny_toggle(self, enabled):
        if self.running:
            self.auto_throw_shiny.blockSignals(True)
            self.auto_throw_shiny.setChecked(
                self.auto_throw_one_poke_ball_on_shiny
            )
            self.auto_throw_shiny.blockSignals(False)
            return
        self.auto_throw_one_poke_ball_on_shiny = bool(enabled)
        self.auto_throw_shiny_changed.emit(bool(enabled))

    def auto_throw_shiny_snapshot(self):
        return bool(self.auto_throw_one_poke_ball_on_shiny)

    def _auto_capture_test_toggle(self, enabled):
        if self.running:
            self.auto_capture_test.blockSignals(True)
            self.auto_capture_test.setChecked(self.auto_capture_test_next_encounter)
            self.auto_capture_test.blockSignals(False)
            return
        self.auto_capture_test_next_encounter = bool(enabled)

    def take_auto_capture_test_snapshot(self):
        """Return and clear the one-shot test arm when a Wild hunt starts."""
        armed = bool(self.auto_capture_test_next_encounter)
        self.auto_capture_test_next_encounter = False
        self.auto_capture_test.blockSignals(True)
        self.auto_capture_test.setChecked(False)
        self.auto_capture_test.blockSignals(False)
        return armed

    def _horde_auto_attack_test_toggle(self, enabled):
        if self.running:
            self.horde_auto_attack_test.blockSignals(True)
            self.horde_auto_attack_test.setChecked(self.horde_auto_attack_test_next_encounter)
            self.horde_auto_attack_test.blockSignals(False)
            return
        self.horde_auto_attack_test_next_encounter = bool(enabled)

    def take_horde_auto_attack_test_snapshot(self):
        """Return and clear the one-shot Horde attack test arm at hunt start."""
        armed = bool(self.horde_auto_attack_test_next_encounter)
        self.horde_auto_attack_test_next_encounter = False
        self.horde_auto_attack_test.blockSignals(True)
        self.horde_auto_attack_test.setChecked(False)
        self.horde_auto_attack_test.blockSignals(False)
        return armed

    def _capture_ball_override_changed(self, _index):
        selected = str(self.capture_ball_combo.currentData() or "best")
        if self.running:
            idx = self.capture_ball_combo.findData(self.capture_ball_override)
            self.capture_ball_combo.blockSignals(True)
            if idx >= 0:
                self.capture_ball_combo.setCurrentIndex(idx)
            self.capture_ball_combo.blockSignals(False)
            return
        self.capture_ball_override = selected
        self.capture_ball_override_changed.emit(selected)

    def capture_ball_override_snapshot(self):
        return str(self.capture_ball_override or "best")

    def _refresh_auto_throw_control(self):
        # Auto Capture is a Wild/Fishing policy. Static hunts intentionally stop
        # on a RAM-confirmed shiny and never send BAG/battle input afterward.
        visible = self.selected_hunt_type == "wild"
        eligible = visible and not self.running
        self.auto_throw_shiny.setVisible(visible)
        self.auto_throw_shiny.setEnabled(eligible)
        self.capture_ball_row.setVisible(visible)
        self.capture_ball_combo.setEnabled(eligible)
        normal_capture_test_visible = visible and self.selected_wild_method != "horde"
        self.auto_capture_test.setVisible(normal_capture_test_visible)
        self.auto_capture_test.setEnabled(eligible and normal_capture_test_visible)
        horde_attack_test_visible = visible and self.selected_wild_method == "horde"
        self.horde_auto_attack_test.setVisible(horde_attack_test_visible)
        self.horde_auto_attack_test.setEnabled(eligible and horde_attack_test_visible)

    def _target_toggle(self, enabled):
        if self.running:
            self.target_enabled.blockSignals(True)
            self.target_enabled.setChecked(bool(self.target_criteria.get("enabled")))
            self.target_enabled.blockSignals(False)
            return

        # Do not permit an empty 1-in-1 target. If Enable is ticked before a
        # target is configured, take the user straight to Configure.
        if enabled and not has_active_constraints(self.target_criteria):
            dialog = TargetDialog(self.target_criteria, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                self.target_enabled.blockSignals(True)
                self.target_enabled.setChecked(False)
                self.target_enabled.blockSignals(False)
                self.target_criteria["enabled"] = False
                self.target_criteria = normalize_target(self.target_criteria)
                self._refresh_target_panel()
                self.target_settings_changed.emit(dict(self.target_criteria))
                return
            proposed = dialog.criteria()
            if not has_active_constraints(proposed):
                self.target_enabled.blockSignals(True)
                self.target_enabled.setChecked(False)
                self.target_enabled.blockSignals(False)
                proposed["enabled"] = False
                self.target_criteria = normalize_target(proposed)
                self._refresh_target_panel()
                self.target_settings_changed.emit(dict(self.target_criteria))
                QMessageBox.warning(
                    self,
                    "Look for Target",
                    "No target criteria were configured. Look for Target was not enabled.\n\n"
                    "Set at least one IV range, Nature, Shininess, Gender or Hidden Power type.",
                )
                return
            proposed["enabled"] = True
            self.target_criteria = normalize_target(proposed)
        else:
            self.target_criteria["enabled"] = bool(enabled)
            self.target_criteria = normalize_target(self.target_criteria)

        self.target_search_seen = 0
        self.target_found = False
        self._refresh_target_panel()
        self.target_settings_changed.emit(dict(self.target_criteria))
        self._tick_session_telemetry()

    def _configure_target(self):
        if self.running:
            return
        dialog = TargetDialog(self.target_criteria, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        enabled = bool(self.target_criteria.get("enabled"))
        proposed = dialog.criteria()
        if enabled and not has_active_constraints(proposed):
            enabled = False
            self.target_enabled.blockSignals(True)
            self.target_enabled.setChecked(False)
            self.target_enabled.blockSignals(False)
            QMessageBox.warning(
                self,
                "Look for Target",
                "All target criteria are set to Any/0-31, so Look for Target has been disabled.\n\n"
                "This prevents an accidental 1-in-1 match on the first Pokémon.",
            )
        proposed["enabled"] = enabled
        self.target_criteria = normalize_target(proposed)
        self.target_search_seen = 0
        self.target_found = False
        self._refresh_target_panel()
        self.target_settings_changed.emit(dict(self.target_criteria))
        self._tick_session_telemetry()

    def target_criteria_snapshot(self):
        return normalize_target(self.target_criteria)

    def _refresh_target_panel(self):
        if not hasattr(self, "target_summary_label"):
            return
        criteria = normalize_target(self.target_criteria)
        summary = target_summary(criteria)
        self.target_summary_label.setText(summary)
        active = bool(criteria.get("enabled"))

        species_ids = [int(x) for x in criteria.get("species", [])]
        if species_ids:
            from pokebot.common.species_names import SPECIES_NAMES
            names = [SPECIES_NAMES.get(sid, f"Species #{sid}") for sid in species_ids]
            if len(names) <= 5:
                species_text = ", ".join(names)
            else:
                species_text = ", ".join(names[:5]) + f" +{len(names) - 5}"
            self.target_species_label.setText(f"Targets: {species_text}")
        else:
            species_text = "Any species"
            self.target_species_label.setText("Targets: Any species")

        if active:
            if species_ids:
                mode_text = "Species target — ACTIVE"
            else:
                mode_text = "Trait / IV target — ACTIVE"
        else:
            mode_text = "OFF"
        self.target_mode_label.setText(f"Target mode: {mode_text}")

        if hasattr(self, "hunt_values") and "Target filter:" in self.hunt_values:
            if active:
                self.hunt_values["Target filter:"].setText(
                    f"ACTIVE — {species_text}" if species_ids else "ACTIVE — traits / IVs"
                )
                self.hunt_values["Target Search:"].setText("RAM-authoritative")
            elif has_active_constraints(criteria):
                self.hunt_values["Target filter:"].setText(
                    f"OFF — configured: {species_text}" if species_ids else "OFF — traits / IVs configured"
                )
                self.hunt_values["Target Search:"].setText("Configured, not armed")
            else:
                self.hunt_values["Target filter:"].setText("OFF — not configured")
                self.hunt_values["Target Search:"].setText("—")

        self.target_panel.title_label.setText(
            "LOOK FOR TARGET — ACTIVE" if active else "LOOK FOR TARGET"
        )
        self._refresh_cached_shiny_history()
        # Keep the dashboard compact when target search is disabled.
        # The configured summary remains visible, but live odds/ETA rows are
        # only useful while the filter is active.
        self.target_odds_label.setVisible(active)
        self.target_eta_label.setVisible(active)
        if not active:
            self.target_odds_label.setText("Target odds: —")
            self.target_eta_label.setText("Until odds: —")

    def _refresh_target_odds(self, live_rate):
        if not hasattr(self, "target_odds_label"):
            return
        criteria = normalize_target(self.target_criteria)
        if not criteria.get("enabled"):
            return
        if not has_active_constraints(criteria):
            self.target_odds_label.setText("Target odds: configure at least one criterion")
            self.target_eta_label.setText("Until odds: —")
            return
        if self.target_found:
            return

        if self.selected_hunt_type == "starter":
            shiny = resolve_shiny_odds(
                game="Alpha Sapphire", hunt_type="Starter",
                shiny_charm_present=self.shiny_charm_present,
                shiny_charm_applies=False,
            )
            if self.selected_starter == "random":
                species = 650 if self.selected_game_family == "xy" else 252
            else:
                species = STARTER_SPECIES.get(self.selected_starter)
        else:
            # Wild Charm state is RAM-authoritative. Until it is known, do not
            # invent target odds when the target actually filters shininess.
            if self.shiny_charm_present is None and criteria.get("shiny") != "Any":
                self.target_odds_label.setText("Target odds: waiting for Shiny Charm RAM authority…")
                self.target_eta_label.setText(f"Target search: {self.target_search_seen:,} seen")
                return
            shiny = resolve_shiny_odds(
                game="Alpha Sapphire", hunt_type="Wild",
                shiny_charm_present=self.shiny_charm_present,
                shiny_charm_applies=True,
            )
            species = int((self.selected_wild_target or {}).get("species", 0) or 0)

        shiny_probability = shiny.probability
        if (
            self.selected_hunt_type == "wild"
            and self.selected_wild_method == "fishing"
            and isinstance(self.fishing_chain_telemetry, dict)
        ):
            next_odds = self.fishing_chain_telemetry.get("next_odds") or {}
            try:
                chain_probability = float(next_odds.get("probability", 0.0) or 0.0)
            except Exception:
                chain_probability = 0.0
            if chain_probability > 0.0:
                shiny_probability = chain_probability

        result = target_probability(
            criteria, species=species,
            shiny_numerator=shiny.numerator, shiny_denominator=shiny.denominator,
            shiny_probability=shiny_probability,
        )
        if result.probability is None:
            self.target_odds_label.setText("Target odds: —")
            self.target_eta_label.setText("Until odds: —")
            return

        if result.exact:
            if result.probability == 0:
                self.target_odds_label.setText("Target odds: Impossible with the current criteria")
                self.target_eta_label.setText("Until odds: —")
                return
            self.target_odds_label.setText(
                f"Target odds: {format_one_in(result.one_in)} • Expected interval, not a guarantee"
            )
            remaining = max(0.0, float(result.expected_encounters) - float(self.target_search_seen))
            if live_rate > 0:
                eta = remaining * 3600.0 / live_rate
                self.target_eta_label.setText(
                    f"Until odds: {remaining:,.0f} encounters • ~{format_duration(eta)} at {live_rate:.1f}/h • "
                    f"{self.target_search_seen:,} target-search encounters seen"
                )
            else:
                self.target_eta_label.setText(
                    f"Until odds: {remaining:,.0f} encounters • live time estimate starts after rate is measured"
                )
        else:
            self.target_odds_label.setText(
                f"Target odds: {format_one_in(result.one_in)} × species gender odds"
            )
            self.target_eta_label.setText(
                f"Exact ETA unavailable: {result.note} • {self.target_search_seen:,} target-search encounters seen"
            )


    def _populate_starter_profiles(self):
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        family = self.selected_game_family
        if family == "xy":
            self.profile_combo.addItem("Chespin — Unlimited — Hardware proven", "chespin")
            self.profile_combo.addItem("Fennekin — Unlimited — Hardware proven", "fennekin")
            self.profile_combo.addItem("Froakie — Unlimited — Hardware proven", "froakie")
        else:
            self.profile_combo.addItem("Random — shuffled Treecko / Torchic / Mudkip (all 3 every 3 resets)", "random")
            self.profile_combo.addItem("Treecko — Unlimited — LOCKED 10/10", "treecko")
            self.profile_combo.addItem("Torchic — Unlimited — LOCKED 10/10", "torchic")
            self.profile_combo.addItem("Mudkip — Unlimited — LOCKED 10/10", "mudkip")
        self.profile_combo.blockSignals(False)


    def _populate_static_profiles(self):
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        game_key = (self.detected_game or {}).get("key")
        profiles = static_profiles_for_game(game_key) if game_key else ()
        if not profiles:
            self.profile_combo.addItem("Detect a supported ORAS game first", None)
        else:
            for profile in profiles:
                suffix = hardware_validation_label(profile, game_key)
                passed = suffix.startswith("Hardware proven")
                if profile.key in {"latias_eon", "latios_eon"}:
                    # Eon Ticket species is version-specific: Omega Ruby exposes
                    # Latias here, while Alpha Sapphire exposes Latios.  The
                    # profiles are already filtered by the detected game.
                    label = f"{profile.name} - Southern Island (Eon Ticket)"
                elif profile.key == "kecleon":
                    # Keep the internal/statistics target as plain Kecleon, but
                    # identify the fixed encounter by its Devon Scope mechanic
                    # in the Static UI.
                    label = "Kecleon - Devon Scope"
                elif profile.key == "voltorb":
                    # Keep the internal/statistics target as plain Voltorb while
                    # identifying the fixed fake-item encounter location in UI.
                    label = "Voltorb - New Mauville"
                elif profile.key == "electrode":
                    # Explicitly surface the hideout fake-item encounter in the
                    # Static selector rather than relying on the generic label.
                    label = "Electrode - Team Magma/Aqua Hideout"
                elif profile.key == "spiritomb":
                    label = "Spiritomb - Sea Mauville"
                elif profile.key == "poochyena_dexnav_tutorial":
                    label = "Poochyena - Route 101 (DexNav Tutorial)"
                else:
                    label = f"{profile.name} — {profile.location}"
                    if not passed:
                        label += f" — {suffix}"
                self.profile_combo.addItem(label, profile.key)
            keys = [profile.key for profile in profiles]
            if self.selected_static_profile not in keys:
                self.selected_static_profile = "kecleon" if "kecleon" in keys else keys[0]
            for i in range(self.profile_combo.count()):
                if self.profile_combo.itemData(i) == self.selected_static_profile:
                    self.profile_combo.setCurrentIndex(i)
                    break
        self.profile_combo.blockSignals(False)

    def select_static(self, emit_signal=True):
        if self.running:
            return
        self.selected_hunt_type = "static"
        self.fishing_mode = False
        self.selected_wild_target = None

        self.reset_btn.setVisible(False)
        self.soft_reset.setVisible(False)
        if hasattr(self, "left_layout"):
            self.left_layout.setSpacing(4)

        for btn in (self.starter_btn, self.wild_btn, self.fishing_btn, self.static_btn, self.gift_btn):
            btn.setObjectName("HuntButtonActive" if btn is self.static_btn else "HuntButtonInactive")
            btn.style().unpolish(btn)
            btn.style().polish(btn)

        self.starter_container.setVisible(False)
        self.wild_type_container.setVisible(False)
        self.profile_combo.setVisible(True)
        self.movement_panel.setVisible(False)
        self.axis_panel.setVisible(False)
        for row in self.starter_rows.values():
            row.setVisible(False)
        for pair in self.phase_labels.values():
            pair.parent().setVisible(False)
        for pair in self.wild_phase_labels.values():
            pair.setVisible(False)

        self.selection_panel.title_label.setText("Static encounter")
        self.profile_panel.title_label.setText("Static target")
        self.hunt_values["Type:"].setText("Static Hunt")
        self.hunt_values["Game:"].setText(
            self.detected_game["name"] if self.detected_game else "Detecting ORAS game…"
        )
        self._populate_static_profiles()
        self.select_static_profile(self.selected_static_profile, sync_combo=False)
        self.start_btn.setText("Start Static hunt")
        self.locked_btn.setText("HW validation")
        self.locked_btn.setEnabled(False)
        self.phase_odds_detail.setText("Static • RAM PK6 shiny authority • physical trigger needs hardware validation")

        # Static profiles already define the exact species. Generic Look For
        # Target filtering is intentionally disabled so it cannot conflict.
        self.target_enabled.setEnabled(False)
        self.target_configure.setEnabled(False)
        self.target_summary_label.setText("Static target species is fixed by the selected profile")
        self.target_odds_label.setVisible(False)
        self.target_eta_label.setVisible(False)
        self._refresh_auto_throw_control()
        self._refresh_start_enabled()
        if emit_signal:
            self.hunt_type_changed.emit("static")
        if self.detected_game is None:
            self.ram_ready.setText("Detecting game…")
            self.probe_requested.emit()
        self._tick_session_telemetry()

    def select_static_profile(self, key, sync_combo=True):
        if self.running or not key:
            return
        try:
            profile = get_static_profile(key)
        except Exception:
            return
        game_key = (self.detected_game or {}).get("key")
        if game_key and game_key not in profile.games:
            return
        self.selected_static_profile = profile.key
        if sync_combo:
            for i in range(self.profile_combo.count()):
                if self.profile_combo.itemData(i) == profile.key:
                    self.profile_combo.blockSignals(True)
                    self.profile_combo.setCurrentIndex(i)
                    self.profile_combo.blockSignals(False)
                    break
        self.hunt_values["Target:"].setText(f"{profile.name} — {profile.location}")
        validation_label = hardware_validation_label(profile, game_key)
        self.hunt_values["Profile:"].setText(
            f"{profile.location} • {profile.trigger_kind} • {validation_label}"
        )
        if profile.trigger_kind == "DEXNAV_TUTORIAL_POOCHYENA":
            method_text = "Saved position → LEFT → adaptive fast dialogue → read DexNav target RAM → fluid CPAD steering → PK6 → reset"
            self.phase_odds_detail.setText(
                "DexNav tutorial • Lv.5 + elemental Fang PK6 authority • live overworld target RAM steering"
            )
        elif profile.reset_kind == "RUN_REINTERACT":
            method_text = "Saved-field anchor → A → PK6 → RUN → field authority → A again"
        else:
            method_text = "Saved-field RAM anchor → bounded trigger → PK6 → soft reset"
        self.hunt_values["Method:"].setText(method_text)
        self._refresh_start_enabled()
        self._tick_session_telemetry()

    def _populate_gift_profiles(self):
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        game_key = (self.detected_game or {}).get("key")
        profiles = gift_profiles_for_game(game_key) if game_key else ()
        if not profiles:
            self.profile_combo.addItem("Detect a supported ORAS game first", None)
        else:
            for profile in profiles:
                label = profile.display_name
                if not profile.automation_ready:
                    label += f" — {gift_validation_label(profile)}"
                self.profile_combo.addItem(label, profile.key)
                idx = self.profile_combo.count() - 1
                if not profile.automation_ready:
                    item = self.profile_combo.model().item(idx)
                    if item is not None:
                        item.setEnabled(False)
                        item.setToolTip(profile.notes or gift_validation_label(profile))
            ready = [p.key for p in profiles if p.automation_ready and not p.shiny_locked]
            if self.selected_gift_profile not in ready and ready:
                self.selected_gift_profile = "beldum" if "beldum" in ready else ready[0]
            for i in range(self.profile_combo.count()):
                if self.profile_combo.itemData(i) == self.selected_gift_profile:
                    self.profile_combo.setCurrentIndex(i)
                    break
        self.profile_combo.blockSignals(False)

    def select_gift(self, emit_signal=True):
        if self.running:
            return
        self.selected_hunt_type = "gift"
        self.fishing_mode = False
        self.selected_wild_target = None
        self.reset_btn.setVisible(False)
        self.soft_reset.setVisible(False)
        if hasattr(self, "left_layout"):
            self.left_layout.setSpacing(4)
        for btn in (self.starter_btn, self.wild_btn, self.fishing_btn, self.static_btn, self.gift_btn):
            btn.setObjectName("HuntButtonActive" if btn is self.gift_btn else "HuntButtonInactive")
            btn.style().unpolish(btn); btn.style().polish(btn)
        self.starter_container.setVisible(False)
        self.wild_type_container.setVisible(False)
        self.profile_combo.setVisible(True)
        self.movement_panel.setVisible(False)
        self.axis_panel.setVisible(False)
        for row in self.starter_rows.values(): row.setVisible(False)
        for pair in self.phase_labels.values(): pair.parent().setVisible(False)
        for pair in self.wild_phase_labels.values(): pair.setVisible(False)
        self.selection_panel.title_label.setText("Gift Pokémon")
        self.profile_panel.title_label.setText("Gift target")
        self.hunt_values["Type:"].setText("Gift Pokémon Hunt")
        self.hunt_values["Game:"].setText(self.detected_game["name"] if self.detected_game else "Detecting ORAS game…")
        self._populate_gift_profiles()
        self.select_gift_profile(self.selected_gift_profile, sync_combo=False)
        self.start_btn.setText("Start Gift hunt")
        self.locked_btn.setText("Party PK6")
        self.locked_btn.setEnabled(False)
        self.phase_odds_detail.setText("Gift • new party PK6 in slot 2-6 • non-shiny resets • shiny HOLD")
        self.target_enabled.setEnabled(False)
        self.target_configure.setEnabled(False)
        self.target_summary_label.setText("Gift species is fixed by the selected profile")
        self.target_odds_label.setVisible(False); self.target_eta_label.setVisible(False)
        self._refresh_auto_throw_control()
        self._refresh_start_enabled()
        if emit_signal: self.hunt_type_changed.emit("gift")
        if self.detected_game is None:
            self.ram_ready.setText("Detecting game…")
            self.probe_requested.emit()
        self._tick_session_telemetry()

    def select_gift_profile(self, key, sync_combo=True):
        if self.running or not key:
            return
        try:
            profile = get_gift_profile(key)
        except Exception:
            return
        game_key = (self.detected_game or {}).get("key")
        if game_key and game_key not in profile.games:
            return
        self.selected_gift_profile = profile.key
        if sync_combo:
            for i in range(self.profile_combo.count()):
                if self.profile_combo.itemData(i) == profile.key:
                    self.profile_combo.blockSignals(True); self.profile_combo.setCurrentIndex(i); self.profile_combo.blockSignals(False); break
        self.hunt_values["Target:"].setText(f"{profile.name} — {profile.location}")
        self.hunt_values["Profile:"].setText(
            f"{gift_validation_label(profile)} • live Party slots 2-6 • {profile.trigger_kind}"
        )
        if profile.trigger_kind == "FOSSIL_BATCH_5":
            self.hunt_values["Method:"].setText(
                "1 lead + 5 empty slots → revive any 5 fossils into slots 2-6 → HOLD on any shiny → reset after 5 non-shiny"
            )
            self.phase_odds_detail.setText("Mixed fossil batch • 5 PK6 per reset • slots 2-6 • any shiny HOLDs immediately")
        elif profile.trigger_kind == "POSTGAME_BIRCH_STARTER":
            self.hunt_values["Method:"].setText(
                "Littleroot house reset → DOWN → 5×A → Hoenn-style starter chooser → Party PK6"
            )
            self.phase_odds_detail.setText(
                "Postgame starter • left/middle/right chooser • gift Party PK6 • non-shiny reset"
            )
        else:
            self.hunt_values["Method:"].setText("Saved-field reset → bounded A → new party PK6 → shiny HOLD/reset")
            self.phase_odds_detail.setText("Gift • live party PK6 authority • non-shiny reset • shiny HOLD")
        self._refresh_start_enabled(); self._tick_session_telemetry()

    def select_fishing(self, emit_signal=True):
        if self.running:
            return

        self.target_enabled.setEnabled(True)
        self.target_configure.setEnabled(True)
        self._refresh_target_panel()
        self.selected_hunt_type = "wild"
        self.fishing_mode = True
        self.selected_wild_method = "fishing"
        self.selected_wild_target = None

        self.reset_btn.setVisible(False)
        self.soft_reset.setVisible(False)
        if hasattr(self, "left_layout"):
            self.left_layout.setSpacing(4)

        self.starter_btn.setObjectName("HuntButtonInactive")
        self.wild_btn.setObjectName("HuntButtonInactive")
        self.fishing_btn.setObjectName("HuntButtonActive")
        self.static_btn.setObjectName("HuntButtonInactive")
        self.gift_btn.setObjectName("HuntButtonInactive")
        for b in (self.starter_btn, self.wild_btn, self.fishing_btn, self.static_btn, self.gift_btn):
            b.style().unpolish(b)
            b.style().polish(b)

        self.starter_container.setVisible(False)
        self.wild_type_container.setVisible(False)
        self.profile_combo.setVisible(True)
        self.movement_panel.setVisible(False)
        self.axis_panel.setVisible(False)
        for row in self.starter_rows.values():
            row.setVisible(False)
        for pair in self.phase_labels.values():
            pair.parent().setVisible(False)
        for pair in self.wild_phase_labels.values():
            pair.setVisible(True)

        self.selection_panel.title_label.setText("Fishing")
        self.profile_panel.title_label.setText("Profile")
        self.hunt_values["Type:"].setText("Fishing Hunt")
        if self.detected_game is not None:
            self.hunt_values["Game:"].setText(self.detected_game["name"])
        else:
            self.hunt_values["Game:"].setText("Detecting Alpha Sapphire…")

        self._populate_wild_profiles()
        for i in range(self.profile_combo.count()):
            if self.profile_combo.itemData(i) == "fishing":
                self.profile_combo.setCurrentIndex(i)
                break

        self.shiny_charm_present = None
        self.shiny_charm_status = "DETECTING"
        self.shiny_charm_hit_address = None
        self._refresh_wild_method_display()
        self._refresh_auto_throw_control()
        self.wild_type_container.setVisible(False)
        self.profile_combo.setVisible(True)
        self.movement_panel.setVisible(False)
        self.axis_panel.setVisible(False)

        if emit_signal:
            self.hunt_type_changed.emit("fishing")
        self.wild_method_changed.emit("fishing")
        self._tick_session_telemetry()

    def _populate_wild_profiles(self):
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()

        if self.fishing_mode:
            keys = ("fishing",)
        elif self.selected_wild_environment == "grass":
            keys = ("walk", "run", "acro_bunny")
        elif self.selected_wild_environment == "horde":
            keys = ("horde",)
        elif self.selected_wild_environment == "water":
            keys = ("surf",)
        else:
            keys = ("cave", "cave_run", "cave_bunny")

        for key in keys:
            meta = WILD_METHODS[key]
            self.profile_combo.addItem(
                f"{meta['name']} — {meta['validation']}",
                key,
            )
        for i in range(self.profile_combo.count()):
            if self.profile_combo.itemData(i) == self.selected_wild_method:
                self.profile_combo.setCurrentIndex(i)
                break
        self.profile_combo.blockSignals(False)

    def select_game_family(self, family):
        if self.running:
            return
        family = str(family or "").lower()
        if family not in STARTER_GROUPS:
            return
        self.selected_game_family = family
        self.selected_starter = self.starter_selection_by_family.get(family, STARTER_GROUPS[family][1])
        self.oras_btn.setObjectName("GameButtonActive" if family == "oras" else "GameButtonInactive")
        self.xy_btn.setObjectName("GameButtonActive" if family == "xy" else "GameButtonInactive")
        for btn in (self.oras_btn, self.xy_btn):
            btn.style().unpolish(btn); btn.style().polish(btn)
        self.select_hunt_type("starter", emit_signal=False)
        self.select_starter(self.selected_starter)

    def select_hunt_type(self, hunt_type, emit_signal=True):
        if self.running:
            return
        hunt_type = str(hunt_type).lower()
        if hunt_type not in ("starter", "wild"):
            return

        self.target_enabled.setEnabled(True)
        self.target_configure.setEnabled(True)
        self._refresh_target_panel()
        self.selected_hunt_type = hunt_type
        self.fishing_mode = False
        starter_mode = hunt_type == "starter"
        wild_mode = hunt_type == "wild"
        self.profile_combo.setVisible(False)

        # 1280x800 dashboard authority:
        # Reset/Soft Reset belong to the starter workflow and are disabled
        # during Wild hunts. Do not spend vertical space on inert controls.
        self.reset_btn.setVisible(starter_mode)
        self.soft_reset.setVisible(starter_mode)
        if hasattr(self, "left_layout"):
            self.left_layout.setSpacing(6 if starter_mode else 4)

        # A Cave card chosen in HUNTS preselects the Cave environment while
        # remaining under the top-level Wild hunt type.
        if wild_mode and self.selected_wild_target:
            target_mode = str(self.selected_wild_target.get("hunt_type") or "wild")
            if target_mode == "cave":
                self.selected_wild_environment = "cave"
                self.selected_wild_movement = "walk"
                self.selected_wild_method = "cave"
            elif target_mode == "surf":
                self.selected_wild_environment = "water"
                self.selected_wild_method = "surf"
            elif self.selected_wild_environment in {"cave", "water"}:
                self.selected_wild_environment = "grass"
                self.selected_wild_movement = "run"
                self.selected_wild_method = "run"

        self.starter_btn.setObjectName(
            "HuntButtonActive" if starter_mode else "HuntButtonInactive"
        )
        self.wild_btn.setObjectName(
            "HuntButtonActive" if wild_mode else "HuntButtonInactive"
        )
        self.fishing_btn.setObjectName("HuntButtonInactive")
        self.static_btn.setObjectName("HuntButtonInactive")
        self.gift_btn.setObjectName("HuntButtonInactive")
        for b in (self.starter_btn, self.wild_btn, self.fishing_btn, self.static_btn, self.gift_btn):
            b.style().unpolish(b)
            b.style().polish(b)

        self.starter_container.setVisible(starter_mode)
        self.wild_type_container.setVisible(wild_mode)
        self.movement_panel.setVisible(wild_mode)
        active_starters = set(STARTER_GROUPS[self.selected_game_family])
        for key, row in self.starter_rows.items():
            row.setVisible(starter_mode and key in active_starters)

        self.axis_panel.setVisible(
            wild_mode and self.selected_wild_method in ("walk", "run", "cave", "cave_run", "surf")
        )

        active_starters = set(STARTER_GROUPS[self.selected_game_family]) - {"random"}
        for key, pair in self.phase_labels.items():
            pair.parent().setVisible(starter_mode and key in active_starters)
        for pair in self.wild_phase_labels.values():
            pair.setVisible(wild_mode)

        if starter_mode:
            self.selection_panel.title_label.setText("Starter")
            self.profile_panel.title_label.setText("Profile")
            self._populate_starter_profiles()
            for i in range(self.profile_combo.count()):
                if self.profile_combo.itemData(i) == self.selected_starter:
                    self.profile_combo.setCurrentIndex(i)
                    break
            name = STARTER_NAMES[self.selected_starter]
            if self.detected_game is not None:
                self.hunt_values["Game:"].setText(self.detected_game["name"])
            else:
                self.hunt_values["Game:"].setText("Detecting X/Y game…" if self.selected_game_family == "xy" else "Detecting ORAS game…")
            self.hunt_values["Type:"].setText("Starter Hunt")
            self.hunt_values["Target:"].setText(name)
            if self.selected_game_family == "xy":
                starter_profile_text = f"{name} — XY RAM verified • CRO-gated one-attempt hardware test"
            else:
                starter_profile_text = (
                    "Random each reset — existing LOCKED 10/10 starter modules"
                    if self.selected_starter == "random" else f"{name} — Unlimited — LOCKED 10/10"
                )
            self.hunt_values["Profile:"].setText(starter_profile_text)
            self.hunt_values["Method:"].setText("Pokebot-Luma RAM + Input 4952")
            self.phase_odds_detail.setText(
                ("Random Starters • each actual starter keeps its own phase • 1/4,096"
                 if self.selected_starter == "random"
                 else "Starter • Shiny Charm does not alter starter odds")
            )
            self.start_btn.setText("Start hunt")
            self.locked_btn.setText("Locked")
            self.locked_btn.setEnabled(False)
        else:
            self.selection_panel.title_label.setText("Wild encounter")
            self.profile_panel.title_label.setText("Profile")
            self.hunt_values["Type:"].setText("Wild Hunt")
            self._populate_wild_profiles()
            self._refresh_wild_type_buttons()
            self._refresh_wild_movement_panel()
            self._refresh_wild_method_display()

            self.shiny_charm_present = None
            self.shiny_charm_status = "DETECTING"
            self.shiny_charm_hit_address = None
            self.locked_btn.setText(
                "Probe-proven" if self.selected_wild_method == "horde" else "Validated"
            )
            self.locked_btn.setEnabled(False)
            self._refresh_axis_panel()

            # Wild selection probes game/controller support once. Until the
            # result arrives, type and movement controls stay unavailable.
            self.wild_methods_ready = False
            for btn in self.wild_type_buttons.values():
                btn.setEnabled(False)
            for btn in self.wild_movement_buttons.values():
                btn.setEnabled(False)
            self.horde_trigger_combo.setEnabled(False)
            self.surf_method_btn.setEnabled(False)

            self.ram_ready.setText("Detecting game…")
            self.ram_ready.setObjectName("AmberText")
            self.ram_ready.style().unpolish(self.ram_ready)
            self.ram_ready.style().polish(self.ram_ready)
            self.probe_requested.emit()

        self._refresh_start_enabled()
        self._refresh_auto_throw_control()
        if emit_signal:
            self.hunt_type_changed.emit(hunt_type)
        self._tick_session_telemetry()


    def _environment_for_method(self, key):
        if key == "horde":
            return "horde"
        if key in {"cave", "cave_run", "cave_bunny"}:
            return "cave"
        if key == "surf":
            return "water"
        return "grass"

    def _type_available(self, environment):
        if environment == "grass":
            return any(
                self.wild_backend_available.get(k, False)
                for k in ("walk", "run", "acro_bunny")
            )
        if environment == "horde":
            return self.wild_backend_available.get("horde", False)
        if environment == "cave":
            return any(self.wild_backend_available.get(k, False) for k in ("cave", "cave_run", "cave_bunny"))
        if environment == "water":
            return self.wild_backend_available.get("surf", False)
        return False

    def _refresh_wild_type_buttons(self):
        if not hasattr(self, "wild_type_buttons"):
            return
        for key, btn in self.wild_type_buttons.items():
            selected = key == self.selected_wild_environment
            btn.setObjectName("SelectedStarter" if selected else "SmallAction")
            btn.setEnabled(
                (not self.running)
                and self.wild_methods_ready
                and self._type_available(key)
            )
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _horde_trigger_changed(self, _index=None):
        if not hasattr(self, "horde_trigger_combo"):
            return
        value = self.horde_trigger_combo.currentData()
        if value in {"auto", "sweet_scent", "honey"}:
            self.selected_horde_trigger = str(value)

    def horde_trigger_snapshot(self):
        value = str(getattr(self, "selected_horde_trigger", "auto") or "auto")
        return value if value in {"auto", "sweet_scent", "honey"} else "auto"

    def _refresh_wild_movement_panel(self):
        if not hasattr(self, "movement_panel"):
            return

        environment = self.selected_wild_environment
        self.movement_panel.setVisible(self.selected_hunt_type == "wild")

        if environment == "horde":
            self.movement_panel.title_label.setText("HORDE METHOD")
            self.movement_button_row.setVisible(False)
            self.horde_trigger_combo.setVisible(True)
            self.surf_method_btn.setVisible(False)
            self.horde_trigger_combo.setEnabled(not self.running and self.wild_methods_ready)
            self.movement_note.setText(
                "Stationary Horde trigger. Sweet Scent keeps the proven any-party-slot route. "
                "Fast Honey mode requires Honey to be the top/first item in the Items pocket. "
                "The bot RAM-checks item ID 94 and the slot before it can use anything."
            )
            return

        if environment == "water":
            self.movement_panel.title_label.setText("SURF / OCEAN METHOD")
            self.movement_button_row.setVisible(False)
            self.horde_trigger_combo.setVisible(False)
            self.surf_method_btn.setVisible(True)
            self.surf_method_btn.setText("Fast Surf Sweep")
            self.surf_method_btn.setObjectName("SelectedStarter")
            self.surf_method_btn.setEnabled(False)
            self.surf_method_btn.style().unpolish(self.surf_method_btn)
            self.surf_method_btn.style().polish(self.surf_method_btn)
            self.movement_note.setText(
                "HARDWARE TEST: manually start already Surfing in OPEN encounter "
                "water. Pokebot sends no B input; 600 ms directional sweeps may "
                "travel 1-6 tiles and are RAM-bounded around the start anchor."
            )
            return

        self.movement_panel.title_label.setText("MOVEMENT METHOD")
        self.movement_button_row.setVisible(True)
        self.horde_trigger_combo.setVisible(False)
        self.surf_method_btn.setVisible(False)

        for key, btn in self.wild_movement_buttons.items():
            if environment == "grass":
                backend_key = key
                enabled = (
                    self.wild_methods_ready
                    and self.wild_backend_available.get(backend_key, False)
                    and not self.running
                )
                selected = self.selected_wild_method == backend_key
                btn.setToolTip(WILD_METHODS[backend_key]["detail"])
            else:
                backend_key = {
                    "walk": "cave",
                    "run": "cave_run",
                    "acro_bunny": "cave_bunny",
                }[key]
                enabled = (
                    self.wild_methods_ready
                    and self.wild_backend_available.get(backend_key, False)
                    and not self.running
                )
                selected = key == self.selected_wild_movement
                btn.setToolTip(WILD_METHODS[backend_key]["detail"])

            btn.setObjectName("SelectedStarter" if selected else "SmallAction")
            btn.setEnabled(enabled)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

        if environment == "grass":
            self.movement_note.setText(
                "Choose Walk, Run or Acro Bunny Hop. Terrain authority remains "
                "the whole-game grass database."
            )
        else:
            self.movement_note.setText(
                "Cave Walk, Cave Run and Cave Acro Bunny are hardware-proven. "
                "Run is on-foot only; Acro Bunny requires the Acro Bike."
            )

    def select_wild_environment(self, environment):
        if self.running or self.selected_hunt_type != "wild":
            return
        environment = str(environment).lower()
        if environment not in ("grass", "horde", "cave", "water"):
            return
        if self.wild_methods_ready and not self._type_available(environment):
            return

        self.selected_wild_environment = environment

        if environment == "horde":
            self.selected_wild_method = "horde"
        elif environment == "water":
            self.selected_wild_method = "surf"
        elif environment == "cave":
            cave_key = {
                "walk": "cave",
                "run": "cave_run",
                "acro_bunny": "cave_bunny",
            }.get(self.selected_wild_movement, "cave")
            if self.wild_methods_ready and not self.wild_backend_available.get(cave_key, False):
                self.selected_wild_movement = "walk"
                cave_key = "cave"
            self.selected_wild_method = cave_key
        else:
            preferred = self.selected_wild_movement
            if preferred not in ("walk", "run", "acro_bunny"):
                preferred = "run"
            candidates = (preferred, "run", "walk", "acro_bunny")
            for key in candidates:
                if (not self.wild_methods_ready) or self.wild_backend_available.get(key, False):
                    self.selected_wild_movement = key
                    self.selected_wild_method = key
                    break

        self._populate_wild_profiles()
        self._refresh_wild_type_buttons()
        self._refresh_wild_movement_panel()
        self._refresh_axis_panel()
        self._refresh_wild_method_display()
        self._refresh_auto_throw_control()
        self.wild_method_changed.emit(self.selected_wild_method)
        self._tick_session_telemetry()

    def select_wild_movement(self, movement):
        if self.running or self.selected_hunt_type != "wild":
            return
        movement = str(movement)
        if movement not in ("walk", "run", "acro_bunny"):
            return

        if self.selected_wild_environment == "grass":
            if not self.wild_backend_available.get(movement, False):
                return
            self.selected_wild_movement = movement
            self.select_wild_method(movement)
        elif self.selected_wild_environment == "cave":
            backend_key = {
                "walk": "cave",
                "run": "cave_run",
                "acro_bunny": "cave_bunny",
            }[movement]
            if not self.wild_backend_available.get(backend_key, False):
                return
            self.selected_wild_movement = movement
            self.select_wild_method(backend_key)

    def select_wild_method(self, key, sync_combo=True):
        if self.running or key not in WILD_METHODS:
            return
        if not self.wild_methods_ready:
            return
        if not self.wild_backend_available.get(key, False):
            return

        self.selected_wild_method = key
        self.selected_wild_environment = self._environment_for_method(key)
        if key in ("walk", "run", "acro_bunny"):
            self.selected_wild_movement = key
        elif key == "cave":
            self.selected_wild_movement = "walk"
        elif key == "cave_run":
            self.selected_wild_movement = "run"
        elif key == "cave_bunny":
            self.selected_wild_movement = "acro_bunny"
        elif key == "surf":
            self.selected_wild_environment = "water"

        if sync_combo:
            self._populate_wild_profiles()

        self._refresh_wild_type_buttons()
        self._refresh_wild_movement_panel()
        self._refresh_axis_panel()
        self._refresh_wild_method_display()
        self._refresh_auto_throw_control()
        self.wild_method_changed.emit(key)
        self._tick_session_telemetry()


    def select_wild_axis(self, axis_key):
        if self.running or axis_key not in self.axis_buttons:
            return
        self.selected_wild_axis = axis_key
        self._refresh_axis_panel()
        self._refresh_wild_method_display()
        self.wild_axis_changed.emit(axis_key)

    def _refresh_axis_panel(self):
        directional = (
            self.selected_hunt_type == "wild"
            and self.selected_wild_method in ("walk", "run", "cave", "cave_run", "surf")
        )
        self.axis_panel.setVisible(directional)
        if not directional:
            return

        method_meta = WILD_METHODS.get(self.selected_wild_method, {})
        allowed = tuple(method_meta.get("axes", ()))
        if self.selected_wild_axis not in allowed and allowed:
            # Preserve the user's physical axis when switching between Grass
            # cardinal-start controls and Cave/Surf legacy axis controls.
            if self.selected_wild_axis in ("up", "down") and "vertical" in allowed:
                self.selected_wild_axis = "vertical"
            elif self.selected_wild_axis in ("left", "right") and "horizontal" in allowed:
                self.selected_wild_axis = "horizontal"
            elif self.selected_wild_axis == "vertical" and "up" in allowed:
                self.selected_wild_axis = "up"
            elif self.selected_wild_axis == "horizontal" and "left" in allowed:
                self.selected_wild_axis = "left"
            else:
                self.selected_wild_axis = allowed[0]

        for key, btn in self.axis_buttons.items():
            visible = key in allowed
            btn.setVisible(visible)
            selected = visible and key == self.selected_wild_axis
            btn.setObjectName("SelectedStarter" if selected else "SmallAction")
            btn.style().unpolish(btn)
            btn.style().polish(btn)
            btn.setEnabled(visible and not self.running)

        location = self.current_wild_location or "current location"
        if self.selected_wild_method == "run":
            self.axis_note.setText(
                f"{location}: choose the exact first Run direction. Start must be an "
                "interior encounter-grass tile (not an edge). The validated W6 "
                "B+direction timing is unchanged; after the first move Pokebot may "
                "reverse on the same axis to stay inside the RAM-authorised corridor. "
                "Unknown/non-grass tiles HOLD."
            )
        elif self.selected_wild_method == "cave":
            self.axis_note.setText(
                f"{location}: Cave / Walk uses one normal D-pad tile out and one tile back. "
                "Every endpoint is verified from RAM."
            )
        elif self.selected_wild_method == "cave_run":
            self.axis_note.setText(
                f"{location}: Cave / Run is hardware-proven ON FOOT. It first RAM-proves "
                "two clear tiles on BOTH sides of the start anchor, then confines "
                "160 ms B+direction pulses to that proven corridor."
            )
        elif self.selected_wild_method == "surf":
            self.axis_note.setText(
                f"{location}: Surf / Ocean fast sweep. Start already Surfing well "
                "away from shore. Direction-only 600 ms bursts may move 1-6 tiles; "
                "Pokebot stays on one axis and always steers back toward its RAM anchor."
            )
        else:
            self.axis_note.setText(
                f"{location}: choose the exact first Walk direction. Start must be "
                "an interior encounter-grass tile (not an edge). Walk keeps the "
                "proven one-tile movement primitive and RAM-authorises every destination."
            )
        self._refresh_start_enabled()

    def _wild_axis_label(self):
        if self.selected_wild_method not in ("walk", "run", "cave", "cave_run", "surf"):
            return "Stationary"
        return AXES.get(self.selected_wild_axis, {"short": "Unknown"})["short"]

    def _wild_start_allowed(self):
        if not self.wild_methods_ready:
            return False
        if not self.wild_backend_available.get(self.selected_wild_method, False):
            return False
        # Route/axis geometry is re-read from RAM and authorized by the
        # relevant Wild backend immediately before gameplay input.
        return True

    def _refresh_start_enabled(self):
        if self.running:
            self.start_btn.setEnabled(False)
        elif self.selected_hunt_type == "starter":
            detected_family = (self.detected_game or {}).get("family")
            if self.selected_game_family == "xy":
                self.start_btn.setEnabled(detected_family in (None, "xy"))
                self.start_btn.setToolTip(
                    "XY hardware test: shared reset backend → CRO-gated Aquacorde chooser → one PK6 read. "
                    "code.ips is not required."
                )
            else:
                self.start_btn.setToolTip("")
                self.start_btn.setEnabled(detected_family in (None, "oras"))
        elif self.selected_hunt_type == "static":
            game_key = (self.detected_game or {}).get("key")
            try:
                profile = get_static_profile(self.selected_static_profile)
                allowed = bool(game_key and game_key in profile.games and not profile.shiny_locked)
            except Exception:
                allowed = False
            self.start_btn.setEnabled(allowed)
        elif self.selected_hunt_type == "gift":
            game_key = (self.detected_game or {}).get("key")
            try:
                profile = get_gift_profile(self.selected_gift_profile)
                allowed = bool(game_key and game_key in profile.games and profile.automation_ready and not profile.shiny_locked)
            except Exception:
                allowed = False
            self.start_btn.setEnabled(allowed)
        else:
            self.start_btn.setEnabled(self._wild_start_allowed())

    def _refresh_wild_method_display(self):
        meta = WILD_METHODS[self.selected_wild_method]
        axis = self._wild_axis_label()
        suffix = (
            f" • {axis}"
            if self.selected_wild_method in ("walk", "run", "cave", "cave_run", "surf")
            else ""
        )
        self.hunt_values["Method:"].setText(meta["name"] + suffix)
        profile = meta["validation"]

        if self.selected_wild_method in {"walk", "run", "acro_bunny"}:
            profile += " • WHOLE-GAME GRASS DB AUTHORITY"
            if self.selected_wild_target:
                self._refresh_selected_wild_target_display()
            else:
                self.hunt_values["Target:"].setText(
                    self.current_wild_location or "Detecting location…"
                )
            self.start_btn.setText("Start wild hunt")
            self.phase_odds_detail.setText(
                "Wild • detecting Shiny Charm from RAM…"
                if self.shiny_charm_present is None
                else self.phase_odds_detail.text()
            )
            self.locked_btn.setText("Validated")
        elif self.selected_wild_method == "fishing":
            profile += " • v0p11 RAM FISHING STATE AUTHORITY"
            self.hunt_values["Target:"].setText(
                self.current_wild_location or "Detecting fishing location…"
            )
            self.start_btn.setText("Start fishing")
            self.phase_odds_detail.setText(
                "Fishing • state 5 immediate reel • state 10 message recovery • RAM shiny authority"
            )
            self.locked_btn.setText("Hardware-proven")
        elif self.selected_wild_method == "horde":
            profile += " • STATIONARY HORDE TRIGGER"
            self.hunt_values["Target:"].setText(
                self.current_wild_location or "Detecting location…"
            )
            self.start_btn.setText("Start Horde hunt")
            self.phase_odds_detail.setText(
                "Horde • Sweet Scent/Honey • 5-slot RAM shiny authority"
            )
            self.locked_btn.setText("Probe-proven")
        elif self.selected_wild_method in {"cave", "cave_run", "cave_bunny"}:
            profile += " • RAM CAVE ZONE/POSITION AUTHORITY"
            if self.selected_wild_target:
                self._refresh_selected_wild_target_display()
            else:
                self.hunt_values["Target:"].setText(
                    self.current_wild_location or "Detecting cave…"
                )
            self.start_btn.setText("Start cave hunt")
            self.phase_odds_detail.setText(
                "Cave • detecting Shiny Charm from RAM…"
                if self.shiny_charm_present is None
                else self.phase_odds_detail.text()
            )
            self.locked_btn.setText("Validated")
        elif self.selected_wild_method == "surf":
            profile += " • RAM ZONE/POSITION AUTHORITY • MANUAL SURF START"
            if self.selected_wild_target:
                self._refresh_selected_wild_target_display()
            else:
                self.hunt_values["Target:"].setText(
                    self.current_wild_location or "Detecting Surf location…"
                )
            self.start_btn.setText("Start Surf / Ocean")
            self.phase_odds_detail.setText(
                "Surf/Ocean • manual already-Surfing start • RAM shiny authority"
            )
            self.locked_btn.setText("Hardware test")

        self.hunt_values["Profile:"].setText(profile)
        self._refresh_start_enabled()

    def _apply_detected_game(self, payload):
        gi = (payload or {}).get("game_info") or {}
        game = game_from_probe(gi)
        self.detected_game = game
        if game is not None:
            family = game.get("family") or ("xy" if game.get("key") in {"pokemon_x", "pokemon_y"} else "oras")
            if family in STARTER_GROUPS and family != self.selected_game_family:
                self.select_game_family(family)
        if "shiny_charm" in (payload or {}):
            self.set_shiny_charm_state(payload.get("shiny_charm") or {})

        if "world_location" in (payload or {}):
            self.set_world_location(payload.get("world_location") or {})

        if self.selected_hunt_type == "starter":
            if game is not None and payload.get("ram_ready"):
                self.hunt_values["Game:"].setText(game["name"])
            else:
                self.hunt_values["Game:"].setText("Unsupported / not detected")
            self._refresh_start_enabled()
            return

        if self.selected_hunt_type == "static":
            if game is not None and payload.get("ram_ready"):
                self.hunt_values["Game:"].setText(game["name"])
                self._populate_static_profiles()
                self.select_static_profile(self.selected_static_profile, sync_combo=False)
            else:
                self.hunt_values["Game:"].setText("Unsupported / not detected")
            self._refresh_start_enabled()
            return

        if self.selected_hunt_type == "gift":
            if game is not None and payload.get("ram_ready"):
                self.hunt_values["Game:"].setText(game["name"])
                self._populate_gift_profiles()
                self.select_gift_profile(self.selected_gift_profile, sync_combo=False)
            else:
                self.hunt_values["Game:"].setText("Unsupported / not detected")
            self._refresh_start_enabled()
            return

        if self.selected_hunt_type != "wild":
            return

        if game is None or not payload.get("ram_ready"):
            self.wild_methods_ready = False
            self.hunt_values["Game:"].setText("Unsupported / not detected")
            for key in self.wild_backend_available:
                self.wild_backend_available[key] = False
            self._refresh_wild_type_buttons()
            self._refresh_wild_movement_panel()
            self.start_btn.setEnabled(False)
            return

        self.hunt_values["Game:"].setText(game["name"])
        caps = int(((payload or {}).get("controller_info") or {}).get(
            "capabilities", self.last_controller_capabilities
        ))
        self.last_controller_capabilities = caps
        supported = {m["key"]: m for m in game["wild_methods"]}
        controller_ready = bool((payload or {}).get("controller_ready"))

        for key in self.wild_backend_available:
            meta = supported.get(key)
            self.wild_backend_available[key] = bool(
                meta is not None
                and method_available(meta, caps, controller_ready)
            )

        self.wild_methods_ready = any(self.wild_backend_available.values())

        # Keep the dedicated Fishing selection separate from the Wild
        # environment selector. Alpha Sapphire exposes it as a stationary method.
        if self.fishing_mode:
            self.selected_wild_method = "fishing"
        # Keep the user's chosen Wild type where possible. If it is not
        # available, fall back to the first supported environment/method.
        elif not self._type_available(self.selected_wild_environment):
            for environment in ("grass", "horde", "cave", "water"):
                if self._type_available(environment):
                    self.selected_wild_environment = environment
                    break

        if not self.fishing_mode:
            if self.selected_wild_environment == "grass":
                if not self.wild_backend_available.get(self.selected_wild_method, False):
                    for key in ("run", "walk", "acro_bunny"):
                        if self.wild_backend_available.get(key, False):
                            self.selected_wild_method = key
                            self.selected_wild_movement = key
                            break
            elif self.selected_wild_environment == "horde":
                self.selected_wild_method = "horde"
            elif self.selected_wild_environment == "water":
                self.selected_wild_method = "surf"
            else:
                cave_key = {
                    "walk": "cave",
                    "run": "cave_run",
                    "acro_bunny": "cave_bunny",
                }.get(self.selected_wild_movement, "cave")
                if not self.wild_backend_available.get(cave_key, False):
                    cave_key = "cave"
                    self.selected_wild_movement = "walk"
                self.selected_wild_method = cave_key

        self._populate_wild_profiles()
        self._refresh_wild_type_buttons()
        self._refresh_wild_movement_panel()
        self._refresh_axis_panel()
        self._refresh_wild_method_display()
        if self.fishing_mode:
            self.wild_type_container.setVisible(False)
            self.movement_panel.setVisible(False)
            self.axis_panel.setVisible(False)
        self.start_btn.setEnabled(self._wild_start_allowed())

    def _combo_changed(self, index):
        key = self.profile_combo.itemData(index)
        if not key:
            return
        if self.selected_hunt_type == "starter":
            self.select_starter(key, sync_combo=False)
        elif self.selected_hunt_type == "static":
            self.select_static_profile(key, sync_combo=False)
        elif self.selected_hunt_type == "gift":
            self.select_gift_profile(key, sync_combo=False)
        else:
            self.select_wild_method(key, sync_combo=False)

    def select_starter(self, key, sync_combo=True):
        if self.running:
            return
        self.selected_starter = key
        if sync_combo:
            for i in range(self.profile_combo.count()):
                if self.profile_combo.itemData(i) == key:
                    self.profile_combo.blockSignals(True)
                    self.profile_combo.setCurrentIndex(i)
                    self.profile_combo.blockSignals(False)
                    break

        for k, row in self.starter_rows.items():
            row.button.setObjectName(
                "SelectedStarter" if k == key else "SmallAction"
            )
            row.button.setText("Selected" if k == key else "Select")
            row.button.style().unpolish(row.button)
            row.button.style().polish(row.button)

        name = STARTER_NAMES[key]
        self.hunt_values["Target:"].setText(name)
        self.starter_selection_by_family[self.selected_game_family] = key
        if self.selected_game_family == "xy":
            profile_text = f"{name} — XY RAM verified • CRO-gated one-attempt hardware test"
        else:
            profile_text = ("Random each reset — existing LOCKED 10/10 starter modules"
                            if key == "random" else f"{name} — Unlimited — LOCKED 10/10")
        self.hunt_values["Profile:"].setText(profile_text)

        # PARTY POKÉMON replaced the old LIVE STARTER DATA panel.
        # Do not write placeholder starter data into the party table here;
        # the party table is populated only by the bounded live RAM snapshot.
        self.starter_changed.emit(key)
        self._tick_session_telemetry()


    def set_running(self, running):
        self.running = bool(running)
        if running:
            self.start_btn.setEnabled(False)
        else:
            self._refresh_start_enabled()
        self.stop_btn.setEnabled(running)
        self.profile_combo.setEnabled(not running)
        self.target_enabled.setEnabled(not running)
        self.target_configure.setEnabled(not running)
        self._refresh_auto_throw_control()
        self.starter_btn.setEnabled(not running)
        self.wild_btn.setEnabled(not running)
        self.fishing_btn.setEnabled(not running)
        self.static_btn.setEnabled(not running)
        self.gift_btn.setEnabled(not running)
        for row in self.starter_rows.values():
            row.button.setEnabled(not running)
        for btn in getattr(self, "axis_buttons", {}).values():
            btn.setEnabled(not running)

        if self.selected_hunt_type in ("static", "gift") and not running:
            self.target_enabled.setEnabled(False)
            self.target_configure.setEnabled(False)
        elif not running:
            self.target_enabled.setEnabled(True)
            self.target_configure.setEnabled(True)

        if self.selected_hunt_type == "wild":
            self._restore_wild_method_enablement()
            if self.fishing_mode:
                self.wild_type_container.setVisible(False)
                self.movement_panel.setVisible(False)
                self.axis_panel.setVisible(False)

    def _restore_wild_method_enablement(self):
        if self.detected_game is None or not self.wild_methods_ready:
            return
        self._refresh_wild_type_buttons()
        self._refresh_wild_movement_panel()
        self._refresh_start_enabled()

    def _start_clicked(self):
        if (
            self.selected_hunt_type == "wild"
            and not self._wild_start_allowed()
        ):
            return

        if (
            self.selected_hunt_type == "wild"
            and self.selected_wild_environment == "horde"
            and self.horde_trigger_snapshot() == "honey"
        ):
            reply = QMessageBox.question(
                self,
                "Honey Horde Setup",
                "Before starting Honey Horde:\n\n"
                "1. Put Honey in slot 1 — the first item in the Items pocket.\n"
                "2. Open the Bag manually and highlight Honey once.\n"
                "3. Back out to the field, leaving Honey as the remembered Bag selection.\n\n"
                "Pokebot will then touch the yellow Bag shortcut, RAM-prove that Honey "
                "(item 94) is still highlighted, and use exactly two A presses: Select, then Use. "
                "It will not scroll or change pockets. Honey use is exactly A then A after the Bag shortcut.\n\n"
                "Press OK when Honey is slot 1 and pre-highlighted.",
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if reply != QMessageBox.StandardButton.Ok:
                return

        criteria = normalize_target(self.target_criteria)
        if self.selected_hunt_type not in ("static", "gift") and criteria.get("enabled") and not has_active_constraints(criteria):
            QMessageBox.warning(
                self,
                "Look for Target",
                "Look for Target is enabled but no filter is configured.\n\n"
                "Press Configure and set at least one criterion before starting.",
            )
            return

        # Session Time begins exactly when START HUNT is pressed.
        # If this is a resume after Stop, the previous elapsed/counters remain.
        if self.target_found:
            self.target_found = False
            self.target_search_seen = 0
        self.worker_resets_seen = 0
        if self.session_active_started is None:
            self.session_active_started = time.monotonic()
        self.set_running(True)
        self._tick_session_telemetry()
        if self.selected_hunt_type == "wild":
            self.wild_start_requested.emit(
                self.selected_wild_method,
                self.selected_wild_axis,
            )
        elif self.selected_hunt_type == "static":
            self.static_start_requested.emit(self.selected_static_profile)
        elif self.selected_hunt_type == "gift":
            self.gift_start_requested.emit(self.selected_gift_profile)
        else:
            self.start_requested.emit(self.selected_starter)

    def _stop_clicked(self):
        # Freeze the displayed session clock at the instant Stop is pressed.
        # Backend safety logic may still finish the current safe boundary, but
        # the user's session timer does not keep running while that happens.
        self.freeze_session_clock()
        self.stop_btn.setEnabled(False)
        self.bot_sub.setText("Stop requested — waiting for safe boundary")
        self.stop_requested.emit()

    def freeze_session_clock(self):
        if self.session_active_started is not None:
            self.session_elapsed_accum += (
                time.monotonic() - self.session_active_started
            )
            self.session_active_started = None
        self._tick_session_telemetry()

    def _session_elapsed_seconds(self):
        elapsed = self.session_elapsed_accum
        if self.session_active_started is not None:
            elapsed += time.monotonic() - self.session_active_started
        return max(0.0, elapsed)

    @staticmethod
    def _format_elapsed(seconds):
        total = max(0, int(seconds))
        hours, rem = divmod(total, 3600)
        minutes, secs = divmod(rem, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    def _tick_session_telemetry(self):
        elapsed = self._session_elapsed_seconds()
        rate = (
            self.session_encounters * 3600.0 / elapsed
            if elapsed > 0 else 0.0
        )

        # This continuously decays between encounters because elapsed time
        # continues increasing while the encounter numerator stays fixed.
        self.session_pairs["Session Time:"].setText(
            self._format_elapsed(elapsed)
        )
        self.session_pairs["Encounters/Hour:"].setText(f"{rate:.1f}")
        field_pace = self.latest_field_pace_per_hour
        self.session_pairs["Field Pace/Hour:"].setText(
            f"{field_pace:.1f}" if field_pace and field_pace > 0 else "—"
        )
        self.session_pairs["Encounters:"].setText(
            str(self.session_encounters)
        )
        self.session_pairs["Shinies:"].setText(
            str(self.session_shinies)
        )
        self.session_pairs["Resets:"].setText(
            str(self.session_resets)
        )

        self.hunt_values["Total Time:"].setText(
            self._format_elapsed(elapsed)
        )
        self.hunt_values["Encounters/Hour:"].setText(f"{rate:.1f}")
        self.hunt_values["Field Pace/Hour:"].setText(
            f"{field_pace:.1f}" if field_pace and field_pace > 0 else "—"
        )
        self.hunt_values["Encounters:"].setText(
            str(self.session_encounters)
        )
        self._refresh_target_odds(rate)

        phase_key = (
            self.selected_starter
            if self.selected_hunt_type == "starter"
            else ("static" if self.selected_hunt_type == "static" else (
                "gift" if self.selected_hunt_type == "gift" else (
                "wild_cave"
                if self.selected_wild_method in {"cave", "cave_run", "cave_bunny"}
                else f"wild_{self.selected_wild_method}"
            )))
        )
        phase = self.target_phases.get(
            phase_key,
            {"phase_index": 1, "phase_seen": 0},
        )
        if self.selected_hunt_type == "starter" and self.selected_starter == "random":
            phase_text = "Mixed • individual starter phases tracked separately"
        else:
            phase_text = (
                f"Phase {phase['phase_index']} • "
                f"{phase['phase_seen']:,} seen"
            )
        self.session_pairs["Phase:"].setText(phase_text)
        self.hunt_values["Phase:"].setText(phase_text)

        def phase_probability(default_odds=None, seen=None):
            explicit = phase.get("phase_cumulative_probability")
            if explicit is not None:
                try:
                    return min(1.0, max(0.0, float(explicit)))
                except Exception:
                    pass
            log_miss = phase.get("phase_log_miss")
            if log_miss is not None:
                return cumulative_probability_from_log_miss(log_miss)
            if default_odds is not None:
                return default_odds.phase_probability(
                    phase["phase_seen"] if seen is None else seen
                )
            return None

        def show_phase(probability, odds_text, detail):
            if probability is None:
                self.phase_progress_header.setText(
                    "CUMULATIVE SHINY CHANCE  —  •  ODDS —"
                )
                self.phase_progress.setValue(0)
            else:
                self.phase_progress_header.setText(
                    f"CUMULATIVE SHINY CHANCE  {format_cumulative_percent(probability)}  •  ODDS {odds_text}"
                )
                self.phase_progress.setValue(phase_progress_bar_value(probability))
            self.phase_odds_detail.setText(detail)

        if self.selected_hunt_type == "starter":
            odds = resolve_shiny_odds(
                game=(self.detected_game or {}).get("name", "ORAS"),
                hunt_type="Starter",
                shiny_charm_present=self.shiny_charm_present,
                shiny_charm_applies=False,
            )
            progress_seen = (
                self.session_encounters
                if self.selected_starter == "random"
                else phase["phase_seen"]
            )
            prob = (
                odds.phase_probability(progress_seen)
                if self.selected_starter == "random"
                else phase_probability(odds)
            )
            show_phase(
                prob, odds.display,
                "Starter • full odds • Shiny Charm does not affect in-game starter gifts",
            )
        elif self.selected_hunt_type == "gift":
            odds = resolve_shiny_odds(
                game=(self.detected_game or {}).get("name", "ORAS"),
                hunt_type="Gift",
                shiny_charm_present=self.shiny_charm_present,
                shiny_charm_applies=False,
            )
            gift_name = "Fossil" if self.selected_gift_profile == "fossil_batch" else "Gift"
            show_phase(
                phase_probability(odds), odds.display,
                f"{gift_name} • full odds • Shiny Charm does not affect in-game gifts/fossil revivals",
            )
        elif self.selected_hunt_type == "static":
            if self.shiny_charm_present is None:
                show_phase(
                    None, "—",
                    f"Static • Shiny Charm RAM status unknown ({self.shiny_charm_status})",
                )
            else:
                odds = resolve_shiny_odds(
                    game=(self.detected_game or {}).get("name", "ORAS"),
                    hunt_type="Static",
                    shiny_charm_present=self.shiny_charm_present,
                    shiny_charm_applies=True,
                )
                charm_text = (
                    "Shiny Charm detected from RAM"
                    if self.shiny_charm_present else
                    "no Shiny Charm detected in live Key Items RAM"
                )
                show_phase(phase_probability(odds), odds.display, f"Static • {charm_text}")
        elif self.shiny_charm_present is None:
            prefix = (
                "Fishing" if self.selected_wild_method == "fishing" else
                "Horde" if self.selected_wild_method == "horde" else
                "Cave" if self.selected_wild_method in {"cave", "cave_run", "cave_bunny"} else
                "Surf/Ocean" if self.selected_wild_method == "surf" else "Wild"
            )
            show_phase(
                None, "—",
                f"{prefix} • Shiny Charm RAM status unknown ({self.shiny_charm_status})",
            )
        elif self.selected_wild_method == "fishing":
            cumulative = phase_probability()
            chain = 0
            peak = 0
            rolls = None
            one_in = None
            if isinstance(self.fishing_chain_telemetry, dict):
                chain = int(self.fishing_chain_telemetry.get("chain", 0) or 0)
                peak = int(self.fishing_chain_telemetry.get("peak_chain", 0) or 0)
                next_odds = self.fishing_chain_telemetry.get("next_odds") or {}
                rolls = int(next_odds.get("rolls", 1) or 1)
                one_in = next_odds.get("one_in")
            if one_in:
                odds_text = f"~1/{float(one_in):,.2f} next"
            else:
                base = resolve_shiny_odds(
                    game=(self.detected_game or {}).get("name", "ORAS"),
                    hunt_type="Wild",
                    shiny_charm_present=self.shiny_charm_present,
                    shiny_charm_applies=True,
                )
                odds_text = base.display
            detail = (
                f"Fishing chain {chain} • peak {peak} • next encounter {rolls or 1} shiny roll(s) • "
                + ("Shiny Charm +2 rolls" if self.shiny_charm_present else "no Shiny Charm")
            )
            show_phase(cumulative, odds_text, detail)
        else:
            odds = resolve_shiny_odds(
                game=(self.detected_game or {}).get("name", "ORAS"),
                hunt_type="Wild",
                shiny_charm_present=self.shiny_charm_present,
                shiny_charm_applies=True,
            )
            prefix = (
                "Horde" if self.selected_wild_method == "horde" else
                "Cave" if self.selected_wild_method in {"cave", "cave_run", "cave_bunny"} else
                "Surf/Ocean" if self.selected_wild_method == "surf" else "Wild"
            )
            charm_text = (
                "Shiny Charm detected from RAM"
                if self.shiny_charm_present else
                "no Shiny Charm detected in live Key Items RAM"
            )
            show_phase(phase_probability(odds), odds.display, f"{prefix} • {charm_text}")

        for key, label in self.phase_labels.items():
            p = self.target_phases.get(
                key,
                {"phase_index": 1, "phase_seen": 0},
            )
            label.setText(
                f"Phase {p['phase_index']} • {p['phase_seen']:,} seen"
            )

        for key, pair in self.wild_phase_labels.items():
            p = self.target_phases.get(
                key,
                {"phase_index": 1, "phase_seen": 0},
            )
            pair.v.setText(
                f"Phase {p['phase_index']} • {p['phase_seen']:,} seen"
            )

    def _verify_clicked(self):
        if self.selected_hunt_type == "static":
            profile = get_static_profile(self.selected_static_profile)
            game_key = (self.detected_game or {}).get("key")
            validation_label = hardware_validation_label(profile, game_key)
            QMessageBox.information(
                self,
                "Pokebot3DS-CFW Static validation",
                f"{profile.name}\n\nRAM authority: saved field anchor → bounded trigger → normal battle PK6 → species/PID/EC validation → shiny HOLD or soft reset.\n\nPhysical trigger status: {validation_label}.\nRaw status: {profile.choreography_status}.\nAll Static profiles fail closed on any RAM/identity mismatch."
            )
            return
        if self.selected_hunt_type == "wild":
            meta = WILD_METHODS[self.selected_wild_method]
            QMessageBox.information(
                self,
                "Pokebot3DS-CFW wild validation",
                f"{meta['name']}\n\n{meta['validation']}\n\n"
                "The validated source is retained separately from the Qt adapter. "
                "The shared encounter backend remains: battle boundary → exactly one "
                "PK6 read → shiny HOLD / non-shiny causal Run → field recovery."
            )
            return
        QMessageBox.information(
            self,
            "Pokebot3DS-CFW starter modules",
            "Pokebot3DS-CFW keeps all three starter modules unchanged from "
            "their 10/10 hardware-validated modular builds.\n\n"
            "Treecko, Torchic and Mudkip are configured for Unlimited hunts. "
            "A starter-specific change cannot modify either of the other modules."
        )

    def _module_clicked(self):
        if self.selected_hunt_type == "static":
            QMessageBox.information(
                self,
                "Pokebot3DS-CFW Static module",
                "Static profiles/state authority:\npokebot/static/oras_static.py\n\nQt worker:\nqt_ui/static_worker.py\n\nReset adapter reuses the existing frozen ORAS reset route and replaces only its final RAM authority with the exact saved field anchor."
            )
            return
        if self.selected_hunt_type == "wild":
            if self.selected_wild_method == "fishing":
                QMessageBox.information(
                    self,
                    "Pokebot3DS-CFW Fishing authority",
                    "Fishing RAM control is integrated in:\n"
                    "qt_ui/wild_worker.py\n\n"
                    "Hardware-proven v0p11 rules are frozen: state 5 reels "
                    "immediately; state 10 never reels and uses MESSAGE_PTR "
                    "readiness before dismissal. Battle handling then rejoins "
                    "the existing production Wild PK6/shiny/Run pipeline."
                )
                return
            if self.selected_wild_method == "horde":
                QMessageBox.information(
                    self,
                    "Pokebot3DS-CFW Horde authority",
                    "Horde authority lives in:\n"
                    "pokebot/wild/horde_authority.py\n\n"
                    "The five ORAS opponent slots were hardware-proven before integration. "
                    "Auto Horde mode sends no field movement. Sweet Scent can come from any proven live party slot. Honey mode requires item ID 94 in the first active Items-pocket slot and still requires exact field-Bag cursor authority before Use. Battle RAM remains final encounter authority."
                )
                return
            if self.selected_wild_method in {"cave", "cave_run", "cave_bunny"}:
                QMessageBox.information(
                    self,
                    "Pokebot3DS-CFW Cave authority",
                    "Cave movement authority lives in:\n"
                    "qt_ui/cave_movement.py\n\n"
                    "Cave Walk, Cave Run and Cave Acro Bunny are hardware-proven. "
                    "Cave Run is ON FOOT; Cave Acro Bunny requires the Acro Bike. "
                    "Cave floors are never treated as grass."
                )
                return
            if self.selected_wild_method == "surf":
                QMessageBox.information(
                    self,
                    "Pokebot3DS-CFW Surf / Ocean authority",
                    "Surf movement authority lives in:\n"
                    "qt_ui/water_movement.py\n\n"
                    "Start already Surfing in open encounter water. Fast Surf "
                    "uses direction-only 600 ms sweeps, accepts only 1-6 tile RAM "
                    "movement on the selected axis, and never sends B/run input."
                )
                return
            folder = (
                "walk_v0p23/w6_unlimited_v0p23.py"
                if self.selected_wild_method in ("walk", "run")
                else "acro_v0p27/acro_bunny_latch_10_v0p27.py"
            )
            QMessageBox.information(
                self,
                "Pokebot3DS-CFW wild module",
                "Validated source:\n"
                f"pokebot/wild/validated/{folder}\n\n"
                "Qt wiring lives in qt_ui/wild_worker.py so the validated "
                "wild source itself stays frozen."
            )
            return
        if self.selected_starter == "random":
            QMessageBox.information(
                self,
                "Pokebot3DS-CFW Random starter orchestrator",
                "Random is an orchestration-only mode in qt_ui/backend_worker.py.\n\n"
                "At each ATTEMPT_BEGIN it uses a shuffled 3-starter bag, so Treecko, Torchic and Mudkip each appear once per 3 resets, "
                "then calls the existing locked starter module unchanged.\n\n"
                "Modules:\n"
                "pokebot/starters/treecko.py\n"
                "pokebot/starters/torchic.py\n"
                "pokebot/starters/mudkip.py"
            )
            return
        QMessageBox.information(
            self,
            "Pokebot3DS-CFW starter module",
            f"Selected module:\n"
            f"pokebot/starters/{self.selected_starter}.py\n\n"
            "Starter-specific timings, slot logic and choreography remain isolated there."
        )

    def set_browser_wild_target(self, selection):
        """Set a HUNTS-browser land target without bypassing live safety gates."""
        if self.running:
            return False
        selection = dict(selection or {})
        target_hunt_type = str(selection.get("hunt_type") or "")
        if target_hunt_type not in {"wild", "cave", "surf"}:
            return False
        section_key = str(selection.get("section_key") or "")
        allowed_sections = {
            "grass", "tall_grass", "surf",
            "yellow_flowers", "purple_flowers", "red_flowers",
            "rough_terrain",
        }
        if section_key not in allowed_sections:
            return False
        if target_hunt_type == "cave" and str(selection.get("section_title") or "").casefold() != "cave":
            return False
        if target_hunt_type == "surf" and section_key != "surf":
            return False
        try:
            species = int(selection.get("species", 0))
        except Exception:
            species = 0
        if species <= 0:
            return False

        self.selected_wild_target = selection
        self.current_wild_location = str(
            selection.get("location_name") or "Unknown Location"
        )
        self._refresh_selected_wild_target_display()
        self._tick_session_telemetry()
        return True

    def clear_browser_wild_target(self):
        if self.running:
            return
        self.selected_wild_target = None
        if self.selected_hunt_type == "wild":
            self.hunt_values["Target:"].setText(
                self.current_wild_location or "Detecting location…"
            )
        self._tick_session_telemetry()

    def _refresh_selected_wild_target_display(self):
        target = dict(self.selected_wild_target or {})
        if not target:
            return
        species = str(target.get("species_name") or "Wild target")
        location = str(target.get("location_name") or "Unknown Location")
        environment = str(
            target.get("environment_name")
            or target.get("section_title")
            or "Land"
        )
        self.hunt_values["Target:"].setText(
            f"{species} • {location} • {environment}"
        )

    def set_shiny_charm_state(self, state):
        state = state or {}
        detected = state.get("detected")
        self.shiny_charm_status = str(state.get("status", "UNKNOWN"))
        self.shiny_charm_present = (
            bool(detected) if detected is not None else None
        )
        hits = state.get("hits") or []
        self.shiny_charm_hit_address = (
            hits[0].get("address") if hits else None
        )
        self._tick_session_telemetry()

    def set_world_location(self, state):
        state = dict(state or {})
        self.current_world_location = state
        if state.get("resolved"):
            name = str(state.get("location_name") or "Unknown Location")
            self.current_wild_location = name
            self.current_wild_zone = state.get("zone_id")
            if self.selected_hunt_type == "wild":
                if self.selected_wild_target:
                    self._refresh_selected_wild_target_display()
                else:
                    self.hunt_values["Target:"].setText(name)
            self._refresh_axis_panel()
        elif self.selected_hunt_type == "wild":
            zone = state.get("zone_id")
            if zone is not None:
                self.current_wild_location = f"Zone {zone}"
                self.current_wild_zone = zone
                self.hunt_values["Target:"].setText(f"Zone {zone} • unresolved")
            else:
                self.hunt_values["Target:"].setText("Location unresolved")

    def set_connection(self, payload):
        if "shiny_charm" in (payload or {}):
            self.set_shiny_charm_state(payload.get("shiny_charm") or {})

        ready = bool(payload.get("ram_ready"))
        if ready:
            gi = payload.get("game_info") or {}
            self.ram_ready.setText(
                f"Ready ({gi.get('process_name', 'sango-2')} PID {gi.get('pid', '—')})"
            )
            self.ram_ready.setObjectName("GreenText")
        else:
            self.ram_ready.setText("Not Ready")
            self.ram_ready.setObjectName("RedText")
        self.ram_ready.style().unpolish(self.ram_ready)
        self.ram_ready.style().polish(self.ram_ready)

        input_text = payload.get(
            "input_status",
            "Pokebot-Luma RAM + Input 4952: Not tested",
        )
        self.input_ready.setText(input_text)
        self.input_ready.setObjectName(
            "GreenText" if payload.get("controller_ready") else "RedText"
        )
        self.input_ready.style().unpolish(self.input_ready)
        self.input_ready.style().polish(self.input_ready)
        self._apply_detected_game(payload)

    def set_status(self, status, message):
        normal = status.upper()
        self.bot_status.setText(normal)
        self.bot_sub.setText(message)

        if "HOLD" in normal:
            self.bot_status.setStyleSheet(
                "color:#ffce5e;font-size:17pt;font-weight:900;"
            )
            self.bot_sub.setStyleSheet("color:#ffce5e;font-weight:800;")
        elif normal == "RUNNING" or normal == "STARTING":
            self.bot_status.setStyleSheet(
                "color:#7bff6a;font-size:17pt;font-weight:900;"
            )
            self.bot_sub.setStyleSheet("color:#7bff6a;font-weight:800;")
        else:
            self.bot_status.setStyleSheet(
                "color:#7bff6a;font-size:17pt;font-weight:900;"
            )
            self.bot_sub.setStyleSheet("color:#7bff6a;font-weight:800;")

    def append_log(self, line):
        self.last_backend_message = str(line)

    def _reset_phase_session_extrema(self):
        """Clear phase-local extrema after a real shiny completes the phase.

        Session time/encounter/shiny totals remain continuous. Only the
        Highest/Lowest SV and IV Sum cards restart for the new phase.
        """
        self.session_high_sv = None
        self.session_low_sv = None
        self.session_high_iv_sum = None
        self.session_low_iv_sum = None
        for label in (
            "Highest SV:", "Lowest SV:",
            "Highest IV Sum:", "Lowest IV Sum:",
        ):
            self.session_pairs[label].setText("—")

    def update_encounter(self, data):
        # A new authoritative PK6 result increases the session numerator.
        # This is what makes Encounters/Hour jump back upward after each hunt
        # cycle; the 500ms timer then lets it decay naturally until the next.
        self.session_encounters += 1
        if self.selected_hunt_type not in ("static", "gift") and self.target_criteria.get("enabled"):
            self.target_search_seen += 1
        if data.get("is_shiny"):
            self.session_shinies += 1
        if data.get("target_match"):
            self.target_found = True
            self.target_odds_label.setText("TARGET FOUND — complete RAM criteria match")
            self.target_eta_label.setText(
                f"Matched after {self.target_search_seen:,} target-search encounter(s)"
            )

        sv = int(data.get("shiny_xor", 0))
        ivs = data.get("ivs") or {}
        iv_sum = sum(int(ivs.get(k, 0)) for k in (
            "hp", "attack", "defense", "sp_attack", "sp_defense", "speed"
        ))
        if self.session_high_sv is None or sv > self.session_high_sv:
            self.session_high_sv = sv
        if self.session_low_sv is None or sv < self.session_low_sv:
            self.session_low_sv = sv
        if self.session_high_iv_sum is None or iv_sum > self.session_high_iv_sum:
            self.session_high_iv_sum = iv_sum
        if self.session_low_iv_sum is None or iv_sum < self.session_low_iv_sum:
            self.session_low_iv_sum = iv_sum

        self.session_pairs["Highest SV:"].setText(str(self.session_high_sv))
        self.session_pairs["Lowest SV:"].setText(str(self.session_low_sv))
        self.session_pairs["Highest IV Sum:"].setText(
            f"{self.session_high_iv_sum}/186"
        )
        self.session_pairs["Lowest IV Sum:"].setText(
            f"{self.session_low_iv_sum}/186"
        )
        self._tick_session_telemetry()

        # The removed right-side Shiny diagnostics panel no longer needs UI
        # updates here. Shiny state remains available in `data` and continues
        # to feed history/session logic below.

        # A shiny completes the current phase. Keep cumulative session totals,
        # but start Highest/Lowest IV Sum and SV fresh for the next phase.
        if data.get("is_shiny"):
            self._reset_phase_session_extrema()

    def _add_history_row(self, table, data, *, force_shiny=None):
        ivs = data.get("ivs") or {}
        def _iv(name):
            value = ivs.get(name)
            try:
                return int(value)
            except Exception:
                return None
        vals = [_iv("hp"), _iv("attack"), _iv("defense"), _iv("sp_attack"), _iv("sp_defense"), _iv("speed")]
        total = sum(v for v in vals if v is not None) if all(v is not None for v in vals) else None

        self._last_seen_sprite_token += 1
        token = int(self._last_seen_sprite_token)
        species_name = str(data.get("species_name") or data.get("species") or data.get("starter") or "—")
        nature = str(data.get("nature", "—"))
        try:
            evolution_species = int(data.get("species_id") or data.get("species") or 0)
        except Exception:
            evolution_species = 0
        evolution = str(data.get("predicted_evolution") or "").strip()
        if not evolution:
            predicted = predict_split_evolution(evolution_species, data.get("ec"))
            evolution = str((predicted or {}).get("line") or "").strip()
        evolution_display = evolution or "—"

        # Prefer the actual PK6 ability ID whenever it is available. HF70's
        # starter Last Seen payload omitted Ability entirely, which is why the
        # starter rows showed an em dash even though PK6 had already decoded it.
        ability_value = data.get("ability")
        if ability_value in (None, "", "—") and data.get("ability_id") is not None:
            ability_value = ability_name(data.get("ability_id"))
        if ability_value in (None, "", "—"):
            try:
                starter_species_id = int(data.get("species_id") or data.get("species") or 0)
            except Exception:
                starter_species_id = 0
            ability_value = STARTER_ABILITY_BY_SPECIES.get(starter_species_id, "—")
        ability = str(ability_value)
        special_move = str(
            data.get("tutorial_fang_name")
            or data.get("special_move")
            or ""
        ).strip()
        ability_display = (
            f"{ability} • {special_move}"
            if special_move else ability
        )
        gender = str(data.get("gender", "—"))
        pid = str(data.get("pokemon_pid") or data.get("pid") or "—")
        try:
            sv = int(data.get("shiny_xor"))
        except Exception:
            sv = None
        shiny = bool(data.get("is_shiny")) if force_shiny is None else bool(force_shiny)

        iv_text = " / ".join("—" if v is None else str(v) for v in vals)
        tooltip = (
            f"<b>{species_name}</b> &nbsp; {gender}<br>"
            f"<b>Nature:</b> {nature}<br>"
            f"<b>Ability:</b> {ability}<br>"
            + (f"<b>Special move:</b> {special_move}<br>" if special_move else "")
            + (f"<b>Evolution:</b> {evolution}<br>" if evolution else "")
            + f"<b>PID:</b> {pid}<br>"
            f"<b>IVs:</b> {iv_text}<br>"
            f"<b>IV Sum:</b> {'—' if total is None else total}<br>"
            f"<b>SV:</b> {'—' if sv is None else sv}"
        )

        table.insertRow(0)
        table.setRowHeight(0, 30)
        sprite_item = QTableWidgetItem("")
        sprite_item.setData(Qt.UserRole, token)
        sprite_item.setToolTip(tooltip)
        sprite_item.setTextAlignment(Qt.AlignCenter)
        table.setItem(0, 0, sprite_item)

        nature_item = QTableWidgetItem(nature)
        nature_item.setTextAlignment(Qt.AlignCenter)
        nature_item.setToolTip(tooltip)
        table.setItem(0, 1, nature_item)

        ability_item = QTableWidgetItem(ability_display)
        ability_item.setTextAlignment(Qt.AlignCenter)
        ability_item.setToolTip(tooltip)
        table.setItem(0, 2, ability_item)

        evolution_item = QTableWidgetItem(evolution_display)
        evolution_item.setTextAlignment(Qt.AlignCenter)
        evolution_item.setToolTip(tooltip)
        if evolution:
            evolution_item.setForeground(QBrush(QColor("#7bff6a")))
        table.setItem(0, 3, evolution_item)

        for offset, value in enumerate(vals, start=4):
            item = QTableWidgetItem("—" if value is None else str(value))
            item.setTextAlignment(Qt.AlignCenter)
            item.setToolTip(tooltip)
            if value is not None:
                item.setForeground(self._iv_colour(value))
            table.setItem(0, offset, item)

        sum_item = QTableWidgetItem("—" if total is None else str(total))
        sum_item.setTextAlignment(Qt.AlignCenter)
        sum_item.setToolTip(tooltip)
        sum_item.setForeground(QBrush(QColor("#eef4ff")))
        table.setItem(0, 10, sum_item)

        sv_item = QTableWidgetItem("—" if sv is None else f"{sv:,}")
        sv_item.setTextAlignment(Qt.AlignCenter)
        sv_item.setToolTip(tooltip)
        sv_item.setForeground(QBrush(QColor("#ffd54f")) if shiny else QBrush(QColor("#ff5a62")))
        table.setItem(0, 11, sv_item)

        species_id = data.get("species_id")
        if species_id is None and isinstance(data.get("species"), int):
            species_id = data.get("species")
        try:
            species_id = int(species_id or 0)
        except Exception:
            species_id = 0
        self.last_seen_sprite_loader.request_sprite(token, species_id, shiny)
        while table.rowCount() > HISTORY_VISIBLE_ROWS:
            table.removeRow(table.rowCount() - 1)

    def add_last_seen(self, data):
        self._add_history_row(self.last_seen_table, data)

    def set_last_seen_history(self, entries):
        self.last_seen_table.setRowCount(0)
        # Stored history is newest-first. Insert oldest first because
        # add_last_seen always inserts at row 0.
        for entry in reversed(list(entries or [])[:HISTORY_VISIBLE_ROWS]):
            self.add_last_seen(entry)

    @staticmethod
    def _iv_colour(value):
        value = int(value)
        if value == 31:
            return QBrush(QColor("#ffd54f"))
        if value >= 25:
            return QBrush(QColor("#4ee07a"))
        if value == 0:
            return QBrush(QColor("#d64cff"))
        if value <= 5:
            return QBrush(QColor("#ff515b"))
        return QBrush(QColor("#f2f4f7"))

    def _last_seen_sprite_ready(self, attempt_token, pixmap):
        if pixmap is None or pixmap.isNull():
            return

        for table in (self.last_seen_table, self.recent_oras_table, self.recent_target_table):
            for row in range(table.rowCount()):
                item = table.item(row, 0)
                if item is None:
                    continue
                if item.data(Qt.UserRole) == attempt_token:
                    item.setIcon(QIcon(pixmap))
                    return

    def update_party(self, party):
        rows = list(party or [])[:6]
        while len(rows) < 6:
            rows.append({
                "slot": len(rows) + 1,
                "species": "Empty",
                "species_id": 0,
                "nature": "—",
                "gender": "—",
                "ivs": {},
                "evs": {},
                "hidden_power": "—",
                "pokerus": "—",
                "sv": None,
                "shiny": False,
            })

        for index, mon in enumerate(rows):
            self.party_cards[index].set_party(mon)
            species_id = int(mon.get("species_id") or 0)
            self.sprite_loader.request_sprite(
                index,
                species_id,
                bool(mon.get("shiny")),
            )

    def _party_sprite_ready(self, index, pixmap):
        if 0 <= index < len(self.party_cards):
            self.party_cards[index].set_sprite(pixmap)

    def update_stats(self, data):
        if str(data.get("hunt_type", "")).lower() == "static":
            self.target_phases["static"] = {
                "phase_index": int(data.get("phase_index", 1)),
                "phase_seen": int(data.get("phase_seen", 0)),
                "phase_log_miss": data.get("phase_log_miss"),
                "phase_cumulative_probability": data.get("phase_cumulative_probability"),
            }
            static_target = str(data.get("target") or self.selected_static_profile)
            static_location = str(data.get("location_name") or "")
            self.hunt_values["Target:"].setText(f"{static_target} — {static_location}" if static_location else static_target)
            last = data.get("last_shiny")
            self.hunt_values["Last Shiny:"].setText(
                last.get("species", "—") if isinstance(last, dict) else "—"
            )
            self.hunt_values["Cycle Avg:"].setText(f"{data.get('average_time', 0.0):.2f}s")
            self.hunt_values["Field Avg:"].setText("—")
            self.hunt_values["Battle/Return Avg:"].setText("—")
            self.latest_field_pace_per_hour = None
            worker_resets = int(data.get("resets", 0))
            if worker_resets > self.worker_resets_seen:
                self.session_resets += worker_resets - self.worker_resets_seen
                self.worker_resets_seen = worker_resets
            self._tick_session_telemetry()
            return

        if str(data.get("hunt_type", "")).lower() == "gift":
            self.target_phases["gift"] = {
                "phase_index": int(data.get("phase_index", 1)),
                "phase_seen": int(data.get("phase_seen", 0)),
                "phase_log_miss": data.get("phase_log_miss"),
                "phase_cumulative_probability": data.get("phase_cumulative_probability"),
            }
            target = str(data.get("target") or self.selected_gift_profile)
            location = str(data.get("location_name") or "")
            self.hunt_values["Target:"].setText(f"{target} — {location}" if location else target)
            last = data.get("last_shiny")
            self.hunt_values["Last Shiny:"].setText(last.get("species", "—") if isinstance(last, dict) else "—")
            self.hunt_values["Cycle Avg:"].setText(f"{data.get('average_time', 0.0):.2f}s")
            self.hunt_values["Field Avg:"].setText("—")
            self.hunt_values["Battle/Return Avg:"].setText("—")
            self.latest_field_pace_per_hour = None
            worker_resets = int(data.get("resets", 0))
            if worker_resets > self.worker_resets_seen:
                self.session_resets += worker_resets - self.worker_resets_seen
                self.worker_resets_seen = worker_resets
            self._tick_session_telemetry()
            return

        if str(data.get("hunt_type", "")).lower() == "wild":
            if data.get("target"):
                self.current_wild_location = str(
                    data.get("location_name")
                    or data.get("target")
                )
                if self.selected_wild_target:
                    self._refresh_selected_wild_target_display()
                else:
                    self.hunt_values["Target:"].setText(
                        str(data.get("target"))
                    )
            method = str(data.get("wild_method", self.selected_wild_method))
            key = (
                "wild_cave"
                if method in {"cave", "cave_run", "cave_bunny"}
                else f"wild_{method}"
            )
            self.target_phases[key] = {
                "phase_index": int(data.get("phase_index", 1)),
                "phase_seen": int(data.get("phase_seen", 0)),
                "phase_log_miss": data.get("phase_log_miss"),
                "phase_cumulative_probability": data.get("phase_cumulative_probability"),
            }
            if method == "fishing" and isinstance(data.get("fishing_chain"), dict):
                self.fishing_chain_telemetry = dict(data.get("fishing_chain"))
            last = data.get("last_shiny")
            self.hunt_values["Last Shiny:"].setText(
                last.get("species", "—")
                if isinstance(last, dict) else "—"
            )
            measured_cycle = float(
                data.get("measured_cycle_average", 0.0) or 0.0
            )
            field_avg = float(
                data.get("field_to_encounter_average", 0.0) or 0.0
            )
            battle_avg = float(
                data.get("battle_to_field_average", 0.0) or 0.0
            )
            field_pace = float(
                data.get("field_pace_per_hour", 0.0) or 0.0
            )
            if field_pace <= 0.0 and field_avg > 0.0:
                field_pace = 3600.0 / field_avg
            self.latest_field_pace_per_hour = (
                field_pace if field_pace > 0.0 else None
            )
            self.hunt_values["Cycle Avg:"].setText(
                f"{measured_cycle:.2f}s" if measured_cycle > 0 else "—"
            )
            self.hunt_values["Field Avg:"].setText(
                f"{field_avg:.2f}s" if field_avg > 0 else "—"
            )
            self.hunt_values["Battle/Return Avg:"].setText(
                f"{battle_avg:.2f}s" if battle_avg > 0 else "—"
            )
            for attr, source in (
                ("session_high_sv", "highest_sv"),
                ("session_low_sv", "lowest_sv"),
                ("session_high_iv_sum", "highest_iv_sum"),
                ("session_low_iv_sum", "lowest_iv_sum"),
            ):
                value = data.get(source)
                if value is not None:
                    setattr(self, attr, int(value))
            self._tick_session_telemetry()
            return

        # HuntWorker reset counters restart at zero each time Start creates a
        # new worker. Convert them into one persistent UI session total.
        worker_resets = int(data.get("resets", 0))
        if worker_resets > self.worker_resets_seen:
            self.session_resets += (
                worker_resets - self.worker_resets_seen
            )
            self.worker_resets_seen = worker_resets

        # Phase state belongs to the selected target, not to the whole bot.
        starter_name = str(data.get("starter", "")).lower()
        if starter_name in self.target_phases:
            self.target_phases[starter_name] = {
                "phase_index": int(data.get("phase_index", 1)),
                "phase_seen": int(data.get("phase_seen", 0)),
                "phase_log_miss": data.get("phase_log_miss"),
                "phase_cumulative_probability": data.get("phase_cumulative_probability"),
            }

        # These values are still backend-authoritative and are not session
        # stopwatch counters.
        last = data.get("last_shiny")
        self.hunt_values["Last Shiny:"].setText(
            last.get("starter", "—")
            if isinstance(last, dict) else "—"
        )
        self.hunt_values["Cycle Avg:"].setText(
            f"{data.get('average_time', 0.0):.2f}s"
        )
        self.hunt_values["Field Avg:"].setText("—")
        self.hunt_values["Battle/Return Avg:"].setText("—")
        self.latest_field_pace_per_hour = None

        # Session extrema are UI-owned so Stop -> Start resumes without
        # clearing them. Backend values are used only to bootstrap a fresh UI.
        if self.session_high_sv is None and data.get("highest_sv") is not None:
            self.session_high_sv = int(data["highest_sv"])
        if self.session_low_sv is None and data.get("lowest_sv") is not None:
            self.session_low_sv = int(data["lowest_sv"])
        if self.session_high_iv_sum is None and data.get("highest_iv_sum") is not None:
            self.session_high_iv_sum = int(data["highest_iv_sum"])
        if self.session_low_iv_sum is None and data.get("lowest_iv_sum") is not None:
            self.session_low_iv_sum = int(data["lowest_iv_sum"])

        self.session_pairs["Highest SV:"].setText(
            "—" if self.session_high_sv is None else str(self.session_high_sv)
        )
        self.session_pairs["Lowest SV:"].setText(
            "—" if self.session_low_sv is None else str(self.session_low_sv)
        )
        self.session_pairs["Highest IV Sum:"].setText(
            "—" if self.session_high_iv_sum is None else f"{self.session_high_iv_sum}/186"
        )
        self.session_pairs["Lowest IV Sum:"].setText(
            "—" if self.session_low_iv_sum is None else f"{self.session_low_iv_sum}/186"
        )

        # Never overwrite Session Time or Encounters/Hour with worker-local
        # values. The UI timer owns them so decay/resume/stop are correct.
        self._tick_session_telemetry()

    def set_target_phase_data(self, phase_data):
        for key in (
            "treecko", "torchic", "mudkip",
            "wild_walk", "wild_run", "wild_acro_bunny",
        ):
            incoming = (phase_data or {}).get(key)
            if not isinstance(incoming, dict):
                continue
            self.target_phases[key] = {
                "phase_index": int(incoming.get("phase_index", 1)),
                "phase_seen": int(incoming.get("phase_seen", 0)),
            }
        self._tick_session_telemetry()

    def set_recent_shinies(self, items):
        self._recent_shiny_items = list(items or [])
        items = self._recent_shiny_items
        criteria = normalize_target(self.target_criteria)
        species_ids = {int(x) for x in criteria.get("species", [])}

        target_items = []
        for item in items:
            try:
                sid = int(item.get("species_id") or item.get("species") or 0)
            except Exception:
                sid = 0
            if species_ids:
                if sid in species_ids:
                    target_items.append(item)
            elif item.get("target_match") is True:
                target_items.append(item)

        self.recent_target_table.setRowCount(0)
        self.recent_oras_table.setRowCount(0)
        for item in reversed(list(target_items or [])[:HISTORY_VISIBLE_ROWS]):
            self._add_history_row(self.recent_target_table, item, force_shiny=True)
        for item in reversed(list(items or [])[:HISTORY_VISIBLE_ROWS]):
            self._add_history_row(self.recent_oras_table, item, force_shiny=True)


    def _refresh_cached_shiny_history(self):
        if hasattr(self, "_recent_shiny_items"):
            self.set_recent_shinies(self._recent_shiny_items)

    def reset_all_statistics_ui(self):
        self.session_elapsed_accum = 0.0
        self.session_active_started = None
        self.session_encounters = 0
        self.session_shinies = 0
        self.session_resets = 0
        self.worker_resets_seen = 0
        self.session_high_sv = None
        self.session_low_sv = None
        self.session_high_iv_sum = None
        self.session_low_iv_sum = None
        self.target_search_seen = 0
        self.target_found = False
        self.fishing_chain_telemetry = None
        self.latest_field_pace_per_hour = None

        self.target_phases = {
            "treecko": {"phase_index": 1, "phase_seen": 0},
            "torchic": {"phase_index": 1, "phase_seen": 0},
            "mudkip": {"phase_index": 1, "phase_seen": 0},
            "wild_walk": {"phase_index": 1, "phase_seen": 0},
            "wild_run": {"phase_index": 1, "phase_seen": 0},
            "wild_acro_bunny": {"phase_index": 1, "phase_seen": 0},
            "wild_horde": {"phase_index": 1, "phase_seen": 0},
            "wild_cave": {"phase_index": 1, "phase_seen": 0},
            "wild_surf": {"phase_index": 1, "phase_seen": 0},
            "wild_fishing": {"phase_index": 1, "phase_seen": 0},
            "static": {"phase_index": 1, "phase_seen": 0},
            "gift": {"phase_index": 1, "phase_seen": 0},
        }

        self.last_seen_table.setRowCount(0)
        self.set_recent_shinies([])
        self.hunt_values["Last Shiny:"].setText("—")
        self.hunt_values["Cycle Avg:"].setText("0.00s")
        self.hunt_values["Field Avg:"].setText("—")
        self.hunt_values["Battle/Return Avg:"].setText("—")
        self.hunt_values["Field Pace/Hour:"].setText("—")
        self.session_pairs["Field Pace/Hour:"].setText("—")
        self.session_pairs["Highest SV:"].setText("—")
        self.session_pairs["Lowest SV:"].setText("—")
        self.session_pairs["Highest IV Sum:"].setText("—")
        self.session_pairs["Lowest IV Sum:"].setText("—")
        self._tick_session_telemetry()

    def support_ready(self, path):
        self.last_support = path
        self.append_log(f"Support ZIP: {path}")
