from __future__ import annotations

import ast
import hashlib
import importlib
import json
import os
import pkgutil
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REQUIRED = [
    "run_qt_live.py",
    "RUN_BOT.bat",
    "requirements.txt",
    "requirements.bat",
    "README.txt",
    "3ds_sd/boot.firm",
    "pokebot/common/shiny_odds.py",
    "pokebot/common/live_party.py",
    "pokebot/gift/oras_gifts.py",
    "pokebot/wild/fishing_chain.py",
    "pokebot/wild/auto_capture.py",
    "pokebot/static/oras_static.py",
    "qt_ui/backend_worker.py",
    "qt_ui/wild_worker.py",
    "qt_ui/static_worker.py",
    "qt_ui/gift_worker.py",
    "qt_ui/dashboard_page.py",
    "qt_ui/stats_page.py",
    "data/oras_gen6_move_metadata.json",
    "tools/offline_regression.py",
    "tools/RELEASE_SELF_TEST.bat",
]



def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    errors = []
    allowed_root_files = {
        "README.txt", "RUN_BOT.bat", "requirements.bat",
        "requirements.txt", "run_qt_live.py",
        "RUN_BOT_CONSOLE_1.bat", "RUN_BOT_CONSOLE_2.bat",
        "RUN_BOT_MULTI_PROFILE.bat", "HF96_CHANGES_PREVIOUS.txt",
        "HF97_CHANGES.txt",
    }
    unexpected_root_files = sorted(
        p.name for p in ROOT.iterdir()
        if p.is_file() and p.name not in allowed_root_files
    )
    if unexpected_root_files:
        errors.append(
            "unexpected top-level release files: " + ", ".join(unexpected_root_files)
        )
    for rel in REQUIRED:
        if not (ROOT / rel).is_file():
            errors.append(f"missing required file: {rel}")

    py_files = [p for p in ROOT.rglob("*.py") if "__pycache__" not in p.parts]
    for path in py_files:
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except Exception as exc:
            errors.append(f"syntax {path.relative_to(ROOT)}: {exc}")

    # Import every pure pokebot module. qt_ui is excluded because this validator
    # is also intended to run on build hosts before PySide6 is installed.
    import pokebot
    import_failures = []
    for mod in pkgutil.walk_packages(pokebot.__path__, prefix="pokebot."):
        name = mod.name
        try:
            importlib.import_module(name)
        except Exception as exc:
            import_failures.append(f"{name}: {type(exc).__name__}: {exc}")
    errors.extend("import " + x for x in import_failures)

    # Import the exact Wild worker path that previously exposed a stripped
    # Horde dependency. A tiny QtCore stub is enough because this is an import
    # graph test, not a GUI execution test.
    qtcore = types.ModuleType("PySide6.QtCore")
    class _QObject:
        pass
    class _SignalInstance:
        def emit(self, *args, **kwargs):
            return None
    def _Signal(*args, **kwargs):
        return _SignalInstance()
    def _Slot(*args, **kwargs):
        def deco(fn):
            return fn
        return deco
    qtcore.QObject = _QObject
    qtcore.Signal = _Signal
    qtcore.Slot = _Slot
    pyside = types.ModuleType("PySide6")
    pyside.QtCore = qtcore
    old_pyside = sys.modules.get("PySide6")
    old_qtcore = sys.modules.get("PySide6.QtCore")
    try:
        sys.modules["PySide6"] = pyside
        sys.modules["PySide6.QtCore"] = qtcore
        importlib.import_module("qt_ui.wild_worker")
        importlib.import_module("qt_ui.static_worker")
        importlib.import_module("qt_ui.gift_worker")
    except Exception as exc:
        errors.append(f"wild_worker import: {type(exc).__name__}: {exc}")
    finally:
        if old_pyside is None:
            sys.modules.pop("PySide6", None)
        else:
            sys.modules["PySide6"] = old_pyside
        if old_qtcore is None:
            sys.modules.pop("PySide6.QtCore", None)
        else:
            sys.modules["PySide6.QtCore"] = old_qtcore

    # Public profile rule: no active DEV_DATA references outside documentation.
    marker = "DEV" + "_DATA"
    for path in py_files:
        if path.resolve() == Path(__file__).resolve():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if marker in text:
            errors.append(f"legacy isolated-storage reference: {path.relative_to(ROOT)}")

    # BO live-party authority must not retain executable stale fixed-copy readers.
    live_party_text = (ROOT / "pokebot/common/live_party.py").read_text(encoding="utf-8", errors="replace")
    for forbidden in ("_read_save260", "_read_working484", "_probe_fixed_copies", "WORKING484_BASE", "SAVE260_BASE"):
        if forbidden in live_party_text:
            errors.append(f"canonical live-party stale reader remains: {forbidden}")

    gift_worker_text = (ROOT / "qt_ui/gift_worker.py").read_text(encoding="utf-8", errors="replace")
    for token in (
        "def _decline_fossil_nickname",
        'inputs.pulse(("B",), hold_ms=180',
        'for attempt in range(1, 5):',
        'FOSSIL POST-REVIVAL B-ONLY CLEAR',
        'FOSSIL POST-REVIVAL B-ONLY CLEAR COMPLETE',
        'AUTO_CAPTURE_STYLE_B_ONLY_CLEAR',
        'self._decline_fossil_nickname(',
    ):
        if token not in gift_worker_text:
            errors.append(f"DI fossil B-only contract missing: {token}")
    try:
        _di_shiny = gift_worker_text.index('if payload.get("is_shiny"):', gift_worker_text.index('for batch_index, expected_slot'))
        _di_decline = gift_worker_text.index('self._decline_fossil_nickname(', _di_shiny)
        if _di_decline <= _di_shiny:
            errors.append("DI fossil B-only clear occurs before shiny HOLD guard")
        _di_helper_start = gift_worker_text.index('def _decline_fossil_nickname')
        _di_helper_end = gift_worker_text.index('def _snapshot_party', _di_helper_start)
        _di_helper = gift_worker_text[_di_helper_start:_di_helper_end]
        if 'inputs.pulse(("A",)' in _di_helper or 'inputs.pulse(("DOWN",)' in _di_helper:
            errors.append("DI fossil post-revival helper must remain B-only")
    except ValueError:
        errors.append("DI fossil B-only/shiny ordering contract missing")

    main_window_text = (ROOT / "qt_ui/main_window.py").read_text(encoding="utf-8", errors="replace")
    for token in (
        "def _request_auxiliary_shutdown",
        "def _finish_deferred_close_when_idle",
        "MAIN_WINDOW_CLOSE_DEFERRED",
        "QTimer.singleShot(0, self.close)",
        "event.ignore()",
    ):
        if token not in main_window_text:
            errors.append(f"DD deferred QThread shutdown contract missing: {token}")
    if "thread.wait(5_000)" in main_window_text:
        errors.append("DD stale synchronous 5-second QThread shutdown wait remains")

    bag_throw_text = (ROOT / "pokebot/wild/battle_bag_throw.py").read_text(encoding="utf-8", errors="replace")
    for token in ("POST_CAPTURE_FLOW_DIRECT_PARTY_RETURN_REGISTERED = 0x0000173E", "POST_CAPTURE_FLOW_DIRECT_PARTY_RETURN_UNREGISTERED = 0x00001767", "POST_CAPTURE_DIRECT_PARTY_RETURN_BY_NICKNAME", "POST_CAPTURE_DIRECT_PARTY_COUNTS = frozenset(range(1, 6))", "POST_CAPTURE_FULL_PARTY_COUNT = 6", "def post_capture_destination_for_party_count", "pre_capture_party_count", "def classify_post_nickname_b_transition", "v0p43CV: stop overfitting free-party recovery", '"FREE_PARTY_SLOT_NO_BOX_MESSAGE"'):
        if token not in bag_throw_text:
            errors.append(f"free-party post-capture resume contract missing: {token}")
    for token in ("ACTSELECT_PHASE_STATE_OFF = 0x93", "ACTSELECT_COMMAND_PHASE = 1", "def _wait_command_touch_ready", "COMMAND_TOUCH_READY_MIN_DWELL_SECONDS = 0.60"):
        if token not in bag_throw_text:
            errors.append(f"single Auto-Capture touch-ready COMMAND authority missing: {token}")
    registry_text = (ROOT / "pokebot/wild/registry.py").read_text(encoding="utf-8", errors="replace")
    omega_block = registry_text.split('"0x000400000011c400":', 1)[1] if '"0x000400000011c400":' in registry_text else ""
    for token in ('"key": "surf"', '"key": "fishing"', 'OMEGA RUBY HARDWARE VALIDATION REQUIRED'):
        if token not in omega_block:
            errors.append(f"CX Omega Ruby Surf/Fishing exposure missing: {token}")
    wild_text = (ROOT / "qt_ui/wild_worker.py").read_text(encoding="utf-8", errors="replace")
    if "Omega Ruby fishing remains disabled pending proof" in wild_text:
        errors.append("CX legacy Omega Ruby Fishing hard-disable remains")
    for token in ("def _pre_capture_party_count_authority", "AUTO-CAPTURE PRE-CATCH PARTY AUTHORITY", "pre_capture_party_count=pre_capture_party_count"):
        if token not in wild_text:
            errors.append(f"all-party-capacity post-capture integration missing: {token}")
    for token in ("def _cave_zone_ids_for_location", "def _promote_runtime_to_cave", "CAVE AUTO-AUTHORITY", "edge tiles are legal starts"):
        if token not in wild_text:
            errors.append(f"CW Cave auto-authority missing: {token}")
    cave_text = (ROOT / "qt_ui/cave_movement.py").read_text(encoding="utf-8", errors="replace")
    for token in ("WALL_BOUNDED_NO_CORRIDOR_PREFLIGHT", "BLOCKED_BY_CAVE_COLLISION", "edge_start_allowed"):
        if token not in cave_text:
            errors.append(f"CW Cave edge-run policy missing: {token}")
    if "needs six clear tiles on BOTH sides" in cave_text:
        errors.append("CW legacy +/-6 Cave Run startup requirement remains")
    static_text = (ROOT / "qt_ui/static_worker.py").read_text(encoding="utf-8", errors="replace")
    dash_text = (ROOT / "qt_ui/dashboard_page.py").read_text(encoding="utf-8", errors="replace")
    main_text = (ROOT / "qt_ui/main_window.py").read_text(encoding="utf-8", errors="replace")
    for token in ("run_reset_to_field_for_profile", "validate_any_loaded_field", "validate_saved_field_anchor", "StaticEncounterStateMachine", "STATIC SHINY HOLD"):
        if token not in static_text:
            errors.append(f"static worker missing integration token: {token}")
    for token in ("self.last_seen_path", "self.encounter_path", "_save_last_seen_entry", "_append_encounter_ledger", "lifetime_hunt_seconds"):
        if token not in static_text:
            errors.append(f"static persistence contract missing: {token}")
    stats_text = (ROOT / "qt_ui/stats_page.py").read_text(encoding="utf-8", errors="replace")
    if 'note("Static",shiny,duration)' not in stats_text or '"Static","Gift","Grass"' not in stats_text:
        errors.append("STATS page missing first-class Static/Gift method aggregation")
    if 'note("Gift",shiny,duration)' not in stats_text:
        errors.append("STATS page missing Gift method aggregation")

    # DB five-fossil batch policy.
    gift_profile_text = (ROOT / "pokebot/gift/oras_gifts.py").read_text(encoding="utf-8", errors="replace")
    gift_worker_text = (ROOT / "qt_ui/gift_worker.py").read_text(encoding="utf-8", errors="replace")
    for token in ("FOSSIL_BATCH_5", "fossil_batch", "FOSSIL_SPECIES", "fossil_aerodactyl", "find_new_party_member"):
        if token not in gift_profile_text:
            errors.append(f"DB fossil profile contract missing: {token}")
    for token in (
        'if p.trigger_kind == "FOSSIL_BATCH_5":',
        'enumerate(range(2, 7), start=1)',
        'FOSSIL BATCH PK6',
        'no further fossil input sent',
        'FOSSIL BATCH RESET',
        'species not in FOSSIL_SPECIES',
    ):
        if token not in gift_worker_text:
            errors.append(f"DB fossil batch runtime contract missing: {token}")

    # DI fossil post-revival handling: Auto-Capture-style B-only clear.
    for token in (
        'inputs.pulse(("B",), hold_ms=180',
        'for attempt in range(1, 5):',
        'FOSSIL POST-REVIVAL B-ONLY CLEAR',
        'FOSSIL POST-REVIVAL B-ONLY CLEAR COMPLETE',
        'AUTO_CAPTURE_STYLE_B_ONLY_CLEAR',
        'Auto-Capture nickname policy: B only; A forbidden until clear completes',
        'allow_unmapped=True',
    ):
        if token not in gift_worker_text:
            errors.append(f"DI fossil B-only nickname contract missing: {token}")
    helper_start = gift_worker_text.find('def _decline_fossil_nickname')
    helper_end = gift_worker_text.find('def _snapshot_party', helper_start)
    helper = gift_worker_text[helper_start:helper_end] if helper_start >= 0 and helper_end > helper_start else ''
    if 'inputs.pulse(("A",)' in helper or 'inputs.pulse(("DOWN",)' in helper:
        errors.append("DI fossil nickname clear must remain B-only; A/DOWN forbidden")

    # Static policy is intentionally manual-capture-only. The release must fail
    # if the Static worker can call the shared Auto Capture dispatcher again.
    for forbidden in ("run_shiny_auto_capture", "clear_captured_shiny_post_capture", "self.auto_capture"):
        if forbidden in static_text:
            errors.append(f"Static Auto Capture path must remain absent: {forbidden}")
    if 'visible = self.selected_hunt_type == "wild"' not in dash_text:
        errors.append("Static must hide Auto-catch/Ball controls in dashboard")

    if "static_start_requested = Signal(str)" not in dash_text or "self.static_btn.setEnabled(False)" in dash_text:
        errors.append("Static dashboard wiring missing or disabled")
    if "def start_static_hunt" not in main_text or "StaticHuntWorker" not in main_text:
        errors.append("Static MainWindow worker wiring missing")
    bootstrap_pos = static_text.find("STATIC BOOTSTRAP RESET RESULT")
    anchor_pos = static_text.find("STATIC SAVED FIELD ANCHOR AFTER BOOTSTRAP")
    if bootstrap_pos < 0 or anchor_pos < 0 or bootstrap_pos >= anchor_pos:
        errors.append("Static reset-first bootstrap order missing: reset must precede saved-anchor capture")
    for token in ("STATIC STARTUP TRANSPORT RETRY", "STATIC STEP TRIGGER ABOUT TO SEND", "STATIC STEP TRIGGER SENT", "STATIC STEP DIALOGUE A ABOUT TO SEND", "STATIC STEP DIALOGUE A SENT", "Regigigas post-step dialogue A budget exhausted without battle", "saved location mismatch"):
        if token not in static_text:
            errors.append(f"Regigigas transport/trigger proof token missing: {token}")
    static_profile_text = (ROOT / "pokebot/static/oras_static.py").read_text(encoding="utf-8", errors="replace")
    if '"regigigas"' not in static_profile_text or 'expected_zone_id=158' not in static_profile_text:
        errors.append("Regigigas Island Cave zone guard missing")
    if 'HARDWARE_PROVEN_2026_08_29' not in static_profile_text:
        errors.append("Regigigas hardware-proven metadata promotion missing")
    for token in ('"latias_eon"', '"latios_eon"', 'reset_kind="RUN_REINTERACT"', 'location="Southern Island (Eon Ticket)"', 'HARDWARE_PROVEN_SHARED_ORAS_RUN_REINTERACT'):
        if token not in static_profile_text:
            errors.append(f"CZ Eon Ticket static profile contract missing: {token}")
    for token in ('self.static_profile.reset_kind == "RUN_REINTERACT"', 'core.causal_run_until_field(battle_br)', 'STATIC RUN_REINTERACT FIELD AUTHORITY', 'INTERACT_A_ON_NEXT_LOOP'):
        if token not in static_text:
            errors.append(f"CZ Eon Ticket RUN_REINTERACT worker contract missing: {token}")
    for token in ('def _trigger_run_reinteract', 'grace_deadline = time.monotonic() + 2.0', 'for index in range(1, 9)', 'self.run_reinteract_pending = True', 'STATIC RUN_REINTERACT TRIGGER'):
        if token not in static_text:
            errors.append(f"DA Eon Ticket re-interact readiness contract missing: {token}")
    for token in ("RING_SHARED_PROVEN_ORAS", "RING_SHARED_PROVEN_AS", "def _ring(", "def hardware_validation_label(", 'family", "ring_static"'):
        if token not in static_profile_text:
            errors.append(f"shared ring hardware-proof metadata missing: {token}")
    for token in ('party_payload.get("payload")', 'mon.get("species_id")', "SOARING RIFT PARTY REQUIREMENT"):
        if token not in static_text:
            errors.append(f"Dimensional Rift live-party schema fix missing: {token}")
    if 'suffix = "Hardware validation needed"' in dash_text:
        errors.append("Static dropdown still hard-codes Hardware validation needed")
    if "hardware_validation_label(profile, game_key)" not in dash_text:
        errors.append("Static dashboard does not render real validation status")

    gift_profile_text = (ROOT / "pokebot/gift/oras_gifts.py").read_text(encoding="utf-8", errors="replace")
    gift_worker_text = (ROOT / "qt_ui/gift_worker.py").read_text(encoding="utf-8", errors="replace")
    for token in ("class GiftPokemonProfile", "def find_new_gift", 'out["party_slot"] = index + 1', "range(1, min(6, len(rows)))"):
        if token not in gift_profile_text:
            errors.append(f"CY Gift slots 2-6 PK6 authority missing: {token}")
    for token in ("class GiftHuntWorker", "gift party count did not increase by exactly one", "gift PK6 authority must be party slot 2-6", "run_reset_to_field_for_profile"):
        if token not in gift_worker_text:
            errors.append(f"CY Gift worker contract missing: {token}")
    for token in ('self.gift_btn = QPushButton("Gifts")', 'gift_start_requested = Signal(str)', 'self.gift_start_requested.emit(self.selected_gift_profile)', 'label = f"{profile.name} — {profile.location}"'):
        if token not in dash_text:
            errors.append(f"CY Gift/Static dashboard contract missing: {token}")
    for token in ("GiftHuntWorker", "def start_gift_hunt", "self.dashboard.gift_start_requested.connect(self.start_gift_hunt)"):
        if token not in main_text:
            errors.append(f"CY Gift MainWindow wiring missing: {token}")

    soaring_text = (ROOT / "pokebot/static/soaring_mapper.py").read_text(encoding="utf-8", errors="replace")
    for token in ("SOARING_RIFT_MAPPER", "expected_zone_id=8", "required_party_species=(480, 481, 482)", "SOARING_WEATHER_UNIMPLEMENTED"):
        if token not in static_profile_text:
            errors.append(f"Soaring Static profile contract missing: {token}")
    for token in ("SOARING RIFT MAPPER Y ABOUT TO SEND", "enter the Dimensional Rift", "self.soaring_trace_path", "_run_soaring_rift_mapper"):
        if token not in static_text:
            errors.append(f"Dimensional Rift mapper worker token missing: {token}")
    for token in ("primary_xyz", "secondary_xyz", "battle_phase", "summarize_trace"):
        if token not in soaring_text:
            errors.append(f"Soaring trace helper missing: {token}")

    skytrip_text = (ROOT / "pokebot/static/skytrip_runtime.py").read_text(encoding="utf-8", errors="replace")
    for token in (
        'SKYTRIP_MODULE = "DllSkyTrip"',
        "SKYTRIP_FILE_SIZE = 0x12000",
        "SKYTRIP_BSS_SIZE = 0x1A4",
        "MAINPROC_VPTR_LITERAL_OFFSET = 0xBF40",
        '"MainProc": 0xDFA0',
        '"Camera": 0xDF70',
        '"BMEncount": 0xDFCC',
        "POINTER_GRAPH_MAX_OBJECTS = 128",
        "READ_CHUNK = 0x200",
        "def discover_skytrip_objects",
        "def read_runtime_module_state",
        "def _runtime_header_layout",
        "def _runtime_segment_table",
        '"relocated_type2_segment"',
        '"runtime_data_resolution"',
        "HEAP_ANCHOR_PROBE_BYTES = 0x10000",
        "def _probe_heap_anchor_vptrs",
        '"anchor_probe"',
        '"module_bss"',
        '"nonzero_words"',
        "def diff_runtime_module_state",
        '"live_state_trace"',
        '"float_words"',
        "allow_anchor_probe: bool = True",
        "EXTERNAL_TRACE_TARGET_MAX = 16",
        "def discover_skytrip_external_references",
        "def read_external_trace_targets",
        "def diff_external_trace_state",
        '"external_reference_mapper"',
        "def _read_runtime_scan_segment",
        '"scanned_segments"',
        '"nominal_code_size"',
        "USERLAND_PTR_MIN = 0x00100000",
        "EXTERNAL_DIRECT_ROOT_MAX = 24",
        "def _skytrip_runtime_ranges",
        "def _extract_userland_ptrs",
        '"FOUND_ESCAPED_EXTERNAL_REFS"',
        '"internal_runtime_literal_values_filtered"',
        '"module_local_escaped_roots"',
        '"read_only_bridge_escaped_roots"',
        "FAST_PLAN_VERSION = 1",
        "FAST_TRACE_TARGET_MAX = 8",
        "def discover_skytrip_fast_references",
        "def _arm_ldr_literal_slots",
        "def build_fast_trace_batches",
        "def read_external_trace_targets_batched",
        "def update_fast_motion_scores",
        '"fast_static_plan"',
    ):
        if token not in skytrip_text:
            errors.append(f"SkyTrip runtime mapper contract missing: {token}")
    for token in (
        "SKYTRIP RUNTIME OBJECT MAP",
        "watching DllSkyTrip load lifecycle",
        "skytrip_runtime_path",
        "skytrip_fast_plan_path",
        "discover_skytrip_fast_references",
        "build_fast_trace_batches",
        "read_external_trace_targets_batched",
        "update_fast_motion_scores",
        "SKYTRIP FAST REFERENCE MAP",
        "SKYTRIP FAST MOTION CANDIDATE LOCK",
        "external_state_change_events",
        "fast_motion_lock",
        "last_fast_ref_retry",
        "CK fast SkyTrip mapper ready",
    ):
        if token not in static_text:
            errors.append(f"SkyTrip worker integration missing: {token}")
    for token in (
        '_locate_skytrip("BEFORE_Y", 0.0)',
        '"AFTER_Y_TRANSITION"',
        '"MANUAL_FLIGHT"',
        '"BATTLE_BOUNDARY" if battle_seen else "MAPPER_TIMEOUT"',
        '"skytrip_lifecycle_events"',
        'last_fast_ref_retry',
        'self._record_encounter(payload)',
        'payload["attempt"] = max(1, int(self.session_seen) + 1)',
    ):
        if token not in static_text:
            errors.append(f"SkyTrip lifecycle integration missing: {token}")
    for token in (
        'default["hardware_validation"] = self.static_profile.choreography_status',
        'self.lifetime["hardware_validation"] = self.static_profile.choreography_status',
    ):
        if token not in static_text:
            errors.append(f"Static hardware-validation synchronization missing: {token}")

    for rel in (
        "pokebot/wild/validated/walk_v0p23/causal_core_v0p14.py",
        "pokebot/wild/validated/acro_v0p27/causal_core_v0p14.py",
    ):
        core_text = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
        for token in (
            'legacy_exact_profile_match',
            '"profile_match": all(authority_checks.values())',
            'vA8/vB4/vB8/vD4 diagnostic-only',
        ):
            if token not in core_text:
                errors.append(f"Dialga state2 profile-policy fix missing in {rel}: {token}")

    if "SOARING_RIFT_MANUAL_MAPPER_HARDWARE_PROVEN_AS_2026_08_29" not in static_profile_text:
        errors.append("Dialga manual Dimensional Rift mapper hardware proof metadata missing")
    if "Manual rift path proven — automation mapping" not in static_profile_text:
        errors.append("Dialga dashboard automation-pending status missing")

    # CL unified ORAS capture/charm and deterministic Wurmple evolution metadata.
    wild_text = (ROOT / "qt_ui/wild_worker.py").read_text(encoding="utf-8", errors="replace")
    odds_text = (ROOT / "pokebot/common/shiny_odds.py").read_text(encoding="utf-8", errors="replace")
    auto_text = (ROOT / "pokebot/wild/auto_capture.py").read_text(encoding="utf-8", errors="replace")
    evo_text = (ROOT / "pokebot/common/evolution_prediction.py").read_text(encoding="utf-8", errors="replace")
    discord_text = (ROOT / "qt_ui/discord_service.py").read_text(encoding="utf-8", errors="replace")
    for token in ("auto_capture_supported_for_game", "oras_auto_capture_game", 'game_key=game_profile["key"]'):
        if token not in wild_text:
            errors.append(f"Omega Ruby Auto-Capture integration missing: {token}")
    if "UNVERIFIED_FOR_OMEGA_RUBY" in wild_text:
        errors.append("Wild worker still disables Omega Ruby Shiny Charm")
    for token in ("ORAS_14_KEY_ITEMS_ADDR", "shared_oras_1.4_key_items", "game_key"):
        if token not in odds_text:
            errors.append(f"shared ORAS Shiny Charm contract missing: {token}")
    if '"omega_ruby"' not in auto_text or "SUPPORTED_ORAS_AUTO_CAPTURE_GAMES" not in auto_text:
        errors.append("shared Auto-Capture dispatcher does not include Omega Ruby")
    for token in ("WURMPLE_SPECIES_ID = 265", "upper16 % 10", '"Silcoon"', '"Cascoon"'):
        if token not in evo_text:
            errors.append(f"Wurmple Gen VI EC prediction contract missing: {token}")
    if 'self._field("Evolution"' not in discord_text or "AUTO_CATCH" not in discord_text:
        errors.append("Discord shiny embed missing evolution/Auto-Capture presentation")

    # CM one-shot ordinary encounter capture test + Party Viewer Wurmple line.
    party_live_text = (ROOT / "pokebot/common/live_party.py").read_text(encoding="utf-8", errors="replace")
    party_monitor_text = (ROOT / "qt_ui/party_monitor.py").read_text(encoding="utf-8", errors="replace")
    pokerus_text = (ROOT / "pokebot/common/pokerus.py").read_text(encoding="utf-8", errors="replace")
    widgets_text = (ROOT / "qt_ui/widgets.py").read_text(encoding="utf-8", errors="replace")
    for token in (
        "Test Auto-Capture on next normal encounter",
        "take_auto_capture_test_snapshot",
        "normal_capture_test_visible = visible and self.selected_wild_method != \"horde\"",
    ):
        if token not in dash_text:
            errors.append(f"Auto-Capture Test dashboard contract missing: {token}")
    if "auto_capture_test_next_encounter" not in main_text:
        errors.append("MainWindow does not pass the one-shot Auto-Capture Test arm")
    for token in (
        "def _run_single_auto_capture_test",
        "AUTO_CAPTURE_TEST_PASS",
        "auto_capture_test_eligible",
        '"test_target_was_shiny": False',
        "and not bool(is_shiny)",
    ):
        source = wild_text if token != "and not bool(is_shiny)" else auto_text
        if token not in source:
            errors.append(f"Auto-Capture Test fail-closed contract missing: {token}")
    if 'target["is_shiny"] = True' in wild_text or "target['is_shiny'] = True" in wild_text:
        errors.append("Auto-Capture Test must never forge the shiny flag")

    horde_live_text = (ROOT / "pokebot/wild/horde_live_capture.py").read_text(encoding="utf-8", errors="replace")
    for token in (
        "Test Auto-Attack on next non-shiny Horde",
        "take_horde_auto_attack_test_snapshot",
        "horde_attack_test_visible",
    ):
        if token not in dash_text:
            errors.append(f"Horde Auto-Attack Test dashboard contract missing: {token}")
    for token in (
        "horde_auto_attack_test_next_encounter",
        "def _run_horde_auto_attack_test",
        "and not shiny_payloads",
        "HORDE_AUTO_ATTACK_TEST_PASS",
    ):
        source = main_text if token == "horde_auto_attack_test_next_encounter" else wild_text
        if token not in source:
            errors.append(f"Horde Auto-Attack Test worker contract missing: {token}")
    for token in (
        "def live_nonshiny_horde_auto_attack_test",
        "D25_PRODUCTION_MOVE_SELECTOR_ONE_ATTACK_NONSHINY_HORDE_TEST",
        "refuses every attack because a real shiny is present",
        "LIVE_AUTO_ATTACK_TEST_MOVE_SLOT_",
        "LIVE_AUTO_ATTACK_TEST_TARGET_",
    ):
        if token not in horde_live_text:
            errors.append(f"Horde Auto-Attack Test live reducer contract missing: {token}")
    for label, text in (("live_party", party_live_text), ("party_monitor", party_monitor_text), ("pokerus", pokerus_text)):
        for token in ("predicted_evolution", "predict_split_evolution"):
            if token not in text:
                errors.append(f"Party Viewer split-evolution telemetry missing in {label}: {token}")
    if 'mon.get("predicted_evolution")' not in widgets_text or "<b>Evolution:</b>" not in widgets_text:
        errors.append("Party card does not visibly surface Wurmple evolution prediction")

    # CN shiny-phase session extrema reset.
    stats_page_text = (ROOT / "qt_ui/stats_page.py").read_text(encoding="utf-8", errors="replace")
    for label, text, tokens in (
        ("dashboard", dash_text, ("def _reset_phase_session_extrema", 'if data.get("is_shiny"):', "self._reset_phase_session_extrema()")),
        ("starter", (ROOT / "qt_ui/backend_worker.py").read_text(encoding="utf-8", errors="replace"), ("def _reset_phase_session_extrema", "self._reset_phase_session_extrema()")),
        ("wild", wild_text, ("def _reset_phase_extrema", "self._reset_phase_extrema()")),
        ("static", (ROOT / "qt_ui/static_worker.py").read_text(encoding="utf-8", errors="replace"), ("def _reset_phase_extrema", "self._reset_phase_extrema()")),
    ):
        for token in tokens:
            if token not in text:
                errors.append(f"CN shiny phase extrema reset missing in {label}: {token}")
    if 'fmt_extreme = lambda value: "—" if value is None else str(value)' not in stats_page_text:
        errors.append("CN Stats page does not render cleared extrema as em dash")


    # CO Omega Ruby delayed Bag-construction transition handling.
    main_window_text = (ROOT / "qt_ui/main_window.py").read_text(encoding="utf-8", errors="replace")
    for token in (
        "def _request_auxiliary_shutdown",
        "def _finish_deferred_close_when_idle",
        "MAIN_WINDOW_CLOSE_DEFERRED",
        "QTimer.singleShot(0, self.close)",
        "event.ignore()",
    ):
        if token not in main_window_text:
            errors.append(f"DD deferred QThread shutdown contract missing: {token}")
    if "thread.wait(5_000)" in main_window_text:
        errors.append("DD stale synchronous 5-second QThread shutdown wait remains")

    bag_throw_text = (ROOT / "pokebot/wild/battle_bag_throw.py").read_text(encoding="utf-8", errors="replace")
    for token in (
        "MENU_TOUCH_TRANSITION_WAIT_SECONDS = 6.00",
        "MENU_TOUCH_TRANSITION_STABLE_SAMPLES = 2",
        "transition_seen = False",
        "OPEN_BAG touch was RAM-confirmed consumed",
        "no touch replayed",
    ):
        if token not in bag_throw_text:
            errors.append(f"CO Bag transition Auto-Capture fix missing: {token}")

    payload = {
        "status": "PASS" if not errors else "FAIL",
        "root": str(ROOT),
        "python_files": len(py_files),
        "pokebot_import_failures": import_failures,
        "errors": errors,
        "required_files": REQUIRED,
    }
    print(json.dumps(payload, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
