
from __future__ import annotations

import json
import struct
import threading
import time
import traceback
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from .appdata_store import (
    get_profile_paths,
    increment_species_shiny_total,
    blocked_shiny_species,
)
from pokebot.wild.registry import WILD_METHODS, AXES, game_from_probe
from pokebot.wild.validated_loader import load_walk_v0p23, load_acro_v0p27
from .wild_movement import movement_until_encounter_axis
from .cave_movement import (
    cave_until_encounter, cave_run_until_encounter, cave_bunny_until_encounter,
    require_cave_position, read_stable_cave_position, wait_for_cave_field
)
from .water_movement import surf_until_encounter, require_water_position, read_stable_water_position, wait_for_surf_field
from pokebot.common.species_names import SPECIES_NAMES
from pokebot.common.shiny_odds import (
    detect_oras_shiny_charm, resolve_shiny_odds, advance_phase_log_miss,
    cumulative_probability_from_log_miss, phase_log_miss_for_constant,
)
from pokebot.common.evolution_prediction import predict_split_evolution
from pokebot.common.target_filter import normalize_target, evaluate_target, hidden_power_from_ivs
from pokebot.wild.world_authority import install_world_authority, WorldMap, DynamicTerrain, nearest_grid
from pokebot.wild.horde_authority import read_opponent_set
from pokebot.common.oras_ram import PARTY0, PARTY_STRIDE, PARTY_SLOTS, PK6_SIZE
from pokebot.common.pk6 import parse_pk6
from pokebot.common.ability_names import ability_name
from pokebot.common.framebuffer import capture_top_screen
from pokebot.common.pokerus import (
    read_party_pokerus_snapshot,
    compare_pokerus_snapshots,
    party_ui_payload,
)
from .party_monitor import probe_battle_party_payload
from pokebot.common.live_party import get_runtime_party_snapshot
from pokebot.common.acknowledged_input import AcknowledgedInput
from pokebot.common.gen6_cro import locate_loaded_modules
from pokebot.common.xy_ram import read_trainer_ids as read_xy_trainer_ids, read_wild_decoded as read_xy_wild_decoded
from pokebot.wild.auto_capture import (
    run_shiny_auto_capture,
    clear_captured_shiny_post_capture,
    auto_capture_supported_for_game,
    auto_capture_test_eligible,
)
from pokebot.wild.best_ball import normalize_ball_override
from pokebot.wild.oras_touch_profile import (
    get_party_field_action_xy, get_battle_move_xy, get_save_menu_xy,
)
from pokebot.wild.oras_move_policy import ensure_move_metadata, choose_safe_attack
from pokebot.wild.horde_live_capture import live_nonshiny_horde_auto_attack_test
from pokebot.wild.fishing_chain import ConsecutiveFishingTracker, shiny_odds_for_chain
from pokebot.wild.honey_horde import (
    HONEY_ITEM_ID, require_honey, read_items_pocket, locate_items_controller,
    read_items_controller_state, honey_active_index, cursor_index_models,
)


NATURE_NAMES = (
    "Hardy", "Lonely", "Brave", "Adamant", "Naughty",
    "Bold", "Docile", "Relaxed", "Impish", "Lax",
    "Timid", "Hasty", "Serious", "Jolly", "Naive",
    "Modest", "Mild", "Quiet", "Bashful", "Rash",
    "Calm", "Gentle", "Sassy", "Careful", "Quirky",
)


# ---------------------------------------------------------------------------
# Fishing authority frozen from standalone hardware proof v0p11.
# Alpha Sapphire 1.4 only until Omega Ruby receives the same RAM proof.
# ---------------------------------------------------------------------------
FISH_CONTROL_ADDR = 0x08C6E718
FISH_CONTROL_LEN = 0x20
FISH_ACTION_PTR_ADDR = 0x08C6E720
FISH_ACTION_ACTIVE_ADDR = 0x08C6E72C

FISH_FOCUS_ADDR = 0x08803C28
FISH_FOCUS_LEN = 0xC0
FISH_STATE_OFF = 0x00
FISH_WAIT_COUNTDOWN_OFF = 0xB8
FISH_BITE_COUNTDOWN_OFF = 0xBC

FISH_WAIT_PTR = 0x08803C10
FISH_MISS_PTR = 0x08806DC0
FISH_MESSAGE_PTR = 0x08804814
FISH_HOOK_PTR = 0x08803D2C
FISH_BATTLE_PTR = 0x08803D80
FISH_BUSY_PTRS = {
    FISH_WAIT_PTR, FISH_MISS_PTR, FISH_MESSAGE_PTR,
    FISH_HOOK_PTR, FISH_BATTLE_PTR,
}

FISH_STATE_NAMES = {
    3: "CAST",
    4: "WAIT",
    5: "BITE_NOW",
    6: "TOO_LATE_MISSED",
    9: "POST_RESULT",
    10: "NO_BITE",
    11: "RESULT_DONE",
}

FISH_CAST_HOLD_MS = 90
FISH_CAST_SETTLE_MS = 80
FISH_REEL_A1_HOLD_MS = 30
FISH_REEL_A1_SETTLE_MS = 10
FISH_REEL_A2_HOLD_MS = 25
FISH_REEL_A2_SETTLE_MS = 10
FISH_REEL_SECOND_EDGE_AFTER_MS = 35.0
FISH_MESSAGE_READY_SETTLE_S = 0.600
FISH_MESSAGE_RETRY_OBSERVE_S = 0.700

SAVE_MENU_TOUCH_HOLD_MS = 90
SAVE_MENU_TOUCH_SETTLE_MS = 350
SAVE_MENU_A_HOLD_MS = 90
SAVE_MENU_A_SETTLE_MS = 900

# v0p43EH: rare long-run Wild sessions on Route 102 showed isolated UDP READ
# timeouts and one delayed overworld restore after Run. Recovery stays bounded
# and read-only; no gameplay input is sent while bridge/field authority is being
# re-established.
# A brief Wi-Fi interruption can outlast the original two probes, especially
# when the 3DS access point is roaming or waking from power-save. Keep recovery
# bounded, but cover a short reconnect window before declaring a hold.
WILD_READ_RECOVERY_ROUNDS = 5
WILD_READ_RECOVERY_DELAYS_S = (0.15, 0.40, 0.80, 1.20, 1.80)
WILD_POST_ESCAPE_FIELD_TIMEOUT_S = 8.0

# Pokémon Y live field/world authority hardware-proven by HF60.
XY_FIELD_X_ADDR = 0x08C670BC
XY_FIELD_Z_ADDR = 0x08C670C4
XY_FIELD_ZONE_ADDR = 0x08C67190
XY_FIELD_TILE_UNITS = 18.0


class UserStop(RuntimeError):
    pass


class _OmegaRubyWildIdentityBridge:
    """Identity-only adapter for the frozen AS wild backends.

    OR S0 hardware proved the same battle/wild/field RAM addresses and the OR
    world archives match the shared topology. The validated v0p23/v0p27
    sources remain byte-identical; only their hard-coded AS title-ID check is
    satisfied through this narrow wrapper. All reads/inputs still hit the real
    Omega Ruby process.
    """
    def __init__(self, bridge, as_title_id):
        self._bridge = bridge
        self._as_title_id = int(as_title_id)

    def __getattr__(self, name):
        return getattr(self._bridge, name)

    def game_info(self):
        gi = dict(self._bridge.game_info())
        gi["title_id"] = self._as_title_id
        gi["title_id_hex"] = f"0x{self._as_title_id:016X}"
        if "process" in gi:
            gi["process"] = "sango-2"
        if "process_name" in gi:
            gi["process_name"] = "sango-2"
        return gi


def _atomic_write(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _cave_zone_ids_for_location(encounter_data, game_key: str, location_name: str):
    """Return zones which the packaged encounter data explicitly marks Cave.

    This is encounter-table authority, not the outdoor grass-mask database.
    A location can contain mixed sections (for example Victory Road also has
    Surf/fishing data), so the live zone must occur in the section titled
    ``Cave`` rather than relying on the location name alone.
    """
    zones = set()
    try:
        locations = encounter_data["games"][str(game_key)]["locations"]
    except Exception:
        return zones
    wanted = str(location_name or "").casefold()
    for loc in locations:
        if str(loc.get("name") or "").casefold() != wanted:
            continue
        for section in loc.get("sections") or []:
            if str(section.get("title") or "").casefold() != "cave":
                continue
            for mon in section.get("pokemon") or []:
                for zone in mon.get("zone_ids") or []:
                    try:
                        zones.add(int(zone))
                    except Exception:
                        pass
    return zones


def _promote_land_method_to_cave(method_key: str):
    return {
        "walk": "cave",
        "run": "cave_run",
        "acro_bunny": "cave_bunny",
    }.get(str(method_key), str(method_key))


class WildHuntWorker(QObject):
    """
    Qt adapter around the hardware-validated ORAS wild backends.

    The source modules in pokebot/wild/validated are copied byte-for-byte from:
      - W6 Unlimited v0p23 (Run / Walk)
      - Acro Bunny Latch v0p27

    This worker owns UI signalling, Stop handling and persistent statistics.
    It does not rewrite either validated movement/encounter state machine.
    """

    log_line = Signal(str)
    status = Signal(str, str)
    connection = Signal(dict)
    encounter = Signal(dict)
    last_seen_entry = Signal(dict)
    party = Signal(list)
    stats = Signal(dict)
    recent_shinies = Signal(list)
    ram_reads = Signal(int)
    support_ready = Signal(str)
    shiny_charm = Signal(dict)
    pokerus_detected = Signal(dict)
    session_finished = Signal(str)

    def __init__(
        self,
        *,
        method_key,
        movement_axis="vertical",
        host,
        base_dir,
        timeout=1.0,
        auto_support_zip=True,
        auto_throw_one_poke_ball_on_shiny=False,
        capture_ball_override="best",
        auto_capture_test_next_encounter=False,
        horde_auto_attack_test_next_encounter=False,
        target_selection=None,
        target_criteria=None,
        horde_trigger="auto",
    ):
        super().__init__()
        if method_key not in WILD_METHODS:
            raise ValueError(f"Unknown wild method: {method_key}")

        self.method_key = method_key
        self.method_meta = WILD_METHODS[method_key]
        self.movement_axis = (
            str(movement_axis) if method_key in ("walk", "run", "cave", "cave_run", "surf") else "stationary"
        )
        if method_key in ("walk", "run", "cave", "cave_run", "surf"):
            if self.movement_axis not in AXES:
                raise ValueError(f"Unknown wild movement axis/direction: {self.movement_axis}")
            allowed_axes = tuple(self.method_meta.get("axes", ()))
            if allowed_axes and self.movement_axis not in allowed_axes:
                raise ValueError(
                    f"Movement selector {self.movement_axis!r} is not valid for {method_key}; "
                    f"allowed={allowed_axes}"
                )
        self.host = str(host)
        self.base_dir = Path(base_dir)
        self.timeout = float(timeout)
        self.auto_support_zip = bool(auto_support_zip)
        self.auto_throw_one_poke_ball_on_shiny = bool(
            auto_throw_one_poke_ball_on_shiny
        )
        self.capture_ball_override = normalize_ball_override(capture_ball_override)
        self.auto_capture_test_pending = bool(auto_capture_test_next_encounter)
        self.auto_capture_test_initially_armed = bool(auto_capture_test_next_encounter)
        self.last_auto_capture_test = None
        self.horde_auto_attack_test_pending = bool(horde_auto_attack_test_next_encounter)
        self.horde_auto_attack_test_initially_armed = bool(horde_auto_attack_test_next_encounter)
        self.last_horde_auto_attack_test = None
        self.target_selection = dict(target_selection or {})
        self.target_criteria = normalize_target(target_criteria)
        trigger = str(horde_trigger or "auto").strip().lower()
        self.horde_trigger = trigger if trigger in {"auto", "sweet_scent", "honey"} else "auto"
        self.horde_trigger_resolved = None
        self.target_species = int(self.target_selection.get("species", 0) or 0)
        self.target_species_name = str(self.target_selection.get("species_name") or "")
        self.target_location_name = str(self.target_selection.get("location_name") or "")

        self.stop_event = threading.Event()
        self.bridge = None
        self.read_count = 0
        self.started_mono = None
        self.started_iso = None
        self.attempt = 0
        self.previous_identity = None
        self.previous_opponent_identities = set()
        self.previous_direction = None
        self.global_cycle = 0
        self.species_counts = Counter()
        self.run_attempt_counts = Counter()
        self.touch_timeout_recoveries = 0
        self.horde_battles = 0
        self.pokemon_seen_session = 0

        # HF95 encounter timing telemetry. These counters are observation-only:
        # they never authorize input and do not alter movement/battle timing.
        # "field" starts immediately before the movement-until-encounter routine;
        # the boundary is recorded as soon as that routine proves an encounter.
        self.field_to_encounter_total_s = 0.0
        self.field_to_encounter_count = 0
        self.field_to_encounter_last_s = None
        self.battle_to_field_total_s = 0.0
        self.battle_to_field_count = 0
        self.battle_to_field_last_s = None
        self.measured_cycle_total_s = 0.0
        self.measured_cycle_count = 0
        self.measured_cycle_last_s = None

        self.last_opponent_set = None
        self.last_encounter = None
        self.last_shiny_auto_throw = None
        self.events = []
        self.location_name = "Unknown Location"
        self.location_zone = None
        self.location_parent_map = None
        self.location_matrix = None
        self.world_terrain = None
        self.game_profile = None
        self.shiny_charm_state = {"detected": None, "status": "UNVERIFIED"}
        self.cave_run_corridor = None
        self.pokerus_baseline = None
        # Dashboard-only live party order captured from the hardware-proven
        # active-battle pointer table. This never authorizes hunt input.
        self.party_live_identity_order = None
        # D19m Horde preflight captures the exact visible party permutation,
        # Sweet Scent holder, and lead PK6 before automation starts. The hunt
        # owns input after this point, so the player cannot reorder the party
        # without stopping the hunt.
        self.sweet_scent_visible_slot = None
        self.sweet_scent_user = None
        self.horde_lead_pk6 = None
        self.horde_move_db = {}
        self.honey_inventory = None
        self.honey_controller = None
        # Pure accounting only: controller/RAM authority remains in the frozen
        # fishing routines below. This tracker formalises chain breaks/odds and
        # never sends input.
        self.fishing_chain = ConsecutiveFishingTracker() if method_key == "fishing" else None

        self.runtime = self.base_dir / "runtime"
        profile = get_profile_paths()
        self.profile = profile
        self.log_dir = profile.root / "logs"
        self.support_dir = profile.root / "support"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.support_dir.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_path = self.log_dir / f"Pokebot3DS-CFW_wild_{method_key}_{stamp}.log"
        self.session_path = self.log_dir / f"Pokebot3DS-CFW_wild_{method_key}_session_{stamp}.json"

        profile.stats_dir.mkdir(parents=True, exist_ok=True)
        profile.history_dir.mkdir(parents=True, exist_ok=True)
        profile.encounters_dir.mkdir(parents=True, exist_ok=True)

        self.stats_storage_key = (
            "cave" if method_key in {"cave", "cave_run", "cave_bunny"} else method_key
        )
        self.stats_path = profile.stats_dir / f"wild_{self.stats_storage_key}.json"
        self.encounter_path = profile.encounters_dir / f"wild_{self.stats_storage_key}.jsonl"
        self.recent_shiny_path = profile.recent_shinies_path
        self.last_seen_path = profile.last_seen_path
        self.pokerus_history_path = profile.pokerus_history_path
        self.lifetime = self._load_stats()
        self._fishing_lifetime_base = {
            "breaks": int(self.lifetime.get("fishing_chain_breaks", 0)),
            "hooked": int(self.lifetime.get("fishing_hooked", 0)),
            "no_bites": int(self.lifetime.get("fishing_no_bites", 0)),
            "missed_hooks": int(self.lifetime.get("fishing_missed_hooks", 0)),
        }

    def _refresh_live_party_order_from_save(self, br):
        """Refresh dashboard through D19m's exact visible-order cache.

        The legacy helper name is retained to avoid touching call sites, but
        this no longer emits the known-stale PokePartySave order.
        """
        try:
            snap = get_runtime_party_snapshot(
                self.host, int(getattr(br, "port", 4952)), br
            )
        except Exception as exc:
            self._log(
                "PARTY LIVE REFRESH FAILED: "
                f"{type(exc).__name__}: {exc} • telemetry only"
            )
            return None
        payload = list(snap.get("payload") or [])
        if not payload:
            return None
        self.party_live_identity_order = [
            {"ec": ec, "pid": pid, "species": species}
            for ec, pid, species in (snap.get("identity_order") or ())
        ]
        self.party.emit(payload)
        self._log(
            "PARTY LIVE ORDER D25: "
            f"source={snap.get('source')} species="
            f"{[row.get('species_id') for row in payload if row.get('species_id')]}"
        )
        return snap


    def _pre_capture_party_count_authority(self, br):
        """Return proven live party count before a successful capture, if available.

        This is corroborating safety authority only.  A missing runtime-party
        mapping never blocks Auto-Capture; post-catch flow remains authoritative.
        Counts 1..5 explicitly mean a direct party return, while 6 means Box.
        """
        try:
            snap = get_runtime_party_snapshot(
                self.host, int(getattr(br, "port", 4952)), br
            )
            live = dict(snap.get("live_source") or {})
            count = int(live.get("count") or 0)
            if bool(snap.get("reorder_proven")) and 1 <= count <= 6:
                free_slots = 6 - count
                expected = "DIRECT_PARTY_RETURN" if count < 6 else "BOX_MESSAGE"
                self._log(
                    "AUTO-CAPTURE PRE-CATCH PARTY AUTHORITY: "
                    f"count={count} free_slots={free_slots} expected={expected} "
                    f"source={snap.get('source')}"
                )
                return count
            self._log(
                "AUTO-CAPTURE PRE-CATCH PARTY AUTHORITY unavailable; "
                "post-capture flow remains final authority"
            )
        except Exception as exc:
            self._log(
                "AUTO-CAPTURE PRE-CATCH PARTY AUTHORITY read failed: "
                f"{type(exc).__name__}: {exc}; flow-only recovery retained"
            )
        return None

    def _refresh_live_party_order_from_battle(self, br):
        """Refresh dashboard party order from ORAS' active battle pointer table.

        Telemetry only. Failure is logged and ignored; it can never HOLD, Run,
        reset, or otherwise change hunt authority.
        """
        try:
            live = probe_battle_party_payload(br)
        except Exception as exc:
            self._log(
                "PARTY LIVE ORDER READ FAILED: "
                f"{type(exc).__name__}: {exc} • telemetry only"
            )
            return None
        if not live:
            return None
        self.party_live_identity_order = list(live.get("identity_order") or [])
        # D25 no longer persists battle-learned party permutations.  The field
        # runtime party is the only membership/order authority.  For Horde,
        # refresh move/PP telemetry from that same source when available.
        try:
            if self.method_key == "horde":
                snap = get_runtime_party_snapshot(
                    self.host, int(getattr(br, "port", 4952)), br
                )
                ordered = list(snap.get("ordered_parsed") or [])
                if ordered and snap.get("reorder_proven"):
                    self.horde_lead_pk6 = dict(ordered[0])
                    self._log(
                        "HORDE LEAD RAM REFRESH: "
                        f"{self.horde_lead_pk6.get('species_name')} moves="
                        f"{self.horde_lead_pk6.get('moves')} PP="
                        f"{self.horde_lead_pk6.get('move_pp')} "
                        f"source={snap.get('source')}"
                    )
        except Exception as exc:
            self._log(
                "PARTY D25 RUNTIME REFRESH FAILED: "
                f"{type(exc).__name__}: {exc} • telemetry only"
            )
        self.party.emit(list(live.get("payload") or []))
        self._log(
            "PARTY LIVE ORDER: battle pointer table -> "
            f"species {list(live.get('species') or [])} "
            f"order {list(live.get('order_indices') or [])}"
        )
        return live

    def _party_payload_preserving_live_order(self, snapshot):
        """Render a fixed-block snapshot in the last hardware-proven live order.

        The cached order is applied only if the exact PID+species identity set
        is unchanged. A party composition change invalidates the cache and falls
        back to the freshly read snapshot rather than guessing.
        """
        fallback = party_ui_payload(snapshot)
        order = list(self.party_live_identity_order or [])
        if not order:
            return fallback

        valid = [
            rec for rec in ((snapshot or {}).get("slots") or [])
            if rec.get("valid")
        ]
        current_keys = [
            (str(rec.get("pid") or ""), int(rec.get("species_id", 0) or 0))
            for rec in valid
        ]
        wanted_keys = [
            (f"0x{int(item.get('pid', 0)):08X}", int(item.get("species", 0)))
            for item in order
        ]
        if (
            len(current_keys) != len(wanted_keys)
            or len(set(current_keys)) != len(current_keys)
            or set(current_keys) != set(wanted_keys)
        ):
            self.party_live_identity_order = None
            return fallback

        by_key = {
            (str(rec.get("pid") or ""), int(rec.get("species_id", 0) or 0)): rec
            for rec in valid
        }
        reordered = []
        for slot_num, key in enumerate(wanted_keys, start=1):
            rec = dict(by_key[key])
            rec["slot"] = slot_num
            reordered.append(rec)
        while len(reordered) < 6:
            reordered.append({
                "slot": len(reordered) + 1,
                "valid": False,
                "species_id": 0,
                "species_name": "Empty",
                "pid": "",
                "nature": "—",
                "gender": "—",
                "shiny_xor": 0,
                "is_shiny": False,
                "ivs": {},
                "evs": {},
                "pokerus_state": 0,
                "pokerus_days": 0,
                "pokerus_strain": 0,
                "pokerus_status": "—",
            })
        ordered_snapshot = dict(snapshot or {})
        ordered_snapshot["slots"] = reordered
        return party_ui_payload(ordered_snapshot)

    def _initialise_pokerus_baseline(self, br):
        """One hunt-start baseline. Failure disables detection until re-baselined."""
        try:
            snap = read_party_pokerus_snapshot(br)
            self.pokerus_baseline = snap
            self.party.emit(party_ui_payload(snap))
            # Replace the fixed physical order immediately when the runtime
            # PokePartySave block is available. This makes an already-reordered
            # party visible at hunt start without waiting for the first battle.
            self._refresh_live_party_order_from_save(br)
            active = list(snap.get("active_contagious_slots") or [])
            self._log(
                "POKERUS BASELINE: "
                + (
                    f"active contagious slot(s) {active}"
                    if active else "no active contagious party Pokérus"
                )
            )
        except Exception as exc:
            self.pokerus_baseline = None
            self._log(
                "POKERUS BASELINE UNAVAILABLE: "
                f"{type(exc).__name__}: {exc} • telemetry only; hunt continues"
            )

    def _persist_pokerus_event(self, event):
        try:
            self.pokerus_history_path.parent.mkdir(parents=True, exist_ok=True)
            with self.pokerus_history_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, separators=(",", ":")) + "\n")
        except Exception as exc:
            self._log(
                "POKERUS HISTORY WRITE FAILED: "
                f"{type(exc).__name__}: {exc} • telemetry only"
            )

    def _post_battle_pokerus_check(self, br):
        """Exactly one six-slot party snapshot after a completed battle.

        No exception from this telemetry path is allowed to HOLD, Run, reset or
        otherwise alter the hunt. If a read fails the baseline is discarded so
        the next successful read becomes a fresh baseline instead of guessing.
        """
        try:
            current = read_party_pokerus_snapshot(br)
        except Exception as exc:
            self.pokerus_baseline = None
            self._log(
                "POKERUS POST-BATTLE READ FAILED: "
                f"{type(exc).__name__}: {exc} • detector will re-baseline; hunt continues"
            )
            return None

        # Keep the dashboard party panel fresh. First try the direct runtime
        # PokePartySave block, which follows overworld party swaps in slot order.
        # If it is temporarily unavailable, preserve the last battle-proven
        # order over the existing fixed-block details instead of guessing.
        direct_party = self._refresh_live_party_order_from_save(br)
        if direct_party is None:
            self.party.emit(self._party_payload_preserving_live_order(current))

        if self.pokerus_baseline is None:
            self.pokerus_baseline = current
            self._log(
                "POKERUS DETECTOR RE-BASELINED after a previous telemetry read failure"
            )
            return None

        event = compare_pokerus_snapshots(self.pokerus_baseline, current)
        self.pokerus_baseline = current
        if not event:
            return None

        event.update({
            "battle_index": int(self.attempt),
            "game": (self.game_profile or {}).get("name", "ORAS"),
            "location_name": self.location_name,
            "method": self.method_meta.get("name", self.method_key),
            "hunt_type": "Wild",
            "ram_authority": "POST_BATTLE_PARTY_PK6",
        })

        infections = list(event.get("new_infections") or [])
        self.lifetime["lifetime_pokerus_events"] = int(
            self.lifetime.get("lifetime_pokerus_events", 0)
        ) + 1
        self.lifetime["lifetime_pokerus_new_slots"] = int(
            self.lifetime.get("lifetime_pokerus_new_slots", 0)
        ) + len(infections)
        self._save_stats()
        self._persist_pokerus_event(event)

        details = ", ".join(
            f"slot {rec['slot']} {rec['species_name']} "
            f"strain {rec['strain']} days {rec['days']}"
            for rec in infections
        )
        self._log(
            "🦠 POKERUS DETECTED: "
            f"{details} • source={event.get('source')} • "
            "post-battle telemetry only"
        )
        self.events.append({
            "time": event.get("time"),
            "type": "POKERUS_DETECTED",
            "battle_index": int(self.attempt),
            "source": event.get("source"),
            "new_infections": infections,
        })
        self.pokerus_detected.emit(dict(event))
        self.stats.emit(self._stats_payload())
        return event

    def _promote_runtime_to_cave(self, reason: str):
        """Promote ordinary land movement to its Cave counterpart before input.

        The worker is constructed before live zone authority exists, so the UI
        may still have supplied Walk/Run/Bunny.  Once Cave encounter authority
        is proven, switch the runtime method and stats bucket before the first
        encounter or movement command.
        """
        old_method = self.method_key
        new_method = _promote_land_method_to_cave(old_method)
        if new_method == old_method:
            return False
        self.method_key = new_method
        self.method_meta = WILD_METHODS[new_method]
        self.stats_storage_key = "cave"
        self.stats_path = self.profile.stats_dir / "wild_cave.json"
        self.encounter_path = self.profile.encounters_dir / "wild_cave.jsonl"
        self.lifetime = self._load_stats()
        self.current_world_terrain_name = "Cave"
        self._log(
            f"CAVE AUTO-MODE: {reason}; promoted {old_method} -> {new_method}. "
            "Cave stats/history authority selected before movement."
        )
        return True

    def _load_backend(self):
        # Horde mode is stationary, but it still reuses the validated Wild
        # v0p23 battle/state/Run backend. It must be accepted here before
        # Horde-specific stationary preflight and Sweet Scent handling run.
        if self.method_key in ("walk", "run", "horde", "cave", "cave_run", "surf", "fishing"):
            return load_walk_v0p23()
        if self.method_key in ("acro_bunny", "cave_bunny"):
            return load_acro_v0p27()
        raise ValueError(self.method_key)

    def _load_stats(self):
        default = {
            "hunt_type": "Wild",
            "method": self.method_key,
            "movement_axis": self.movement_axis,
            "lifetime_seen": 0,
            "lifetime_shinies": 0,
            "phase_seen": 0,
            "last_phase_seen": 0,
            "phase_log_miss": None,
            "phase_cumulative_probability": 0.0,
            "last_phase_cumulative_probability": None,
            "last_shiny": None,
            "highest_sv": None,
            "lowest_sv": None,
            "highest_iv_sum": None,
            "lowest_iv_sum": None,
            "lifetime_hunt_seconds": 0.0,
            "lifetime_fastest_rate": None,
            "lifetime_pokerus_events": 0,
            "lifetime_pokerus_new_slots": 0,
            "fishing_peak_chain": 0,
            "fishing_chain_breaks": 0,
            "fishing_hooked": 0,
            "fishing_no_bites": 0,
            "fishing_missed_hooks": 0,
        }
        data = _load_json(self.stats_path, default)
        if not isinstance(data, dict):
            data = default
        for k, v in default.items():
            data.setdefault(k, v)
        return data

    def _initialize_phase_probability(self):
        """Migrate an existing phase to cumulative probability telemetry.

        Fishing reconstructs its current phase from the persistent encounter
        ledger when possible because each hook can have different chain odds.
        Other Wild methods use the RAM-confirmed Charm state for the existing
        phase on first migration.
        """
        if self.lifetime.get("phase_log_miss") is not None:
            self.lifetime["phase_cumulative_probability"] = cumulative_probability_from_log_miss(
                self.lifetime.get("phase_log_miss", 0.0)
            )
            return
        seen = int(self.lifetime.get("phase_seen", 0) or 0)
        if seen <= 0:
            self.lifetime["phase_log_miss"] = 0.0
            self.lifetime["phase_cumulative_probability"] = 0.0
            return
        detected = (self.shiny_charm_state or {}).get("detected")
        if detected is None:
            self.lifetime["phase_log_miss"] = None
            self.lifetime["phase_cumulative_probability"] = None
            return
        charm = detected is True
        if self.method_key == "fishing":
            records = []
            try:
                if self.encounter_path.exists():
                    for line in self.encounter_path.read_text(encoding="utf-8").splitlines():
                        if not line.strip():
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        if not isinstance(rec, dict):
                            continue
                        if bool(rec.get("is_shiny", rec.get("shiny", False))):
                            records = []
                            continue
                        records.append(rec)
            except Exception:
                records = []
            records = records[-seen:]
            log_miss = 0.0
            for rec in records:
                chain_snap = rec.get("fishing_chain") or {}
                try:
                    post_chain = int(chain_snap.get("chain", 1) or 1)
                except Exception:
                    post_chain = 1
                rec_charm = bool(chain_snap.get("shiny_charm", charm))
                odds = shiny_odds_for_chain(max(0, post_chain - 1), rec_charm)
                log_miss = advance_phase_log_miss(log_miss, float(odds.get("probability", 0.0)))
            missing = max(0, seen - len(records))
            if missing:
                base = resolve_shiny_odds(
                    game=(self.game_profile or {}).get("name", "ORAS"),
                    hunt_type="Wild", shiny_charm_present=charm,
                    shiny_charm_applies=True,
                )
                log_miss += phase_log_miss_for_constant(missing, base.probability)
        else:
            base = resolve_shiny_odds(
                game=(self.game_profile or {}).get("name", "ORAS"),
                hunt_type="Wild", shiny_charm_present=charm,
                shiny_charm_applies=True,
            )
            log_miss = phase_log_miss_for_constant(seen, base.probability)
        self.lifetime["phase_log_miss"] = log_miss
        self.lifetime["phase_cumulative_probability"] = cumulative_probability_from_log_miss(log_miss)

    def _save_stats(self):
        _atomic_write(self.stats_path, self.lifetime)

    def _sync_fishing_chain_stats(self):
        tracker = self.fishing_chain
        if tracker is None:
            return None
        snap = tracker.snapshot()
        self.lifetime["fishing_peak_chain"] = max(
            int(self.lifetime.get("fishing_peak_chain", 0)),
            int(snap.get("peak_chain", 0)),
        )
        base = getattr(self, "_fishing_lifetime_base", {})
        self.lifetime["fishing_chain_breaks"] = int(base.get("breaks", 0)) + int(snap.get("breaks", 0))
        self.lifetime["fishing_hooked"] = int(base.get("hooked", 0)) + int(snap.get("hooked", 0))
        self.lifetime["fishing_no_bites"] = int(base.get("no_bites", 0)) + int(snap.get("no_bites", 0))
        self.lifetime["fishing_missed_hooks"] = int(base.get("missed_hooks", 0)) + int(snap.get("missed_hooks", 0))
        self.lifetime["fishing_current_chain"] = int(snap.get("chain", 0))
        self.lifetime["fishing_session_peak_chain"] = int(snap.get("peak_chain", 0))
        self.lifetime["fishing_next_shiny_rolls"] = int((snap.get("next_odds") or {}).get("rolls", 1))
        self._save_stats()
        return snap

    def _load_last_seen(self):
        if not self.last_seen_path.exists():
            return []
        try:
            data = json.loads(
                self.last_seen_path.read_text(encoding="utf-8")
            )
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _save_last_seen_entry(self, item):
        """Persist a Wild encounter into the shared ORAS Last Seen ledger.

        This is intentionally done immediately after the authoritative PK6 read,
        before post-battle escape/stat commit. Therefore a user Stop after seeing
        the Pokémon cannot make that RAM-read encounter disappear on restart.
        """
        history = self._load_last_seen()
        history.insert(0, dict(item))
        history = history[:7]

        tmp = self.last_seen_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(history, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.last_seen_path)

    def _log(self, message):
        line = str(message)
        try:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    datetime.now().astimezone().isoformat(timespec="milliseconds")
                    + " "
                    + line
                    + "\n"
                )
        except Exception:
            pass
        self.log_line.emit(line)

    def request_stop(self):
        self.stop_event.set()
        threading.Thread(
            target=self._release_now,
            name="Pokebot3DS-CFW-Wild-ReleaseAll",
            daemon=True,
        ).start()

    def _release_now(self):
        br = self.bridge
        if br is None:
            return
        try:
            br.release_all()
        except Exception:
            pass

    def _check_stop(self):
        if self.stop_event.is_set():
            raise UserStop("User requested Stop")

    def _make_stop_aware_bridge(self, core):
        worker = self

        class StopAwareBridge(core.Bridge):
            @staticmethod
            def _manual_stop_if_requested(exc=None):
                if worker.stop_event.is_set():
                    raise UserStop(
                        "MANUAL_STOP — Stop was requested during an in-flight "
                        "bounded RAM/controller operation"
                    ) from exc

            def read(self, address, length, retries=2):
                worker._check_stop()
                try:
                    raw = super().read(address, length, retries=retries)
                except Exception as exc:
                    self._manual_stop_if_requested(exc)
                    # Read-only recovery only. Three Route 102 long-run holds
                    # were identical exhausted CMD_READ timeouts after hundreds
                    # of otherwise clean encounters. A timed-out READ is safe to
                    # repeat, unlike gameplay input. Re-prove bridge/process
                    # identity before retrying the requested memory read.
                    if not (
                        isinstance(exc, core.BridgeError)
                        and "UDP timeout command=4" in str(exc)
                    ):
                        raise

                    worker._log(
                        "BRIDGE READ RECOVERY: exhausted CMD_READ timeout; "
                        "starting bounded read-only bridge revalidation"
                    )
                    last_exc = exc
                    expected = getattr(worker, "_wild_bridge_identity", None)
                    try:
                        super().release_all()
                    except Exception as release_exc:
                        worker._log(
                            "BRIDGE READ RECOVERY: release probe unavailable "
                            f"({type(release_exc).__name__}: {release_exc})"
                        )

                    for recovery_round, delay_s in enumerate(
                        WILD_READ_RECOVERY_DELAYS_S[:WILD_READ_RECOVERY_ROUNDS],
                        start=1,
                    ):
                        worker._check_stop()
                        time.sleep(float(delay_s))
                        try:
                            # PING + GAME_INFO prove that the same game process
                            # still owns the bridge before the timed-out READ is
                            # repeated. These are observational commands only.
                            super().ping()
                            gi = super().game_info()
                            if expected is not None:
                                actual = {
                                    "title_id": int(gi.get("title_id", 0)),
                                    "pid": int(gi.get("pid", 0)),
                                    "process": str(gi.get("process") or ""),
                                }
                                if actual != expected:
                                    raise core.BridgeError(
                                        "bridge recovery process identity changed: "
                                        f"expected={expected} actual={actual}"
                                    )

                            raw = super().read(address, length, retries=1)
                            worker._log(
                                "BRIDGE READ RECOVERY: PASS on round "
                                f"{recovery_round} for 0x{int(address):08X}+0x{int(length):X}"
                            )
                            worker.read_count += 1
                            worker.ram_reads.emit(worker.read_count)
                            self._manual_stop_if_requested()
                            return raw
                        except Exception as recovery_exc:
                            self._manual_stop_if_requested(recovery_exc)
                            last_exc = recovery_exc
                            if (
                                isinstance(recovery_exc, core.BridgeError)
                                and "process identity changed" in str(recovery_exc)
                            ):
                                worker._log(
                                    "BRIDGE READ RECOVERY: HOLD — game process identity changed"
                                )
                                raise
                            worker._log(
                                "BRIDGE READ RECOVERY: round "
                                f"{recovery_round} did not recover ({recovery_exc})"
                            )

                    worker._log(
                        "BRIDGE READ RECOVERY: exhausted bounded recovery; "
                        "preserving fail-closed Safety HOLD"
                    )
                    raise last_exc from exc

                worker.read_count += 1
                worker.ram_reads.emit(worker.read_count)
                self._manual_stop_if_requested()
                return raw

            def hid_pulse_no_retransmit(self, raw_hid, hold_ms, settle_ms):
                worker._check_stop()
                try:
                    rec = super().hid_pulse_no_retransmit(
                        raw_hid, hold_ms, settle_ms
                    )
                except Exception as exc:
                    self._manual_stop_if_requested(exc)
                    raise
                # RELEASE_ALL can intentionally make INPUT_STATUS stop reporting
                # COMPLETED for a command that was active at the instant Stop
                # was clicked. User Stop owns that outcome; it is not a
                # controller Safety HOLD.
                self._manual_stop_if_requested()
                return rec

            def touch_pulse_no_retransmit(self, touch_state, hold_ms, settle_ms):
                worker._check_stop()
                try:
                    rec = super().touch_pulse_no_retransmit(
                        touch_state, hold_ms, settle_ms
                    )
                except Exception as exc:
                    self._manual_stop_if_requested(exc)
                    raise
                self._manual_stop_if_requested()
                return rec

            def hid_latch_no_retransmit(self, raw_hid):
                worker._check_stop()
                try:
                    rec = super().hid_latch_no_retransmit(raw_hid)
                except Exception as exc:
                    self._manual_stop_if_requested(exc)
                    raise
                self._manual_stop_if_requested()
                return rec

        return StopAwareBridge(self.host, timeout=min(self.timeout, 1.5))

    def _encounter_ui_payload(self, pk6, *, horde_slot=None, horde_size=1):
        ivs = dict(pk6.get("ivs") or {})
        species = int(pk6.get("species", 0))
        nature_id = int(pk6.get("nature_id", 0))
        nature_name = (
            NATURE_NAMES[nature_id]
            if 0 <= nature_id < len(NATURE_NAMES)
            else f"Nature #{nature_id}"
        )
        target_result = evaluate_target({
            "species": species,
            "nature": nature_name,
            "ability_id": pk6.get("ability_id"),
            "ability": ability_name(pk6.get("ability_id")),
            "gender": pk6.get("gender", "—"),
            "moves": list(pk6.get("moves") or []),
            "is_shiny": bool(pk6.get("is_shiny")),
            "ivs": ivs,
        }, self.target_criteria)
        hp_type, hp_power = hidden_power_from_ivs(ivs)
        split_evolution = predict_split_evolution(species, pk6.get("ec"))
        payload = {
            "attempt": self.attempt,
            "battle_index": self.attempt,
            "horde": int(horde_size) == 5,
            "horde_size": int(horde_size),
            "horde_slot": horde_slot,
            "game": (self.game_profile or {}).get("name", "ORAS"),
            "species": species,
            "species_name": SPECIES_NAMES.get(species, f"Species #{species}"),
            "pokemon_pid": pk6.get("pid", "—"),
            "pid": pk6.get("pid", "—"),
            "ec": pk6.get("ec", "—"),
            "tid": pk6.get("tid", "—"),
            "sid": pk6.get("sid", "—"),
            "shiny_xor": int(pk6.get("shiny_xor", 0)),
            "is_shiny": bool(pk6.get("is_shiny")),
            "ivs": ivs,
            "nature": nature_name,
            "ability_id": pk6.get("ability_id"),
            "ability": ability_name(pk6.get("ability_id")),
            "gender": pk6.get("gender", "—"),
            "hidden_power": hp_type,
            "hidden_power_power": hp_power,
            "target_mode": bool(target_result.get("enabled")),
            "target_match": bool(target_result.get("match")),
            "target_checks": target_result.get("checks", {}),
            "hunt_type": "Wild",
            "method": self.method_key,
            "movement_axis": self.movement_axis,
            "method_name": self.method_meta["name"],
            "environment": (
                self.target_selection.get("environment_name")
                or self.target_selection.get("section_title")
                or ((self.current_world_terrain_name if hasattr(self, "current_world_terrain_name") else None))
                or "Land"
            ),
            "location_name": self.location_name,
            "zone_id": self.location_zone,
            "parent_map": self.location_parent_map,
            "matrix_id": self.location_matrix,
            "selected_target_species": self.target_species or None,
            "selected_target_name": self.target_species_name or None,
            "selected_target_location": self.target_location_name or None,
            "selected_species_match": (
                species == self.target_species
                if self.target_species > 0 else None
            ),
            "movement_axis": self.movement_axis,
            "movement_axis_name": (
                AXES[self.movement_axis]["name"]
                if self.movement_axis in AXES else "Stationary"
            ),
        }
        if split_evolution:
            payload["split_evolution"] = split_evolution
            payload["predicted_evolution"] = split_evolution["line"]
        return payload

    def _commit_nonshiny_horde_seen(self, payloads):
        """Commit validated non-shiny Horde Pokémon before escape.

        Once all five opponent PK6s are checksum-valid, trainer-valid, unique and
        shiny-checked, those five Pokémon have genuinely been seen. Escape is a
        later controller action and must not decide whether the encounter counts.
        This helper intentionally does not append the final encounter-history row;
        that row is written after escape so it can include the accepted Run tap
        and post-escape grid.
        """
        if any(bool(p.get("is_shiny")) for p in payloads):
            raise RuntimeError("non-shiny Horde commit received a shiny payload")

        for payload in payloads:
            iv_sum = sum(int(v) for v in (payload.get("ivs") or {}).values())
            sv = int(payload.get("shiny_xor", 0))
            self.lifetime["lifetime_seen"] = int(
                self.lifetime.get("lifetime_seen", 0)
            ) + 1
            self.lifetime["phase_seen"] = int(
                self.lifetime.get("phase_seen", 0)
            ) + 1
            for key, value, fn in (
                ("highest_sv", sv, max),
                ("lowest_sv", sv, min),
                ("highest_iv_sum", iv_sum, max),
                ("lowest_iv_sum", iv_sum, min),
            ):
                old = self.lifetime.get(key)
                self.lifetime[key] = value if old is None else fn(int(old), value)
            self.species_counts[int(payload["species"])] += 1

        self.pokemon_seen_session += len(payloads)
        self._save_stats()
        self.stats.emit(self._stats_payload())

    def _append_encounter_history_only(
        self, payload, causal=None, field_pos=None, duration_s=None,
        field_to_encounter_s=None, battle_to_field_s=None,
    ):
        """Append final escape metadata without incrementing seen totals again."""
        record = {
            **payload,
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "accepted_run_attempt": (
                (causal or {}).get("accepted_attempt")
                if causal is not None else None
            ),
            "post_escape_grid": (
                (field_pos or {}).get("grid")
                if field_pos is not None else None
            ),
            "phase_length": (
                payload.get("phase_length")
                if payload.get("is_shiny") else None
            ),
            "duration_s": (
                round(float(duration_s), 3) if duration_s is not None else None
            ),
            "field_to_encounter_s": (
                round(float(field_to_encounter_s), 3)
                if field_to_encounter_s is not None else None
            ),
            "battle_to_field_s": (
                round(float(battle_to_field_s), 3)
                if battle_to_field_s is not None else None
            ),
        }
        try:
            with self.encounter_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        except Exception:
            pass

    def _causal_run_until_field(self, br, core):
        """Use frozen causal Run first, then a bounded recovery extension.

        Hardware evidence on Route 123 showed successful Hordes accepting Run on
        taps 17, 19 and 22, while one Shuppet Horde remained battle-active through
        the normal 24-tap ceiling. Entry-ability/message presentation can therefore
        outlast the single-battle bound. The same delayed battle-menu transition
        can affect a single encounter, so recovery is allowed for every encounter.
        The frozen normal Wild core is left untouched; this wrapper adds only up to
        24 additional identical causal Run touches (48 total), still requiring a
        verified inactive battle and stable field before returning success.
        """
        causal = core.causal_run_until_field(br)
        if causal.get("success"):
            return causal

        reason = str(causal.get("reason") or "")
        attempts = list(causal.get("attempts") or [])
        if not reason.startswith("battle stayed active through"):
            return causal

        try:
            still_active = br.u32(core.BATTLE_ADDR) == core.BATTLE_ACTIVE
        except Exception:
            return causal
        if not still_active:
            return causal

        base_attempts = len(attempts)
        extra_attempts = 24
        total_limit = base_attempts + extra_attempts
        self._check_stop()
        time.sleep(0.50)
        self._check_stop()
        self._log(
            f"RUN RECOVERY EXTENSION: battle still active after "
            f"{base_attempts} causal Run taps; allowing up to {extra_attempts} "
            f"additional bounded Run taps ({total_limit} total)"
        )

        for attempt in range(base_attempts + 1, total_limit + 1):
            self._check_stop()
            before = br.u32(core.BATTLE_ADDR)
            if before != core.BATTLE_ACTIVE:
                field = core.wait_field_stable(br, core.FIELD_RETURN_TIMEOUT_SEC)
                return {
                    "success": bool(field.get("stable")),
                    "accepted_attempt": None,
                    "attempts": attempts,
                    "field": field,
                    "note": "battle left active before extended Run touch",
                    "run_recovery_extension_used": True,
                }

            touch = core.one_run_touch(br)
            if not touch.get("completed"):
                return {
                    "success": False,
                    "attempts": attempts + [{
                        "attempt": attempt,
                        "before_battle": core.hx(before),
                        "touch": touch,
                        "status": "TOUCH_NOT_COMPLETED",
                    }],
                    "reason": "touch pulse did not complete",
                    "run_recovery_extension_used": True,
                }

            observed = core.observe_after_touch(br, core.POST_TAP_OBSERVE_SEC)
            item = {
                "attempt": attempt,
                "before_battle": core.hx(before),
                "touch": touch,
                "observe": observed,
            }
            attempts.append(item)

            if observed.get("left_active"):
                field = core.wait_field_stable(br, core.FIELD_RETURN_TIMEOUT_SEC)
                return {
                    "success": bool(field.get("stable")),
                    "accepted_attempt": attempt,
                    "attempts": attempts,
                    "field": field,
                    "first_nonactive": observed.get("first_nonactive"),
                    "run_recovery_extension_used": True,
                }

        return {
            "success": False,
            "attempts": attempts,
            "reason": (
                f"battle stayed active through {total_limit} bounded Run taps"
            ),
            "run_recovery_extension_used": True,
        }

    def _reset_phase_extrema(self):
        """Clear extrema that belong to the just-completed shiny phase."""
        for key in (
            "highest_sv", "lowest_sv",
            "highest_iv_sum", "lowest_iv_sum",
        ):
            self.lifetime[key] = None

    def _record_encounter(
        self, payload, causal=None, field_pos=None, duration_s=None,
        field_to_encounter_s=None, battle_to_field_s=None,
    ):
        iv_sum = sum(int(v) for v in (payload.get("ivs") or {}).values())
        sv = int(payload.get("shiny_xor", 0))
        shiny = bool(payload.get("is_shiny"))

        self.lifetime["lifetime_seen"] = int(self.lifetime.get("lifetime_seen", 0)) + 1
        self.lifetime["phase_seen"] = int(self.lifetime.get("phase_seen", 0)) + 1

        detected_charm = (self.shiny_charm_state or {}).get("detected")
        encounter_probability = None
        if detected_charm is not None:
            if self.method_key == "fishing":
                odds_payload = payload.get("fishing_encounter_odds") or {}
                encounter_probability = float(odds_payload.get("probability", 0.0) or 0.0)
                if encounter_probability <= 0.0:
                    chain_snap = payload.get("fishing_chain") or {}
                    post_chain = max(1, int(chain_snap.get("chain", 1) or 1))
                    encounter_probability = float(
                        shiny_odds_for_chain(
                            max(0, post_chain - 1), detected_charm is True
                        )["probability"]
                    )
            else:
                encounter_probability = resolve_shiny_odds(
                    game=(self.game_profile or {}).get("name", "ORAS"),
                    hunt_type="Wild",
                    shiny_charm_present=detected_charm,
                    shiny_charm_applies=True,
                ).probability
        if encounter_probability is not None:
            current_log = self.lifetime.get("phase_log_miss")
            if current_log is None:
                current_log = 0.0
            self.lifetime["phase_log_miss"] = advance_phase_log_miss(
                current_log, encounter_probability
            )
            self.lifetime["phase_cumulative_probability"] = cumulative_probability_from_log_miss(
                self.lifetime["phase_log_miss"]
            )
        else:
            self.lifetime["phase_log_miss"] = None
            self.lifetime["phase_cumulative_probability"] = None
        payload["encounter_shiny_probability"] = encounter_probability
        payload["phase_cumulative_probability"] = self.lifetime.get(
            "phase_cumulative_probability"
        )

        for key, value, fn in (
            ("highest_sv", sv, max),
            ("lowest_sv", sv, min),
            ("highest_iv_sum", iv_sum, max),
            ("lowest_iv_sum", iv_sum, min),
        ):
            old = self.lifetime.get(key)
            self.lifetime[key] = value if old is None else fn(int(old), value)

        completed_phase = None
        if shiny:
            increment_species_shiny_total(
                int(payload["species"]),
                payload.get("species_name") or f"Species {payload['species']}",
                found_time=datetime.now().astimezone().isoformat(timespec="seconds"),
            )
            completed_phase = int(self.lifetime["phase_seen"])
            self.lifetime["lifetime_shinies"] = int(
                self.lifetime.get("lifetime_shinies", 0)
            ) + 1
            self.lifetime["last_phase_seen"] = completed_phase
            self.lifetime["last_phase_cumulative_probability"] = float(
                self.lifetime.get("phase_cumulative_probability", 0.0) or 0.0
            )
            self.lifetime["last_shiny"] = {
                "target": (
                    self.target_species_name
                    if self.target_species_name else self.location_name
                ),
                "location": self.location_name,
                "method": self.method_meta["name"],
                "species": payload["species_name"],
                "pid": payload["pokemon_pid"],
                "shiny_xor": sv,
                "predicted_evolution": payload.get("predicted_evolution"),
                "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            # Next phase starts at zero after the held shiny.
            self.lifetime["phase_seen"] = 0
            self.lifetime["phase_log_miss"] = 0.0
            self.lifetime["phase_cumulative_probability"] = 0.0
            self._reset_phase_extrema()
            self._append_recent_shiny(payload)

        self._save_stats()

        record = {
            **payload,
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "accepted_run_attempt": (
                (causal or {}).get("accepted_attempt")
                if causal is not None else None
            ),
            "post_escape_grid": (
                (field_pos or {}).get("grid")
                if field_pos is not None else None
            ),
            "phase_length": completed_phase if shiny else None,
            "duration_s": (round(float(duration_s), 3) if duration_s is not None else None),
            "field_to_encounter_s": (
                round(float(field_to_encounter_s), 3)
                if field_to_encounter_s is not None else None
            ),
            "battle_to_field_s": (
                round(float(battle_to_field_s), 3)
                if battle_to_field_s is not None else None
            ),
        }
        try:
            with self.encounter_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        except Exception:
            pass

        self.stats.emit(self._stats_payload())

    def _capture_wild_shiny_framebuffer(self, payload):
        """Best-effort one-shot top-screen capture after RAM shiny confirmation.

        This function cannot veto or create a shiny decision. For blocklisted
        shinies it deliberately completes before causal Run so Discord gets the
        actual battle presentation instead of the overworld after escape.
        """
        try:
            # RAM can become authoritative a little before the battle model is
            # visually settled. This delay is presentation-only; no input is
            # sent and shiny authority has already been established.
            # A Wild PK6 can be readable while the encounter transition is
            # still covering the opponent.  Give the Gen 6 presentation enough
            # time to place the Pokemon on screen before freezing the evidence
            # frame.  This is telemetry-only: RAM authority and HOLD have
            # already been established and no input is sent during the wait.
            time.sleep(3.25)
            self.profile.screenshots_dir.mkdir(parents=True, exist_ok=True)
            species = int(payload.get("species", 0) or 0)
            pid = str(payload.get("pokemon_pid") or payload.get("pid") or "unknown")
            safe_pid = "".join(ch for ch in pid if ch.isalnum())[-16:] or "unknown"
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            out = self.profile.screenshots_dir / (
                f"shiny_{species}_{safe_pid}_{stamp}.png"
            )
            meta = capture_top_screen(
                self.host,
                out,
                port=4952,
                timeout=min(max(float(self.timeout), 0.5), 1.25),
            )
            self._log(
                "FRAMEBUFFER SHINY CAPTURE: "
                f"{meta['width']}x{meta['height']} • {meta['path']}"
            )
            return meta
        except Exception as exc:
            self._log(
                "FRAMEBUFFER SHINY CAPTURE unavailable: "
                f"{type(exc).__name__}: {exc} • shiny authority unchanged"
            )
            return None

    def _commit_blocklisted_shiny_authority(self, payloads):
        """Commit a RAM-confirmed shiny encounter before automatic escape.

        This is used only when every shiny in the opponent set is explicitly
        blocklisted for RUN. The shiny is already real at this boundary, so a
        later controller/escape failure must not erase it from totals/history.

        One simultaneous Horde remains one completed phase, matching the
        existing Horde shiny-HOLD semantics.
        """
        shiny_payloads = [
            p for p in payloads if bool(p.get("is_shiny"))
        ]
        if not shiny_payloads:
            raise RuntimeError(
                "blocklisted shiny authority commit received no shiny"
            )
        if any(not bool(p.get("shiny_blocked")) for p in shiny_payloads):
            raise RuntimeError(
                "blocklisted shiny authority commit received an unblocked shiny"
            )

        for payload in payloads:
            iv_sum = sum(
                int(v) for v in (payload.get("ivs") or {}).values()
            )
            sv = int(payload.get("shiny_xor", 0))
            self.lifetime["lifetime_seen"] = int(
                self.lifetime.get("lifetime_seen", 0)
            ) + 1
            self.lifetime["phase_seen"] = int(
                self.lifetime.get("phase_seen", 0)
            ) + 1
            for key, value, fn in (
                ("highest_sv", sv, max),
                ("lowest_sv", sv, min),
                ("highest_iv_sum", iv_sum, max),
                ("lowest_iv_sum", iv_sum, min),
            ):
                old = self.lifetime.get(key)
                self.lifetime[key] = (
                    value if old is None else fn(int(old), value)
                )
            self.species_counts[int(payload["species"])] += 1

        self.pokemon_seen_session += len(payloads)

        completed_phase = int(self.lifetime.get("phase_seen", 0))
        found_time = datetime.now().astimezone().isoformat(
            timespec="seconds"
        )
        for payload in shiny_payloads:
            increment_species_shiny_total(
                int(payload["species"]),
                payload.get("species_name")
                or f"Species {payload['species']}",
                found_time=found_time,
            )
            payload["phase_length"] = completed_phase

        self.lifetime["lifetime_shinies"] = int(
            self.lifetime.get("lifetime_shinies", 0)
        ) + len(shiny_payloads)
        self.lifetime["last_phase_seen"] = completed_phase

        last = shiny_payloads[-1]
        self.lifetime["last_shiny"] = {
            "target": (
                self.target_species_name
                if self.target_species_name else self.location_name
            ),
            "location": self.location_name,
            "method": self.method_meta["name"],
            "species": last["species_name"],
            "pid": last["pokemon_pid"],
            "shiny_xor": int(last["shiny_xor"]),
            "time": found_time,
            "shiny_action": "RUN_BLOCKLIST",
        }
        self.lifetime["phase_seen"] = 0
        self._reset_phase_extrema()
        self._save_stats()

        for payload in shiny_payloads:
            self._append_recent_shiny(payload)

        self.stats.emit(self._stats_payload())
        return completed_phase

    def _record_horde_shiny_hold(self, payloads, duration_s=None):
        """Record one simultaneous Horde as a single completed shiny phase."""
        completed_phase = None
        shiny_payloads = [p for p in payloads if p.get("is_shiny")]

        for payload in payloads:
            iv_sum = sum(int(v) for v in (payload.get("ivs") or {}).values())
            sv = int(payload.get("shiny_xor", 0))
            self.lifetime["lifetime_seen"] = int(
                self.lifetime.get("lifetime_seen", 0)
            ) + 1
            self.lifetime["phase_seen"] = int(
                self.lifetime.get("phase_seen", 0)
            ) + 1
            for key, value, fn in (
                ("highest_sv", sv, max),
                ("lowest_sv", sv, min),
                ("highest_iv_sum", iv_sum, max),
                ("lowest_iv_sum", iv_sum, min),
            ):
                old = self.lifetime.get(key)
                self.lifetime[key] = value if old is None else fn(int(old), value)

        if shiny_payloads:
            completed_phase = int(self.lifetime.get("phase_seen", 0))
            found_time = datetime.now().astimezone().isoformat(timespec="seconds")
            for payload in shiny_payloads:
                increment_species_shiny_total(
                    int(payload["species"]),
                    payload.get("species_name")
                    or f"Species {payload['species']}",
                    found_time=found_time,
                )
            self.lifetime["lifetime_shinies"] = int(
                self.lifetime.get("lifetime_shinies", 0)
            ) + len(shiny_payloads)
            self.lifetime["last_phase_seen"] = completed_phase
            last = shiny_payloads[-1]
            self.lifetime["last_shiny"] = {
                "target": (
                    self.target_species_name
                    if self.target_species_name else self.location_name
                ),
                "location": self.location_name,
                "method": self.method_meta["name"],
                "species": last["species_name"],
                "pid": last["pokemon_pid"],
                "shiny_xor": int(last["shiny_xor"]),
                "time": found_time,
            }
            self.lifetime["phase_seen"] = 0
            self._reset_phase_extrema()

        self._save_stats()

        now = datetime.now().astimezone().isoformat(timespec="seconds")
        for payload in payloads:
            record = {
                **payload,
                "time": now,
                "accepted_run_attempt": None,
                "post_escape_grid": None,
                "phase_length": (
                    completed_phase if payload.get("is_shiny") else None
                ),
                "duration_s": (
                    round(float(duration_s), 3)
                    if duration_s is not None else None
                ),
            }
            try:
                with self.encounter_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, separators=(",", ":")) + "\n")
            except Exception:
                pass

        for payload in shiny_payloads:
            self._append_recent_shiny(payload)

        self.stats.emit(self._stats_payload())

    def _append_recent_shiny(self, payload):
        items = _load_json(self.recent_shiny_path, [])
        if not isinstance(items, list):
            items = []
        items.insert(0, {
            "hunt_type": "Wild",
            "target": (
                self.target_species_name
                if self.target_species_name else self.location_name
            ),
            "location": self.location_name,
            "method": self.method_meta["name"],
            "starter": (
                self.target_species_name
                if self.target_species_name else self.location_name
            ),
            "species": payload["species_name"],
            "species_id": int(payload["species"]),
            "pid": payload["pokemon_pid"],
            "shiny_xor": int(payload["shiny_xor"]),
            "ivs": dict(payload.get("ivs") or {}),
            "nature": payload.get("nature", "—"),
            "ability_id": payload.get("ability_id"),
            "ability": payload.get("ability", "—"),
            "gender": payload.get("gender", "—"),
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "phase_length": int(self.lifetime.get("last_phase_seen", 0) or 0),
            "game": (self.game_profile or {}).get("name", "ORAS"),
            "target_match": bool(payload.get("target_match")),
            "shiny_blocked": bool(payload.get("shiny_blocked")),
            "shiny_action": payload.get("shiny_action"),
            "predicted_evolution": payload.get("predicted_evolution"),
            "split_evolution": payload.get("split_evolution"),
        })
        items = items[:20]
        _atomic_write(self.recent_shiny_path, items)
        self.recent_shinies.emit(items)

    def _note_field_to_encounter(self, seconds):
        """Record movement/field time up to the proven encounter boundary."""
        value = max(0.0, float(seconds))
        self.field_to_encounter_last_s = value
        self.field_to_encounter_total_s += value
        self.field_to_encounter_count += 1
        return value

    def _complete_encounter_timing(self, cycle_seconds, field_seconds):
        """Record normal battle/Run/field-return timing after authority is restored."""
        cycle = max(0.0, float(cycle_seconds))
        field = max(0.0, float(field_seconds))
        battle_return = max(0.0, cycle - field)

        self.measured_cycle_last_s = cycle
        self.measured_cycle_total_s += cycle
        self.measured_cycle_count += 1

        self.battle_to_field_last_s = battle_return
        self.battle_to_field_total_s += battle_return
        self.battle_to_field_count += 1
        return battle_return

    @staticmethod
    def _timing_average(total, count):
        return (float(total) / int(count)) if int(count) > 0 else 0.0

    def _stats_payload(self):
        elapsed = (
            max(0.0, time.monotonic() - self.started_mono)
            if self.started_mono is not None else 0.0
        )
        seen = int(self.lifetime.get("lifetime_seen", 0))
        phase_seen = int(self.lifetime.get("phase_seen", 0))
        return {
            "hunt_type": "Wild",
            "wild_method": self.method_key,
            "movement_axis": self.movement_axis,
            "method_name": self.method_meta["name"],
            "movement_axis": self.movement_axis,
            "movement_axis_name": (
                AXES[self.movement_axis]["name"]
                if self.movement_axis in AXES else "Stationary"
            ),
            "target": (
                self.target_species_name
                if self.target_species_name else self.location_name
            ),
            "location_name": self.location_name,
            "target_species": self.target_species or None,
            "target_species_name": self.target_species_name or None,
            "lifetime_seen": seen,
            "lifetime_shinies": int(self.lifetime.get("lifetime_shinies", 0)),
            "phase_index": int(self.lifetime.get("lifetime_shinies", 0)) + 1,
            "phase_seen": phase_seen,
            "highest_sv": self.lifetime.get("highest_sv"),
            "lowest_sv": self.lifetime.get("lowest_sv"),
            "highest_iv_sum": self.lifetime.get("highest_iv_sum"),
            "lowest_iv_sum": self.lifetime.get("lowest_iv_sum"),
            "last_shiny": self.lifetime.get("last_shiny"),
            "battles_seen_session": int(self.attempt),
            "pokemon_seen_session": int(self.pokemon_seen_session),
            "horde_battles_session": int(self.horde_battles),
            "horde_trigger": self.horde_trigger if self.method_key == "horde" else None,
            "horde_trigger_resolved": self.horde_trigger_resolved if self.method_key == "horde" else None,
            "honey_quantity": (self.honey_inventory or {}).get("quantity") if self.method_key == "horde" else None,
            "fishing_chain": (self.fishing_chain.snapshot() if self.fishing_chain is not None else None),
            "shiny_charm": dict(self.shiny_charm_state or {}),
            "phase_log_miss": self.lifetime.get("phase_log_miss"),
            "phase_cumulative_probability": self.lifetime.get("phase_cumulative_probability"),
            "shiny_odds_display": (
                f"~1/{float((self.fishing_chain.snapshot().get('next_odds') or {}).get('one_in')):,.2f}"
                if self.fishing_chain is not None and (self.fishing_chain.snapshot().get('next_odds') or {}).get('one_in')
                else resolve_shiny_odds(
                    game=(self.game_profile or {}).get("name", "ORAS"),
                    hunt_type="Wild",
                    shiny_charm_present=(self.shiny_charm_state or {}).get("detected"),
                    shiny_charm_applies=True,
                ).display
            ),
            # HF95: split the hunt cycle so encounter-rate patches can be
            # measured independently of battle/Run/field-return overhead.
            "field_to_encounter_last": self.field_to_encounter_last_s,
            "field_to_encounter_average": self._timing_average(
                self.field_to_encounter_total_s, self.field_to_encounter_count
            ),
            "field_to_encounter_samples": int(self.field_to_encounter_count),
            "battle_to_field_last": self.battle_to_field_last_s,
            "battle_to_field_average": self._timing_average(
                self.battle_to_field_total_s, self.battle_to_field_count
            ),
            "battle_to_field_samples": int(self.battle_to_field_count),
            "measured_cycle_last": self.measured_cycle_last_s,
            "measured_cycle_average": self._timing_average(
                self.measured_cycle_total_s, self.measured_cycle_count
            ),
            "measured_cycle_samples": int(self.measured_cycle_count),
            # HF96: completed throughput and field-only trigger pace are
            # deliberately different metrics.  `rate` is the real number of
            # Pokemon processed per active worker hour; field pace excludes
            # battle/run/return overhead and exposes encounter-rate effects.
            "rate": (
                self.pokemon_seen_session * 3600.0 / elapsed
                if elapsed > 0.0 else 0.0
            ),
            "field_pace_per_hour": (
                3600.0 / self._timing_average(
                    self.field_to_encounter_total_s, self.field_to_encounter_count
                )
                if self.field_to_encounter_count > 0
                and self.field_to_encounter_total_s > 0.0
                else 0.0
            ),
            "cycle_pace_per_hour": (
                3600.0 / self._timing_average(
                    self.measured_cycle_total_s, self.measured_cycle_count
                )
                if self.measured_cycle_count > 0
                and self.measured_cycle_total_s > 0.0
                else 0.0
            ),
            "average_time": (
                elapsed / max(1, self.pokemon_seen_session)
                if self.pokemon_seen_session else 0.0
            ),
            "resets": 0,
        }

    def _session_snapshot(self, status, reason=None):
        elapsed = (
            max(0.0, time.monotonic() - self.started_mono)
            if self.started_mono is not None else 0.0
        )
        return {
            "tool": "Pokebot3DS-CFW v0p43EI Wild Run Bridge Recovery + Field Reacquire",
            "status": status,
            "reason": reason,
            "game": (
                (self.target_selection or {}).get("game_name")
                or (self.game_profile or {}).get("name")
                or "RAM-detected Gen 6"
            ),
            "target_selection": self.target_selection or None,
            "look_for_target": self.target_criteria,
            "auto_throw_one_poke_ball_on_shiny": (
                self.auto_throw_one_poke_ball_on_shiny
            ),
            "capture_ball_override": self.capture_ball_override,
            "auto_capture_test_initially_armed": self.auto_capture_test_initially_armed,
            "auto_capture_test_pending": self.auto_capture_test_pending,
            "last_auto_capture_test": self.last_auto_capture_test,
            "horde_auto_attack_test_initially_armed": self.horde_auto_attack_test_initially_armed,
            "horde_auto_attack_test_pending": self.horde_auto_attack_test_pending,
            "last_horde_auto_attack_test": self.last_horde_auto_attack_test,
            "location_name": self.location_name,
            "zone_id": self.location_zone,
            "parent_map": self.location_parent_map,
            "matrix_id": self.location_matrix,
            "method": self.method_key,
            "movement_axis": self.movement_axis,
            "method_name": self.method_meta["name"],
            "movement_axis": self.movement_axis,
            "movement_axis_name": (
                AXES[self.movement_axis]["name"]
                if self.movement_axis in AXES else "Stationary"
            ),
            "host": self.host,
            "started": self.started_iso,
            "finished": datetime.now().astimezone().isoformat(timespec="seconds"),
            "elapsed_seconds": round(elapsed, 3),
            "encounters_completed": self.attempt,
            "battles_completed": self.attempt,
            "pokemon_seen_session": self.pokemon_seen_session,
            "encounter_timing": {
                "field_to_encounter_last": self.field_to_encounter_last_s,
                "field_to_encounter_average": self._timing_average(
                    self.field_to_encounter_total_s, self.field_to_encounter_count
                ),
                "field_to_encounter_samples": self.field_to_encounter_count,
                "battle_to_field_last": self.battle_to_field_last_s,
                "battle_to_field_average": self._timing_average(
                    self.battle_to_field_total_s, self.battle_to_field_count
                ),
                "battle_to_field_samples": self.battle_to_field_count,
                "measured_cycle_last": self.measured_cycle_last_s,
                "measured_cycle_average": self._timing_average(
                    self.measured_cycle_total_s, self.measured_cycle_count
                ),
                "measured_cycle_samples": self.measured_cycle_count,
            },
            "horde_battles": self.horde_battles,
            "horde_trigger": self.horde_trigger if self.method_key == "horde" else None,
            "horde_trigger_resolved": self.horde_trigger_resolved if self.method_key == "horde" else None,
            "honey_inventory": dict(self.honey_inventory or {}) if self.method_key == "horde" else None,
            "last_opponent_set": self.last_opponent_set,
            "global_trigger_cycles": self.global_cycle,
            "species_counts": {str(k): v for k, v in sorted(self.species_counts.items())},
            # Diagnostic only. A valid escape path may have no accepted
            # attempt number. Never let support export fail because a legacy
            # Counter contains mixed None/int keys.
            "run_attempt_counts": {
                ("unknown" if k is None else str(k)): v
                for k, v in self.run_attempt_counts.items()
            },
            "touch_timeout_recoveries": self.touch_timeout_recoveries,
            "last_encounter": self.last_encounter,
            "last_shiny_auto_throw": self.last_shiny_auto_throw,
            "fishing_chain": (self.fishing_chain.snapshot() if self.fishing_chain is not None else None),
            "events": self.events[-40:],
        }

    def _save_support(self, status, reason=None):
        snapshot = self._session_snapshot(status, reason)
        _atomic_write(self.session_path, snapshot)
        if not self.auto_support_zip:
            return None
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = self.support_dir / f"Pokebot3DS-CFW_wild_{self.method_key}_{stamp}_{status}.zip"
        try:
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
                if self.session_path.exists():
                    zf.write(self.session_path, self.session_path.name)
                if self.log_path.exists():
                    zf.write(self.log_path, self.log_path.name)
            self.support_ready.emit(str(out))
            return out
        except Exception:
            return None

    def _require_stationary_horde_position(self, pos, label):
        """Validate a stationary Sweet Scent position without requiring grass.

        Horde mode sends no field movement, so the relevant authority is that
        both coordinate copies agree, the player is settled on a tile centre,
        and the live zone remains the one armed at hunt start. Sweet Scent can
        be used from valid non-grass overworld tiles, so normal Wild core1
        encounter-grass authority must not be reused here.
        """
        if self.location_zone is not None and int(pos["zone_id"]) != int(self.location_zone):
            raise RuntimeError(
                f"{label}: live zone changed from {self.location_zone} to {pos['zone_id']}"
            )
        if not pos.get("duplicates_match"):
            raise RuntimeError(f"{label}: duplicate world coordinates disagree")
        if not pos.get("settled_tile_center"):
            raise RuntimeError(f"{label}: player is not settled on a tile center")
        return tuple(pos.get("grid") or ())

    def _wait_for_stationary_horde_field(self, br, core, backend, anchor_grid, timeout=12.0):
        """Wait for post-Run field authority for stationary Horde mode.

        Unlike normal Wild, Horde/Sweet Scent authority does not require an
        encounter-grass tile. It requires the same zone and same tile that was
        armed before the battle, with duplicate coordinates agreeing and the
        player settled.
        """
        deadline = time.monotonic() + float(timeout)
        stable = 0
        last_pos = None
        while time.monotonic() < deadline:
            self._check_stop()
            battle = br.u32(core.BATTLE_ADDR)
            pos = backend.read_position(br)
            last_pos = pos
            same_zone = int(pos.get("zone_id", -1)) == int(self.location_zone)
            same_grid = tuple(pos.get("grid") or ()) == tuple(anchor_grid)
            safe = (
                battle == core.BATTLE_INACTIVE
                and same_zone
                and same_grid
                and bool(pos.get("duplicates_match"))
                and bool(pos.get("settled_tile_center"))
            )
            if safe:
                stable += 1
                if stable >= 2:
                    return {"ready": True, "status": "STATIONARY_FIELD_AUTHORITY", "final_position": pos}
            else:
                stable = 0
            time.sleep(0.12)
        return {
            "ready": False,
            "status": "STATIONARY_FIELD_TIMEOUT",
            "final_position": last_pos,
            "expected_zone": self.location_zone,
            "expected_grid": list(anchor_grid),
        }


    SWEET_SCENT_MOVE_ID = 230
    # Field-usable moves that may appear before Summary/Switch/Item in the
    # ORAS party action menu. v0p38j requires Sweet Scent to be the only such
    # move list authority retained; D19m resolves the actual visible Sweet Scent user.
    ORAS_FIELD_MOVE_IDS = {
        15,   # Cut
        19,   # Fly
        57,   # Surf
        70,   # Strength
        91,   # Dig
        100,  # Teleport
        127,  # Waterfall
        230,  # Sweet Scent
        249,  # Rock Smash
        290,  # Secret Power
        291,  # Dive
    }

    def _sleep_stop_aware(self, seconds):
        deadline = time.monotonic() + float(seconds)
        while time.monotonic() < deadline:
            self._check_stop()
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def _validate_auto_sweet_scent_setup(self, br):
        """Resolve a usable Sweet Scent holder from proven visible party RAM.

        D25 removes the slot-1 assumption. The idle Party Viewer now maps and
        revalidates a live RAM order source independently of hunt execution.
        We then inspect each
        member's four PK6 move IDs and current PP and choose the first visible
        Sweet Scent holder whose party action menu is deterministic. Party slots 1-6 now have ORAS layout defaults, with saved calibration overriding them.

        The fixed Sweet Scent menu touch at (170,104) was hardware-proven only
        when Sweet Scent is the holder's only recognized ORAS field move, so a
        holder that also knows Surf/Fly/etc. is intentionally rejected here.
        """
        snap = get_runtime_party_snapshot(
            self.host, int(getattr(br, "port", 4952)), br
        )
        ordered = list(snap.get("ordered_parsed") or [])
        if not ordered:
            raise RuntimeError("AUTO HORDE SETUP: live party order could not be read")
        if not bool(snap.get("reorder_proven")):
            raise RuntimeError(
                "AUTO HORDE SETUP: D25 has not yet mapped the live field runtime party pointer chain. "
                "No party-slot touch was sent. The idle Party Viewer is independent of hunt state; "
                "use its party_refresh.log evidence/support ZIP if this remains unmapped."
            )

        candidates = []
        rejected_field = []
        for index, parsed in enumerate(ordered[:PARTY_SLOTS], 1):
            if not (parsed.get("valid") and parsed.get("checksum_valid")):
                continue
            moves = list(parsed.get("moves") or [])[:4]
            pp = list(parsed.get("move_pp") or [])[:4]
            while len(pp) < 4:
                pp.append(0)
            for move_index, move_id in enumerate(moves):
                if int(move_id or 0) != self.SWEET_SCENT_MOVE_ID or int(pp[move_index] or 0) <= 0:
                    continue
                other_field = [
                    int(mid) for mid in moves
                    if int(mid or 0) in self.ORAS_FIELD_MOVE_IDS
                    and int(mid or 0) != self.SWEET_SCENT_MOVE_ID
                ]
                if other_field:
                    rejected_field.append((index, parsed.get("species_name"), other_field))
                    break
                candidates.append((index, parsed, move_index + 1, int(pp[move_index])))
                break

        if not candidates:
            if rejected_field:
                detail = ", ".join(
                    f"slot {slot} {name} also field-moves {moves}"
                    for slot, name, moves in rejected_field
                )
                raise RuntimeError(
                    "AUTO HORDE SETUP: Sweet Scent exists but its field-action menu is ambiguous: "
                    + detail
                    + ". Use a Sweet Scent holder whose only recognized ORAS field move is Sweet Scent."
                )
            raise RuntimeError(
                "AUTO HORDE SETUP: no live visible party Pokémon has usable Sweet Scent PP"
            )

        visible_slot, sweet, move_slot, pp_left = candidates[0]
        field_xy = get_party_field_action_xy(self.base_dir, visible_slot)
        if field_xy is None:
            raise RuntimeError(
                f"AUTO HORDE SETUP: Sweet Scent user {sweet.get('species_name')} is visible party slot "
                f"{visible_slot}, but no party field-action touch coordinate is available."
            )

        self.horde_lead_pk6 = dict(ordered[0]) if ordered else None
        self.sweet_scent_visible_slot = int(visible_slot)
        self.sweet_scent_user = {
            "species": sweet.get("species_name"),
            "visible_slot": int(visible_slot),
            "move_slot": int(move_slot),
            "pp": int(pp_left),
            "field_xy": list(field_xy),
            "party_order_source": snap.get("source"),
            "reorder_proven": True,
        }

        # Load bundled ORAS move metadata during hunt preflight. D25 performs no
        # network lookup; unknown metadata remains fail-closed in the reducer.
        if self.auto_throw_one_poke_ball_on_shiny or self.horde_auto_attack_test_pending:
            self.horde_move_db = ensure_move_metadata(self.base_dir, timeout=3.0)
            available_slots = {
                slot for slot in range(1, 5)
                if get_battle_move_xy(self.base_dir, slot) is not None
            }
            move_choice = choose_safe_attack(
                self.horde_lead_pk6 or {}, self.horde_move_db, available_slots
            )
            if move_choice.get("selected") is None:
                rows = ", ".join(
                    f"slot{r['move_slot']} {r['name']} PP{r['pp']}: {r['reason']}"
                    + ("" if r.get("touch_profile_available") else " / touch unavailable")
                    for r in move_choice.get("moves", [])
                )
                raise RuntimeError(
                    "AUTO HORDE SETUP: lead has no currently-authorized safe single-target attack. "
                    + rows
                    + ". A saved ORAS touch calibration can override the built-in layout coordinates."
                )

        self._log(
            "AUTO SWEET SCENT SETUP PASS D25: "
            f"user={sweet.get('species_name')} visible_slot={visible_slot} move_slot={move_slot} "
            f"PP={pp_left} field_xy={field_xy} party_source={snap.get('source')} "
            f"lead={((self.horde_lead_pk6 or {}).get('species_name'))} "
            f"lead_moves={((self.horde_lead_pk6 or {}).get('moves'))} "
            f"lead_pp={((self.horde_lead_pk6 or {}).get('move_pp'))}"
        )
        return self.sweet_scent_user

    def _prepare_horde_lead_for_honey(self, br):
        """Capture lead/move authority without requiring Sweet Scent."""
        snap = get_runtime_party_snapshot(
            self.host, int(getattr(br, "port", 4952)), br
        )
        ordered = list(snap.get("ordered_parsed") or [])
        if not ordered or not bool(snap.get("reorder_proven")):
            raise RuntimeError(
                "HONEY HORDE SETUP: live visible party order could not be proven"
            )
        self.horde_lead_pk6 = dict(ordered[0])
        if not (
            self.horde_lead_pk6.get("valid")
            and self.horde_lead_pk6.get("checksum_valid")
        ):
            raise RuntimeError("HONEY HORDE SETUP: lead PK6 is not authoritative")

        if self.auto_throw_one_poke_ball_on_shiny or self.horde_auto_attack_test_pending:
            self.horde_move_db = ensure_move_metadata(self.base_dir, timeout=3.0)
            available_slots = {
                slot for slot in range(1, 5)
                if get_battle_move_xy(self.base_dir, slot) is not None
            }
            move_choice = choose_safe_attack(
                self.horde_lead_pk6 or {}, self.horde_move_db, available_slots
            )
            if move_choice.get("selected") is None:
                rows = ", ".join(
                    f"slot{r['move_slot']} {r['name']} PP{r['pp']}: {r['reason']}"
                    for r in move_choice.get("moves", [])
                )
                raise RuntimeError(
                    "HONEY HORDE SETUP: lead has no authorized safe single-target attack. "
                    + rows
                )
        return snap

    def _validate_honey_setup(self, br):
        row = require_honey(br, first_active_slot=True)
        self.honey_inventory = dict(row)
        self._prepare_horde_lead_for_honey(br)
        self._log(
            "HONEY HORDE PREFLIGHT PASS: "
            f"item_id={HONEY_ITEM_ID} save_slot={row['index']} active_slot={row.get('active_index', -1) + 1} "
            f"quantity={row['quantity']} items_active={row['items_active_count']} "
            f"lead={(self.horde_lead_pk6 or {}).get('species_name')}"
        )
        return row

    def _resolve_horde_trigger_setup(self, br):
        requested = self.horde_trigger
        if requested == "sweet_scent":
            self._validate_auto_sweet_scent_setup(br)
            self.horde_trigger_resolved = "sweet_scent"
            return
        if requested == "honey":
            self._validate_honey_setup(br)
            self.horde_trigger_resolved = "honey"
            return

        # Auto preserves the proven Sweet Scent path when available.  The
        # fallback happens during read-only preflight only; no failed gameplay
        # input can silently switch trigger methods.
        try:
            self._validate_auto_sweet_scent_setup(br)
            self.horde_trigger_resolved = "sweet_scent"
            self._log("HORDE TRIGGER AUTO: selected Sweet Scent")
            return
        except Exception as exc:
            self._log(
                "HORDE TRIGGER AUTO: Sweet Scent preflight unavailable; "
                f"trying Honey read-only preflight: {type(exc).__name__}: {exc}"
            )
        self._validate_honey_setup(br)
        self.horde_trigger_resolved = "honey"
        self._log("HORDE TRIGGER AUTO: selected Honey")

    @staticmethod
    def _raw_button(core, bit):
        return int(core.HID_NEUTRAL) & ~(1 << int(bit))

    def _horde_hid(self, br, core, bit, *, label, hold_ms=110, settle_ms=180):
        self._check_stop()
        rec = br.hid_pulse_no_retransmit(
            self._raw_button(core, bit), int(hold_ms), int(settle_ms)
        )
        if not rec.get("completed"):
            raise core.SafetyHold(
                f"{label} did not complete; no input replay authorized"
            )
        self._log(f"{label}: acknowledged COMPLETED")
        return rec

    def _wait_horde_rearm(self, br, core, backend, anchor_grid, label):
        if self.horde_battles <= 0:
            return
        self._log(f"{label} RE-ARM: waiting 1.00 s after prior Horde field return")
        self._sleep_stop_aware(1.00)
        if br.u32(core.BATTLE_ADDR) != core.BATTLE_INACTIVE:
            raise RuntimeError(f"{label} RE-ARM: battle became active before trigger input")
        pos = backend.read_position(br)
        self._require_stationary_horde_position(pos, f"{label} re-arm")
        if tuple(pos.get("grid") or ()) != tuple(anchor_grid):
            raise RuntimeError(
                f"{label} RE-ARM: player moved from armed tile {list(anchor_grid)} "
                f"to {pos.get('grid')}"
            )

    def _locate_field_items_controller(self, br, core):
        """Locate the already-open Items pocket without sending any navigation.

        Honey slot-1 mode requires the user to highlight Honey once in the Bag
        before starting the hunt, then back out to the field. ORAS remembers that
        Bag pocket/cursor. The bot therefore must never try to correct the pocket
        with R/L or move the item cursor. If the yellow shortcut does not reopen
        the exact Items controller, fail closed before either A is sent.
        """
        self._check_stop()
        self._log("HONEY FIELD BAG: proving remembered Items pocket; no navigation authorized")
        try:
            authority = locate_items_controller(br, fallback=False)
        except Exception as first_exc:
            self._log(
                "HONEY FIELD BAG: fast proof missed; one read-only fallback scan, still no navigation"
            )
            try:
                authority = locate_items_controller(br, fallback=True)
            except Exception as exc:
                raise RuntimeError(
                    "HONEY FIELD BAG: remembered Bag state is not the Items pocket; "
                    "put Honey first, highlight Honey once, back out to the field, then start. "
                    f"fast={type(first_exc).__name__}:{first_exc} | "
                    f"fallback={type(exc).__name__}:{exc}"
                )
        self._log(
            "HONEY FIELD BAG: exact Items controller located without navigation "
            f"controller=0x{authority['controller']:08X} cursor=0x{authority['cursor']:08X} "
            f"count={authority['entry_count']} page={authority['page']} selector={authority['selector']}"
        )
        return authority

    def _navigate_field_bag_to_honey(self, br, core, authority):
        """Prove preselected slot-1 Honey; never send D-pad/pocket input.

        The setup contract is intentionally simple and deterministic:
        Honey is the first active Items entry, the user highlights it once before
        starting, and ORAS reopens the remembered Bag cursor on Honey. We require
        page=0 and selector=0 from the live controller plus item ID 94 at compact
        entry 0. Any mismatch safety-holds instead of attempting to scroll.
        """
        target_index = honey_active_index(authority)
        if int(target_index) != 0:
            raise RuntimeError(
                f"HONEY FIELD BAG: Honey is not active slot 1; active_index={target_index}"
            )

        state = read_items_controller_state(br, authority)
        page = int(state.get("page") or 0)
        selector = int(state.get("selector") or 0)
        selected = dict(authority["entries"][0])
        if int(selected.get("item_id") or 0) != HONEY_ITEM_ID:
            raise RuntimeError("HONEY FIELD BAG: first active Items entry is not Honey")
        if page != 0 or selector != 0:
            raise RuntimeError(
                "HONEY FIELD BAG: Honey is slot 1 but is not currently highlighted. "
                "Open the Bag manually, highlight Honey once, back out to the field, then start. "
                f"live page={page} selector={selector}; no D-pad and no item-use A sent"
            )

        self._log(
            "HONEY FIELD BAG: PRESELECTED SLOT-1 authority PASS — "
            f"item_id={selected.get('item_id')} quantity={selected.get('quantity')} "
            f"page={page} selector={selector}; no navigation sent"
        )
        return {
            **state,
            "selected_index": 0,
            "selected": selected,
            "cursor_models": ["page0_selector0"],
            "cursor_encoding": "PRESELECTED_SLOT1_PROVEN",
        }

    @staticmethod
    def _honey_quantity(snapshot):
        rows = list(snapshot.get("honey") or [])
        return int(rows[0].get("quantity") or 0) if len(rows) == 1 else None

    def _trigger_honey_horde(self, br, core, backend, anchor_grid):
        self._check_stop()
        if br.u32(core.BATTLE_ADDR) != core.BATTLE_INACTIVE:
            raise RuntimeError("HONEY HORDE: expected overworld before input")
        pos = backend.read_position(br)
        self._require_stationary_horde_position(pos, "HONEY HORDE pre-input")
        if tuple(pos.get("grid") or ()) != tuple(anchor_grid):
            raise RuntimeError(
                f"HONEY HORDE: player moved from armed tile {list(anchor_grid)} to {pos.get('grid')}"
            )
        self._wait_horde_rearm(br, core, backend, anchor_grid, "HONEY HORDE")

        before = read_items_pocket(br)
        before_qty = self._honey_quantity(before)
        if before_qty is None or before_qty <= 0:
            raise RuntimeError("HONEY HORDE: Honey disappeared before use")
        self.honey_inventory = {
            **dict(self.honey_inventory or {}),
            "quantity": int(before_qty),
        }
        self.status.emit(
            "RUNNING",
            f"Horde • {self.location_name} • Honey x{before_qty} • preselected slot 1",
        )

        # Fast Honey path: use the persistent yellow Bag shortcut on the
        # PokéNav/PlayNav bottom icon row.  This removes the old X -> Bag
        # choreography from every Honey cycle.  (133,230) is deliberately
        # treated as hardware-test input only; the next step must still prove
        # the exact live Items controller before any item-use A is authorized.
        self._sweet_scent_touch(
            br, (133, 230), label="HONEY YELLOW BAG SHORTCUT", after=1.10
        )

        # Hardware result from DY: the field Bag does not expose the battle-DllBag
        # controller signature at all (raw_pattern_hits=0), so controller discovery
        # must not block the item-use inputs.  Honey mode has an explicit setup
        # contract instead: Honey is live Items slot 1 and was manually highlighted
        # once before returning to the field.  The save-backed preflight above proves
        # the slot/quantity; the yellow Bag shortcut reopens the remembered cursor.
        # No D-pad/pocket navigation is authorized in this direct path.
        self.honey_controller = {
            "mode": "PRESELECTED_SLOT1_DIRECT_TWO_A",
            "controller_scan": "disabled_after_hardware_raw_pattern_hits_0",
        }
        self._log(
            "HONEY DIRECT PATH: save RAM proves Honey active slot 1; "
            "field-controller scan bypassed; no navigation authorized"
        )

        def _honey_probe(tag):
            snap = read_items_pocket(br)
            qty = self._honey_quantity(snap)
            vals = {}
            for name, addr in (
                ("battle", int(core.BATTLE_ADDR)),
                ("flow", int(getattr(core, "FLOW_ADDR", 0x081FB390))),
                ("state710", 0x081FB710),
                ("state714", 0x081FB714),
                ("state718", 0x081FB718),
                ("state7A0", 0x081FB7A0),
                ("state7F0", 0x081FB7F0),
            ):
                try:
                    vals[name] = int(br.u32(addr))
                except Exception:
                    vals[name] = None
            try:
                vals["ui78"] = int(br.u16(0x08C41778))
                vals["ui7A"] = int(br.u16(0x08C4177A))
            except Exception:
                vals["ui78"] = None
                vals["ui7A"] = None
            self._log(
                f"HONEY PROBE {tag}: qty={qty} "
                + " ".join(
                    (
                        f"{k}={'NA' if v is None else f'0x{v:04X}'}"
                        if k in {"ui78", "ui7A"}
                        else f"{k}={'NA' if v is None else f'0x{v:08X}'}"
                    )
                    for k, v in vals.items()
                )
            )
            return qty, vals

        _honey_probe("PRE_A1")

        # User-hardware-confirmed Honey choreography: A1 selects Honey and opens
        # the action menu; A2 activates Use.  Firmware completion is acknowledged
        # for each pulse and inputs are never replayed.
        self._horde_hid(br, core, 0, label="HONEY A1 SELECT", hold_ms=90, settle_ms=650)
        qty_after_a1, a1_state = _honey_probe("POST_A1")

        # If A1 unexpectedly consumed Honey/started battle, do not send A2. This
        # makes the direct path tolerant of a UI variant while still forbidding a
        # duplicate item-use input.
        if br.u32(core.BATTLE_ADDR) == core.BATTLE_ACTIVE:
            expected = int(before_qty) - 1
            if qty_after_a1 != expected:
                raise RuntimeError(
                    "HONEY HORDE: battle became active after A1 but Honey decrement "
                    f"was not exactly one; before={before_qty} after={qty_after_a1}"
                )
            self.honey_inventory = {
                **dict(self.honey_inventory or {}),
                "quantity": int(qty_after_a1),
            }
            self._log(
                f"HONEY HORDE PASS: battle active after A1 and Honey decremented "
                f"exactly once ({before_qty}->{qty_after_a1}); A2 suppressed"
            )
            return {
                "encounter": True,
                "mode": "honey_direct_a1",
                "honey_before": int(before_qty),
                "honey_after": int(qty_after_a1),
                "honey_active_index": 0,
                "bag_controller": self.honey_controller,
            }

        if qty_after_a1 != int(before_qty):
            raise RuntimeError(
                "HONEY HORDE: inventory changed after A1 before battle; A2 suppressed. "
                f"before={before_qty} after_a1={qty_after_a1}"
            )

        # Hardware DZ cycle-2 proof: A1 successfully opened the Use action menu,
        # but an A2 issued about half a second later could be acknowledged by the
        # input firmware before ORAS was ready to consume it.  Give the field Bag
        # action menu a host-side readiness dwell before the one authorized A2.
        # Do not replay A2 if it is ignored.
        self._log("HONEY A2 READINESS: waiting 1.25 s after A1 before Use")
        self._sleep_stop_aware(1.25)
        self._horde_hid(br, core, 0, label="HONEY A2 USE", hold_ms=90, settle_ms=180)
        qty_post_a2, _ = _honey_probe("POST_A2")

        # Hardware EA proof: an acknowledged A2 can still be ignored by ORAS on
        # a later Honey cycle.  Honey consumption itself is a safe readiness
        # authority: successful Use decrements the save-backed quantity quickly.
        # Observe before authorizing exactly one retry; never replay if quantity
        # changed or battle RAM became active.
        expected = int(before_qty) - 1
        retry_needed = True
        observe_deadline = time.monotonic() + 1.80
        last_qty = int(qty_post_a2)
        while time.monotonic() < observe_deadline:
            self._check_stop()
            if br.u32(core.BATTLE_ADDR) == core.BATTLE_ACTIVE:
                retry_needed = False
                self._log("HONEY A2 RETRY SUPPRESSED: battle became active after first Use")
                break
            current = read_items_pocket(br)
            last_qty = self._honey_quantity(current)
            if last_qty == expected:
                retry_needed = False
                self._log(
                    f"HONEY A2 RETRY SUPPRESSED: Honey consumption observed "
                    f"({before_qty}->{last_qty}) after first Use"
                )
                break
            if last_qty != int(before_qty):
                raise RuntimeError(
                    "HONEY HORDE: unexpected Honey quantity after A2; retry suppressed. "
                    f"before={before_qty} current={last_qty} expected={expected}"
                )
            self._sleep_stop_aware(0.10)

        if retry_needed:
            self._log(
                "HONEY A2 RETRY AUTHORIZED: first Use was acknowledged but Honey quantity "
                "and battle RAM remained unchanged for 1.80 s"
            )
            self._sleep_stop_aware(0.75)
            # One final guard immediately before the retry.
            guard_qty = self._honey_quantity(read_items_pocket(br))
            if br.u32(core.BATTLE_ADDR) == core.BATTLE_ACTIVE or guard_qty == expected:
                self._log(
                    "HONEY A2 RETRY SUPPRESSED AT FINAL GUARD: "
                    f"battle_active={br.u32(core.BATTLE_ADDR) == core.BATTLE_ACTIVE} "
                    f"qty={guard_qty}"
                )
            elif guard_qty != int(before_qty):
                raise RuntimeError(
                    "HONEY HORDE: unexpected Honey quantity at A2 retry guard; "
                    f"before={before_qty} current={guard_qty} expected={expected}"
                )
            else:
                self._horde_hid(
                    br, core, 0, label="HONEY A2 RETRY USE", hold_ms=110, settle_ms=220
                )
                _honey_probe("POST_A2_RETRY")

        deadline = time.monotonic() + 14.0
        while time.monotonic() < deadline:
            self._check_stop()
            if br.u32(core.BATTLE_ADDR) == core.BATTLE_ACTIVE:
                expected = int(before_qty) - 1
                after_qty = None
                qty_deadline = time.monotonic() + 1.5
                while time.monotonic() < qty_deadline:
                    after = read_items_pocket(br)
                    after_qty = self._honey_quantity(after)
                    if after_qty == expected:
                        break
                    self._sleep_stop_aware(0.10)
                if after_qty != expected:
                    raise RuntimeError(
                        "HONEY HORDE: battle started but inventory decrement was not exactly one; "
                        f"before={before_qty} after={after_qty} expected={expected}"
                    )
                self.honey_inventory = {
                    **dict(self.honey_inventory or {}),
                    "quantity": int(after_qty),
                }
                self._log(
                    "HONEY HORDE PASS: battle active and Honey inventory decremented exactly once "
                    f"({before_qty}->{after_qty}); selected_index=0"
                )
                return {
                    "encounter": True,
                    "mode": "honey_field_bag",
                    "honey_before": int(before_qty),
                    "honey_after": int(after_qty),
                    "honey_active_index": 0,
                    "bag_controller": self.honey_controller,
                }
            time.sleep(0.15)

        raise RuntimeError(
            "HONEY HORDE TIMEOUT: save-proven slot-1 Honey received A1 Select + A2 Use, "
            "but battle-active RAM never appeared. Inputs were not replayed."
        )


    @staticmethod
    def _fishing_control_fields(blob):
        ptr = struct.unpack_from(
            "<I", blob, FISH_ACTION_PTR_ADDR - FISH_CONTROL_ADDR
        )[0]
        active = struct.unpack_from(
            "<I", blob, FISH_ACTION_ACTIVE_ADDR - FISH_CONTROL_ADDR
        )[0]
        return ptr, active

    @staticmethod
    def _fishing_raw_button(core, bit):
        return int(core.HID_NEUTRAL) & ~(1 << int(bit))

    def _fishing_button(self, br, core, bit, hold_ms, settle_ms, label):
        self._check_stop()
        rec = br.hid_pulse_no_retransmit(
            self._fishing_raw_button(core, bit),
            int(hold_ms),
            int(settle_ms),
        )
        if not rec.get("completed"):
            raise core.SafetyHold(
                f"{label} input did not complete; no gameplay replay authorized"
            )
        return rec

    def _fishing_sample(self, br, core, *, focus=True):
        battle = br.u32(core.BATTLE_ADDR, retries=1)
        ctrl = br.read(FISH_CONTROL_ADDR, FISH_CONTROL_LEN, retries=1)
        ptr, active = self._fishing_control_fields(ctrl)
        state = None
        wait_cd = None
        bite_cd = None
        if focus:
            fb = br.read(FISH_FOCUS_ADDR, FISH_FOCUS_LEN, retries=1)
            state = fb[FISH_STATE_OFF]
            wait_cd = struct.unpack_from(
                "<I", fb, FISH_WAIT_COUNTDOWN_OFF
            )[0]
            bite_cd = struct.unpack_from(
                "<I", fb, FISH_BITE_COUNTDOWN_OFF
            )[0]
        return {
            "battle": battle,
            "ptr": ptr,
            "active": active,
            "state": state,
            "wait_cd": wait_cd,
            "bite_cd": bite_cd,
        }

    def _wait_for_fishing_field(self, br, core, backend, *, timeout=5.0):
        """Fishing field authority: same zone + battle inactive + action object idle."""
        deadline = time.monotonic() + float(timeout)
        stable = 0
        samples = []
        final_pos = None

        while time.monotonic() < deadline:
            self._check_stop()
            try:
                snap = self._fishing_sample(br, core, focus=False)
                pos = backend.read_position(br)
            except Exception as exc:
                if "UDP timeout" in str(exc):
                    time.sleep(0.08)
                    continue
                raise

            zone = int(pos.get("zone_id", -1))
            samples.append({
                "time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "battle": backend.hx(snap["battle"]),
                "ptr": backend.hx(snap["ptr"]),
                "zone": zone,
                "grid": list(pos.get("grid") or []),
            })

            if (
                snap["battle"] == core.BATTLE_INACTIVE
                and snap["ptr"] not in FISH_BUSY_PTRS
                and zone == int(self.location_zone)
            ):
                stable += 1
                final_pos = pos
                if stable >= 2:
                    return {
                        "ready": True,
                        "status": "FISHING_FIELD_READY",
                        "samples": samples,
                        "final_position": final_pos,
                    }
            else:
                stable = 0

            time.sleep(0.10)

        return {
            "ready": False,
            "status": "FISHING_FIELD_TIMEOUT",
            "samples": samples,
            "final_position": final_pos,
        }

    def _dismiss_fishing_no_bite(self, br, core, backend):
        """Frozen v0p11 no-bite recovery with readiness-gated message dismissal."""

        def field_ready_sample(snap):
            return (
                snap["battle"] == core.BATTLE_INACTIVE
                and snap["ptr"] not in FISH_BUSY_PTRS
            )

        # MESSAGE_PTR usually appears naturally ~0.42 s after state 10.
        message_seen_at = None
        deadline = time.monotonic() + 1.50
        while time.monotonic() < deadline:
            self._check_stop()
            try:
                snap = self._fishing_sample(br, core)
            except Exception as exc:
                if "UDP timeout" in str(exc):
                    time.sleep(0.05)
                    continue
                raise

            if field_ready_sample(snap):
                time.sleep(0.20)
                return {"ready": True, "ack_count": 0, "mode": "field_without_a"}

            if snap["ptr"] == FISH_MESSAGE_PTR:
                message_seen_at = time.monotonic()
                self._log(
                    "FISH NO-BITE: MESSAGE_PTR proven; waiting 600 ms input-readiness settle"
                )
                break
            time.sleep(0.05)

        ack_count = 0
        if message_seen_at is None:
            # One bounded advance A only when MESSAGE_PTR did not appear naturally.
            self._log(
                "FISH NO-BITE: MESSAGE_PTR not natural within 1.50 s; "
                "sending one bounded advance A"
            )
            self._fishing_button(br, core, 0, 65, 60, "Fishing no-bite advance A")
            ack_count += 1

            transition_deadline = time.monotonic() + 1.50
            while time.monotonic() < transition_deadline:
                self._check_stop()
                try:
                    snap = self._fishing_sample(br, core)
                except Exception as exc:
                    if "UDP timeout" in str(exc):
                        time.sleep(0.05)
                        continue
                    raise

                if field_ready_sample(snap):
                    time.sleep(0.20)
                    return {
                        "ready": True,
                        "ack_count": ack_count,
                        "mode": "field_after_advance_a",
                    }
                if snap["ptr"] == FISH_MESSAGE_PTR:
                    message_seen_at = time.monotonic()
                    self._log(
                        "FISH NO-BITE: MESSAGE_PTR proven after advance A; "
                        "waiting 600 ms input-readiness settle"
                    )
                    break
                time.sleep(0.05)

            if message_seen_at is None:
                raise backend.IntegrationHold(
                    "Fishing no-bite result did not reach MESSAGE_PTR or field-ready"
                )

        # MESSAGE_PTR appearing is earlier than textbox input-readiness.
        settle_deadline = message_seen_at + FISH_MESSAGE_READY_SETTLE_S
        while time.monotonic() < settle_deadline:
            self._check_stop()
            try:
                snap = self._fishing_sample(br, core)
            except Exception as exc:
                if "UDP timeout" in str(exc):
                    time.sleep(0.05)
                    continue
                raise

            if field_ready_sample(snap):
                time.sleep(0.20)
                return {
                    "ready": True,
                    "ack_count": ack_count,
                    "mode": "field_during_message_settle",
                }
            if snap["ptr"] != FISH_MESSAGE_PTR:
                raise backend.IntegrationHold(
                    "Fishing no-bite MESSAGE_PTR changed before readiness settle"
                )
            time.sleep(0.05)

        snap = self._fishing_sample(br, core)
        if (
            snap["battle"] != core.BATTLE_INACTIVE
            or snap["ptr"] != FISH_MESSAGE_PTR
        ):
            raise backend.IntegrationHold(
                "Fishing MESSAGE_PTR was not still proven at the dismiss edge"
            )

        self._log(
            "FISH NO-BITE: MESSAGE_PTR stable >=600 ms; sending dismiss A"
        )
        self._fishing_button(br, core, 0, 65, 60, "Fishing no-bite dismiss A")
        ack_count += 1

        # One retry is allowed only if the exact same message pointer remains.
        observe_deadline = time.monotonic() + FISH_MESSAGE_RETRY_OBSERVE_S
        while time.monotonic() < observe_deadline:
            self._check_stop()
            try:
                snap = self._fishing_sample(br, core)
            except Exception as exc:
                if "UDP timeout" in str(exc):
                    time.sleep(0.05)
                    continue
                raise
            if field_ready_sample(snap):
                time.sleep(0.20)
                return {
                    "ready": True,
                    "ack_count": ack_count,
                    "mode": "field_after_dismiss_a1",
                }
            if snap["ptr"] != FISH_MESSAGE_PTR:
                break
            time.sleep(0.05)

        snap = self._fishing_sample(br, core)
        if (
            snap["battle"] == core.BATTLE_INACTIVE
            and snap["ptr"] == FISH_MESSAGE_PTR
        ):
            self._log(
                "FISH NO-BITE: MESSAGE_PTR still proven 700 ms after A1; "
                "sending one gated retry A"
            )
            self._fishing_button(br, core, 0, 65, 60, "Fishing no-bite retry A")
            ack_count += 1

        field = self._wait_for_fishing_field(
            br, core, backend, timeout=4.5
        )
        if not field.get("ready"):
            raise backend.IntegrationHold(
                "Fishing no-bite message remained unresolved after readiness-gated dismissal"
            )
        return {
            "ready": True,
            "ack_count": ack_count,
            "mode": "field_after_message_recovery",
            "field": field,
        }

    def _trigger_fishing_until_encounter(self, br, core, backend):
        """Production fishing trigger; loops through no-bites until a battle begins."""
        casts = 0
        no_bites = 0

        while True:
            self._check_stop()

            field = self._wait_for_fishing_field(
                br, core, backend, timeout=4.0
            )
            if not field.get("ready"):
                raise backend.IntegrationHold(
                    "Fishing pre-cast field authority failed: "
                    + str(field.get("status"))
                )

            if self.fishing_chain is not None:
                pos = field.get("final_position") or {}
                event = self.fishing_chain.observe_anchor(
                    pos.get("zone_id", self.location_zone), pos.get("grid")
                )
                if event and event.get("event") == "ANCHOR_CHANGED":
                    self._log(
                        "CHAIN FISHING RESET: fishing spot changed "
                        f"{event.get('old')} -> {event.get('new')}"
                    )
                self.fishing_chain.record_cast()
                self._sync_fishing_chain_stats()

            casts += 1
            br.release_all()
            self._log(f"FISH CAST #{casts}: Y registered-item pulse")
            self._fishing_button(
                br, core, 11,
                FISH_CAST_HOLD_MS, FISH_CAST_SETTLE_MS,
                "Fishing cast Y",
            )
            cast_started = time.monotonic()

            # v0p8/v0p11 hardware proof: avoid needless RAM traffic during
            # the initial rod animation.
            while time.monotonic() - cast_started < 2.20:
                self._check_stop()
                time.sleep(0.05)

            saw_state4 = False
            last_state = None
            cached_ptr = FISH_WAIT_PTR
            last_ptr_check = 0.0
            deadline = cast_started + 8.20

            while time.monotonic() < deadline:
                self._check_stop()
                loop_started = time.monotonic()

                try:
                    fb = br.read(FISH_FOCUS_ADDR, FISH_FOCUS_LEN, retries=1)
                except Exception as exc:
                    if "UDP timeout" in str(exc):
                        time.sleep(0.02)
                        continue
                    raise

                state = fb[FISH_STATE_OFF]
                wait_cd = struct.unpack_from(
                    "<I", fb, FISH_WAIT_COUNTDOWN_OFF
                )[0]
                bite_cd = struct.unpack_from(
                    "<I", fb, FISH_BITE_COUNTDOWN_OFF
                )[0]

                now = time.monotonic()
                if now - last_ptr_check >= 0.20:
                    try:
                        ctrl = br.read(
                            FISH_CONTROL_ADDR, FISH_CONTROL_LEN, retries=1
                        )
                        cached_ptr, _ = self._fishing_control_fields(ctrl)
                    except Exception as exc:
                        if "UDP timeout" not in str(exc):
                            raise
                    last_ptr_check = now

                if state == 4:
                    saw_state4 = True

                if state != last_state:
                    self._log(
                        "FISH STATE "
                        f"{state}({FISH_STATE_NAMES.get(state, 'OTHER')}) "
                        f"ptr={backend.hx(cached_ptr)} "
                        f"wait_cd={wait_cd} bite_cd={bite_cd}"
                    )
                    last_state = state

                trigger_reason = None
                if saw_state4 and cached_ptr == FISH_WAIT_PTR and state == 5:
                    trigger_reason = "STATE5_FIRST_READ"
                elif saw_state4 and cached_ptr == FISH_WAIT_PTR and state == 6:
                    trigger_reason = "STATE6_FIRST_READ_FALLBACK"

                if trigger_reason:
                    trigger_ms = (time.monotonic() - cast_started) * 1000.0
                    self._log(
                        f"FISH REEL: {trigger_reason} @ {trigger_ms:.1f} ms "
                        "-> immediate A"
                    )
                    self._fishing_button(
                        br, core, 0,
                        FISH_REEL_A1_HOLD_MS, FISH_REEL_A1_SETTLE_MS,
                        "Fishing reel A1",
                    )

                    second_used = False
                    post_deadline = time.monotonic() + 4.0
                    while time.monotonic() < post_deadline:
                        self._check_stop()
                        try:
                            snap = self._fishing_sample(br, core)
                        except Exception as exc:
                            if "UDP timeout" in str(exc):
                                time.sleep(0.02)
                                continue
                            raise

                        if (
                            not second_used
                            and snap["ptr"] == FISH_WAIT_PTR
                            and snap["state"] in (5, 6)
                            and ((time.monotonic() - cast_started) * 1000.0 - trigger_ms)
                                >= FISH_REEL_SECOND_EDGE_AFTER_MS
                        ):
                            second_used = True
                            self._log(
                                "FISH REEL: bounded second A edge "
                                "(same v0p8/v0p11 rule)"
                            )
                            self._fishing_button(
                                br, core, 0,
                                FISH_REEL_A2_HOLD_MS, FISH_REEL_A2_SETTLE_MS,
                                "Fishing reel A2",
                            )

                        if snap["battle"] == core.BATTLE_ACTIVE:
                            chain_snap = None
                            if self.fishing_chain is not None:
                                hook_event = self.fishing_chain.record_hooked_encounter()
                                chain_snap = self._sync_fishing_chain_stats()
                                odds = hook_event.get("encounter_odds") or {}
                                next_odds = hook_event.get("next_odds") or {}
                                self._log(
                                    "CHAIN FISHING HOOK: "
                                    f"chain={chain_snap.get('chain')} "
                                    f"encounter_rolls={odds.get('rolls')} "
                                    f"next_rolls={next_odds.get('rolls')} "
                                    f"peak={chain_snap.get('peak_chain')}"
                                )
                            self._log(
                                f"FISH BATTLE ACTIVE: casts={casts} no_bites={no_bites}"
                            )
                            return {
                                "encounter": True,
                                "mode": "fishing",
                                "casts": casts,
                                "no_bites": no_bites,
                                "reel_reason": trigger_reason,
                                "second_reel_edge": second_used,
                                "fishing_chain": chain_snap,
                                "fishing_encounter_odds": dict(odds),
                            }

                        time.sleep(0.012)

                    if self.fishing_chain is not None:
                        self.fishing_chain.record_missed_hook("REEL_TRIGGERED_NO_BATTLE")
                        self._sync_fishing_chain_stats()
                    raise backend.IntegrationHold(
                        "Fishing reel triggered but battle did not become active"
                    )

                if state == 10:
                    no_bites += 1
                    if self.fishing_chain is not None:
                        chain_event = self.fishing_chain.record_no_bite()
                        self._sync_fishing_chain_stats()
                        self._log(
                            "CHAIN FISHING RESET: NO_BITE "
                            f"previous_chain={chain_event.get('previous_chain')}"
                        )
                    self._log(
                        f"FISH NO-BITE #{no_bites}: state10 proven; NO reel A"
                    )
                    rec = self._dismiss_fishing_no_bite(
                        br, core, backend
                    )
                    self._log(
                        "FISH NO-BITE RECOVERY PASS: "
                        f"ack_count={rec.get('ack_count')} mode={rec.get('mode')} "
                        "-> automatic recast"
                    )
                    break

                if saw_state4 and cached_ptr != FISH_WAIT_PTR:
                    if self.fishing_chain is not None:
                        self.fishing_chain.record_missed_hook("ACTION_POINTER_LEFT_WAIT_UNRESOLVED")
                        self._sync_fishing_chain_stats()
                    raise backend.IntegrationHold(
                        "Fishing action pointer left WAIT before a resolved "
                        f"bite/no-bite: {backend.hx(cached_ptr)}"
                    )

                poll_ms = 5.0 if state == 4 and wait_cd <= 8 else 20.0
                elapsed = time.monotonic() - loop_started
                time.sleep(max(0.0, poll_ms / 1000.0 - elapsed))
            else:
                if self.fishing_chain is not None:
                    self.fishing_chain.record_missed_hook("CAST_TIMEOUT_BEFORE_RESOLUTION")
                    self._sync_fishing_chain_stats()
                raise backend.IntegrationHold(
                    "Fishing cast timed out before state 5/6/10 resolution"
                )

    def _wait_post_capture_field_authority(self, br, core, backend, movement, anchor_grid):
        """Require the same method-specific field proof used after ordinary Run."""
        if self.method_key in {"cave", "cave_run", "cave_bunny"}:
            field_authority = wait_for_cave_field(
                br,
                backend,
                self.location_zone,
                movement.get("safe_return_grids") or [],
            )
            if not field_authority.get("ready"):
                raise backend.IntegrationHold(
                    "post-capture Cave field authority failed: "
                    + str(field_authority.get("status"))
                )
            field_pos = field_authority["final_position"]
            field_grid = require_cave_position(
                backend, field_pos, self.location_zone, "post-capture Cave field"
            )
            if self.method_key == "cave_bunny" and tuple(field_grid) != tuple(anchor_grid):
                raise backend.IntegrationHold(
                    f"Cave Acro post-capture anchor changed from {list(anchor_grid)} "
                    f"to {list(field_grid)}"
                )
            if self.method_key in {"cave_run", "cave_bunny"}:
                br.release_all()
                time.sleep(1.00)
                self._check_stop()
                rearm = read_stable_cave_position(
                    br, backend, self.location_zone,
                    "Cave post-capture re-arm",
                    timeout=2.50, required_samples=2, poll_sec=0.10,
                )
                rearm_grid = tuple(rearm["grid"])
                if br.u32(core.BATTLE_ADDR) != core.BATTLE_INACTIVE:
                    raise backend.IntegrationHold(
                        "Cave post-capture re-arm did not retain battle-inactive field"
                    )
                expected = tuple(anchor_grid) if self.method_key == "cave_bunny" else tuple(field_grid)
                if rearm_grid != expected:
                    raise backend.IntegrationHold(
                        f"Cave post-capture re-arm grid changed: {list(expected)} -> {list(rearm_grid)}"
                    )
                field_pos = rearm["position"]
            return field_pos

        if self.method_key == "surf":
            field_authority = wait_for_surf_field(
                br,
                backend,
                self.location_zone,
                movement.get("safe_return_grids") or [],
            )
            if not field_authority.get("ready"):
                raise backend.IntegrationHold(
                    "post-capture Surf/Ocean field authority failed: "
                    + str(field_authority.get("status"))
                )
            field_pos = field_authority["final_position"]
            require_water_position(
                backend, field_pos, self.location_zone, "post-capture Surf/Ocean field"
            )
            return field_pos

        if self.method_key == "fishing":
            field_authority = self._wait_for_fishing_field(
                br, core, backend, timeout=5.0
            )
            if not field_authority.get("ready"):
                raise backend.IntegrationHold(
                    "post-capture Fishing field authority failed: "
                    + str(field_authority.get("status"))
                )
            return field_authority["final_position"]

        field_authority = backend.wait_for_field_authority(br)
        if not field_authority.get("ready"):
            raise backend.IntegrationHold(
                "post-capture field authority failed: "
                + str(field_authority.get("status"))
            )
        field_pos = field_authority["final_position"]
        backend.require_safe_position(field_pos, "post-capture field")
        if self.method_key == "acro_bunny":
            if not field_pos["mask"]["core2"]:
                raise backend.IntegrationHold(
                    "Acro post-capture tile is no longer interior/core2 grass"
                )
            if tuple(field_pos["grid"]) != tuple(anchor_grid):
                raise backend.IntegrationHold(
                    f"Acro post-capture drifted from anchor {list(anchor_grid)} "
                    f"to {field_pos['grid']}"
                )
        return field_pos

    @staticmethod
    def _encode_touch_xy(x, y):
        x = max(0, min(319, int(x)))
        y = max(0, min(239, int(y)))
        xr = int(x * 4095.0 / 320.0) & 0xFFF
        yr = int(y * 4095.0 / 240.0) & 0xFFF
        return 0x01000000 | (yr << 12) | xr

    def _save_after_shiny_capture(self, br, core, backend, movement, anchor_grid):
        """Save through the overworld PlayNav control before re-arming Run."""
        x, y = get_save_menu_xy(self.base_dir)
        self._check_stop()
        self._log(f"SHINY SAVE: opening PlayNav save control at ({x},{y})")
        touch = br.touch_pulse_no_retransmit(
            self._encode_touch_xy(x, y),
            SAVE_MENU_TOUCH_HOLD_MS,
            SAVE_MENU_TOUCH_SETTLE_MS,
        )
        if not touch.get("completed"):
            raise backend.IntegrationHold(
                "PlayNav save-control touch did not complete"
            )
        self._check_stop()
        raw_a = int(core.HID_NEUTRAL) & ~(1 << 0)
        save_press = br.hid_pulse_no_retransmit(
            raw_a, SAVE_MENU_A_HOLD_MS, SAVE_MENU_A_SETTLE_MS
        )
        if not save_press.get("completed"):
            raise backend.IntegrationHold("PlayNav save A did not complete")
        field_pos = self._wait_post_capture_field_authority(
            br, core, backend, movement, anchor_grid
        )
        self._log(
            "SHINY SAVE PASS: game save command completed and field authority "
            f"restored at grid {field_pos.get('grid')}"
        )
        return {
            "touch": touch,
            "a": save_press,
            "screen_xy": [x, y],
            "field_grid": list(field_pos.get("grid") or []),
        }

    def _run_horde_auto_attack_test(self, br, core, opponent_set, payloads):
        """Run one production-selected attack on a proven ordinary Horde.

        The one-shot arm is consumed before any input. Any real shiny prevents
        entry at the caller and the lower-level validator independently refuses
        every attack if a shiny is present.
        """
        self.horde_auto_attack_test_pending = False
        self.status.emit(
            "RUNNING",
            "HORDE AUTO-ATTACK TEST • zero shinies proven • selecting one live damaging move",
        )
        self.events.append({
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "type": "HORDE_AUTO_ATTACK_TEST_START",
            "horde_species": [p.get("species_name") for p in payloads],
            "is_shiny": False,
        })
        report = None
        try:
            # Refresh immediately before the test so move order and current PP
            # reflect the lead now, not only the hunt-start snapshot.
            live_lead = self.horde_lead_pk6
            try:
                snap = get_runtime_party_snapshot(
                    self.host, int(getattr(br, "port", 4952)), br
                )
                ordered = list(snap.get("ordered_parsed") or [])
                if ordered and snap.get("reorder_proven") and ordered[0].get("valid") and ordered[0].get("checksum_valid"):
                    live_lead = dict(ordered[0])
                    self.horde_lead_pk6 = dict(ordered[0])
                    self._log(
                        "HORDE AUTO-ATTACK TEST LEAD REFRESH: "
                        f"{live_lead.get('species_name')} moves={live_lead.get('moves')} "
                        f"PP={live_lead.get('move_pp')} source={snap.get('source')}"
                    )
            except Exception as exc:
                self._log(
                    "HORDE AUTO-ATTACK TEST LEAD REFRESH FAILED: "
                    f"{type(exc).__name__}: {exc}; using hunt-start authoritative lead snapshot"
                )

            if not self.horde_move_db:
                self.horde_move_db = ensure_move_metadata(self.base_dir, timeout=3.0)

            report = live_nonshiny_horde_auto_attack_test(
                core,
                br,
                opponent_set,
                lead_pk6=live_lead,
                move_db=self.horde_move_db,
                base_dir=self.base_dir,
                check_stop=self._check_stop,
                log=self._log,
            )
            self.last_horde_auto_attack_test = dict(report)
            selected = dict(report.get("selected_attack") or {})
            resolution = dict(report.get("attack_resolution") or {})
            self.events.append({
                "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                "type": "HORDE_AUTO_ATTACK_TEST_PASS",
                "move_id": selected.get("move_id"),
                "move_name": selected.get("name"),
                "move_slot": selected.get("move_slot"),
                "resolution": resolution.get("status"),
                "is_shiny": False,
            })
            self._log(
                "HORDE AUTO-ATTACK TEST COMPLETE: "
                f"selected {selected.get('name')} from slot {selected.get('move_slot')} • "
                f"{resolution.get('status')} • normal Horde escape resumes"
            )
            self.status.emit(
                "RUNNING",
                f"HORDE AUTO-ATTACK TEST PASS • {selected.get('name')} slot {selected.get('move_slot')} • escaping normally",
            )
            return True
        except UserStop:
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            preserved = dict(report or {})
            preserved.update({
                "result": "SAFETY_HOLD",
                "error": error,
                "test_mode": True,
                "test_target_was_shiny": False,
            })
            self.last_horde_auto_attack_test = preserved
            self.events.append({
                "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                "type": "HORDE_AUTO_ATTACK_TEST_SAFETY_HOLD",
                "error": error,
                "is_shiny": False,
            })
            self._log(
                "HORDE AUTO-ATTACK TEST STOPPED SAFELY: "
                f"{error}; no further test attack is authorized"
            )
            raise core.SafetyHold(
                "HORDE AUTO-ATTACK TEST SAFETY HOLD: " + error
            )

    def _run_single_auto_capture_test(
        self, br, core, backend, opponent_set, payload, movement, anchor_grid,
        encounter_started_mono, game_profile
    ):
        """Exercise the real single-battle capture path without inventing a shiny.

        The caller only enters this for a checksum-valid non-shiny single encounter.
        `is_shiny` is never modified, so normal encounter accounting remains non-shiny.
        The one-shot arm is consumed before any controller input is sent.
        """
        self.auto_capture_test_pending = False
        target = dict(payload)
        target["auto_capture_test"] = True
        self._log(
            "AUTO-CAPTURE TEST START: "
            f"{target.get('species_name')} EC={target.get('ec')} PID={target.get('pokemon_pid')} "
            f"Ball={self.capture_ball_override}; shiny flag remains {bool(target.get('is_shiny'))}"
        )
        self.status.emit(
            "RUNNING",
            f"AUTO-CAPTURE TEST • catching non-shiny {target.get('species_name')} through real capture state machine",
        )
        self.events.append({
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "type": "AUTO_CAPTURE_TEST_START",
            "species": target.get("species_name"),
            "pokemon_pid": target.get("pokemon_pid"),
            "ec": target.get("ec"),
            "is_shiny": False,
            "ball_override": self.capture_ball_override,
        })
        report = None
        pre_capture_party_count = self._pre_capture_party_count_authority(br)
        try:
            report = run_shiny_auto_capture(
                core,
                br,
                opponent_set=opponent_set,
                shiny=target,
                horde_size=1,
                method_key=self.method_key,
                environment=target.get("environment", ""),
                ball_override=self.capture_ball_override,
                lead_pk6=None,
                move_db=None,
                base_dir=self.base_dir,
                check_stop=self._check_stop,
                log=self._log,
                game_key=game_profile["key"],
            )
            if report.get("result") != "CAPTURED":
                raise backend.IntegrationHold(
                    "Auto-Capture Test did not produce a confirmed capture: "
                    + str(report.get("result"))
                )

            self.status.emit(
                "RUNNING",
                f"AUTO-CAPTURE TEST • {target.get('species_name')} captured • resolving post-catch screens",
            )
            post_capture = clear_captured_shiny_post_capture(
                br, core, check_stop=self._check_stop, log=self._log,
                pre_capture_party_count=pre_capture_party_count,
            )
            report["post_capture"] = post_capture
            if post_capture.get("result") not in {
                "POST_CAPTURE_RAM_RECOVERY_COMPLETE",
                "POST_CAPTURE_RECOVERY_COMPLETE_POKEDEX_NOT_OBSERVED",
            }:
                raise backend.IntegrationHold(
                    "Auto-Capture Test post-capture RAM authority failed: "
                    + str(post_capture.get("result"))
                )
            field_pos = self._wait_post_capture_field_authority(
                br, core, backend, movement, anchor_grid
            )
            report["post_capture_field_grid"] = list(field_pos.get("grid") or [])
            report["test_mode"] = True
            report["test_target_was_shiny"] = False
            self.last_auto_capture_test = dict(report)

            self._post_battle_pokerus_check(br)
            self.species_counts[int(target["species"])] += 1
            self.pokemon_seen_session += 1
            duration = time.monotonic() - encounter_started_mono
            recorded = dict(target)
            recorded["auto_capture_test"] = {
                "result": "CAPTURED",
                "throw_count": report.get("throw_count"),
                "post_capture": post_capture.get("result"),
            }
            self._record_encounter(
                recorded, field_pos=field_pos, duration_s=duration
            )
            self.last_encounter = {
                **recorded,
                "horde_size": 1,
                "result": "AUTO_CAPTURE_TEST_CAPTURED",
                "throw_count": report.get("throw_count"),
                "post_capture_grid": list(field_pos.get("grid") or []),
                "pokedex_ram_observed": bool(post_capture.get("pokedex_observed")),
                "pokedex_ram_cleared": bool(post_capture.get("pokedex_cleared")),
            }
            self.events.append({
                "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                "type": "AUTO_CAPTURE_TEST_PASS",
                "species": target.get("species_name"),
                "pokemon_pid": target.get("pokemon_pid"),
                "throw_count": report.get("throw_count"),
                "pokedex_ram_observed": bool(post_capture.get("pokedex_observed")),
                "pokedex_ram_cleared": bool(post_capture.get("pokedex_cleared")),
                "is_shiny": False,
            })
            self._log(
                "AUTO-CAPTURE TEST PASS: non-shiny "
                f"{target.get('species_name')} captured after {report.get('throw_count')} Ball(s); "
                f"field authority restored at {field_pos.get('grid')}"
            )
            self.status.emit(
                "RUNNING",
                (
                    ("Fishing • " if self.method_key == "fishing" else "Wild • ")
                    + (f"{self.target_species_name} • " if self.target_species_name else "")
                    + f"{self.location_name} • {self.method_meta['name']} • Auto-Capture TEST passed"
                ),
            )
            return True
        except UserStop:
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            preserved = dict(report or {})
            preserved.update({
                "result": "SAFETY_HOLD",
                "error": error,
                "test_mode": True,
                "test_target_was_shiny": False,
            })
            self.last_auto_capture_test = preserved
            self.last_encounter = {
                **target,
                "horde_size": 1,
                "result": "AUTO_CAPTURE_TEST_SAFETY_HOLD",
                "auto_capture_test_error": error,
            }
            self.events.append({
                "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                "type": "AUTO_CAPTURE_TEST_SAFETY_HOLD",
                "species": target.get("species_name"),
                "pokemon_pid": target.get("pokemon_pid"),
                "error": error,
                "is_shiny": False,
            })
            self._log(
                "AUTO-CAPTURE TEST STOPPED SAFELY: "
                f"{error}; one-shot arm consumed and no further input authorized"
            )
            raise core.SafetyHold(
                f"AUTO-CAPTURE TEST SAFETY HOLD on non-shiny {target.get('species_name')}: {error}"
            )

    @staticmethod
    def _sweet_scent_touch_state(x: int, y: int) -> int:
        # ORAS/native 3DS touch encoding. Integer division is floor() for the
        # positive 320x240 screen coordinates used here.
        xr = (int(x) * 4095) // 320
        yr = (int(y) * 4095) // 240
        return 0x01000000 | ((yr & 0xFFF) << 12) | (xr & 0xFFF)

    def _sweet_scent_touch(self, br, xy, *, label, after=0.0):
        self._check_stop()
        x, y = (int(xy[0]), int(xy[1]))
        touch_state = self._sweet_scent_touch_state(x, y)
        rec = br.touch_pulse_no_retransmit(touch_state, 120, 140)
        self._log(
            f"AUTO SWEET SCENT TOUCH: {label} xy=({x},{y}) "
            f"touch_state=0x{touch_state:08X} completed={bool(rec.get('completed'))}"
        )
        if not rec.get("completed"):
            raise RuntimeError(
                f"AUTO SWEET SCENT: {label} touch did not complete; "
                "touch was not retransmitted and no fallback input is authorized"
            )
        if after:
            self._sleep_stop_aware(after)
        return {
            "label": label,
            "screen_xy": [x, y],
            "touch_state": f"0x{touch_state:08X}",
            "completed": True,
        }

    def _trigger_auto_sweet_scent(self, br, core, backend, anchor_grid):
        """Trigger Sweet Scent with the previously hardware-proven touch route.

        D25 keeps the proven touchscreen route and resolves the
        Sweet Scent user from live party RAM:
          - green Poké Ball / Pokémon shortcut: (60, 230)
          - silver/grey field-action icon for the resolved visible party slot
          - Sweet Scent: (170, 104)

        Slot 1 remains hardware-proven at (119,55). D25 supplies layout-derived
        defaults for slots 2-6; a saved read-only calibration overrides them.

        Hardware tuning retained from the earlier Horde work:
          - 1.75 s for the party screen to open
          - 0.75 s from the silver field-action icon to the Sweet Scent choice
          - (60,230), not the older higher touch, to avoid the Town Map hit

        Every touch uses the acknowledged native 4952 touchscreen path exactly
        once and is never retransmitted. Battle-active RAM remains the final
        authority. If no Horde starts, no touch sequence is replayed.
        """
        self._check_stop()
        if br.u32(core.BATTLE_ADDR) != core.BATTLE_INACTIVE:
            raise RuntimeError("AUTO SWEET SCENT: expected overworld before touch input")

        pos = backend.read_position(br)
        self._require_stationary_horde_position(pos, "AUTO SWEET SCENT pre-input")
        if tuple(pos.get("grid") or ()) != tuple(anchor_grid):
            raise RuntimeError(
                f"AUTO SWEET SCENT: player moved from armed tile {list(anchor_grid)} "
                f"to {pos.get('grid')}"
            )

        self.status.emit(
            "RUNNING",
            f"Horde • {self.location_name} • using Sweet Scent automatically",
        )

        # Keep the already-proven repeated-Horde field re-arm. Battle/position
        # authority can return before ORAS' bottom-screen field UI accepts the
        # next interaction. This wait prevents an input from landing on a stale UI.
        if self.horde_battles > 0:
            self._log(
                "AUTO SWEET SCENT RE-ARM: waiting 1.00 s after prior Horde "
                "field return before touchscreen route"
            )
            self._sleep_stop_aware(1.00)
            if br.u32(core.BATTLE_ADDR) != core.BATTLE_INACTIVE:
                raise RuntimeError(
                    "AUTO SWEET SCENT RE-ARM: battle became active before touch input"
                )
            rearm_pos = backend.read_position(br)
            self._require_stationary_horde_position(
                rearm_pos, "AUTO SWEET SCENT re-arm"
            )
            if tuple(rearm_pos.get("grid") or ()) != tuple(anchor_grid):
                raise RuntimeError(
                    f"AUTO SWEET SCENT RE-ARM: player moved from armed tile "
                    f"{list(anchor_grid)} to {rearm_pos.get('grid')}"
                )
            self._log(
                "AUTO SWEET SCENT RE-ARM: PASS — battle inactive and anchor "
                f"grid {rearm_pos.get('grid')} retained"
            )

        visible_slot = int(self.sweet_scent_visible_slot or 0)
        field_xy = get_party_field_action_xy(self.base_dir, visible_slot)
        if visible_slot < 1 or field_xy is None:
            raise RuntimeError(
                "AUTO SWEET SCENT: live Sweet Scent visible slot/touch profile was not armed by preflight"
            )
        route = [
            ("GREEN_POKEBALL_PARTY", (60, 230), 1.75),
            (f"SLOT{visible_slot}_SILVER_FIELD_ACTION", field_xy, 0.75),
            ("SWEET_SCENT", (170, 104), 0.10),
        ]
        self._log(
            "AUTO SWEET SCENT D25 TOUCH ROUTE: "
            f"green Poké Ball (60,230) -> silver visible-slot-{visible_slot} field icon {field_xy} -> "
            "Sweet Scent (170,104)"
        )

        events = []
        for label, xy, after in route:
            events.append(
                self._sweet_scent_touch(br, xy, label=label, after=after)
            )

        # Battle RAM, not timing or firmware ACK, decides whether Sweet Scent
        # actually produced the encounter. No sequence replay is allowed.
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            self._check_stop()
            state = br.u32(core.BATTLE_ADDR)
            if state == core.BATTLE_ACTIVE:
                self._log("AUTO SWEET SCENT: battle active — Horde boundary acquired")
                return {
                    "encounter": True,
                    "mode": "auto_sweet_scent_touchscreen",
                    "touch_route": events,
                    "menu_sequence": [],
                }
            time.sleep(0.15)

        raise RuntimeError(
            "AUTO SWEET SCENT TIMEOUT: no battle became active after the D25 "
            "touchscreen route. Touches were not replayed. Verify the live Sweet Scent "
            "party slot/touch profile and export support."
        )


    def _run_xy_normal_wild_main(self, real_br, game_profile, core):
        """Pokémon Y normal-wild loop with hardware-proven Kalos containment."""
        import math

        if self.method_key != "run":
            raise RuntimeError("XY SAFETY HOLD: only Normal Wild (Run) is enabled")
        if str(game_profile.get("key") or "") != "pokemon_y":
            raise RuntimeError("XY SAFETY HOLD: HF61 containment is certified for Pokémon Y only")

        selected_game = str(self.target_selection.get("game_key") or "")
        if selected_game and selected_game != game_profile["key"]:
            raise RuntimeError(
                f"HUNTS target game mismatch: selected {selected_game}, connected {game_profile['key']}"
            )
        selected_section = str(self.target_selection.get("section_key") or "")
        allowed_land = {
            "grass", "yellow_flowers", "purple_flowers", "red_flowers", "rough_terrain"
        }
        if self.target_selection and selected_section not in allowed_land:
            raise RuntimeError(
                "XY SAFETY HOLD: selected HUNTS table is not an enabled normal-land encounter table"
            )

        self.bridge = real_br
        self.location_name = str(self.target_selection.get("location_name") or "Selected XY area")
        self.location_parent_map = self.target_selection.get("parent_map")
        self.current_world_terrain_name = str(
            self.target_selection.get("environment_name")
            or self.target_selection.get("section_title")
            or "Land"
        )

        db_path = self.base_dir / "pokebot" / "wild" / "world" / "pokemon_y_grass_runtime.sqlite"
        if not db_path.is_file():
            raise RuntimeError("XY SAFETY HOLD: Pokémon Y Kalos runtime terrain database is missing")
        world = WorldMap(db_path)
        terrain = DynamicTerrain(world)

        caps = real_br.input_ping()
        if not caps.get("hid_pulse") or not caps.get("touch_pulse"):
            raise RuntimeError("XY SAFETY HOLD: Pokebot-Luma HID + native touch pulse support is required")

        self.shiny_charm_state = {
            "detected": None,
            "status": "XY_UNVERIFIED",
            "note": "XY Shiny Charm telemetry is not yet RAM-mapped; shiny PK6 detection is unaffected.",
        }
        self.shiny_charm.emit(dict(self.shiny_charm_state))
        self.connection.emit({
            "ram_ready": True,
            "controller_ready": True,
            "game_info": {
                "title_id": f"0x{int(self._wild_bridge_identity['title_id']):016X}",
                "pid": self._wild_bridge_identity["pid"],
                "process_name": self._wild_bridge_identity["process"],
            },
            "game_profile": game_profile,
            "wild_identity_mode": "xy_hardware_proven_wild0_world_contained",
            "input_status": "Pokebot-Luma RAM + Input 4952: Ready • XY HF61 Kalos containment",
            "controller_info": {
                "capabilities": int(caps.get("capability_flags", caps.get("capabilities", 0)) or 0),
                "runtime_flags": int(caps.get("runtime_flags", 0) or 0),
            },
            "shiny_charm": dict(self.shiny_charm_state),
        })

        ctl = AcknowledgedInput(self.host, port=4952, timeout=min(self.timeout, 1.5))
        cro_br = core.Bridge(self.host, timeout=min(self.timeout, 2.0))
        run_touch_state = 0x01EA97FF

        def cro_state():
            self._check_stop()
            found = locate_loaded_modules(cro_br, ("DllField", "DllBattle"))
            return {
                "field": bool(found.get("DllField")),
                "battle": bool(found.get("DllBattle")),
                "field_base": (found.get("DllField") or {}).get("base_hex"),
                "battle_base": (found.get("DllBattle") or {}).get("base_hex"),
            }

        def wait_field(timeout=12.0, stable_samples=2):
            deadline = time.monotonic() + float(timeout)
            stable = 0
            last = {}
            while time.monotonic() < deadline:
                self._check_stop()
                last = cro_state()
                if last["field"] and not last["battle"]:
                    stable += 1
                    if stable >= int(stable_samples):
                        return True, last
                else:
                    stable = 0
                time.sleep(0.20)
            return False, last

        def identity(p):
            if not p or not p.get("valid") or not p.get("checksum_valid"):
                return None
            return (
                str(p.get("ec") or ""),
                str(p.get("pid") or ""),
                int(p.get("species") or 0),
                str(p.get("checksum") or ""),
            )

        def read_live_world(require_encounter=True):
            self._check_stop()
            bx = real_br.read(XY_FIELD_X_ADDR, 4)
            bz = real_br.read(XY_FIELD_Z_ADDR, 4)
            bzone = real_br.read(XY_FIELD_ZONE_ADDR, 2)
            if len(bx) != 4 or len(bz) != 4 or len(bzone) != 2:
                raise RuntimeError("XY SAFETY HOLD: short live field-authority read")
            x = struct.unpack("<f", bx)[0]
            z = struct.unpack("<f", bz)[0]
            zone = struct.unpack("<H", bzone)[0]
            if not math.isfinite(x) or not math.isfinite(z):
                raise RuntimeError(f"XY SAFETY HOLD: invalid live coordinates x={x!r} z={z!r}")

            grid = [nearest_grid(x), nearest_grid(z)]
            resolved = world.resolve(zone, grid)
            if not resolved.get("resolved"):
                raise RuntimeError(
                    f"XY SAFETY HOLD: live world unresolved zone={zone} grid={grid} "
                    f"reason={resolved.get('reason')}"
                )
            if require_encounter and not resolved.get("encounter_terrain"):
                raise RuntimeError(
                    f"XY SAFETY HOLD: live tile is outside encounter terrain "
                    f"{resolved.get('location_name')} zone={zone} grid={grid}"
                )
            terrain.lock_zone(zone)
            return {"x": x, "z": z, "zone": zone, "grid": grid, "resolved": resolved}

        def wait_new_wild(baseline, tid, sid, timeout=0.55):
            deadline = time.monotonic() + float(timeout)
            last = None
            while time.monotonic() < deadline:
                self._check_stop()
                try:
                    p = read_xy_wild_decoded(real_br, 0)
                    last = p
                    ident = identity(p)
                    if ident is not None and ident != baseline:
                        if int(p.get("tid", -1)) != tid or int(p.get("sid", -1)) != sid:
                            return "TRAINER_MISMATCH", p
                        return "VALID", p
                except Exception as exc:
                    last = {"read_error": f"{type(exc).__name__}: {exc}"}
                time.sleep(0.10)
            return "TIMEOUT", last

        def run_until_field(max_taps=8):
            # Retain the exact HF57 repeated RUN-touch behavior.
            attempts = []
            for attempt in range(1, int(max_taps) + 1):
                self._check_stop()
                before = cro_state()
                if before["field"] and not before["battle"]:
                    return True, attempt - 1, attempts
                ack = ctl.touch_pulse(
                    run_touch_state, hold_ms=120, resume_settle_ms=0,
                    packet_interval_ms=20, release_ms=160,
                )
                rec = {
                    "attempt": attempt,
                    "acknowledged": bool(ack.get("acknowledged", True)),
                    "sequence": ack.get("sequence"),
                    "before": before,
                }
                time.sleep(0.35)
                ok, after = wait_field(timeout=2.0, stable_samples=2)
                rec["after"] = after
                attempts.append(rec)
                if ok:
                    return True, attempt, attempts
            return False, None, attempts

        def choose_plan(pos, previous_direction):
            terrain.lock_zone(pos["zone"])
            plan, corridors = terrain.choose_fluid_plan(pos["grid"], previous_direction)
            if plan is not None:
                return plan, corridors
            reposition = terrain.choose_reposition(pos["grid"], previous_direction)
            if reposition is None:
                raise RuntimeError(
                    f"XY SAFETY HOLD: no contained movement plan from "
                    f"{pos['resolved'].get('location_name')} grid={pos['grid']}"
                )
            return reposition, corridors

        def verify_after_move(before_zone):
            state = cro_state()
            if state["battle"]:
                return None
            if not state["field"]:
                deadline = time.monotonic() + 1.2
                while time.monotonic() < deadline:
                    self._check_stop()
                    state = cro_state()
                    if state["battle"]:
                        return None
                    if state["field"]:
                        break
                    time.sleep(0.08)
            if state["battle"]:
                return None
            if not state["field"]:
                raise RuntimeError(
                    "XY SAFETY HOLD: movement left DllField without entering DllBattle"
                )
            pos = read_live_world(require_encounter=True)
            if pos["zone"] != before_zone:
                raise RuntimeError(
                    f"XY SAFETY HOLD: contained movement changed live zone "
                    f"{before_zone} -> {pos['zone']}"
                )
            return pos

        try:
            ctl.input_ping()
            ctl.release_all()
            ok, _ = wait_field(timeout=8.0, stable_samples=2)
            if not ok:
                raise RuntimeError(
                    "XY SAFETY HOLD: start in overworld with stable DllField and no DllBattle"
                )

            tid, sid = read_xy_trainer_ids(real_br)
            try:
                baseline = identity(read_xy_wild_decoded(real_br, 0))
            except Exception:
                baseline = None

            start_pos = read_live_world(require_encounter=True)
            actual_location = str(start_pos["resolved"].get("location_name") or self.location_name)
            selected_location = str(self.target_selection.get("location_name") or "")
            if selected_location and selected_location.casefold() != actual_location.casefold():
                raise RuntimeError(
                    f"XY SAFETY HOLD: HUNTS selected {selected_location}, "
                    f"but live world resolves {actual_location}"
                )

            self.location_name = actual_location
            self.current_world_terrain_name = str(
                (start_pos["resolved"].get("terrain") or {}).get("terrain_name")
                or self.current_world_terrain_name
            )
            self.status.emit("RUNNING", f"XY Normal Wild • {self.location_name} • contained")
            self._log(
                f"XY NORMAL WILD LOCKED: {game_profile['name']} • {self.location_name} • "
                f"{self.current_world_terrain_name} • HF61 Kalos world containment"
            )
            self._log(
                "XY FIELD AUTHORITY: X=0x08C670BC Z=0x08C670C4 "
                "zone=0x08C67190 • hardware-proven HF60"
            )
            self._log(
                f"XY WORLD START: zone={start_pos['zone']} grid={start_pos['grid']} "
                f"matrix={start_pos['resolved'].get('matrix_id')} "
                f"region={start_pos['resolved'].get('region_id')} "
                f"local={start_pos['resolved'].get('local_tile')}"
            )
            self._log(f"XY TRAINER AUTHORITY: TID/SID {tid}/{sid}")

            previous_direction = None

            while True:
                self._check_stop()
                encounter_started = time.monotonic()
                found = None

                for pulse_no in range(1, 81):
                    self._check_stop()
                    pos = read_live_world(require_encounter=True)
                    plan, corridors = choose_plan(pos, previous_direction)
                    direction = str(plan["direction"])
                    hold_ms = int(plan.get("hold_ms", 160))
                    max_observed = int(plan.get("max_observed_tiles", 1))
                    corridor = plan.get("corridor") or []

                    if plan.get("mode") == "fluid":
                        if len(corridor) < max_observed:
                            raise RuntimeError(
                                "XY SAFETY HOLD: corridor shorter than maximum observed movement"
                            )
                        for cell in corridor[:max_observed]:
                            if not terrain.in_core1(cell["grid"]):
                                raise RuntimeError(
                                    f"XY SAFETY HOLD: movement window includes "
                                    f"non-encounter tile {cell['grid']}"
                                )
                    else:
                        target = plan.get("target_grid")
                        if not target or not terrain.in_core1(target):
                            raise RuntimeError(
                                "XY SAFETY HOLD: reposition target lacks encounter authority"
                            )

                    self._log(
                        f"XY CONTAINED MOVE #{pulse_no}: zone={pos['zone']} "
                        f"grid={pos['grid']} {direction} {hold_ms}ms "
                        f"mode={plan.get('mode')} corridor={len(corridor)}"
                    )
                    ack = ctl.pulse(
                        (direction, "B"), hold_ms=hold_ms, resume_settle_ms=0,
                        packet_interval_ms=20, release_ms=140,
                    )
                    if not bool(ack.get("acknowledged", True)):
                        raise RuntimeError(
                            "XY SAFETY HOLD: contained movement pulse not acknowledged"
                        )

                    status, p = wait_new_wild(baseline, tid, sid, timeout=0.55)
                    if status == "TRAINER_MISMATCH":
                        raise RuntimeError(
                            "XY SAFETY HOLD: checksum-valid wild PK6 TID/SID mismatch"
                        )
                    if status == "VALID":
                        found = p
                        break

                    verify_after_move(pos["zone"])
                    previous_direction = direction

                if found is None:
                    raise RuntimeError(
                        "XY SAFETY HOLD: no new valid wild PK6 after 80 contained pulses"
                    )

                ctl.release_all()
                if not found.get("valid") or not found.get("checksum_valid"):
                    raise RuntimeError("XY SAFETY HOLD: authoritative wild PK6 invalid")

                self.attempt += 1
                self.pokemon_seen_session += 1
                self.species_counts[int(found.get("species") or 0)] += 1
                payload = self._encounter_ui_payload(found, horde_size=1)
                payload["xy_wild_address"] = "0x081FF744"
                payload["xy_hf61_world_contained"] = True
                payload["xy_field_authority"] = {
                    "x": "0x08C670BC", "z": "0x08C670C4", "zone": "0x08C67190"
                }
                payload["trainer_match"] = True
                self.last_encounter = dict(payload)
                self._save_last_seen_entry(payload)
                self.encounter.emit(payload)
                self.last_seen_entry.emit(payload)
                self._log(
                    f"XY ENCOUNTER #{self.attempt}: {payload['species_name']} • "
                    f"shiny={payload['is_shiny']} xor={payload['shiny_xor']}"
                )

                blocked = bool(
                    payload["is_shiny"]
                    and int(payload["species"]) in blocked_shiny_species(self.profile)
                )
                payload["shiny_blocked"] = blocked
                payload["shiny_action"] = (
                    "RUN" if blocked else ("HOLD" if payload["is_shiny"] else None)
                )
                self._record_encounter(
                    payload, duration_s=time.monotonic() - encounter_started
                )

                if payload["is_shiny"] and not blocked:
                    self.status.emit(
                        "SHINY_HOLD",
                        f"Shiny {payload['species_name']} found • no Run input sent",
                    )
                    raise RuntimeError(
                        f"SHINY FOUND {payload['species_name']} — XY authoritative PK6 HOLD"
                    )
                if bool(payload.get("target_match")):
                    self.status.emit(
                        "TARGET_HOLD",
                        f"Target match {payload['species_name']} • no Run input sent",
                    )
                    raise RuntimeError(
                        f"TARGET FOUND {payload['species_name']} — XY target filter HOLD"
                    )

                time.sleep(1.00)
                state = cro_state()
                if not state.get("battle"):
                    deadline = time.monotonic() + 5.0
                    while time.monotonic() < deadline:
                        self._check_stop()
                        state = cro_state()
                        if state.get("battle"):
                            break
                        time.sleep(0.20)
                if not state.get("battle"):
                    raise RuntimeError(
                        "XY SAFETY HOLD: valid wild PK6 appeared but DllBattle never became present"
                    )

                escaped, accepted, attempts = run_until_field(max_taps=8)
                self.run_attempt_counts[int(accepted or 0)] += 1
                self.events.append({
                    "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "type": "XY_RUN_SEQUENCE",
                    "attempt": self.attempt,
                    "accepted_run_touch": accepted,
                    "run_attempts": attempts,
                    "world_contained": True,
                })
                if not escaped:
                    raise RuntimeError(
                        "XY SAFETY HOLD: battle did not return to stable DllField "
                        "after 8 bounded Run touches"
                    )

                ctl.release_all()
                time.sleep(1.00)
                ok, _ = wait_field(timeout=4.0, stable_samples=2)
                if not ok:
                    raise RuntimeError(
                        "XY SAFETY HOLD: DllField did not remain stable after Run"
                    )

                post = read_live_world(require_encounter=True)
                post_location = str(post["resolved"].get("location_name") or "")
                if self.location_name and post_location.casefold() != self.location_name.casefold():
                    raise RuntimeError(
                        f"XY SAFETY HOLD: post-battle location changed "
                        f"{self.location_name} -> {post_location}"
                    )

                self._log(
                    f"XY PASS #{self.attempt}: Run touch {accepted} -> "
                    f"contained field zone={post['zone']} grid={post['grid']}"
                )
                self.status.emit(
                    "RUNNING",
                    f"XY Normal Wild • {self.location_name} • "
                    f"{self.attempt} encounter(s) • contained",
                )
                self.stats.emit(self._stats_payload())
                baseline = identity(found)
                previous_direction = None
        finally:
            try:
                ctl.release_all()
            except Exception:
                pass
            try:
                ctl.close()
            except Exception:
                pass

    @Slot()
    def run(self):
        final_status = "SAFETY_HOLD"
        reason = None
        backend = core = terrain = None

        self.started_mono = time.monotonic()
        self.started_iso = datetime.now().astimezone().isoformat(timespec="seconds")

        try:
            backend, core, terrain = self._load_backend()
            real_br = self._make_stop_aware_bridge(core)

            self.status.emit("STARTING", f"{self.method_meta['name']} RAM preflight")
            self._log(
                f"WILD {self.method_meta['name']} — loading validated backend"
            )

            gi = real_br.game_info()
            self._wild_bridge_identity = {
                "title_id": int(gi.get("title_id", 0)),
                "pid": int(gi.get("pid", 0)),
                "process": str(gi.get("process") or ""),
            }
            game_profile = game_from_probe(gi)
            self.game_profile = game_profile
            if game_profile is None:
                raise backend.IntegrationHold(
                    f"Unsupported game {gi['title_id_hex']} {gi['process']}; "
                    "this build supports Pokémon X/Y and ORAS"
                )

            # X/Y must branch before any ORAS world/terrain authority is installed.
            # This is the hard family boundary that prevents Hunts/runtime mixing.
            if game_profile.get("family") == "xy":
                self._run_xy_normal_wild_main(real_br, game_profile, core)
                raise UserStop("MANUAL_STOP — XY normal-wild loop ended")

            # ORAS only from this point onward. Keep the frozen v0p23/v0p27
            # movement and encounter engines byte-identical, but replace their
            # Route101 proof fixture with the compiled whole-game terrain authority.
            world_db = (
                self.base_dir / "pokebot" / "wild" / "world"
                / "alpha_sapphire_grass_runtime.sqlite"
            )
            self.world_terrain = install_world_authority(backend, world_db)
            terrain = self.world_terrain

            # v0p43CX: Omega Ruby now exposes the same fail-closed Fishing
            # controller for hardware validation. The RAM addresses remain
            # treated as provisional on OR until support ZIP evidence proves
            # the state machine; any mismatch still Integration/Safety Holds.

            if self.target_selection:
                selected_game = str(
                    self.target_selection.get("game_key") or ""
                )
                if selected_game and selected_game != game_profile["key"]:
                    raise backend.IntegrationHold(
                        "HUNTS target game mismatch: selected "
                        f"{selected_game}, connected {game_profile['key']}"
                    )
                selected_section = str(
                    self.target_selection.get("section_key") or ""
                )
                selected_title = str(
                    self.target_selection.get("section_title") or ""
                )
                if selected_section not in {"grass", "tall_grass", "surf"}:
                    raise backend.IntegrationHold(
                        "Selected HUNTS target is not an enabled ORAS Grass/Cave/Surf encounter"
                    )
                if self.method_key in {"walk", "run", "acro_bunny"} and selected_title.casefold() == "cave":
                    self._promote_runtime_to_cave("HUNTS target is classified as Cave")
                if self.method_key in {"cave", "cave_run", "cave_bunny"} and selected_title.casefold() != "cave":
                    raise backend.IntegrationHold(
                        "Cave mode requires a HUNTS target classified as Cave"
                    )
                if self.method_key in {"walk", "run", "acro_bunny"} and selected_section == "surf":
                    raise backend.IntegrationHold(
                        "This HUNTS target requires its dedicated Surf/Ocean Wild type"
                    )
                if self.method_key == "surf" and selected_section != "surf":
                    raise backend.IntegrationHold(
                        "Surf/Ocean mode requires a HUNTS target from a Water / Surf encounter table"
                    )

            if game_profile["key"] == "omega_ruby":
                br = _OmegaRubyWildIdentityBridge(real_br, core.AS_TITLE_ID)
                identity_mode = "omega_ruby_to_frozen_as_title_gate"
            else:
                br = real_br
                identity_mode = "native_alpha_sapphire"
            self.bridge = br

            self._log(
                f"ORAS GAME PROFILE LOCKED: {game_profile['name']} "
                f"{gi['title_id_hex']} {gi['process']} "
                f"identity_mode={identity_mode}"
            )

            caps = br.input_ping()
            if not caps.get("hid_pulse"):
                raise backend.IntegrationHold("Pokebot-Luma input transport lacks HID pulse support")
            if not caps.get("touch_pulse"):
                raise backend.IntegrationHold("CFW bridge lacks native touch pulse")
            if self.method_key in {"acro_bunny", "cave_bunny"} and not caps.get("hid_latch"):
                raise backend.IntegrationHold(
                    "Acro Bike method requires Pokebot-Luma retained HID latch support"
                )

            if self.auto_throw_one_poke_ball_on_shiny:
                if not auto_capture_supported_for_game(game_profile["key"]):
                    self._log(
                        f"SHINY AUTO-CATCH: requested but unavailable for {game_profile['name']}; "
                        "an unblocked shiny will use the normal Safety HOLD"
                    )
                elif self.method_key == "horde":
                    self._log(
                        f"SHINY AUTO-CATCH ARMED: {game_profile['name']} Horde mode; "
                        "exactly one shiny => protect its RAM slot, KO the other four, then capture; "
                        "2+ shinies => unconditional Safety HOLD with no attacks or Balls. "
                        "The hardware-proven Horde reducer waits for five-alive readiness and exact owner+0x93 COMMAND=1, then uses the per-battle COMMAND/MOVE fingerprint, exact target masks, one-step KO proof and protected-survivor capture path. The shiny coordinate is never touched."
                    )
                else:
                    self._log(
                        f"SHINY AUTO-CATCH ARMED: {game_profile['name']} 1.4 singles plus natural Hordes; "
                        "exactly one Horde shiny is isolated/captured, 2+ Horde shinies HOLD; "
                        "confirmed catches clear post-capture screens and resume"
                    )

            if self.auto_capture_test_pending:
                self._log(
                    "AUTO-CAPTURE TEST ARMED: next checksum-valid non-shiny SINGLE encounter "
                    "will exercise BAG -> Ball/retry -> confirmed capture -> post-capture field recovery. "
                    "The encounter remains non-shiny in all telemetry/counters; Hordes and target HOLDs are skipped."
                )
                self.status.emit(
                    "RUNNING",
                    "Auto-Capture TEST armed • next non-shiny single encounter will be caught",
                )
                self.events.append({
                    "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "type": "AUTO_CAPTURE_TEST_ARMED",
                    "ball_override": self.capture_ball_override,
                })

            # Re-verify Shiny Charm once at Wild Start. This never changes
            # encounter/shiny authority; it only drives UI/stat odds telemetry.
            charm_state = detect_oras_shiny_charm(
                br, game_key=game_profile["key"]
            )
            self.shiny_charm_state = dict(charm_state or {})
            self._initialize_phase_probability()
            self._save_stats()
            self.shiny_charm.emit(charm_state)
            if self.fishing_chain is not None:
                self.fishing_chain.set_shiny_charm(charm_state.get("detected"))
                fs = self._sync_fishing_chain_stats()
                self._log(
                    "CHAIN FISHING: formal streak accounting armed; "
                    f"Shiny Charm={fs.get('shiny_charm')} next_rolls={(fs.get('next_odds') or {}).get('rolls')}"
                )
            if charm_state.get("detected") is True:
                self._log(
                    "SHINY CHARM RAM: PRESENT "
                    f"(item 632 hit(s): {charm_state.get('hits', [])})"
                )
            elif charm_state.get("detected") is False:
                self._log("SHINY CHARM RAM: NOT PRESENT")
            else:
                self._log(
                    "SHINY CHARM RAM: UNKNOWN — "
                    f"{charm_state.get('status')} "
                    f"{charm_state.get('error', '')}".strip()
                )

            self.connection.emit({
                "ram_ready": True,
                "controller_ready": True,
                "game_info": {
                    "title_id": gi["title_id_hex"],
                    "pid": gi["pid"],
                    "process_name": gi["process"],
                    "flags": gi["flags"],
                },
                "game_profile": game_profile,
                "wild_identity_mode": identity_mode,
                "input_status": (
                    "Pokebot-Luma RAM + Input 4952: Ready"
                    + (" • HID Latch" if caps.get("hid_latch") else "")
                ),
                "controller_info": {
                    "capabilities": int(caps.get("capability_flags", 0)),
                    "runtime_flags": int(caps.get("runtime_flags", 0)),
                },
                "shiny_charm": charm_state,
            })

            br.release_all()
            if br.u32(core.BATTLE_ADDR) != core.BATTLE_INACTIVE:
                raise backend.IntegrationHold("Start in the overworld, not in battle")

            raw_ids = br.read(core.TRAINER_IDS_ADDR, 4)
            save_tid, save_sid = struct.unpack("<HH", raw_ids)

            # Pokérus telemetry baseline: one bounded party snapshot at hunt start.
            # It is not encounter/shiny authority and cannot fail the hunt.
            self._initialise_pokerus_baseline(br)

            start_pos = backend.read_position(br)
            resolved = start_pos.get("world_resolve") or {}
            self.location_name = str(
                resolved.get("location_name")
                or start_pos.get("location_name")
                or f"Zone {start_pos['zone_id']}"
            )
            self.location_zone = int(start_pos["zone_id"])
            self.location_parent_map = resolved.get("parent_map")
            self.location_matrix = resolved.get("matrix_id")
            self.current_world_terrain_name = str((resolved.get("terrain") or {}).get("terrain_name") or "Land")

            # Cave encounter floors are not outdoor grass-mask cells. If the
            # user chose ordinary Walk/Run/Bunny while physically standing in
            # a zone which the ORAS encounter tables explicitly classify as
            # Cave, promote to the matching Cave movement backend automatically.
            # This makes edge cave tiles legal starts instead of rejecting them
            # as "not encounter grass".
            encounter_data = _load_json(
                self.base_dir / "data" / "oras_encounters.json", {}
            )
            live_cave_zones = _cave_zone_ids_for_location(
                encounter_data, game_profile["key"], self.location_name
            )
            live_zone_is_cave = int(self.location_zone) in live_cave_zones
            if live_zone_is_cave and self.method_key in {"walk", "run", "acro_bunny"}:
                self._promote_runtime_to_cave(
                    "live encounter data classifies "
                    f"{self.location_name} zone {self.location_zone} as Cave"
                )
                self._log(
                    "CAVE AUTO-AUTHORITY: outdoor grass/core1/core2 containment "
                    "is disabled for this live Cave zone; edge tiles are legal starts."
                )

            if self.target_selection:
                selected_location = str(
                    self.target_selection.get("location_name") or ""
                )
                if (
                    selected_location
                    and selected_location.casefold()
                    != self.location_name.casefold()
                ):
                    raise backend.IntegrationHold(
                        "HUNTS target location mismatch: selected "
                        f"{selected_location!r}, live RAM resolved "
                        f"{self.location_name!r}. No movement authorized."
                    )
                self._log(
                    "HUNTS TARGET AUTHORIZED: "
                    f"{self.target_species_name or self.target_species} • "
                    f"{self.location_name} • "
                    f"{self.target_selection.get('section_title', 'Land')}"
                )

            if self.method_key in ("walk", "run"):
                start_grid = backend.require_safe_position(start_pos, "preflight")
                if not bool((start_pos.get("mask") or {}).get("core2")):
                    raise backend.IntegrationHold(
                        f"preflight: {self.location_name} grid {list(start_grid)} is an "
                        "encounter-grass edge/boundary tile; start at least one tile "
                        "further inside the grass"
                    )
                self._log(
                    "GRASS INTERIOR AUTHORITY: start grid "
                    f"{list(start_grid)} is core2/interior; requested first direction="
                    f"{AXES[self.movement_axis].get('initial_direction', self.movement_axis)}"
                )
                if self.method_key == "run" and resolved.get("enable_running") is False:
                    raise backend.IntegrationHold(
                        f"Run is disabled by the game in {self.location_name}; use Walk instead"
                    )
                anchor_grid = None
            elif self.method_key in {"cave", "cave_run", "cave_bunny"}:
                # Cave floors are not grass-mask cells. Use zone + duplicate
                # coordinate + tile-centre authority, then verify each exact
                # one-tile movement endpoint in RAM.
                cave_preflight = read_stable_cave_position(
                    br, backend, self.location_zone, "Cave preflight"
                )
                start_pos = cave_preflight["position"]
                anchor_grid = cave_preflight["grid"]
                self._log(
                    "CAVE GRID AUTHORITY: logical grid "
                    f"{list(anchor_grid)} stable across repeated RAM samples; "
                    "land-style exact tile-centre settle is diagnostic only "
                    f"(settled_tile_center={cave_preflight.get('settled_tile_center')})"
                )
                cave_zone_ids = _cave_zone_ids_for_location(
                    encounter_data, game_profile["key"], self.location_name
                )
                if int(self.location_zone) not in cave_zone_ids:
                    raise backend.IntegrationHold(
                        f"{self.location_name} zone {self.location_zone} is not classified as a Cave encounter zone in the packaged ORAS data"
                    )
                self.current_world_terrain_name = "Cave"
                if (
                    self.method_key == "cave_bunny"
                    and resolved.get("enable_cycling") is False
                ):
                    raise backend.IntegrationHold(
                        f"Cycling is disabled by the game in {self.location_name}; "
                        "Cave Acro Bunny cannot start here"
                    )
                if (
                    self.method_key == "cave_run"
                    and resolved.get("enable_running") is False
                ):
                    raise backend.IntegrationHold(
                        f"Running is disabled by the game in {self.location_name}; "
                        "Cave Run cannot start here"
                    )
                if self.method_key == "cave_run":
                    self.cave_run_corridor = {
                        "anchor_grid": list(anchor_grid),
                        "axis": self.movement_axis,
                        "proven": True,
                        "policy": "WALL_BOUNDED_NO_CORRIDOR_PREFLIGHT",
                    }
                    self._log(
                        "CAVE RUN EDGE AUTHORITY: any stable Cave grid is a legal "
                        "start; no grass/core1/core2 or +/-6 corridor preflight. "
                        "Cave collision bounds movement and blocked directions reverse."
                    )
                elif self.method_key == "cave_bunny":
                    # A stable cave grid can become visible slightly before the
                    # overworld is ready to honour a retained B latch. Give the
                    # initial Acro Bunny latch one bounded re-arm window, then
                    # prove that the same anchor is still present.
                    self._log(
                        "CAVE ACRO RE-ARM: waiting 0.75 s before initial B latch"
                    )
                    br.release_all()
                    time.sleep(0.75)
                    self._check_stop()
                    acro_rearm = read_stable_cave_position(
                        br, backend, self.location_zone,
                        "Cave Acro initial re-arm",
                        timeout=2.50, required_samples=2, poll_sec=0.10,
                    )
                    acro_grid = tuple(acro_rearm["grid"])
                    battle_rearm = br.u32(core.BATTLE_ADDR)
                    if battle_rearm != core.BATTLE_INACTIVE:
                        raise backend.IntegrationHold(
                            "Cave Acro initial re-arm found battle state "
                            f"{backend.hx(battle_rearm)} instead of field inactive"
                        )
                    if acro_grid != tuple(anchor_grid):
                        raise backend.IntegrationHold(
                            "Cave Acro initial re-arm anchor changed while idle: "
                            f"{list(anchor_grid)} -> {list(acro_grid)}"
                        )
                    start_pos = acro_rearm["position"]
                    anchor_grid = acro_grid
                    self._log(
                        "CAVE ACRO RE-ARM: PASS — battle inactive and anchor "
                        f"grid {list(anchor_grid)} retained"
                    )
            elif self.method_key == "surf":
                # v0p40a is deliberately manual-mount: the player must already
                # be Surfing before Start. We do not invent a Surf-state RAM
                # address or interpret unclassified raw permissions as water.
                surf_preflight = read_stable_water_position(
                    br, backend, self.location_zone, "Surf preflight"
                )
                start_pos = surf_preflight["position"]
                anchor_grid = surf_preflight["grid"]
                self._log(
                    "SURF GRID AUTHORITY: logical grid "
                    f"{list(anchor_grid)} stable across repeated RAM samples; "
                    "exact tile-centre settle is not required while mounted "
                    f"(settled_tile_center={surf_preflight.get('settled_tile_center')})"
                )
                encounter_data = _load_json(
                    self.base_dir / "data" / "oras_encounters.json", {}
                )
                known_surf = {}
                try:
                    locations = encounter_data["games"][game_profile["key"]]["locations"]
                    for loc in locations:
                        has_surf = any(
                            str(s.get("key") or "") == "surf"
                            and bool(s.get("pokemon"))
                            for s in (loc.get("sections") or [])
                        )
                        if has_surf:
                            known_surf[str(loc.get("name") or "")] = str(
                                loc.get("environment_hint") or "land"
                            ).casefold()
                except Exception:
                    known_surf = {}

                if self.location_name not in known_surf:
                    raise backend.IntegrationHold(
                        f"{self.location_name} has no packaged ORAS Surf encounter pool"
                    )

                self.current_world_terrain_name = (
                    "Ocean"
                    if known_surf[self.location_name] == "ocean"
                    else "Surf"
                )
                self._log(
                    "SURF MANUAL PRECONDITION: player must already be Surfing "
                    "in open encounter water; no automatic Surf activation is used"
                )
            elif self.method_key == "fishing":
                # Fishing is stationary. The v0p11 proof authorizes control from
                # the fishing state object itself rather than grass/water terrain.
                anchor_grid = tuple(start_pos.get("grid") or ())
                if len(anchor_grid) != 2:
                    raise backend.IntegrationHold(
                        "Fishing preflight could not establish a stable player grid"
                    )
                fish_field = self._wait_for_fishing_field(
                    br, core, backend, timeout=3.0
                )
                if not fish_field.get("ready"):
                    raise backend.IntegrationHold(
                        "Fishing preflight field authority failed: "
                        + str(fish_field.get("status"))
                    )
                start_pos = fish_field["final_position"]
                self.current_world_terrain_name = "Fishing"
                self._log(
                    "FISHING PREFLIGHT PASS: battle inactive, action object idle, "
                    f"zone {self.location_zone}, grid {list(anchor_grid)}. "
                    "Registered fishing rod must be assigned to Y."
                )
            elif self.method_key == "horde":
                # Horde mode is stationary and may trigger through either the
                # proven Sweet Scent party route or read-only-located Honey.
                anchor_grid = self._require_stationary_horde_position(
                    start_pos, "Horde preflight"
                )
                self._resolve_horde_trigger_setup(br)
                self._log(
                    f"HORDE TRIGGER LOCKED: requested={self.horde_trigger} "
                    f"resolved={self.horde_trigger_resolved}"
                )
            else:
                if resolved.get("enable_cycling") is False:
                    raise backend.IntegrationHold(
                        f"Cycling is disabled by the game in {self.location_name}"
                    )
                anchor_grid = backend.require_bunny_interior_position(
                    start_pos, "preflight"
                )

            # Once preflight succeeds, the hunt is locked to this live zone.
            # Any map transition during movement/field recovery is a HOLD.
            self.world_terrain.lock_zone(self.location_zone)

            world_location = {
                "resolved": bool(resolved.get("resolved")),
                "location_name": self.location_name,
                "zone_id": self.location_zone,
                "parent_map": self.location_parent_map,
                "matrix_id": self.location_matrix,
                "enable_running": resolved.get("enable_running"),
                "enable_cycling": resolved.get("enable_cycling"),
                "region_id": resolved.get("region_id"),
                "local_tile": resolved.get("local_tile"),
                "grid": start_pos.get("grid"),
                "encounter_terrain": bool(resolved.get("encounter_terrain")) or self.method_key in {"cave", "cave_run", "cave_bunny", "surf", "fishing"},
                "terrain": (
                    {"terrain_name": "Cave", "authority": "RAM_CAVE_MOVEMENT_AUTHORITY"}
                    if self.method_key in {"cave", "cave_run", "cave_bunny"}
                    else (
                        {
                            "terrain_name": self.current_world_terrain_name,
                            "authority": "RAM_SURF_TWO_TILE_HW_TEST",
                            "manual_precondition": "ALREADY_SURFING",
                        }
                        if self.method_key == "surf"
                        else (
                            {
                                "terrain_name": "Fishing",
                                "authority": "RAM_FISHING_STATE_V0P11",
                                "manual_precondition": "ROD_REGISTERED_TO_Y",
                            }
                            if self.method_key == "fishing"
                            else resolved.get("terrain")
                        )
                    )
                ),
                "cave_authority": self.method_key in {"cave", "cave_run", "cave_bunny"},
                "surf_authority": self.method_key == "surf",
                "fishing_authority": self.method_key == "fishing",
                "core2_interior": bool(start_pos.get("mask", {}).get("core2")),
            }
            self.connection.emit({
                "ram_ready": True,
                "controller_ready": True,
                "game_info": {
                    "title_id": gi["title_id_hex"],
                    "pid": gi["pid"],
                    "process_name": gi["process"],
                    "flags": gi["flags"],
                },
                "game_profile": game_profile,
                "wild_identity_mode": identity_mode,
                "input_status": (
                    "Pokebot-Luma RAM + Input 4952: Ready"
                    + (" • HID Latch" if caps.get("hid_latch") else "")
                ),
                "controller_info": {
                    "capabilities": int(caps.get("capability_flags", 0)),
                    "runtime_flags": int(caps.get("runtime_flags", 0)),
                },
                "shiny_charm": charm_state,
                "world_location": world_location,
            })

            self._log(
                f"START {game_profile['name']} {self.location_name} "
                f"zone={self.location_zone} grid={start_pos['grid']} "
                f"method={self.method_meta['name']} axis={self.movement_axis}"
            )
            axis_text = (
                AXES[self.movement_axis]["short"]
                if self.movement_axis in AXES else "Stationary"
            )
            self.status.emit(
                "RUNNING",
                (
                    ("Fishing • " if self.method_key == "fishing" else "Wild • ")
                    + (
                        f"{self.target_species_name} • "
                        if self.target_species_name else ""
                    )
                    + f"{self.location_name} • "
                    f"{self.method_meta['name']} • {axis_text}"
                ),
            )
            self.stats.emit(self._stats_payload())

            while True:
                self._check_stop()
                next_attempt = self.attempt + 1
                encounter_started_mono = time.monotonic()

                if self.method_key in ("walk", "run"):
                    movement, self.previous_direction, self.global_cycle = (
                        movement_until_encounter_axis(
                            br,
                            backend,
                            movement_mode=self.method_key,
                            axis_key=self.movement_axis,
                            previous_direction=self.previous_direction,
                            global_burst_start=self.global_cycle,
                        )
                    )
                elif self.method_key == "cave":
                    movement, self.previous_direction, self.global_cycle = (
                        cave_until_encounter(
                            br,
                            backend,
                            axis_key=self.movement_axis,
                            expected_zone=self.location_zone,
                            previous_direction=self.previous_direction,
                            global_burst_start=self.global_cycle,
                        )
                    )
                elif self.method_key == "cave_run":
                    movement, self.previous_direction, self.global_cycle = (
                        cave_run_until_encounter(
                            br,
                            backend,
                            axis_key=self.movement_axis,
                            expected_zone=self.location_zone,
                            anchor_grid=anchor_grid,
                            corridor_state=self.cave_run_corridor,
                            previous_direction=self.previous_direction,
                            global_burst_start=self.global_cycle,
                        )
                    )
                elif self.method_key == "cave_bunny":
                    movement, self.global_cycle = cave_bunny_until_encounter(
                        br,
                        backend,
                        expected_zone=self.location_zone,
                        anchor_grid=anchor_grid,
                        global_burst_start=self.global_cycle,
                    )
                elif self.method_key == "surf":
                    movement, self.previous_direction, self.global_cycle = (
                        surf_until_encounter(
                            br,
                            backend,
                            axis_key=self.movement_axis,
                            expected_zone=self.location_zone,
                            anchor_grid=anchor_grid,
                            previous_direction=self.previous_direction,
                            global_burst_start=self.global_cycle,
                        )
                    )
                elif self.method_key == "horde":
                    if self.horde_trigger_resolved == "honey":
                        movement = self._trigger_honey_horde(
                            br, core, backend, anchor_grid
                        )
                    elif self.horde_trigger_resolved == "sweet_scent":
                        movement = self._trigger_auto_sweet_scent(
                            br, core, backend, anchor_grid
                        )
                    else:
                        raise backend.IntegrationHold(
                            f"Horde trigger was not resolved: {self.horde_trigger_resolved!r}"
                        )
                elif self.method_key == "fishing":
                    movement = self._trigger_fishing_until_encounter(
                        br, core, backend
                    )
                else:
                    movement, self.global_cycle = backend.bunny_until_encounter(
                        br,
                        anchor_grid,
                        self.global_cycle,
                    )

                self._check_stop()
                if not movement.get("encounter"):
                    raise backend.IntegrationHold(
                        "wild trigger returned without an encounter"
                    )

                # HF95: movement began at encounter_started_mono.  The movement
                # routine returns only after an encounter boundary is proven, so
                # this isolates the part Encounter Power actually changes.
                field_to_encounter_s = self._note_field_to_encounter(
                    time.monotonic() - encounter_started_mono
                )

                br.release_all()
                hf97_boundary_wait_started = time.monotonic()
                boundary = core.wait_for_state2_for_pk6(br)
                hf97_boundary_wait_s = time.monotonic() - hf97_boundary_wait_started
                if not boundary.get("ready_for_pk6"):
                    # Some hardware-proven Horde presentations never expose
                    # the single-battle state_3C==2 marker. Preserve the
                    # proven normal gate, but after its bounded timeout permit
                    # ONE read-only five-slot snapshot iff battle authority is
                    # still active. There is no PK6 polling.
                    battle_now = br.u32(core.BATTLE_ADDR)
                    if (
                        boundary.get("status") == "STATE2_TIMEOUT"
                        and battle_now == core.BATTLE_ACTIVE
                    ):
                        self._log(
                            "HORDE BOUNDARY FALLBACK: STATE2_TIMEOUT while "
                            "battle remains active; taking one bounded "
                            "five-slot opponent snapshot"
                        )
                    else:
                        raise backend.IntegrationHold(
                            f"PK6 safety boundary failed: {boundary.get('status')}"
                        )

                # Dashboard-only order telemetry while the player battle pointer
                # table is live. This is deliberately placed after the existing
                # PK6 safety boundary so it cannot race movement/encounter entry.
                # Any failure is ignored and cannot alter hunt authority.
                self._refresh_live_party_order_from_battle(br)

                opponent_set = read_opponent_set(
                    br, core, save_tid, save_sid
                )
                if not opponent_set.get("valid"):
                    raise backend.IntegrationHold(
                        "opponent-set authority failed: "
                        + str(opponent_set.get("reason"))
                    )

                current_identities = set(opponent_set.get("identities") or [])
                stale = current_identities.intersection(
                    self.previous_opponent_identities
                )
                if stale:
                    raise backend.IntegrationHold(
                        "duplicate/stale opponent PK6 identity: "
                        + ", ".join(sorted(stale))
                    )
                self.previous_opponent_identities = current_identities
                self.previous_identity = (
                    next(iter(current_identities))
                    if len(current_identities) == 1 else None
                )

                self.attempt = next_attempt
                horde_size = int(opponent_set.get("size", 0))
                if self.method_key == "horde" and horde_size != 5:
                    raise backend.IntegrationHold(
                        f"Horde mode expected 5 occupied opponent slots, got {horde_size}; no Run authorized"
                    )
                if horde_size == 5:
                    self.horde_battles += 1

                payloads = []
                for slot_rec in opponent_set["occupied"]:
                    payload = self._encounter_ui_payload(
                        slot_rec["pk6"],
                        horde_slot=int(slot_rec["slot"]),
                        horde_size=horde_size,
                    )
                    payload["encounter_kind"] = "Horde" if horde_size == 5 else "Single"
                    payload["horde_size"] = horde_size
                    payload["natural_horde"] = bool(horde_size == 5 and self.method_key != "horde")
                    payload["trigger_method"] = (
                        f"Horde ({self.horde_trigger_resolved})"
                        if self.method_key == "horde" else self.method_meta["name"]
                    )
                    if self.method_key == "horde":
                        payload["horde_trigger"] = self.horde_trigger_resolved
                    if self.method_key == "fishing" and movement.get("fishing_chain"):
                        payload["fishing_chain"] = dict(movement["fishing_chain"])
                        payload["fishing_encounter_odds"] = dict(
                            movement.get("fishing_encounter_odds") or {}
                        )
                    payloads.append(payload)

                self.last_opponent_set = {
                    "battle_index": self.attempt,
                    "classification": opponent_set.get("classification"),
                    "size": horde_size,
                    "slots": [
                        {
                            "slot": p.get("horde_slot"),
                            "species": p.get("species"),
                            "species_name": p.get("species_name"),
                            "pid": p.get("pokemon_pid"),
                            "shiny_xor": p.get("shiny_xor"),
                            "is_shiny": p.get("is_shiny"),
                        }
                        for p in payloads
                    ],
                }

                if horde_size == 5:
                    names = ", ".join(p["species_name"] for p in payloads)
                    source = "AUTO SWEET SCENT" if self.method_key == "horde" else "NATURAL WILD"
                    self._log(f"#{self.attempt} HORDE x5 [{source}]: {names}")
                    for p in payloads:
                        self._log(
                            f"  slot{p['horde_slot']} {p['species_name']} "
                            f"PID {p['pokemon_pid']} XOR {p['shiny_xor']} "
                            f"shiny={p['is_shiny']}"
                            + (f" evolution={p.get('predicted_evolution')}" if p.get("predicted_evolution") else "")
                        )
                else:
                    p = payloads[0]
                    self._log(
                        f"#{self.attempt} {p['species_name']} "
                        f"PID {p['pokemon_pid']} XOR {p['shiny_xor']} "
                        f"shiny={p['is_shiny']}"
                        + (f" evolution={p.get('predicted_evolution')}" if p.get("predicted_evolution") else "")
                    )

                shiny_payloads = [p for p in payloads if p["is_shiny"]]
                target_match_payloads = [p for p in payloads if p.get("target_match")]
                if self.target_criteria.get("enabled"):
                    matches = ", ".join(
                        f"slot {p.get('horde_slot')} {p.get('species_name')}"
                        if horde_size == 5 else str(p.get("species_name"))
                        for p in target_match_payloads
                    ) or "none"
                    self._log(
                        f"TARGET CHECK #{self.attempt}: {matches} • "
                        f"{len(target_match_payloads)}/{len(payloads)} complete match"
                    )

                # Live blocklist snapshot. The file is atomically updated by
                # the HUNTS tab, so this takes effect during a running hunt
                # without restarting Pokebot. Any read/corruption problem
                # fail-safes to an empty set => normal shiny HOLD.
                try:
                    blocked_species_now = blocked_shiny_species(self.profile)
                except Exception:
                    blocked_species_now = set()

                oras_auto_capture_game = auto_capture_supported_for_game(
                    game_profile["key"]
                )
                single_auto_catch_available = bool(
                    self.auto_throw_one_poke_ball_on_shiny
                    and horde_size == 1
                    and oras_auto_capture_game
                )
                horde_auto_catch_available = bool(
                    self.auto_throw_one_poke_ball_on_shiny
                    and horde_size == 5
                    and len(shiny_payloads) == 1
                    and oras_auto_capture_game
                )
                multi_shiny_horde = bool(
                    horde_size == 5 and len(shiny_payloads) >= 2
                )
                auto_catch_available = bool(
                    single_auto_catch_available or horde_auto_catch_available
                )
                blocked_shiny_payloads = []
                hold_shiny_payloads = []
                for payload in shiny_payloads:
                    blocked = (
                        int(payload["species"]) in blocked_species_now
                    )
                    payload["shiny_blocked"] = bool(blocked)

                    # D19 policy: any 2+ shiny Horde is an unconditional HOLD.
                    # Blocklist RUN never overrides simultaneous multi-shiny safety.
                    if multi_shiny_horde:
                        payload["shiny_action"] = "HOLD_MULTI_SHINY"
                        hold_shiny_payloads.append(payload)
                    else:
                        payload["shiny_action"] = (
                            "RUN" if blocked
                            else ("AUTO_CATCH" if auto_catch_available else "HOLD")
                        )
                        if blocked:
                            blocked_shiny_payloads.append(payload)
                        else:
                            hold_shiny_payloads.append(payload)

                # Capture the real battle image only AFTER RAM has confirmed the
                # shiny. This is presentation telemetry, never shiny authority.
                # For a blocklisted shiny this deliberately occurs before Run.
                if shiny_payloads:
                    frame_meta = self._capture_wild_shiny_framebuffer(
                        shiny_payloads[0]
                    )
                    if frame_meta:
                        for payload in shiny_payloads:
                            payload["framebuffer_path"] = frame_meta["path"]
                            payload["framebuffer_capture"] = dict(frame_meta)

                # Emit true HOLD shinies last. A blocklisted shiny remains a
                # real shiny payload but explicitly carries shiny_action=RUN.
                nonshiny_payloads = [
                    p for p in payloads if not p["is_shiny"]
                ]
                emit_order = (
                    nonshiny_payloads
                    + blocked_shiny_payloads
                    + hold_shiny_payloads
                )
                for payload in emit_order:
                    # Persist immediately after authoritative RAM read.
                    self._save_last_seen_entry(payload)
                    self.encounter.emit(payload)
                    self.last_seen_entry.emit(payload)

                if hold_shiny_payloads:
                    duration = time.monotonic() - encounter_started_mono
                    self.pokemon_seen_session += len(payloads)
                    for p in payloads:
                        self.species_counts[int(p["species"])] += 1

                    # Existing shiny-HOLD accounting records every shiny in a
                    # simultaneous Horde, including any other blocklisted shiny
                    # that happened to appear alongside an unblocked one.
                    defer_horde_shiny_record = bool(
                        horde_size == 5
                        and horde_auto_catch_available
                        and not multi_shiny_horde
                    )
                    if horde_size == 5:
                        # D19f: a one-shiny Auto-Catch Horde is not a HOLD yet.
                        # Record it only after capture succeeds, or immediately
                        # before a genuine safety HOLD if the capture front-end fails.
                        if not defer_horde_shiny_record:
                            self._record_horde_shiny_hold(
                                payloads, duration_s=duration
                            )
                    else:
                        self._record_encounter(
                            payloads[0], duration_s=duration
                        )

                    shiny = hold_shiny_payloads[0]
                    auto_throw_report = None
                    auto_throw_error = None
                    auto_throw_eligible = auto_catch_available
                    if auto_throw_eligible:
                        try:
                            live_lead = self.horde_lead_pk6
                            if horde_size == 5:
                                self.status.emit(
                                    "RUNNING",
                                    f"Shiny {shiny['species_name']} in Horde slot {shiny['horde_slot']} • protecting it and isolating capture",
                                )
                                # Refresh the visible lead PK6 immediately before the reducer so
                                # current move PP is used if earlier Hordes/captures consumed it.
                                # Failure to refresh does not invent data: the hunt-start preflight
                                # snapshot remains the bounded fallback and downstream target-selector
                                # proof still fails closed if a move cannot actually be used.
                                try:
                                    lead_snap = get_runtime_party_snapshot(
                                        self.host, int(getattr(br, "port", 4952)), br
                                    )
                                    ordered_lead = list(lead_snap.get("ordered_parsed") or [])
                                    if ordered_lead and ordered_lead[0].get("valid") and ordered_lead[0].get("checksum_valid"):
                                        live_lead = dict(ordered_lead[0])
                                        self.horde_lead_pk6 = dict(ordered_lead[0])
                                        self._log(
                                            "HORDE AUTO-BATTLE LEAD REFRESH: "
                                            f"{live_lead.get('species_name')} moves={live_lead.get('moves')} "
                                            f"PP={live_lead.get('move_pp')} source={lead_snap.get('source')}"
                                        )
                                except Exception as exc:
                                    self._log(
                                        "HORDE AUTO-BATTLE LEAD REFRESH FAILED: "
                                        f"{type(exc).__name__}: {exc}; using hunt-start bounded PK6 snapshot"
                                    )

                            pre_capture_party_count = self._pre_capture_party_count_authority(br)
                            auto_throw_report = run_shiny_auto_capture(
                                core,
                                br,
                                opponent_set=opponent_set,
                                shiny=shiny,
                                horde_size=horde_size,
                                method_key=self.method_key,
                                environment=shiny.get("environment", ""),
                                ball_override=self.capture_ball_override,
                                lead_pk6=live_lead,
                                move_db=self.horde_move_db,
                                base_dir=self.base_dir,
                                check_stop=self._check_stop,
                                log=self._log,
                                game_key=game_profile["key"],
                            )
                            self.last_shiny_auto_throw = auto_throw_report
                            shiny["auto_throw_one_ball"] = auto_throw_report
                            self.events.append({
                                "time": datetime.now().astimezone().isoformat(
                                    timespec="seconds"
                                ),
                                "type": "SHINY_AUTO_CATCH_RESULT",
                                "species": shiny["species_name"],
                                "pokemon_pid": shiny["pokemon_pid"],
                                "result": auto_throw_report["result"],
                                "throw_count": auto_throw_report.get("throw_count"),
                            })

                            if auto_throw_report.get("result") == "CAPTURED":
                                # A confirmed catch is not a SHINY_HOLD. Keep the
                                # worker visibly RUNNING while the captured-shiny
                                # nickname/Pokédex/Box flow is resolved. SHINY_HOLD
                                # is reserved for an actual post-catch failure.
                                self.status.emit(
                                    "RUNNING",
                                    f"Shiny {shiny['species_name']} captured • resolving nickname / post-catch",
                                )
                                post_capture = clear_captured_shiny_post_capture(
                                    br, core, check_stop=self._check_stop, log=self._log,
                                    pre_capture_party_count=pre_capture_party_count,
                                )
                                auto_throw_report["post_capture"] = post_capture
                                self.last_shiny_auto_throw = auto_throw_report
                                shiny["auto_throw_one_ball"] = auto_throw_report
                                if post_capture.get("result") not in {
                                    "POST_CAPTURE_RAM_RECOVERY_COMPLETE",
                                    "POST_CAPTURE_RECOVERY_COMPLETE_POKEDEX_NOT_OBSERVED",
                                }:
                                    raise backend.IntegrationHold(
                                        "mapped post-capture RAM authority failed: "
                                        + str(post_capture.get("result"))
                                    )
                                field_pos = self._wait_post_capture_field_authority(
                                    br, core, backend, movement, anchor_grid
                                )
                                auto_throw_report["post_capture_field_grid"] = list(
                                    field_pos.get("grid") or []
                                )
                                save_report = self._save_after_shiny_capture(
                                    br, core, backend, movement, anchor_grid
                                )
                                auto_throw_report["post_capture_save"] = save_report
                                field_pos["grid"] = list(save_report["field_grid"])
                                self.last_shiny_auto_throw = auto_throw_report
                                shiny["auto_throw_one_ball"] = auto_throw_report

                                if horde_size == 5 and defer_horde_shiny_record:
                                    self._record_horde_shiny_hold(
                                        payloads, duration_s=duration
                                    )
                                    defer_horde_shiny_record = False

                                self._post_battle_pokerus_check(br)
                                self.last_encounter = {
                                    **shiny,
                                    "horde_size": horde_size,
                                    "shiny_slots": [p["horde_slot"] for p in shiny_payloads],
                                    "blocked_shiny_slots": [
                                        p["horde_slot"] for p in blocked_shiny_payloads
                                    ],
                                    "result": "SHINY_CAPTURED_CONTINUE",
                                    "auto_throw_requested": True,
                                    "auto_throw_eligible": True,
                                    "throw_count": auto_throw_report.get("throw_count"),
                                    "horde_auto_capture": (
                                        {
                                            "protected_shiny_slot": auto_throw_report.get("protected_shiny_slot"),
                                            "protected_shiny_visual": auto_throw_report.get("protected_shiny_visual"),
                                            "total_attack_turns_before_ball": auto_throw_report.get("total_attack_turns_before_ball"),
                                            "survivor_proof": auto_throw_report.get("survivor_proof"),
                                        }
                                        if horde_size == 5 else None
                                    ),
                                    "post_capture_grid": list(field_pos.get("grid") or []),
                                    "pokedex_ram_observed": bool(post_capture.get("pokedex_observed")),
                                    "pokedex_ram_cleared": bool(post_capture.get("pokedex_cleared")),
                                    "pokedex_ram_base": post_capture.get("pokedex_base"),
                                }
                                self._log(
                                    f"SHINY AUTO-CATCH PASS: {shiny['species_name']} caught "
                                    f"after {auto_throw_report.get('throw_count')} Ball(s); "
                                    f"field authority restored at grid {field_pos.get('grid')} — continuing hunt"
                                )
                                self.status.emit(
                                    "RUNNING",
                                    (
                                        ("Fishing • " if self.method_key == "fishing" else "Wild • ")
                                        + (f"{self.target_species_name} • " if self.target_species_name else "")
                                        + f"{self.location_name} • {self.method_meta['name']}"
                                    ),
                                )
                                self.events.append({
                                    "time": datetime.now().astimezone().isoformat(
                                        timespec="seconds"
                                    ),
                                    "type": "SHINY_CAPTURED_CONTINUE",
                                    "species": shiny["species_name"],
                                    "pokemon_pid": shiny["pokemon_pid"],
                                    "throw_count": auto_throw_report.get("throw_count"),
                                    "field_grid": list(field_pos.get("grid") or []),
                                    "pokedex_ram_observed": bool(post_capture.get("pokedex_observed")),
                                    "pokedex_ram_cleared": bool(post_capture.get("pokedex_cleared")),
                                })
                                continue
                        except UserStop:
                            raise
                        except Exception as exc:
                            auto_throw_error = f"{type(exc).__name__}: {exc}"
                            preserved = dict(auto_throw_report or {})
                            preserved.update({
                                "result": "SAFETY_HOLD",
                                "error": auto_throw_error,
                                "maximum_throws": 50,
                            })
                            self.last_shiny_auto_throw = preserved
                            shiny["auto_throw_one_ball"] = dict(
                                self.last_shiny_auto_throw
                            )
                            self._log(
                                "SHINY AUTO-CATCH STOPPED SAFELY: "
                                f"{auto_throw_error}; no further input authorized"
                            )
                            self.events.append({
                                "time": datetime.now().astimezone().isoformat(
                                    timespec="seconds"
                                ),
                                "type": "SHINY_AUTO_CATCH_SAFETY_HOLD",
                                "error": auto_throw_error,
                            })

                    if horde_size == 5 and defer_horde_shiny_record:
                        self._record_horde_shiny_hold(
                            payloads, duration_s=duration
                        )
                        defer_horde_shiny_record = False

                    hold_result = "SHINY_HOLD"
                    if auto_throw_report is not None:
                        hold_result = "SHINY_AUTO_CATCH_INCOMPLETE_HOLD"
                    elif auto_throw_error is not None:
                        hold_result = "SHINY_AUTO_CATCH_SAFETY_HOLD"
                    self.last_encounter = {
                        **shiny,
                        "horde_size": horde_size,
                        "shiny_slots": [
                            p["horde_slot"] for p in shiny_payloads
                        ],
                        "blocked_shiny_slots": [
                            p["horde_slot"]
                            for p in blocked_shiny_payloads
                        ],
                        "result": hold_result,
                        "auto_throw_requested": (
                            self.auto_throw_one_poke_ball_on_shiny
                        ),
                        "auto_throw_eligible": auto_throw_eligible,
                    }
                    # Exactly-one-shiny Auto-Catch failures are safety holds,
                    # not ordinary SHINY_HOLD events. Multi-shiny/manual-hold
                    # policy remains SHINY_HOLD.
                    final_status = (
                        "SAFETY_HOLD"
                        if (auto_throw_error is not None or auto_throw_report is not None)
                        else "SHINY_HOLD"
                    )
                    reason = (
                        (
                            f"MULTI-SHINY HORDE ({len(shiny_payloads)} shinies) — SAFETY HOLD, no attacks or Balls"
                            if multi_shiny_horde
                            else (
                                f"SHINY FOUND {shiny['species_name']} "
                                f"PID {shiny['pokemon_pid']}"
                                + (
                                    f" in Horde slot {shiny['horde_slot']}"
                                    if horde_size == 5 else ""
                                )
                            )
                        )
                    )
                    if auto_throw_error is not None:
                        reason += (
                            " — auto-catch stopped safely: " + auto_throw_error
                        )
                    elif auto_throw_report is not None:
                        reason += " — auto-catch did not reach a confirmed field return"
                    raise core.SafetyHold(reason)

                # Look for Target is an orthogonal RAM filter. A normal shiny
                # above still wins with SHINY_HOLD. If a target match survives
                # that check (ordinary Pokémon or an explicitly blocklisted
                # shiny), HOLD it here before any Run input is authorized.
                if target_match_payloads:
                    duration = time.monotonic() - encounter_started_mono
                    if blocked_shiny_payloads:
                        completed_phase = self._commit_blocklisted_shiny_authority(payloads)
                        for payload in payloads:
                            payload["target_hold_phase_length"] = completed_phase
                    elif horde_size == 5:
                        self._commit_nonshiny_horde_seen(payloads)
                    else:
                        self.pokemon_seen_session += 1
                        self.species_counts[int(payloads[0]["species"])] += 1
                        self._record_encounter(payloads[0], duration_s=duration)

                    target = target_match_payloads[0]
                    self.last_encounter = {
                        **target,
                        "horde_size": horde_size,
                        "target_match_slots": [
                            p.get("horde_slot") for p in target_match_payloads
                        ],
                        "result": "TARGET_HOLD",
                    }
                    final_status = "TARGET_HOLD"
                    reason = (
                        f"TARGET FOUND {target['species_name']} "
                        f"PID {target['pokemon_pid']} • "
                        f"{target.get('nature')} • {target.get('gender')} • "
                        f"Hidden Power {target.get('hidden_power')} "
                        f"{target.get('hidden_power_power', 60)}"
                    )
                    self._log("TARGET MATCH: " + reason)
                    self.events.append({
                        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "type": "TARGET_MATCH",
                        "species": int(target["species"]),
                        "species_name": target["species_name"],
                        "pid": target["pokemon_pid"],
                        "criteria": self.target_criteria,
                        "horde_slots": [p.get("horde_slot") for p in target_match_payloads],
                    })
                    raise core.SafetyHold(reason)

                # One-shot Horde Auto-Attack Test: run the exact production
                # D25 live move selector against a proven ordinary five-slot Horde.
                # Any actual shiny or Target HOLD always takes priority. The test
                # executes one attack only, then the normal Horde escape path below
                # resumes; it never forges a shiny or enters the capture reducer.
                if (
                    self.horde_auto_attack_test_pending
                    and horde_size == 5
                    and not shiny_payloads
                    and not target_match_payloads
                ):
                    self._run_horde_auto_attack_test(
                        br, core, opponent_set, payloads
                    )

                # One-shot Auto-Capture Test: deliberately uses an ordinary
                # checksum-valid SINGLE opponent so hardware capture/retry/Pokédex
                # recovery can be tested without waiting for a shiny.  Never changes
                # is_shiny, never emits a shiny notification, and never increments
                # shiny counters.  A real shiny or Target HOLD always takes priority.
                if (
                    self.auto_capture_test_pending
                    and auto_capture_test_eligible(
                        game_key=game_profile["key"],
                        horde_size=horde_size,
                        is_shiny=bool(shiny_payloads),
                        target_match=bool(target_match_payloads),
                    )
                ):
                    self._run_single_auto_capture_test(
                        br, core, backend, opponent_set, payloads[0], movement,
                        anchor_grid, encounter_started_mono, game_profile
                    )
                    continue

                # Every shiny in this opponent set is explicitly blocklisted:
                # count/save it NOW, then continue through the normal causal
                # escape path. A later controller fault cannot erase the shiny.
                horde_seen_committed = False
                if blocked_shiny_payloads:
                    duration = time.monotonic() - encounter_started_mono
                    completed_phase = (
                        self._commit_blocklisted_shiny_authority(payloads)
                    )
                    horde_seen_committed = True
                    names = ", ".join(
                        p["species_name"] for p in blocked_shiny_payloads
                    )
                    self._log(
                        "SHINY BLOCKLIST RUN: "
                        f"{names} • completed phase {completed_phase} • "
                        "RAM shiny recorded; causal Run authorized"
                    )
                    self.events.append({
                        "time": datetime.now().astimezone().isoformat(
                            timespec="seconds"
                        ),
                        "type": "SHINY_BLOCKLIST_RUN",
                        "species": [
                            int(p["species"])
                            for p in blocked_shiny_payloads
                        ],
                        "species_names": [
                            p["species_name"]
                            for p in blocked_shiny_payloads
                        ],
                        "pids": [
                            p["pokemon_pid"]
                            for p in blocked_shiny_payloads
                        ],
                    })

                # A validated ordinary non-shiny Horde counts as five Pokémon
                # seen now, before escape.
                # before escape. A later Run/presentation failure must not erase
                # five already-authoritative shiny decisions.
                if horde_size == 5 and not horde_seen_committed:
                    self._commit_nonshiny_horde_seen(payloads)
                    horde_seen_committed = True
                    primary_pending = payloads[0]
                    self.last_encounter = {
                        **primary_pending,
                        "horde_size": horde_size,
                        "horde_species": [p["species_name"] for p in payloads],
                        "result": "AUTHORITY_PASS_ESCAPE_PENDING",
                    }
                    self._log(
                        f"HORDE AUTHORITY COMMITTED: +{len(payloads)} Pokémon seen "
                        "before escape"
                    )

                self._check_stop()
                hf97_prerun_processing_s = max(
                    0.0, time.monotonic() - hf97_boundary_wait_started - hf97_boundary_wait_s
                )
                hf97_run_started = time.monotonic()
                causal = self._causal_run_until_field(br, core)
                hf97_run_to_core_field_s = time.monotonic() - hf97_run_started
                if not causal.get("success"):
                    raise backend.IntegrationHold(
                        "causal Run failed: "
                        + causal.get("reason", "field did not become stable")
                    )

                if self.method_key in {"cave", "cave_run", "cave_bunny"}:
                    field_authority = wait_for_cave_field(
                        br,
                        backend,
                        self.location_zone,
                        movement.get("safe_return_grids") or [],
                    )
                    if not field_authority.get("ready"):
                        raise backend.IntegrationHold(
                            "post-escape Cave field authority failed: "
                            + str(field_authority.get("status"))
                        )
                    field_pos = field_authority["final_position"]
                    field_grid = require_cave_position(
                        backend, field_pos, self.location_zone, "post-escape Cave field"
                    )

                    # Cave Run hardware evidence showed ORAS can report stable
                    # field RAM before the overworld is ready to accept the next
                    # movement input. Re-arm exactly like the proven Horde fix:
                    # wait a bounded 1.00 s, then require the same logical cave
                    # grid again before another B+direction pulse is allowed.
                    if self.method_key == "cave_run":
                        self._log(
                            "CAVE RUN RE-ARM: waiting 1.00 s after battle field "
                            "return before next movement"
                        )
                        br.release_all()
                        time.sleep(1.00)
                        self._check_stop()
                        rearm = read_stable_cave_position(
                            br, backend, self.location_zone,
                            "Cave Run post-battle re-arm",
                            timeout=2.50, required_samples=2, poll_sec=0.10,
                        )
                        rearm_grid = tuple(rearm["grid"])
                        battle_rearm = br.u32(core.BATTLE_ADDR)
                        if battle_rearm != core.BATTLE_INACTIVE:
                            raise backend.IntegrationHold(
                                "Cave Run post-battle re-arm found battle state "
                                f"{backend.hx(battle_rearm)} instead of field inactive"
                            )
                        if rearm_grid != tuple(field_grid):
                            raise backend.IntegrationHold(
                                "Cave Run post-battle re-arm grid changed while idle: "
                                f"{list(field_grid)} -> {list(rearm_grid)}"
                            )
                        field_pos = rearm["position"]
                        field_grid = rearm_grid
                        self._log(
                            "CAVE RUN RE-ARM: PASS — battle inactive and cave "
                            f"grid {list(field_grid)} retained"
                        )

                    if self.method_key == "cave_bunny":
                        if tuple(field_grid) != tuple(anchor_grid):
                            raise backend.IntegrationHold(
                                f"Cave Acro drifted from anchor {list(anchor_grid)} "
                                f"to {list(field_grid)}"
                            )

                        # As with Cave Run/Hordes, field RAM can become stable
                        # before ORAS is ready for the next retained input. A
                        # premature B latch can therefore appear acknowledged
                        # while the bike does not resume clean bunny hops.
                        self._log(
                            "CAVE ACRO RE-ARM: waiting 1.00 s after battle field "
                            "return before next B latch"
                        )
                        br.release_all()
                        time.sleep(1.00)
                        self._check_stop()
                        acro_rearm = read_stable_cave_position(
                            br, backend, self.location_zone,
                            "Cave Acro post-battle re-arm",
                            timeout=2.50, required_samples=2, poll_sec=0.10,
                        )
                        acro_grid = tuple(acro_rearm["grid"])
                        battle_rearm = br.u32(core.BATTLE_ADDR)
                        if battle_rearm != core.BATTLE_INACTIVE:
                            raise backend.IntegrationHold(
                                "Cave Acro post-battle re-arm found battle state "
                                f"{backend.hx(battle_rearm)} instead of field inactive"
                            )
                        if acro_grid != tuple(anchor_grid):
                            raise backend.IntegrationHold(
                                "Cave Acro post-battle re-arm anchor changed while idle: "
                                f"{list(anchor_grid)} -> {list(acro_grid)}"
                            )
                        field_pos = acro_rearm["position"]
                        field_grid = acro_grid
                        self._log(
                            "CAVE ACRO RE-ARM: PASS — battle inactive and anchor "
                            f"grid {list(anchor_grid)} retained"
                        )
                elif self.method_key == "surf":
                    field_authority = wait_for_surf_field(
                        br,
                        backend,
                        self.location_zone,
                        movement.get("safe_return_grids") or [],
                    )
                    if not field_authority.get("ready"):
                        raise backend.IntegrationHold(
                            "post-escape Surf/Ocean field authority failed: "
                            + str(field_authority.get("status"))
                        )
                    field_pos = field_authority["final_position"]
                    require_water_position(
                        backend,
                        field_pos,
                        self.location_zone,
                        "post-escape Surf/Ocean field",
                    )
                elif self.method_key == "fishing":
                    field_authority = self._wait_for_fishing_field(
                        br, core, backend, timeout=5.0
                    )
                    if not field_authority.get("ready"):
                        raise backend.IntegrationHold(
                            "post-escape Fishing field authority failed: "
                            + str(field_authority.get("status"))
                        )
                    field_pos = field_authority["final_position"]
                    self._log(
                        "FISHING FIELD RE-ARM: PASS — battle inactive and "
                        "fishing action object idle; next Y cast authorized"
                    )
                elif self.method_key == "horde":
                    field_authority = self._wait_for_stationary_horde_field(
                        br, core, backend, anchor_grid
                    )
                    if not field_authority.get("ready"):
                        raise backend.IntegrationHold(
                            "post-escape stationary Horde authority failed: "
                            + str(field_authority.get("status"))
                        )
                    field_pos = field_authority["final_position"]
                    self._require_stationary_horde_position(
                        field_pos, "post-escape Horde field"
                    )
                else:
                    # v0p43EH: one Route 102 session restored field state
                    # later than the frozen 4 s window after 700 clean battles.
                    # Extend only this post-escape reacquisition window. The
                    # authority predicate is unchanged and no input is sent.
                    field_authority = backend.wait_for_field_authority(
                        br, timeout=WILD_POST_ESCAPE_FIELD_TIMEOUT_S
                    )
                    if not field_authority.get("ready"):
                        raise backend.IntegrationHold(
                            "post-escape field authority failed after bounded "
                            f"{WILD_POST_ESCAPE_FIELD_TIMEOUT_S:.1f}s reacquire: "
                            + str(field_authority.get("status"))
                        )
                    field_pos = field_authority["final_position"]
                    backend.require_safe_position(field_pos, "post-escape field")

                if self.method_key == "acro_bunny":
                    if not field_pos["mask"]["core2"]:
                        raise backend.IntegrationHold(
                            "Acro post-escape tile is no longer interior/core2 grass"
                        )
                    if tuple(field_pos["grid"]) != tuple(anchor_grid):
                        raise backend.IntegrationHold(
                            f"Acro drifted from anchor {list(anchor_grid)} "
                            f"to {field_pos['grid']}"
                        )

                # Battle has ended and the proven field/movement authority is back.
                # Take exactly one party snapshot and look for a strain-0 -> non-zero
                # Pokérus transition. This telemetry can never affect hunt control.
                self._post_battle_pokerus_check(br)

                if not horde_seen_committed:
                    for payload in payloads:
                        self.species_counts[int(payload["species"])] += 1
                    self.pokemon_seen_session += len(payloads)

                accepted = causal.get("accepted_attempt")
                # Keep diagnostic keys homogeneous. Some otherwise valid Run
                # paths do not report a numeric accepted_attempt.
                accepted_bucket = "unknown" if accepted is None else str(int(accepted))
                self.run_attempt_counts[accepted_bucket] += 1
                for a in causal.get("attempts", []):
                    if (a.get("touch") or {}).get("response_timeout_recovery"):
                        self.touch_timeout_recoveries += 1

                duration = time.monotonic() - encounter_started_mono
                battle_to_field_s = self._complete_encounter_timing(
                    duration, field_to_encounter_s
                )
                hf97_postcore_field_s = max(
                    0.0, time.monotonic() - hf97_run_started - hf97_run_to_core_field_s
                )
                self._log(
                    f"TIMING #{self.attempt}: field->encounter "
                    f"{field_to_encounter_s:.3f}s • battle+return "
                    f"{battle_to_field_s:.3f}s • cycle {duration:.3f}s"
                )
                self._log(
                    f"HF97 PHASE #{self.attempt}: boundary_wait "
                    f"{hf97_boundary_wait_s:.3f}s • pre_run_processing "
                    f"{hf97_prerun_processing_s:.3f}s • run_to_core_field "
                    f"{hf97_run_to_core_field_s:.3f}s • postcore_field_authority "
                    f"{hf97_postcore_field_s:.3f}s • accepted_run_attempt "
                    f"{causal.get('accepted_attempt')}"
                )
                for payload in payloads:
                    if horde_seen_committed:
                        self._append_encounter_history_only(
                            payload,
                            causal=causal,
                            field_pos=field_pos,
                            duration_s=duration,
                            field_to_encounter_s=field_to_encounter_s,
                            battle_to_field_s=battle_to_field_s,
                        )
                    else:
                        self._record_encounter(
                            payload,
                            causal=causal,
                            field_pos=field_pos,
                            duration_s=duration,
                            field_to_encounter_s=field_to_encounter_s,
                            battle_to_field_s=battle_to_field_s,
                        )

                if horde_seen_committed:
                    # _append_encounter_history_only deliberately does not emit
                    # stats, so publish the completed HF95 timing sample here.
                    self.stats.emit(self._stats_payload())

                primary = payloads[0]
                self.last_encounter = {
                    **primary,
                    "horde_size": horde_size,
                    "horde_species": [p["species_name"] for p in payloads],
                    "accepted_run_attempt": accepted,
                    "post_escape_grid": field_pos["grid"],
                    "result": "PASS",
                }
                self._log(
                    f"PASS #{self.attempt}: "
                    + (f"Horde x5 • " if horde_size == 5 else "")
                    + f"Run tap {accepted} -> "
                    f"{self.location_name} grid {field_pos['grid']}"
                )

        except UserStop as exc:
            final_status = "IDLE"
            reason = str(exc)
            self._log(
                "MANUAL_STOP: inputs released; no new wild input authorized"
            )
            self.events.append({
                "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                "type": "MANUAL_STOP",
                "reason": str(exc),
            })
        except Exception as exc:
            reason = str(exc)
            if final_status not in {"SHINY_HOLD", "TARGET_HOLD", "SAFETY_HOLD"}:
                # Explicit shiny/target paths set final_status before raising.
                if "SHINY FOUND" in reason:
                    final_status = "SHINY_HOLD"
                elif "TARGET FOUND" in reason:
                    final_status = "TARGET_HOLD"
                else:
                    final_status = "SAFETY_HOLD"
            self._log(f"{final_status}: {type(exc).__name__}: {exc}")
            self.events.append({
                "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                "type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            })
        finally:
            try:
                if self.bridge is not None:
                    self.bridge.release_all()
            except Exception as exc:
                self.events.append({
                    "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "release_error": f"{type(exc).__name__}: {exc}",
                })

            try:
                elapsed = max(0.0, time.monotonic() - self.started_mono) if self.started_mono else 0.0
                self.lifetime["lifetime_hunt_seconds"] = round(
                    float(self.lifetime.get("lifetime_hunt_seconds", 0.0)) + elapsed, 3
                )
                current_rate = (self.attempt * 3600.0 / elapsed) if elapsed > 0 else 0.0
                prior_rate = self.lifetime.get("lifetime_fastest_rate")
                if prior_rate is None or current_rate > float(prior_rate):
                    self.lifetime["lifetime_fastest_rate"] = round(current_rate, 3)
                self._save_stats()
            except Exception:
                pass
            self._save_support(final_status, reason)
            self.stats.emit(self._stats_payload())
            self.session_finished.emit(final_status)
