from __future__ import annotations

from pokebot.common.ability_names import ability_name
import json
import struct
import time
import traceback
import zipfile
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from pokebot.common.bridge import Bridge
from pokebot.common.acknowledged_input import AcknowledgedInput
from pokebot.common.oras_profiles import profile_from_game_info, BATTLE_STATE, BATTLE_INACTIVE
from pokebot.common.reset_adapter import run_reset_to_field_for_profile
from pokebot.common.species_names import SPECIES_NAMES
from pokebot.common.pk6 import NATURE_NAMES
from pokebot.common.shiny_odds import (
    detect_oras_shiny_charm, resolve_shiny_odds, advance_phase_log_miss,
    cumulative_probability_from_log_miss, phase_log_miss_for_constant,
)
from pokebot.common.live_party import get_runtime_party_snapshot_for_bridge
from pokebot.static.soaring_mapper import read_soaring_sample, summarize_trace
from pokebot.static.skytrip_runtime import (
    locate_skytrip_module,
    verify_skytrip_relocation,
    discover_skytrip_objects,
    choose_trace_objects,
    read_trace_objects,
    read_runtime_module_state,
    diff_runtime_module_state,
    discover_skytrip_external_references,
    read_external_trace_targets,
    diff_external_trace_state,
    discover_skytrip_fast_references,
    build_fast_trace_batches,
    read_external_trace_targets_batched,
    update_fast_motion_scores,
    runtime_contract as skytrip_runtime_contract,
)
from pokebot.static.dexnav_target_nav import (
    DEXNAV_TARGET_BLOCK, DEXNAV_TARGET_BLOCK_LEN, decode_target_block, steering_command,
)
from pokebot.static.oras_static import (
    BATTLE_ACTIVE,
    BATTLE_TRANSITION,
    StaticEncounterStateMachine,
    get_static_profile,
    read_saved_field_anchor,
    validate_any_loaded_field,
    validate_saved_field_anchor,
)
from pokebot.wild.validated_loader import load_walk_v0p23

from .appdata_store import get_profile_paths, increment_species_shiny_total


class UserStop(RuntimeError):
    pass


class StaticSafetyHold(RuntimeError):
    pass


DEXNAV_TUTORIAL_START_WORLD = (1917.0, 2349.0)
DEXNAV_TUTORIAL_VISIBLE_WORLD = (1791.0, 2349.0)
DEXNAV_TUTORIAL_POSITION_EPSILON = 1.75
DEXNAV_TUTORIAL_A_PRESSES = 17
DEXNAV_TUTORIAL_FANG_NAMES = {422: "Thunder Fang", 423: "Ice Fang", 424: "Fire Fang"}


class StaticHuntWorker(QObject):
    """Save-facing ORAS Static hunt integration candidate.

    Hardware-proven primitives reused unchanged:
    - read-only RAM bridge
    - acknowledged controller transport
    - normal single-opponent PK6 address/decoder + state=2 boundary
    - ORAS soft-reset/title/Continue route for ordinary statics
    - validated Wild causal RUN touch for RUN_REINTERACT statics
    - shiny confirmation always stops with no further controller input

    New/unproven part: per-static physical trigger profile.  Any failed trigger,
    wrong species, stale identity, reset-position mismatch or unexpected battle
    state becomes an explicit Safety Hold.
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
    session_finished = Signal(str)

    def __init__(
        self,
        *,
        profile_key: str,
        host: str,
        base_dir,
        bridge_port: int = 4952,
        input_port: int = 4952,
        timeout: float = 1.5,
        auto_support_zip: bool = True,
        use_code_ips: bool = False,
    ):
        super().__init__()
        self.profile_key = str(profile_key or "").strip().lower()
        self.static_profile = get_static_profile(self.profile_key)
        self.host = str(host)
        self.base_dir = Path(base_dir)
        self.bridge_port = int(bridge_port)
        self.input_port = int(input_port)
        self.timeout = float(timeout)
        self.auto_support_zip = bool(auto_support_zip)
        self.use_code_ips = bool(use_code_ips)
        self.stop_requested = False
        self.read_count = 0
        self.started_monotonic = None
        self.started_iso = datetime.now().astimezone().isoformat(timespec="seconds")

        profile = get_profile_paths()
        self.profile_paths = profile
        self.log_dir = profile.root / "logs"
        self.support_dir = profile.root / "support"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.support_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_path = self.log_dir / f"static_{self.profile_key}_{stamp}.log"
        self.soaring_trace_path = self.log_dir / f"static_{self.profile_key}_{stamp}_soaring_trace.jsonl"
        self.skytrip_runtime_path = self.log_dir / f"static_{self.profile_key}_{stamp}_skytrip_runtime.json"
        self.skytrip_fast_plan_path = profile.root / "cache" / "skytrip_as14_fast_reference_plan.json"
        self.skytrip_fast_plan_path.parent.mkdir(parents=True, exist_ok=True)
        self.soaring_trace_summary = None
        self.skytrip_runtime_summary = None
        profile.stats_dir.mkdir(parents=True, exist_ok=True)
        profile.history_dir.mkdir(parents=True, exist_ok=True)
        profile.encounters_dir.mkdir(parents=True, exist_ok=True)
        self.stats_path = profile.stats_dir / f"static_{self.profile_key}.json"
        self.session_path = profile.session_stats_dir / f"static_{self.profile_key}_latest.json"
        self.encounter_path = profile.encounters_dir / f"static_{self.profile_key}.jsonl"
        self.last_seen_path = profile.last_seen_path
        self.lifetime = self._load_stats()
        self.session_seen = 0
        self.session_shinies = 0
        self.session_resets = 0
        self.session_reinteracts = 0
        self.anchor = None
        self.game_profile = None
        self.shiny_charm_state = {"detected": None, "status": "UNVERIFIED"}
        self.state_machine = None
        self.last_pk6 = None
        self.last_reset = None
        self.last_trigger = None
        self.run_reinteract_pending = False
        self.last_boundary = None
        self.last_encounter_monotonic = None
        self._lifetime_time_committed = False

    def _default_stats(self):
        return {
            "hunt_type": "Static",
            "static_key": self.profile_key,
            "target": self.static_profile.name,
            "species": self.static_profile.species,
            "location_name": self.static_profile.location,
            "lifetime_seen": 0,
            "lifetime_shinies": 0,
            "phase_seen": 0,
            "last_phase_seen": 0,
            "phase_log_miss": None,
            "phase_cumulative_probability": 0.0,
            "last_phase_cumulative_probability": None,
            "lifetime_resets": 0,
            "lifetime_reinteracts": 0,
            "last_shiny": None,
            "highest_sv": None,
            "lowest_sv": None,
            "highest_iv_sum": None,
            "lowest_iv_sum": None,
            "lifetime_hunt_seconds": 0.0,
            "hardware_validation": self.static_profile.choreography_status,
        }

    def _load_stats(self):
        default = self._default_stats()
        try:
            data = json.loads(self.stats_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                default.update(data)
        except Exception:
            pass
        # Validation status is build/profile authority, not historical telemetry.
        # Never let an older persistent stats file pin the dashboard/support data
        # to NEEDS_HARDWARE_VALIDATION after the profile has been promoted.
        default["hardware_validation"] = self.static_profile.choreography_status
        return default

    @staticmethod
    def _atomic_json(path: Path, payload: dict | list):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _initialize_phase_probability(self):
        if (self.shiny_charm_state or {}).get("detected") is None:
            self.lifetime["phase_log_miss"] = None
            self.lifetime["phase_cumulative_probability"] = None
            return
        if self.lifetime.get("phase_log_miss") is not None:
            self.lifetime["phase_cumulative_probability"] = cumulative_probability_from_log_miss(
                self.lifetime.get("phase_log_miss", 0.0)
            )
            return
        odds = resolve_shiny_odds(
            game=(self.game_profile or {}).get("name", "ORAS"),
            hunt_type="Static",
            shiny_charm_present=(self.shiny_charm_state or {}).get("detected"),
            shiny_charm_applies=True,
        )
        self.lifetime["phase_log_miss"] = phase_log_miss_for_constant(
            int(self.lifetime.get("phase_seen", 0) or 0), odds.probability
        )
        self.lifetime["phase_cumulative_probability"] = cumulative_probability_from_log_miss(
            self.lifetime["phase_log_miss"]
        )

    def _save_stats(self):
        # Keep the lifetime JSON synchronized with the current profile authority.
        # This fixes old static_dialga.json files retaining stale validation text.
        self.lifetime["hardware_validation"] = self.static_profile.choreography_status
        self._atomic_json(self.stats_path, self.lifetime)
        elapsed = max(0.0, time.monotonic() - self.started_monotonic) if self.started_monotonic else 0.0
        self._atomic_json(self.session_path, {
            "hunt_type": "Static",
            "static_key": self.profile_key,
            "target": self.static_profile.name,
            "species": self.static_profile.species,
            "location_name": self.static_profile.location,
            "session_seen": self.session_seen,
            "session_shinies": self.session_shinies,
            "session_resets": self.session_resets,
            "session_reinteracts": self.session_reinteracts,
            "elapsed_seconds": elapsed,
            "saved_field_anchor": self.anchor.as_dict() if self.anchor else None,
            "state_machine": self.state_machine.snapshot() if self.state_machine else None,
            "last_trigger": self.last_trigger,
            "last_boundary": self.last_boundary,
            "last_pk6": self.last_pk6,
            "last_reset": self.last_reset,
            "hardware_validation": self.static_profile.choreography_status,
            "updated": datetime.now().astimezone().isoformat(timespec="seconds"),
        })
        self.stats.emit(self._stats_payload())

    def _stats_payload(self):
        elapsed = max(0.0, time.monotonic() - self.started_monotonic) if self.started_monotonic else 0.0
        seen = int(self.session_seen)
        return {
            **dict(self.lifetime),
            "hunt_type": "Static",
            "static_key": self.profile_key,
            "target": self.static_profile.name,
            "species": self.static_profile.species,
            "location_name": self.static_profile.location,
            "phase_index": int(self.lifetime.get("lifetime_shinies", 0)) + 1,
            "phase_seen": int(self.lifetime.get("phase_seen", 0)),
            "phase_log_miss": self.lifetime.get("phase_log_miss"),
            "phase_cumulative_probability": self.lifetime.get("phase_cumulative_probability"),
            "shiny_charm": dict(self.shiny_charm_state or {}),
            "shiny_odds_display": resolve_shiny_odds(
                game=(self.game_profile or {}).get("name", "ORAS"),
                hunt_type="Static",
                shiny_charm_present=(self.shiny_charm_state or {}).get("detected"),
                shiny_charm_applies=True,
            ).display,
            "session_seen": seen,
            "resets": int(self.session_resets),
            "reinteracts": int(self.session_reinteracts),
            "average_time": (elapsed / seen) if seen else 0.0,
            "hardware_validation": self.static_profile.choreography_status,
        }

    def _log(self, text: str, **fields):
        stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
        suffix = ""
        if fields:
            suffix = " " + json.dumps(fields, sort_keys=True, default=str)
        line = f"{stamp} {text}{suffix}"
        try:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass
        self.log_line.emit(line)

    def _check_stop(self):
        if self.stop_requested:
            raise UserStop("user stop requested")

    def request_stop(self):
        self.stop_requested = True
        self.status.emit("STOPPING", "Waiting for Static safe boundary")

    def _read_u32(self, bridge: Bridge, address: int) -> int:
        self.read_count += 1
        self.ram_reads.emit(self.read_count)
        # Static battle-state reads are idempotent. Hardware support traces have
        # shown rare single lost UDP replies while the game/bridge remain healthy,
        # so retry the exact same 4-byte read once before escalating to SAFETY HOLD.
        raw = self._transport_read(
            bridge, address, 4, attempts=2, label="STATIC_U32"
        )
        return struct.unpack("<I", raw)[0]

    def _battle_active(self, bridge: Bridge) -> bool:
        return self._read_u32(bridge, BATTLE_STATE) == BATTLE_ACTIVE

    def _wait_transition_resolution(self, bridge: Bridge, timeout: float = 8.0) -> dict:
        """Resolve the hardware-proven ORAS 0x00000000 battle transition neutrally.

        Wild/Cave/Surf already treat 0x00000000 as a legitimate transition
        between field (0x00040000) and active battle (0x00040001).  BP
        incorrectly treated it as an impossible state.  Static now sends no
        further controller input while the transition is unresolved.
        """
        deadline = time.monotonic() + float(timeout)
        samples = []
        while time.monotonic() < deadline:
            self._check_stop()
            state = self._read_u32(bridge, BATTLE_STATE)
            samples.append(f"0x{state:08X}")
            if state == BATTLE_ACTIVE:
                return {"battle": True, "resolved": "BATTLE_ACTIVE", "samples": samples}
            if state == BATTLE_INACTIVE:
                return {"battle": False, "resolved": "FIELD_INACTIVE", "samples": samples}
            if state != BATTLE_TRANSITION:
                return {"battle": False, "resolved": "UNEXPECTED", "unexpected": f"0x{state:08X}", "samples": samples}
            time.sleep(0.08)
        return {"battle": False, "resolved": "UNRESOLVED", "samples": samples, "timeout": True}

    def _wait_battle_active(self, bridge: Bridge, timeout: float) -> dict:
        deadline = time.monotonic() + float(timeout)
        samples = []
        transitions = []
        while time.monotonic() < deadline:
            self._check_stop()
            state = self._read_u32(bridge, BATTLE_STATE)
            samples.append(f"0x{state:08X}")
            if state == BATTLE_ACTIVE:
                return {"battle": True, "samples": samples, "transitions": transitions}
            if state == BATTLE_TRANSITION:
                transition = self._wait_transition_resolution(bridge, 8.0)
                transitions.append(transition)
                samples.extend(list(transition.get("samples") or []))
                if transition.get("battle"):
                    return {"battle": True, "samples": samples, "transitions": transitions}
                if transition.get("unexpected"):
                    return {"battle": False, "unexpected": transition.get("unexpected"), "samples": samples, "transitions": transitions}
                if transition.get("resolved") == "UNRESOLVED":
                    return {"battle": False, "transition_unresolved": True, "samples": samples, "transitions": transitions}
                # A transition that resolves back to field is not unsafe.  It
                # simply means this interaction did not enter battle; allow
                # the bounded trigger loop to continue.
                continue
            if state != BATTLE_INACTIVE:
                return {"battle": False, "unexpected": f"0x{state:08X}", "samples": samples, "transitions": transitions}
            time.sleep(0.10)
        return {"battle": False, "samples": samples, "transitions": transitions, "timeout": True}

    def _pulse(self, inputs: AcknowledgedInput, buttons, hold=90, settle=90):
        self._check_stop()
        return inputs.pulse(
            tuple(buttons),
            hold_ms=int(hold),
            resume_settle_ms=0,
            packet_interval_ms=20,
            release_ms=int(settle),
        )

    def _wait_saved_field_authority(self, bridge: Bridge, timeout: float = 6.0, stable_samples: int = 3) -> dict:
        """Wait for exact saved-field/grid authority after a non-reset battle exit.

        RUN_REINTERACT deliberately sends no A after RUN until battle teardown and
        the same saved Southern Island position are RAM-proven.  This prevents a
        fast return animation from consuming the next interaction early.
        """
        deadline = time.monotonic() + float(timeout)
        consecutive = 0
        samples = []
        last = None
        while time.monotonic() < deadline:
            self._check_stop()
            last = validate_saved_field_anchor(bridge, self.anchor)
            sample = {
                "authority": bool(last.get("authority")),
                "battle": last.get("battle"),
                "zone": last.get("zone"),
                "grid": last.get("grid"),
                "checks": last.get("checks"),
            }
            samples.append(sample)
            if last.get("authority"):
                consecutive += 1
                if consecutive >= max(1, int(stable_samples)):
                    return {
                        "authority": True,
                        "stable_samples": consecutive,
                        "samples": samples[-12:],
                        "last": last,
                    }
            else:
                consecutive = 0
            time.sleep(0.10)
        return {
            "authority": False,
            "stable_samples": consecutive,
            "samples": samples[-12:],
            "last": last,
            "timeout": True,
        }

    def _run_reinteract_return(self, bridge: Bridge, battle_br, core) -> dict:
        """Exit a validated non-shiny battle via the shared causal RUN controller.

        This is intentionally generic: any Static profile with reset_kind
        RUN_REINTERACT can reuse it.  No soft reset/title/Continue input occurs.
        """
        before = self._read_u32(bridge, BATTLE_STATE)
        if before != BATTLE_ACTIVE:
            raise StaticSafetyHold(
                f"RUN_REINTERACT requires active battle before RUN; observed 0x{before:08X}"
            )
        self._log(
            "STATIC RUN_REINTERACT RUN ABOUT TO SEND",
            profile=self.static_profile.key,
            target=self.static_profile.name,
        )
        causal = core.causal_run_until_field(battle_br)
        self._log("STATIC RUN_REINTERACT RUN RESULT", result=causal)
        if not causal.get("success"):
            raise StaticSafetyHold(
                f"RUN_REINTERACT failed to leave battle safely: {causal.get('reason') or causal}"
            )
        field = self._wait_saved_field_authority(bridge, timeout=6.0, stable_samples=3)
        self._log("STATIC RUN_REINTERACT FIELD AUTHORITY", result=field)
        if not field.get("authority"):
            raise StaticSafetyHold(
                f"RUN_REINTERACT did not return to the saved interaction tile: {field}"
            )
        return {
            "status": "RUN_REINTERACT_FIELD_READY",
            "run": causal,
            "field": field,
            "next_action": "INTERACT_A_ON_NEXT_LOOP",
        }

    def _mapper_read(self, bridge: Bridge, address: int, length: int):
        """Counted, bounded read used only by the active Soaring mapper."""
        self.read_count += 1
        self.ram_reads.emit(self.read_count)
        return self._transport_read(
            bridge, address, length, attempts=3, label="SOARING_MAPPER_RAM"
        )

    def _append_soaring_trace(self, record: dict):
        row = dict(record)
        row.setdefault("time", datetime.now().astimezone().isoformat(timespec="milliseconds"))
        try:
            with self.soaring_trace_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")
        except Exception as exc:
            self._log("SOARING TRACE WRITE FAILED", error=f"{type(exc).__name__}: {exc}")

    def _save_skytrip_runtime(self, payload: dict):
        self.skytrip_runtime_summary = dict(payload or {})
        try:
            self._atomic_json(self.skytrip_runtime_path, self.skytrip_runtime_summary)
        except Exception as exc:
            self._log("SKYTRIP RUNTIME EVIDENCE WRITE FAILED", error=f"{type(exc).__name__}: {exc}")

    def _run_soaring_rift_mapper(
        self, bridge: Bridge, inputs: AcknowledgedInput, core, save_tid: int, save_sid: int, party_payload: dict | None
    ):
        """Hardware mapper for the Dewford Dimensional Rift takeoff path.

        The bot performs only the deterministic reset-to-save and one Y press.
        Flight/navigation remains entirely manual in this build.  While the
        user flies, shared ORAS battle/zone/coordinate RAM is sampled until a
        battle becomes active, at which point the normal validated PK6 boundary
        is used to confirm the expected legendary.
        """
        p = self.static_profile
        required = {int(x) for x in (p.required_party_species or ())}
        snapshot = dict(party_payload or {})
        payload = list(snapshot.get("payload") or [])
        parsed = list(snapshot.get("parsed") or [])
        present = {
            int(mon.get("species_id") or 0)
            for mon in payload
            if int(mon.get("species_id") or 0) > 0
        }
        # The UI payload's ``species`` field is a display name ("Uxie"), while
        # ``species_id`` is numeric. Parsed PK6 rows use numeric ``species``.
        # CA used the wrong snapshot key/field and therefore saw an empty party.
        if not present:
            present = {
                int(mon.get("species") or 0)
                for mon in parsed
                if int(mon.get("species") or 0) > 0
            }
        self._log(
            "SOARING RIFT PARTY REQUIREMENT",
            source=snapshot.get("source"),
            required_species=sorted(required),
            present_species=sorted(present),
            payload_species_ids=[int(mon.get("species_id") or 0) for mon in payload],
        )
        if required and not present and str(snapshot.get("source") or "").startswith("UNMAPPED"):
            raise StaticSafetyHold(
                f"{p.name} Soaring mapper could not map the live party; open Party Viewer or retry, then export support if it repeats"
            )
        missing = sorted(required - present)
        if missing:
            names = [SPECIES_NAMES.get(x, f"Species {x}") for x in missing]
            raise StaticSafetyHold(
                f"{p.name} Soaring mapper missing required party species: {', '.join(names)}"
            )

        try:
            if self.soaring_trace_path.exists():
                self.soaring_trace_path.unlink()
        except Exception:
            pass

        anchor_dict = self.anchor.as_dict() if self.anchor else None
        self._append_soaring_trace({
            "kind": "mapper_start",
            "profile": p.key,
            "target": p.name,
            "expected_species": p.species,
            "saved_anchor": anchor_dict,
            "required_party_species": sorted(required),
            "present_party_species": sorted(present),
            "policy": "bot sends Y once; user manually flies/enters rift; no bot movement",
        })

        # Baseline sample before Y gives us the exact Dewford field signature.
        samples = []
        baseline = read_soaring_sample(lambda a, n: self._mapper_read(bridge, a, n))
        baseline.update({"kind": "sample", "index": 0, "elapsed_s": 0.0, "phase": "BEFORE_Y"})
        samples.append(baseline)
        self._append_soaring_trace(baseline)

        # CD maps DllSkyTrip across its complete lifecycle, beginning before Y.
        # This catches modules that are already resident, load only during the
        # takeoff transition, load later during manual flight, or unload as the
        # battle transition takes over.  All discovery remains read-only.
        self._append_soaring_trace({"kind": "skytrip_contract", **skytrip_runtime_contract()})
        lifecycle = {
            "events": [],
            "first_present": None,
            "last_present": None,
            "first_unload": None,
            "locate_attempts": 0,
        }
        skytrip_locate = None
        skytrip_target = None
        relocation = {"verified": False, "reason": "MODULE_NOT_FOUND"}
        discovery = {"status": "SKIPPED_RELOCATION_NOT_VERIFIED", "candidates": {}}
        selected_objects = []
        mapped_base = None
        module_present = False
        anchor_probe_done = False
        previous_module_state = None
        state_change_events = []
        external_reference_map = {"status": "NOT_SCANNED", "trace_targets": []}
        external_trace_targets = []
        external_trace_batches = []
        external_scanned_base = None
        previous_external_state = None
        external_state_change_events = []
        fast_motion_state = {}
        fast_motion_lock = None
        try:
            cached_fast_plan = json.loads(self.skytrip_fast_plan_path.read_text(encoding="utf-8"))
            if not isinstance(cached_fast_plan, dict):
                cached_fast_plan = None
        except Exception:
            cached_fast_plan = None

        def _target_base(target):
            if not isinstance(target, dict):
                return None
            value = target.get("base_int")
            if value is not None:
                try:
                    return int(value)
                except Exception:
                    pass
            value = target.get("base")
            try:
                return int(str(value), 0) if value is not None else None
            except Exception:
                return None

        def _locate_skytrip(phase: str, elapsed_s: float | None = None):
            nonlocal skytrip_locate, module_present
            lifecycle["locate_attempts"] += 1
            attempt = int(lifecycle["locate_attempts"])
            try:
                locate = locate_skytrip_module(bridge)
            except Exception as exc:
                locate = {"present": False, "error": f"{type(exc).__name__}: {exc}"}
            skytrip_locate = locate
            present_now = bool((locate or {}).get("present"))
            target = (locate or {}).get("target") if present_now else None
            base = _target_base(target)
            transition = None
            if present_now:
                event = {
                    "phase": phase,
                    "attempt": attempt,
                    "elapsed_s": round(float(elapsed_s), 3) if elapsed_s is not None else None,
                    "base": f"0x{base:08X}" if base is not None else None,
                }
                lifecycle["last_present"] = dict(event)
                if lifecycle["first_present"] is None:
                    lifecycle["first_present"] = dict(event)
                    transition = "LOAD"
                elif not module_present:
                    transition = "RELOAD"
                if transition:
                    lifecycle["events"].append({"event": transition, **event})
                module_present = True
            else:
                if module_present:
                    event = {
                        "event": "UNLOAD",
                        "phase": phase,
                        "attempt": attempt,
                        "elapsed_s": round(float(elapsed_s), 3) if elapsed_s is not None else None,
                    }
                    lifecycle["events"].append(event)
                    if lifecycle["first_unload"] is None:
                        lifecycle["first_unload"] = dict(event)
                    transition = "UNLOAD"
                module_present = False
            self._append_soaring_trace({
                "kind": "skytrip_locate",
                "phase": phase,
                "attempt": attempt,
                "transition": transition,
                "present": present_now,
                "module_base": f"0x{base:08X}" if base is not None else None,
                "query_count": (locate or {}).get("query_count"),
                "elapsed_seconds": (locate or {}).get("elapsed_seconds"),
                "error": (locate or {}).get("error"),
            })
            return locate, target, transition

        def _map_loaded_target(target, phase: str, *, force_external: bool = False):
            nonlocal relocation, discovery, selected_objects, mapped_base, anchor_probe_done
            nonlocal external_reference_map, external_trace_targets, external_trace_batches, external_scanned_base
            nonlocal previous_external_state, cached_fast_plan, fast_motion_state, fast_motion_lock
            if not target:
                relocation = {"verified": False, "reason": "MODULE_NOT_FOUND"}
                discovery = {"status": "CK_LEGACY_OBJECT_PROBES_SKIPPED", "candidates": {}}
                selected_objects = []
                mapped_base = None
                return
            relocation = verify_skytrip_relocation(bridge, target)
            discovery = {
                "status": "CK_LEGACY_OBJECT_PROBES_SKIPPED",
                "reason": "hardware disproved repeated local vptr/.data/.bss motion probes; fast external-reference mapper is authoritative",
                "candidates": {},
            }
            selected_objects = []
            mapped_base = _target_base(target)
            if relocation.get("verified"):
                if force_external or external_scanned_base != mapped_base:
                    cache_text = "cached" if cached_fast_plan else "first-pass"
                    self.status.emit(
                        "MAPPER",
                        f"{p.name}: DllSkyTrip verified • CK fast reference map ({cache_text})",
                    )
                    try:
                        external_reference_map = discover_skytrip_fast_references(
                            bridge, target, static_plan=cached_fast_plan
                        )
                        plan = external_reference_map.get("static_reference_plan")
                        if isinstance(plan, dict):
                            cached_fast_plan = plan
                            try:
                                self._atomic_json(self.skytrip_fast_plan_path, plan)
                            except Exception as cache_exc:
                                self._log("SKYTRIP FAST PLAN CACHE WRITE FAILED", error=f"{type(cache_exc).__name__}: {cache_exc}")
                        external_trace_targets = list(external_reference_map.get("trace_targets") or [])
                        external_trace_batches = build_fast_trace_batches(external_trace_targets)
                        external_scanned_base = mapped_base
                        previous_external_state = None
                        fast_motion_state = {}
                        fast_motion_lock = None
                        self._log(
                            "SKYTRIP FAST REFERENCE MAP",
                            phase=phase,
                            status=external_reference_map.get("status"),
                            cache_status=external_reference_map.get("cache_status"),
                            literal_decoder=external_reference_map.get("literal_decoder"),
                            literal_slots=external_reference_map.get("literal_slot_count"),
                            resolved_literal_slots=external_reference_map.get("resolved_literal_slot_count"),
                            scanned_segments=len(external_reference_map.get("scanned_segments") or []),
                            writable_roots=len(external_reference_map.get("writable_roots") or []),
                            trace_targets=len(external_trace_targets),
                            read_batches=len(external_trace_batches),
                        )
                    except Exception as ext_exc:
                        external_reference_map = {
                            "status": "FAST_REFERENCE_EXCEPTION",
                            "error": f"{type(ext_exc).__name__}: {ext_exc}",
                            "trace_targets": [],
                        }
                        external_trace_targets = []
                        external_trace_batches = []
                        external_scanned_base = mapped_base
            else:
                self._log("SKYTRIP RELOCATION VERIFY FAILED", phase=phase, relocation=relocation)
            self._append_soaring_trace({
                "kind": "skytrip_discovery_summary",
                "phase": phase,
                "module_present": True,
                "module_base": f"0x{mapped_base:08X}" if mapped_base is not None else None,
                "relocation_verified": bool(relocation.get("verified")),
                "discovery_status": discovery.get("status"),
                "selected_trace_objects": selected_objects,
                "external_reference_status": external_reference_map.get("status"),
                "external_cache_status": external_reference_map.get("cache_status"),
                "external_trace_target_count": len(external_trace_targets),
                "external_read_batch_count": len(external_trace_batches),
            })
            self._log(
                "SKYTRIP RUNTIME OBJECT MAP",
                phase=phase,
                module_base=f"0x{mapped_base:08X}" if mapped_base is not None else None,
                relocation=relocation,
                discovery_status=discovery.get("status"),
                selected_trace_objects=selected_objects,
                external_reference_status=external_reference_map.get("status"),
                external_cache_status=external_reference_map.get("cache_status"),
                external_trace_target_count=len(external_trace_targets),
                external_read_batch_count=len(external_trace_batches),
            )

        # First lifecycle sample occurs before the Y press.
        self.status.emit("MAPPER", f"{p.name}: pre-Y DllSkyTrip lifecycle sample")
        pre_y_locate, pre_y_target, pre_y_transition = _locate_skytrip("BEFORE_Y", 0.0)
        if pre_y_target:
            skytrip_target = pre_y_target
            _map_loaded_target(pre_y_target, "BEFORE_Y")

        self.status.emit("MAPPER", f"{p.name} Dimensional Rift mapper • pressing Y once")
        self._log("SOARING RIFT MAPPER Y ABOUT TO SEND", profile=p.key, saved_anchor=anchor_dict)
        y_ack = self._pulse(inputs, ("Y",), 120, 300)
        self._log("SOARING RIFT MAPPER Y SENT", profile=p.key, ack=y_ack)
        self._append_soaring_trace({"kind": "input", "buttons": ["Y"], "ack": y_ack})

        # Poll aggressively during takeoff. If the module was not present before
        # Y, discovery starts on the first sample where it becomes resident.
        self.status.emit("MAPPER", f"{p.name}: Y sent • watching DllSkyTrip load lifecycle")
        y_time = time.monotonic()
        skytrip_deadline = y_time + 15.0
        while time.monotonic() < skytrip_deadline:
            self._check_stop()
            elapsed_y = time.monotonic() - y_time
            locate, target, transition = _locate_skytrip("AFTER_Y_TRANSITION", elapsed_y)
            if target:
                # Always remap once after Y even if DllSkyTrip was already
                # resident before takeoff; runtime objects may be constructed
                # only after the Soaring transition begins.
                skytrip_target = target
                _map_loaded_target(target, "AFTER_Y_TRANSITION")
                break
            skytrip_target = None
            selected_objects = []
            mapped_base = None
            time.sleep(0.40)

        def _save_runtime_evidence(stage: str):
            runtime_evidence = {
                "contract": skytrip_runtime_contract(),
                "stage": stage,
                "pre_y_locate": pre_y_locate,
                "latest_locate": skytrip_locate,
                "relocation": relocation,
                "discovery": discovery,
                "selected_trace_objects": selected_objects,
                "lifecycle": lifecycle,
                "state_change_events": state_change_events,
                "external_reference_map": external_reference_map,
                "external_state_change_events": external_state_change_events,
                "fast_motion_lock": fast_motion_lock,
                "fast_motion_ranking": fast_motion_state.get("ranking") if isinstance(fast_motion_state, dict) else None,
                "fast_plan_cache_path": str(self.skytrip_fast_plan_path),
                "policy": "CK fast read-only mapper: cached instruction-derived references, <=8 batched external targets, repeated-motion scoring and early trace lock; user manually flies; no bot navigation input after Y",
            }
            self._save_skytrip_runtime(runtime_evidence)

        _save_runtime_evidence("READY_FOR_MANUAL_FLIGHT")

        selected_names = [obj.get("class") for obj in selected_objects]
        mapped_text = ", ".join(str(x) for x in selected_names) if selected_names else "lifecycle watch active"
        self.status.emit(
            "MAPPER",
            f"{p.name}: CK fast SkyTrip mapper ready (targets={len(external_trace_targets)}, batches={len(external_trace_batches)}, cache={external_reference_map.get('cache_status')}) — steer clearly; once a motion candidate locks, enter the Dimensional Rift",
        )
        start = time.monotonic()
        deadline = start + 120.0
        index = 0
        last_status = -999.0
        last_lifecycle_poll = -999.0
        last_fast_ref_retry = -999.0
        last_full_diagnostic = -999.0
        battle_seen = False
        fast_trace_locked = False

        while time.monotonic() < deadline:
            self._check_stop()
            index += 1
            elapsed = time.monotonic() - start

            # Lifecycle enumeration is deliberately slower than the hot trace.
            # Once loaded, DllSkyTrip has remained resident for ~15-38 seconds on
            # hardware; polling it at 1.5 s is enough while avoiding repeated full
            # CRO enumeration between high-frequency candidate reads.
            if elapsed - last_lifecycle_poll >= 1.5:
                last_lifecycle_poll = elapsed
                locate, target, transition = _locate_skytrip("MANUAL_FLIGHT", elapsed)
                if target:
                    base = _target_base(target)
                    skytrip_target = target
                    if transition in ("LOAD", "RELOAD") or mapped_base != base:
                        _map_loaded_target(target, "MANUAL_FLIGHT")
                    elif not external_trace_targets and elapsed - last_fast_ref_retry >= 2.5:
                        # Cached retries are cheap: literal buckets + tiny local
                        # mutable segments only, no ~56 KiB code rescan.
                        last_fast_ref_retry = elapsed
                        _map_loaded_target(target, "MANUAL_FLIGHT", force_external=True)
                else:
                    skytrip_target = None
                    selected_objects = []
                    mapped_base = None
                    previous_external_state = None
                    external_trace_targets = []
                    external_trace_batches = []
                    external_scanned_base = None

            # Shared field XYZ was hardware-proven stale during Soaring.  Read it
            # only every two seconds for diagnostics; the hot loop reads battle
            # authority + batched external candidates.
            if elapsed - last_full_diagnostic >= 2.0:
                last_full_diagnostic = elapsed
                sample = read_soaring_sample(lambda a, n: self._mapper_read(bridge, a, n))
            else:
                battle_raw = struct.unpack("<I", self._mapper_read(bridge, BATTLE_STATE, 4))[0]
                sample = {
                    "errors": [],
                    "battle_u32": int(battle_raw),
                    "battle": f"0x{battle_raw:08X}",
                    "battle_phase": (
                        "ACTIVE" if battle_raw == BATTLE_ACTIVE else
                        "TRANSITION" if battle_raw == BATTLE_TRANSITION else
                        "FIELD" if battle_raw == BATTLE_INACTIVE else "UNKNOWN"
                    ),
                    "fast_battle_only": True,
                }
            sample.update({"kind": "sample", "index": index, "elapsed_s": round(elapsed, 3), "phase": "AFTER_Y_SKYTRIP_FAST_MANUAL_FLIGHT"})

            if skytrip_target and external_trace_targets and not fast_trace_locked:
                external_state = read_external_trace_targets_batched(
                    bridge, external_trace_targets, external_trace_batches
                )
                if previous_external_state is not None:
                    external_delta = diff_external_trace_state(previous_external_state, external_state)
                    if external_delta.get("changed"):
                        external_event = {
                            "kind": "skytrip_external_state_delta",
                            "phase": "MANUAL_FLIGHT",
                            "index": index,
                            "elapsed_s": round(elapsed, 3),
                            **external_delta,
                        }
                        external_state_change_events.append(external_event)
                        sample["skytrip_external_state_delta"] = external_delta
                        scored = update_fast_motion_scores(fast_motion_state, external_delta, elapsed)
                        fast_motion_state = scored.get("state") or fast_motion_state
                        sample["skytrip_fast_motion_top"] = scored.get("top_candidates")
                        if scored.get("locked"):
                            fast_trace_locked = True
                            fast_motion_lock = {
                                "elapsed_s": round(elapsed, 3),
                                "index": index,
                                "winner": scored.get("winner"),
                                "top_candidates": scored.get("top_candidates"),
                            }
                            self._log("SKYTRIP FAST MOTION CANDIDATE LOCK", **fast_motion_lock)
                            self.status.emit(
                                "MAPPER",
                                f"{p.name}: motion candidate LOCKED at {scored.get('winner', {}).get('address')} • heavy trace stopped — enter the Dimensional Rift",
                            )
                        else:
                            self._log(
                                "SKYTRIP FAST EXTERNAL DELTA",
                                elapsed_s=round(elapsed, 3),
                                targets=external_delta.get("target_change_count"),
                                words=external_delta.get("word_change_count"),
                                top=scored.get("top_candidates"),
                            )
                previous_external_state = external_state

            samples.append(sample)
            self._append_soaring_trace(sample)

            if elapsed - last_status >= 4.0:
                last_status = elapsed
                top = None
                if isinstance(fast_motion_state, dict):
                    ranking = fast_motion_state.get("ranking") or []
                    top = ranking[0] if ranking else None
                self.status.emit(
                    "MAPPER",
                    f"{p.name}: CK trace {elapsed:.0f}s • targets={len(external_trace_targets)} • batches={len(external_trace_batches)} • deltas={len(external_state_change_events)} • top={top.get('score') if top else 0} • locked={fast_trace_locked} • battle={sample.get('battle')}",
                )

            if sample.get("battle_phase") == "ACTIVE":
                battle_seen = True
                break
            time.sleep(0.18 if not fast_trace_locked else 0.25)

        # One final enumeration captures an unload that coincides with battle
        # activation, which is a useful lifecycle boundary for the next mapper.
        try:
            locate, target, transition = _locate_skytrip("BATTLE_BOUNDARY" if battle_seen else "MAPPER_TIMEOUT", time.monotonic() - start)
            if target:
                skytrip_target = target
            elif transition == "UNLOAD":
                skytrip_target = None
                selected_objects = []
                mapped_base = None
                previous_external_state = None
                external_trace_targets = []
                external_trace_batches = []
                external_scanned_base = None
        except Exception as exc:
            self._log("SKYTRIP FINAL LIFECYCLE SAMPLE FAILED", error=f"{type(exc).__name__}: {exc}")

        self.soaring_trace_summary = summarize_trace(samples, anchor=anchor_dict)
        self.soaring_trace_summary.update({
            "profile": p.key,
            "target": p.name,
            "y_ack": y_ack,
            "battle_seen": battle_seen,
            "skytrip_lifecycle_events": list(lifecycle.get("events") or []),
            "skytrip_first_present": lifecycle.get("first_present"),
            "skytrip_last_present": lifecycle.get("last_present"),
            "skytrip_first_unload": lifecycle.get("first_unload"),
            "skytrip_locate_attempts": lifecycle.get("locate_attempts"),
        })
        _save_runtime_evidence("BATTLE_SEEN" if battle_seen else "MAPPER_TIMEOUT")
        self._append_soaring_trace({"kind": "mapper_summary", **self.soaring_trace_summary})
        self._log("SOARING RIFT TRACE SUMMARY", summary=self.soaring_trace_summary)

        if not battle_seen:
            raise StaticSafetyHold(
                f"{p.name} Dimensional Rift mapper timed out after 120s without battle; trace preserved"
            )

        # Once battle is active, reuse the proven normal battle PK6 boundary.
        self.status.emit("MAPPER", f"{p.name}: battle detected • confirming PK6")
        core_read = self._core_adapter(bridge)
        boundary = core.wait_for_state2_for_pk6(core_read, timeout=15.0)
        self.last_boundary = boundary
        self._append_soaring_trace({"kind": "pk6_boundary", "boundary": boundary})
        if not boundary.get("ready_for_pk6"):
            raise StaticSafetyHold(
                f"{p.name} mapper reached battle but PK6 boundary failed: {boundary.get('status')}"
            )

        raw = self._transport_read(
            bridge, core.WILD_PK6_ADDR, core.PK6_STORED_SIZE, attempts=3, label="SOARING_MAPPER_PK6"
        )
        pk6 = core.decode_stored_pk6(raw, save_tid, save_sid)
        payload = self._pk6_payload(pk6)
        self.last_pk6 = dict(payload)
        self._append_soaring_trace({
            "kind": "pk6",
            "species": payload.get("species"),
            "species_name": payload.get("species_name"),
            "pid": payload.get("pid"),
            "ec": payload.get("ec"),
            "is_shiny": payload.get("is_shiny"),
            "shiny_xor": payload.get("shiny_xor"),
        })
        if not payload.get("valid") or not payload.get("checksum_valid", True):
            raise StaticSafetyHold(f"{p.name} mapper decoded invalid PK6")
        if int(payload.get("species") or 0) != int(p.species):
            raise StaticSafetyHold(
                f"{p.name} mapper expected species {p.species}, observed {payload.get('species')}"
            )

        # Mapper encounters are real authoritative PK6 observations too.
        # Persist them before SHINY_HOLD/MAPPER_COMPLETE so Last Seen, Static
        # counters, encounter ledger and Recent Shinies do not silently miss
        # a Pokémon found during hardware mapping.
        if int(payload.get("attempt") or 0) <= 0:
            payload["attempt"] = max(1, int(self.session_seen) + 1)
        self.last_pk6 = dict(payload)
        self._record_encounter(payload)
        self.encounter.emit(dict(payload))
        if payload.get("is_shiny"):
            self._finish(
                "SHINY_HOLD",
                f"Shiny {p.name} confirmed during Dimensional Rift mapper; no further controller input",
                support=True,
            )
        else:
            self._finish(
                "MAPPER_COMPLETE",
                f"{p.name} Dimensional Rift trace captured successfully; upload the support ZIP",
                support=True,
            )

    def _startup_rpc(self, callback, *, label: str, attempts: int = 4, delay_s: float = 0.20):
        """Bounded retry for idempotent startup/query RPCs only.

        Hardware Regigigas traces showed that a single GAME_INFO UDP reply can
        be lost before the Static reset bootstrap begins.  Retrying a read-only
        query is safe and prevents the hunt from stopping before it ever reaches
        the encounter trigger.  Controller pulses are deliberately NOT retried
        here.
        """
        last_exc = None
        max_attempts = max(1, int(attempts))
        for attempt in range(1, max_attempts + 1):
            self._check_stop()
            try:
                value = callback()
                if attempt > 1:
                    self._log(
                        "STATIC STARTUP TRANSPORT RECOVERED",
                        label=str(label),
                        attempt=attempt,
                    )
                return value
            except TimeoutError as exc:
                last_exc = exc
                self._log(
                    "STATIC STARTUP TRANSPORT RETRY",
                    label=str(label),
                    attempt=attempt,
                    attempts=max_attempts,
                    error=f"{type(exc).__name__}: {exc}",
                )
                if attempt < max_attempts:
                    time.sleep(float(delay_s))
        raise last_exc if last_exc is not None else TimeoutError(f"{label} timed out")

    @staticmethod
    def _world_close(world, expected, epsilon=DEXNAV_TUTORIAL_POSITION_EPSILON):
        try:
            return (
                abs(float(world[0]) - float(expected[0])) <= float(epsilon)
                and abs(float(world[1]) - float(expected[1])) <= float(epsilon)
            )
        except Exception:
            return False

    def _wait_world_position(self, bridge: Bridge, expected, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + float(timeout)
        samples = []
        last = None
        while time.monotonic() < deadline:
            self._check_stop()
            current = validate_any_loaded_field(bridge)
            last = current
            samples.append({
                "authority": bool(current.get("authority")),
                "battle": current.get("battle"),
                "zone": current.get("zone"),
                "world_primary": current.get("world_primary"),
            })
            if (
                current.get("authority")
                and int(current.get("zone") or -1) == 23
                and self._world_close(current.get("world_primary") or (), expected)
            ):
                return {"authority": True, "expected": list(expected), "current": current, "samples": samples[-12:]}
            time.sleep(0.10)
        return {"authority": False, "expected": list(expected), "current": last, "samples": samples[-12:]}

    def _read_dexnav_tutorial_target(self, bridge: Bridge) -> dict:
        """Read the HF89-probed Route 101 DexNav overworld target object."""
        self.read_count += 1
        self.ram_reads.emit(self.read_count)
        raw = self._transport_read(
            bridge, DEXNAV_TARGET_BLOCK, DEXNAV_TARGET_BLOCK_LEN,
            attempts=3, label="DEXNAV_TARGET_RAM",
        )
        target = decode_target_block(raw)
        target["address"] = f"0x{DEXNAV_TARGET_BLOCK:08X}"
        target["object_ptr_hex"] = f"0x{int(target['object_ptr']):08X}"
        target["active_ptr_hex"] = f"0x{int(target['active_ptr']):08X}"
        return target

    def _trigger_dexnav_tutorial_poochyena(self, bridge: Bridge, inputs: AcknowledgedInput) -> dict:
        """Run the Route 101 tutorial, then RAM-steer to the actual DexNav target.

        HF89's overworld probe identified the live tutorial target at
        0x08D3B560: X/Y/Z floats at +0/+4/+8 and an active object pointer at
        +0x14.  The target was absent before spawn, appeared as
        (1701.0, 2.0, 2331.0), stayed fixed while the player manually sneaked,
        and its active pointer cleared immediately before battle.

        This replaces all fixed-duration/fixed-destination CPAD guesses.
        """
        if self._battle_active(bridge):
            return {"success": True, "kind": "DEXNAV_TUTORIAL_POOCHYENA", "battle_already_active": True}

        pulses = []
        self.status.emit("RUNNING", "DexNav tutorial • triggering rival with LEFT")
        left_ack = self._pulse(inputs, ("LEFT",), 120, 900)
        pulses.append({"index": 0, "buttons": ["LEFT"], "ack": left_ack})

        left_stage = self._wait_world_position(bridge, (1899.0, 2349.0), timeout=4.0)
        if not left_stage.get("authority"):
            return {
                "success": False, "kind": "DEXNAV_TUTORIAL_POOCHYENA",
                "reason": "LEFT did not reach the probed tutorial entry position",
                "pulses": pulses, "left_stage": left_stage,
            }

        # HF94 adaptive dialogue: the older fixed 17-A path waited 1.3-1.8 s
        # after every press.  Keep the resilience of extra A presses, but use a
        # much tighter cadence and stop as soon as RAM proves the DexNav target
        # has spawned.  Missed text frames therefore cost another press rather
        # than a multi-second fixed delay.
        visible_stage = None
        target = None
        max_dialogue_presses = 22
        for index in range(1, max_dialogue_presses + 1):
            self._check_stop()
            cleanup = index > 14
            phase = "cleanup" if cleanup else "dialogue"
            self.status.emit(
                "RUNNING",
                f"DexNav tutorial • {phase} {index}/{max_dialogue_presses}",
            )
            settle_ms = 850 if cleanup else 650
            ack = self._pulse(inputs, ("A",), 105, settle_ms)
            row = {
                "index": index, "buttons": ["A"], "ack": ack,
                "cleanup": cleanup, "settle_ms": settle_ms,
            }
            pulses.append(row)

            # The target cannot exist until late in the tutorial.  Starting the
            # cheap 24-byte target read at press 10 avoids unnecessary bridge
            # traffic during the opening dialogue while still terminating the
            # sequence immediately when the overworld Poochyena appears.
            if index >= 10:
                probe_target = self._read_dexnav_tutorial_target(bridge)
                row["target_active_after_press"] = bool(probe_target.get("active"))
                if probe_target.get("active"):
                    visible_stage = self._wait_world_position(
                        bridge, DEXNAV_TUTORIAL_VISIBLE_WORLD, timeout=1.5
                    )
                    if visible_stage.get("authority"):
                        target = probe_target
                        break

        if visible_stage is None or not visible_stage.get("authority"):
            visible_stage = self._wait_world_position(
                bridge, DEXNAV_TUTORIAL_VISIBLE_WORLD, timeout=3.0
            )
        if not visible_stage.get("authority"):
            return {
                "success": False, "kind": "DEXNAV_TUTORIAL_POOCHYENA",
                "reason": "adaptive tutorial dialogue did not reach the probed Poochyena-visible position",
                "pulses": pulses, "left_stage": left_stage, "visible_stage": visible_stage,
            }

        if target is None or not target.get("active"):
            target = self._read_dexnav_tutorial_target(bridge)
        self._log("DEXNAV TUTORIAL RAM TARGET ACQUIRED", target=target, visible_stage=visible_stage)
        if not target.get("active"):
            return {
                "success": False, "kind": "DEXNAV_TUTORIAL_POOCHYENA",
                "reason": "Poochyena-visible position was reached but the HF89 DexNav target object was not active",
                "pulses": pulses, "left_stage": left_stage, "visible_stage": visible_stage,
                "target": target,
            }

        self.status.emit("RUNNING", "DexNav tutorial • RAM-steering to overworld Poochyena")
        cpad_steps = []
        deadline = time.monotonic() + 40.0
        previous_distance = None
        previous_player = None
        stalled_steps = 0
        vertical_stall = 0
        observed = None

        for step_index in range(1, 25):
            self._check_stop()
            if time.monotonic() >= deadline:
                break

            # Battle always wins over target/object state; the target pointer
            # legitimately clears just before BATTLE_STATE becomes active.
            if self._battle_active(bridge):
                observed = {"battle": True, "source": "pre_step_battle_read"}
                break

            field = validate_any_loaded_field(bridge)
            if not field.get("authority") or int(field.get("zone") or -1) != 23:
                return {
                    "success": False, "kind": "DEXNAV_TUTORIAL_POOCHYENA",
                    "reason": "field authority lost during RAM-target DexNav steering",
                    "pulses": pulses, "cpad_steps": cpad_steps, "field": field,
                    "target": target,
                }
            world = field.get("world_primary") or ()
            if len(world) < 2:
                return {
                    "success": False, "kind": "DEXNAV_TUTORIAL_POOCHYENA",
                    "reason": "player world coordinates unavailable during RAM-target steering",
                    "pulses": pulses, "cpad_steps": cpad_steps, "field": field,
                }

            live_target = self._read_dexnav_tutorial_target(bridge)
            if not live_target.get("active"):
                transition = self._wait_battle_active(bridge, 1.5)
                if transition.get("battle"):
                    observed = transition
                    target = live_target
                    break
                return {
                    "success": False, "kind": "DEXNAV_TUTORIAL_POOCHYENA",
                    "reason": "DexNav overworld target disappeared before battle; refusing blind movement",
                    "pulses": pulses, "cpad_steps": cpad_steps, "field": field,
                    "target": live_target, "observe": transition,
                }

            # Feedback from the previous correction.  HF90 hardware proved that
            # a command can make good X progress while Z remains completely
            # unchanged; treat that as an axis-specific dead-zone signal.
            axis_feedback = None
            if previous_player is not None:
                moved_x = float(world[0]) - float(previous_player[0])
                moved_z = float(world[1]) - float(previous_player[1])
                axis_feedback = {"moved_x": moved_x, "moved_z": moved_z}
                remaining_z = float(live_target["z"]) - float(world[1])
                if abs(remaining_z) > 4.0 and abs(moved_z) < 0.30:
                    vertical_stall = min(vertical_stall + 1, 4)
                elif abs(moved_z) >= 0.30:
                    vertical_stall = 0

            command = steering_command(
                float(world[0]), float(world[1]),
                float(live_target["x"]), float(live_target["z"]),
                vertical_stall=vertical_stall,
            )
            row = {
                "index": step_index,
                "player": [float(world[0]), float(world[1])],
                "target": [float(live_target["x"]), float(live_target["z"])],
                "active_ptr": live_target.get("active_ptr_hex"),
                "axis_feedback": axis_feedback,
                **command,
            }

            # Fail closed if repeated acknowledged movement is not converging.
            if previous_distance is not None:
                progress = float(previous_distance) - float(command["distance"])
                row["progress_since_previous_step"] = progress
                if progress < 0.35:
                    stalled_steps += 1
                else:
                    stalled_steps = 0
                if stalled_steps >= 3:
                    cpad_steps.append(row)
                    return {
                        "success": False, "kind": "DEXNAV_TUTORIAL_POOCHYENA",
                        "reason": "RAM-target steering stopped converging for three consecutive corrections",
                        "pulses": pulses, "cpad_steps": cpad_steps, "field": field,
                        "target": live_target,
                    }
            previous_distance = float(command["distance"])

            self.status.emit(
                "RUNNING",
                f"DexNav tutorial • target {command['distance']:.1f} units away • correction {step_index}",
            )
            previous_player = (float(world[0]), float(world[1]))
            ack = inputs.circle_pad_pulse(
                command["x"], command["y"],
                hold_ms=command["hold_ms"],
                # HF92: do not insert an artificial neutral settle between
                # steering corrections.  The next RAM-guided vector follows
                # immediately after the acknowledged hold completes.
                release_ms=0, packet_interval_ms=15,
            )
            row["ack"] = ack
            cpad_steps.append(row)

            # HF94: do not add a separate post-pulse wait.  CPAD already
            # returns neutral at the firmware pulse boundary; immediately start
            # the next RAM read/correction and let the pre-step battle read be
            # authoritative.  This removes another visible pause per correction.
            row["observe"] = {"deferred_to_next_pre_step_read": True}

        if not (observed or {}).get("battle"):
            return {
                "success": False, "kind": "DEXNAV_TUTORIAL_POOCHYENA",
                "reason": "RAM-target DexNav steering timed out without battle",
                "pulses": pulses, "cpad_steps": cpad_steps,
                "target": target, "observe": observed,
            }

        return {
            "success": True, "kind": "DEXNAV_TUTORIAL_POOCHYENA",
            "pulses": pulses, "cpad_steps": cpad_steps,
            "left_stage": left_stage, "visible_stage": visible_stage,
            "automatic_circle_pad_sneak": True,
            "navigation": "HF94_RAM_TARGET_LOW_GAP_STEERING",
            "target": target, "observe": observed,
        }

    @staticmethod
    def _decode_dexnav_tutorial_extras(core, raw: bytes) -> dict:
        if len(raw) != int(core.PK6_STORED_SIZE):
            return {"error": f"wrong PK6 length {len(raw)}"}
        ec = struct.unpack_from("<I", raw, 0x00)[0]
        encrypted_payload = bytearray(raw[8:232])
        core.crypt_pk6_payload(encrypted_payload, ec)
        sv = (ec >> 13) & 31
        canonical_payload = core.unshuffle_pk6_payload(encrypted_payload, sv)
        dec = bytearray(raw[:8]) + canonical_payload
        exp = struct.unpack_from("<I", dec, 0x10)[0]
        moves = [struct.unpack_from("<H", dec, off)[0] for off in (0x5A, 0x5C, 0x5E, 0x60)]
        level = 1
        for candidate in range(1, 101):
            if candidate ** 3 <= exp:
                level = candidate
            else:
                break
        fangs = [
            {"move_id": move_id, "name": DEXNAV_TUTORIAL_FANG_NAMES[move_id]}
            for move_id in moves if move_id in DEXNAV_TUTORIAL_FANG_NAMES
        ]
        fang_name = str(fangs[0]["name"]) if fangs else None
        return {
            "experience": int(exp),
            "level": int(level),
            "move_ids": moves,
            "tutorial_fangs": fangs,
            "tutorial_fang_present": bool(fangs),
            "tutorial_fang_name": fang_name,
            "special_move": fang_name,
        }

    def _trigger_run_reinteract(self, bridge: Bridge, inputs: AcknowledgedInput) -> dict:
        """Re-interact after a successful RUN with a slower bounded field-side cadence.

        Southern Island hardware (2026-08-29) proved RUN and exact field return,
        but the Eon Ticket actor was not yet interactable during the generic
        Static trigger's short four-A window.  Do not change the proven RUN
        path.  Instead, give the overworld actor a short grace period, then
        send bounded A pulses while continuously proving the player remains on
        the exact saved interaction tile.  No movement input is ever sent.
        """
        grace_deadline = time.monotonic() + 2.0
        grace_samples = []
        while time.monotonic() < grace_deadline:
            self._check_stop()
            field = validate_saved_field_anchor(bridge, self.anchor)
            grace_samples.append({
                "authority": bool(field.get("authority")),
                "battle": field.get("battle"),
                "zone": field.get("zone"),
                "grid": field.get("grid"),
            })
            if not field.get("authority"):
                return {
                    "success": False,
                    "kind": "RUN_REINTERACT_A",
                    "reason": "saved field authority lost during re-interact grace",
                    "grace": grace_samples[-12:],
                    "field": field,
                }
            time.sleep(0.10)

        pulses = []
        # Hardware attempt 1 needed three A presses to enter the battle.  After
        # RUN the actor can respawn materially later than field/grid authority,
        # so use a larger *Eon-only* bounded budget instead of weakening the
        # normal Static trigger for every target.
        for index in range(1, 9):
            self._check_stop()
            field_before = validate_saved_field_anchor(bridge, self.anchor)
            if not field_before.get("authority"):
                return {
                    "success": False,
                    "kind": "RUN_REINTERACT_A",
                    "reason": "saved field authority lost before re-interact A",
                    "pulses": pulses,
                    "field": field_before,
                }
            ack = self._pulse(inputs, ("A",), 90, 120)
            observed = self._wait_battle_active(bridge, 2.0)
            row = {
                "index": index,
                "buttons": ["A"],
                "ack": ack,
                "observe": observed,
            }
            pulses.append(row)
            if observed.get("battle"):
                return {
                    "success": True,
                    "kind": "RUN_REINTERACT_A",
                    "accepted_pulse": index,
                    "grace": grace_samples[-12:],
                    "pulses": pulses,
                    "observe": observed,
                }
            if observed.get("unexpected"):
                return {
                    "success": False,
                    "kind": "RUN_REINTERACT_A",
                    "reason": "unexpected battle-state value",
                    "grace": grace_samples[-12:],
                    "pulses": pulses,
                    "observe": observed,
                }
            if observed.get("transition_unresolved"):
                return {
                    "success": False,
                    "kind": "RUN_REINTERACT_A",
                    "reason": "battle transition did not resolve",
                    "grace": grace_samples[-12:],
                    "pulses": pulses,
                    "observe": observed,
                }

            # If A did not start a battle, the player must still be on the exact
            # saved tile before another A is allowed.  A transient script state
            # may temporarily make the field validator unavailable; give it a
            # small read-only recovery window rather than firing another input.
            field_after = self._wait_saved_field_authority(bridge, timeout=1.2, stable_samples=2)
            row["field_after"] = field_after
            if not field_after.get("authority"):
                return {
                    "success": False,
                    "kind": "RUN_REINTERACT_A",
                    "reason": "saved field authority did not recover after re-interact A",
                    "grace": grace_samples[-12:],
                    "pulses": pulses,
                }

        return {
            "success": False,
            "kind": "RUN_REINTERACT_A",
            "pulses": pulses,
            "grace": grace_samples[-12:],
            "reason": "bounded Eon Ticket re-interact A budget exhausted without battle",
        }

    def _trigger_static(self, bridge: Bridge, inputs: AcknowledgedInput) -> dict:
        p = self.static_profile
        if self._battle_active(bridge):
            return {"success": True, "kind": p.trigger_kind, "battle_already_active": True, "pulses": []}

        pulses = []
        if p.trigger_kind == "DEXNAV_TUTORIAL_POOCHYENA":
            return self._trigger_dexnav_tutorial_poochyena(bridge, inputs)

        if p.trigger_kind == "INTERACT_A":
            for index in range(1, int(p.max_trigger_pulses) + 1):
                pulses.append({"index": index, "buttons": ["A"], "ack": self._pulse(inputs, ("A",), 90, 100)})
                observed = self._wait_battle_active(bridge, 1.40)
                if observed.get("battle"):
                    return {"success": True, "kind": p.trigger_kind, "accepted_pulse": index, "pulses": pulses, "observe": observed}
                if observed.get("unexpected"):
                    return {"success": False, "kind": p.trigger_kind, "pulses": pulses, "observe": observed, "reason": "unexpected battle-state value"}
                if observed.get("transition_unresolved"):
                    return {"success": False, "kind": p.trigger_kind, "pulses": pulses, "observe": observed, "reason": "battle transition did not resolve"}
            return {"success": False, "kind": p.trigger_kind, "pulses": pulses, "reason": "bounded A budget exhausted without battle"}

        if p.trigger_kind == "STEP_NORTH":
            self._log(
                "STATIC STEP TRIGGER ABOUT TO SEND",
                profile=p.key,
                direction="UP",
                hold_ms=120,
                settle_ms=90,
            )
            ack = self._pulse(inputs, ("UP",), 120, 90)
            pulses.append({"index": 1, "buttons": ["UP"], "ack": ack})
            self._log(
                "STATIC STEP TRIGGER SENT",
                profile=p.key,
                direction="UP",
                ack=ack,
            )

            # Regigigas hardware proof (2026-08-28): the north step is the
            # correct room trigger, but it opens text before the battle.  Do
            # not wait passively for a battle that cannot start until the
            # dialogue is acknowledged.  Advance the text with a bounded A
            # budget and re-check RAM after every press.  The first observed
            # transition/battle ends input immediately.
            early = self._wait_battle_active(bridge, 0.70)
            if early.get("battle"):
                return {"success": True, "kind": p.trigger_kind, "accepted_pulse": 1, "pulses": pulses, "observe": early}
            if early.get("unexpected"):
                return {"success": False, "kind": p.trigger_kind, "pulses": pulses, "observe": early, "reason": "unexpected battle-state value after north step"}
            if early.get("transition_unresolved"):
                return {"success": False, "kind": p.trigger_kind, "pulses": pulses, "observe": early, "reason": "battle transition after north step did not resolve"}

            dialogue_budget = 6
            for press_index in range(1, dialogue_budget + 1):
                self._log(
                    "STATIC STEP DIALOGUE A ABOUT TO SEND",
                    profile=p.key,
                    press=press_index,
                    budget=dialogue_budget,
                )
                a_ack = self._pulse(inputs, ("A",), 90, 220)
                pulses.append({"index": 1 + press_index, "buttons": ["A"], "ack": a_ack})
                self._log(
                    "STATIC STEP DIALOGUE A SENT",
                    profile=p.key,
                    press=press_index,
                    ack=a_ack,
                )
                observed = self._wait_battle_active(bridge, 1.40)
                if observed.get("battle"):
                    return {
                        "success": True,
                        "kind": p.trigger_kind,
                        "accepted_pulse": 1 + press_index,
                        "dialogue_presses": press_index,
                        "pulses": pulses,
                        "observe": observed,
                    }
                if observed.get("unexpected"):
                    return {"success": False, "kind": p.trigger_kind, "pulses": pulses, "observe": observed, "reason": "unexpected battle-state value during Regigigas dialogue"}
                if observed.get("transition_unresolved"):
                    return {"success": False, "kind": p.trigger_kind, "pulses": pulses, "observe": observed, "reason": "Regigigas dialogue battle transition did not resolve"}

            return {
                "success": False,
                "kind": p.trigger_kind,
                "pulses": pulses,
                "dialogue_presses": dialogue_budget,
                "reason": "Regigigas post-step dialogue A budget exhausted without battle",
            }

        if p.trigger_kind == "STEP_NORTH_MENU_REVEAL":
            pulses.append({"index": 1, "buttons": ["UP"], "ack": self._pulse(inputs, ("UP",), 120, 120)})
            early = self._wait_battle_active(bridge, 0.70)
            if early.get("battle"):
                return {"success": True, "kind": p.trigger_kind, "pulses": pulses, "observe": early}
            pulses.append({"index": 2, "buttons": ["X"], "ack": self._pulse(inputs, ("X",), 100, 350)})
            if self._battle_active(bridge):
                return {"success": True, "kind": p.trigger_kind, "pulses": pulses, "battle_after_menu_open": True}
            pulses.append({"index": 3, "buttons": ["B"], "ack": self._pulse(inputs, ("B",), 100, 160)})
            observed = None
            for a_index in range(1, 5):
                pulses.append({"index": 3 + a_index, "buttons": ["A"], "ack": self._pulse(inputs, ("A",), 100, 220)})
                observed = self._wait_battle_active(bridge, 0.9)
                if observed.get("battle"):
                    break
            return {"success": bool(observed and observed.get("battle")), "kind": p.trigger_kind, "pulses": pulses, "observe": observed, "reason": None if observed and observed.get("battle") else "Spiritomb menu-reveal sequence did not start battle after four bounded Shahhh! A presses"}

        return {"success": False, "kind": p.trigger_kind, "reason": "unsupported static trigger kind"}

    def _transport_read(self, bridge: Bridge, address: int, length: int, *, attempts: int = 3, label: str = "RAM_READ"):
        """Bounded retry for idempotent RAM reads only.

        Hardware traces have shown rare single UDP reply losses even while the
        game and bridge remain healthy. Retrying an idempotent READ is safe;
        controller writes are deliberately never retried here. Completed but
        invalid data still fails at the normal authority checks.
        """
        last_exc = None
        max_attempts = max(1, int(attempts))
        for attempt in range(1, max_attempts + 1):
            try:
                return bridge.read(address, length)
            except TimeoutError as exc:
                last_exc = exc
                self._log(
                    "STATIC RAM TRANSPORT RETRY",
                    label=label,
                    address=f"0x{int(address):08X}",
                    length=int(length),
                    attempt=attempt,
                    max_attempts=max_attempts,
                    error=f"{type(exc).__name__}: {exc}",
                )
                if attempt >= max_attempts:
                    raise
                time.sleep(0.15 if attempt == 1 else 0.30)
        raise last_exc  # pragma: no cover

    def _core_adapter(self, bridge: Bridge):
        worker = self
        class Adapter:
            def read(self, address, length, retries=2):
                # causal_core passes a retry hint; keep at least three transport
                # attempts for Static because the battle boundary is read-only.
                attempts = max(3, int(retries or 0) + 1)
                return worker._transport_read(bridge, address, length, attempts=attempts, label="BATTLE_BOUNDARY")
            def u32(self, address, retries=2):
                attempts = max(3, int(retries or 0) + 1)
                raw = worker._transport_read(bridge, address, 4, attempts=attempts, label="BATTLE_BOUNDARY_U32")
                return struct.unpack("<I", raw)[0]
        return Adapter()

    def _pk6_payload(self, pk6: dict) -> dict:
        species = int(pk6.get("species") or 0)
        nature_id = int(pk6.get("nature_id") or 0)
        return {
            **dict(pk6),
            "hunt_type": "Static",
            "static_key": self.profile_key,
            "target": self.static_profile.name,
            "location_name": self.static_profile.location,
            "species": species,
            "species_name": SPECIES_NAMES.get(species, f"Species {species}"),
            "pokemon_pid": pk6.get("pid"),
            "pid": pk6.get("pid"),
            "nature": NATURE_NAMES[nature_id] if 0 <= nature_id < len(NATURE_NAMES) else f"Nature {nature_id}",
            "ability_id": pk6.get("ability_id"),
            "ability": ability_name(pk6.get("ability_id")),
            "attempt": int(self.state_machine.attempts if self.state_machine else self.session_seen + 1),
            "horde_size": 1,
            "shiny_action": "HOLD",
            "hardware_validation": self.static_profile.choreography_status,
        }

    def _reset_phase_extrema(self):
        """Clear current-phase IV/SV extrema after a shiny completes it."""
        for key in (
            "highest_sv", "lowest_sv",
            "highest_iv_sum", "lowest_iv_sum",
        ):
            self.lifetime[key] = None

    def _record_encounter(self, payload: dict):
        shiny = bool(payload.get("is_shiny"))
        sv = int(payload.get("shiny_xor") or 0)
        iv_sum = int(payload.get("iv_sum") or sum(int(v) for v in (payload.get("ivs") or {}).values()))
        now_mono = time.monotonic()
        if self.last_encounter_monotonic is None:
            duration_s = max(0.0, now_mono - self.started_monotonic) if self.started_monotonic else None
        else:
            duration_s = max(0.0, now_mono - self.last_encounter_monotonic)
        self.last_encounter_monotonic = now_mono
        self.session_seen += 1
        self.lifetime["lifetime_seen"] = int(self.lifetime.get("lifetime_seen", 0)) + 1
        self.lifetime["phase_seen"] = int(self.lifetime.get("phase_seen", 0)) + 1
        detected_charm = (self.shiny_charm_state or {}).get("detected")
        odds = None
        if detected_charm is not None:
            odds = resolve_shiny_odds(
                game=(self.game_profile or {}).get("name", "ORAS"),
                hunt_type="Static",
                shiny_charm_present=detected_charm,
                shiny_charm_applies=True,
            )
            current_log = self.lifetime.get("phase_log_miss")
            if current_log is None:
                current_log = 0.0
            self.lifetime["phase_log_miss"] = advance_phase_log_miss(
                current_log, odds.probability
            )
            self.lifetime["phase_cumulative_probability"] = cumulative_probability_from_log_miss(
                self.lifetime["phase_log_miss"]
            )
        else:
            self.lifetime["phase_log_miss"] = None
            self.lifetime["phase_cumulative_probability"] = None
        payload["encounter_shiny_probability"] = odds.probability if odds else None
        payload["phase_cumulative_probability"] = self.lifetime.get(
            "phase_cumulative_probability"
        )
        payload["shiny_odds_display"] = odds.display if odds else None
        for key, value, fn in (
            ("highest_sv", sv, max), ("lowest_sv", sv, min),
            ("highest_iv_sum", iv_sum, max), ("lowest_iv_sum", iv_sum, min),
        ):
            old = self.lifetime.get(key)
            self.lifetime[key] = value if old is None else fn(int(old), value)

        if shiny:
            self.session_shinies += 1
            phase = int(self.lifetime.get("phase_seen", 0))
            self.lifetime["lifetime_shinies"] = int(self.lifetime.get("lifetime_shinies", 0)) + 1
            self.lifetime["last_phase_seen"] = phase
            self.lifetime["last_phase_cumulative_probability"] = float(
                self.lifetime.get("phase_cumulative_probability", 0.0) or 0.0
            )
            self.lifetime["last_shiny"] = {
                "hunt_type": "Static",
                "static_key": self.profile_key,
                "target": self.static_profile.name,
                "species": payload.get("species_name"),
                "species_id": payload.get("species"),
                "pid": payload.get("pid"),
                "ec": payload.get("ec"),
                "shiny_xor": sv,
                "ivs": dict(payload.get("ivs") or {}),
                "nature": payload.get("nature", "—"),
                "ability_id": payload.get("ability_id"),
                "ability": payload.get("ability", "—"),
                "gender": payload.get("gender", "—"),
                "location": self.static_profile.location,
                "method": "Static",
                "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            increment_species_shiny_total(
                int(payload["species"]),
                payload.get("species_name"),
                found_time=datetime.now().astimezone().isoformat(timespec="seconds"),
            )
            self.lifetime["phase_seen"] = 0
            self.lifetime["phase_log_miss"] = 0.0
            self.lifetime["phase_cumulative_probability"] = 0.0
            self._reset_phase_extrema()
            self._append_recent_shiny(self.lifetime["last_shiny"])
        persisted = dict(payload)
        persisted["time"] = datetime.now().astimezone().isoformat(timespec="seconds")
        persisted["method"] = "Static"
        persisted["method_name"] = "Static"
        persisted["game"] = (self.game_profile or {}).get("name")
        persisted["location_name"] = self.static_profile.location
        persisted["duration_s"] = round(float(duration_s), 3) if duration_s is not None else None
        self._save_last_seen_entry(persisted)
        self._append_encounter_ledger(payload, duration_s=duration_s)
        self._save_stats()

    def _load_last_seen(self):
        if not self.last_seen_path.exists():
            return []
        try:
            data = json.loads(self.last_seen_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _save_last_seen_entry(self, item: dict):
        """Persist Static encounter to the shared ORAS Last Seen ledger.

        Commit immediately after the authoritative PK6 read, before any reset.
        This mirrors Wild/Starter durability: a Stop or reset cannot erase a
        Pokémon that was already proven in RAM.
        """
        history = self._load_last_seen()
        record = dict(item)
        record.setdefault("time", datetime.now().astimezone().isoformat(timespec="seconds"))
        history.insert(0, record)
        history = history[:7]
        self._atomic_json(self.last_seen_path, history)

    def _append_encounter_ledger(self, payload: dict, *, duration_s: float | None = None):
        record = dict(payload)
        record["time"] = datetime.now().astimezone().isoformat(timespec="seconds")
        record["hunt_type"] = "Static"
        record["method"] = "Static"
        record["method_name"] = "Static"
        record["environment"] = "Static"
        record["target"] = self.static_profile.name
        record["static_key"] = self.profile_key
        record["location_name"] = self.static_profile.location
        record["game"] = (self.game_profile or {}).get("name")
        if duration_s is not None:
            record["duration_s"] = round(max(0.0, float(duration_s)), 3)
        record["phase_length"] = (
            int(self.lifetime.get("last_phase_seen", 0) or 0)
            if bool(payload.get("is_shiny")) else None
        )
        try:
            with self.encounter_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
        except Exception as exc:
            self._log("STATIC ENCOUNTER LEDGER WRITE FAILED", error=f"{type(exc).__name__}: {exc}")

    def _append_recent_shiny(self, item: dict):
        path = self.profile_paths.recent_shinies_path
        try:
            items = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(items, list):
                items = []
        except Exception:
            items = []
        items.insert(0, dict(item))
        items = items[:50]
        self._atomic_json(path, items)
        self.recent_shinies.emit(items)

    def _support(self, status: str, reason: str):
        try:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            out = self.support_dir / f"Pokebot3DS-CFW_static_{self.profile_key}_{stamp}_{status}.zip"
            summary = self.support_dir / f"static_{self.profile_key}_{stamp}_summary.json"
            self._atomic_json(summary, {
                "status": status,
                "reason": reason,
                "profile": self.static_profile.__dict__,
                "game_profile": self.game_profile,
                "saved_field_anchor": self.anchor.as_dict() if self.anchor else None,
                "state_machine": self.state_machine.snapshot() if self.state_machine else None,
                "last_trigger": self.last_trigger,
                "last_boundary": self.last_boundary,
                "last_pk6": self.last_pk6,
                "last_reset": self.last_reset,
                "ram_reads_counted": self.read_count,
                "soaring_trace_path": str(self.soaring_trace_path) if self.soaring_trace_path.exists() else None,
                "soaring_trace_summary": self.soaring_trace_summary,
                "skytrip_runtime_path": str(self.skytrip_runtime_path) if self.skytrip_runtime_path.exists() else None,
                "skytrip_runtime_summary": self.skytrip_runtime_summary,
                "offline_candidate": True,
            })
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
                support_files = (
                    self.log_path, self.stats_path, self.session_path, summary,
                    self.encounter_path, self.last_seen_path, self.profile_paths.recent_shinies_path, self.soaring_trace_path, self.skytrip_runtime_path,
                )
                for p in support_files:
                    if p.exists():
                        zf.write(p, p.name)
            try:
                summary.unlink()
            except Exception:
                pass
            self.support_ready.emit(str(out))
            return str(out)
        except Exception as exc:
            self._log("STATIC SUPPORT EXPORT FAILED", error=f"{type(exc).__name__}: {exc}")
            return None

    def _finish(self, status: str, reason: str, *, support=False):
        if not self._lifetime_time_committed and self.started_monotonic is not None:
            elapsed = max(0.0, time.monotonic() - self.started_monotonic)
            self.lifetime["lifetime_hunt_seconds"] = round(
                float(self.lifetime.get("lifetime_hunt_seconds", 0.0)) + elapsed, 3
            )
            self._lifetime_time_committed = True
        self._save_stats()
        self.status.emit(status, reason)
        self._log("STATIC SESSION FINISHED", status=status, reason=reason)
        if support and self.auto_support_zip:
            self._support(status, reason)
        self.session_finished.emit(status)

    @Slot()
    def run(self):
        self.started_monotonic = time.monotonic()
        bridge = None
        inputs = None
        try:
            self.status.emit("STARTING", f"Static {self.static_profile.name} preflight")
            self._log("STATIC OFFLINE-INTEGRATION CANDIDATE START", profile=self.static_profile.__dict__)

            if self.static_profile.shiny_locked:
                raise StaticSafetyHold(f"{self.static_profile.name} is shiny-locked in vanilla ORAS")
            if self.bridge_port != 4952 or self.input_port != 4952:
                raise StaticSafetyHold("Static requires unified Pokebot-Luma UDP 4952")

            bridge = Bridge(self.host, port=self.bridge_port, timeout=min(self.timeout, 1.5))
            inputs = AcknowledgedInput(self.host, port=self.input_port, timeout=min(self.timeout, 1.5))
            gi = self._startup_rpc(bridge.game_info, label="GAME_INFO", attempts=4)
            gp = profile_from_game_info(gi)
            if not gp:
                raise StaticSafetyHold(f"Unsupported ORAS GAME_INFO: {gi}")
            self.game_profile = gp
            if gp["key"] not in self.static_profile.games:
                raise StaticSafetyHold(f"{self.static_profile.name} is not available in {gp['name']}")
            if gp["key"] == "omega_ruby" and not self.use_code_ips:
                raise StaticSafetyHold("Omega Ruby Static reset currently requires the hardware-proven code.ips reset route")

            ctrl = self._startup_rpc(inputs.input_ping, label="INPUT_PING", attempts=4)
            inputs.release_all()
            self.shiny_charm_state = detect_oras_shiny_charm(bridge, game_key=gp["key"])
            self._initialize_phase_probability()
            self.connection.emit({"ram_ready": True, "input_ready": True, "controller_ready": True, "input_status": "Pokebot-Luma RAM + Input 4952: Ready", "game_info": gi, "game_profile": gp, "controller_info": ctrl, "shiny_charm": self.shiny_charm_state})

            # Static bootstrap mirrors Starter behaviour: the PC hunt may be
            # started from ANY current game state/position.  First soft-reset
            # through the proven title/Continue route and wait for the loaded
            # save to reach a stable overworld.  Only then capture the exact
            # saved field tile that subsequent non-shiny resets must return to.
            self.status.emit("RESETTING", f"{self.static_profile.name} bootstrap • resetting to saved position")
            bootstrap_ok, bootstrap = run_reset_to_field_for_profile(
                bridge,
                inputs,
                self._log,
                gp,
                validate_any_loaded_field,
                use_code_ips=self.use_code_ips,
            )
            self.last_reset = dict(bootstrap or {})
            self._log("STATIC BOOTSTRAP RESET RESULT", ok=bootstrap_ok, result=self.last_reset)
            if not bootstrap_ok:
                raise StaticSafetyHold(
                    f"Static bootstrap reset/load failed: {self.last_reset.get('status')}"
                )

            self.anchor = read_saved_field_anchor(bridge)
            anchor_check = validate_saved_field_anchor(bridge, self.anchor)
            if not anchor_check.get("authority"):
                raise StaticSafetyHold(f"Static saved-field bootstrap failed: {anchor_check}")
            self._log(
                "STATIC SAVED FIELD ANCHOR AFTER BOOTSTRAP",
                anchor=self.anchor.as_dict(),
                validation=anchor_check,
            )
            expected_zone = self.static_profile.expected_zone_id
            if expected_zone is not None and int(self.anchor.zone) != int(expected_zone):
                raise StaticSafetyHold(
                    f"{self.static_profile.name} saved location mismatch: "
                    f"expected {self.static_profile.location} zone {int(expected_zone)}, "
                    f"got zone {int(self.anchor.zone)}"
                )
            if self.static_profile.trigger_kind == "DEXNAV_TUTORIAL_POOCHYENA":
                start_world = [self.anchor.world_x, self.anchor.world_z]
                if not self._world_close(start_world, DEXNAV_TUTORIAL_START_WORLD):
                    raise StaticSafetyHold(
                        "DexNav tutorial save-position mismatch: expected the probed pre-tutorial "
                        f"world position {list(DEXNAV_TUTORIAL_START_WORLD)}, got {start_world}. "
                        "Use the save from the successful mapper run before the rival tutorial starts."
                    )

            # Use the already-shipped, validated normal battle PK6 decoder/gate.
            _, core, _ = load_walk_v0p23()
            core_read = self._core_adapter(bridge)
            battle_br = core.Bridge(host=self.host, timeout=min(self.timeout, 1.5))
            ids = bridge.read(core.TRAINER_IDS_ADDR, 4)
            save_tid, save_sid = struct.unpack("<HH", ids)

            self.state_machine = StaticEncounterStateMachine(self.static_profile, gp["key"])
            self._save_stats()
            party_payload = None
            try:
                party_payload = get_runtime_party_snapshot_for_bridge(bridge)
                # live_party.py exposes the six UI slots as ``payload``.
                self.party.emit(list(party_payload.get("payload") or []))
                self._log(
                    "STATIC PARTY TELEMETRY",
                    source=party_payload.get("source"),
                    species_ids=[
                        int(mon.get("species_id") or 0)
                        for mon in list(party_payload.get("payload") or [])
                    ],
                )
            except Exception as exc:
                self._log("STATIC PARTY TELEMETRY SKIPPED", error=f"{type(exc).__name__}: {exc}")

            if self.static_profile.trigger_kind == "SOARING_RIFT_MAPPER":
                self._run_soaring_rift_mapper(bridge, inputs, core, save_tid, save_sid, party_payload)
                return
            if self.static_profile.trigger_kind == "SOARING_WEATHER_UNIMPLEMENTED":
                raise StaticSafetyHold(
                    f"{self.static_profile.name} uses the Storm Cloud Soaring trigger; weather mapper not implemented yet"
                )

            while True:
                self._check_stop()
                field = validate_saved_field_anchor(bridge, self.anchor)
                if not field.get("authority"):
                    raise StaticSafetyHold(f"Static field moved/changed before trigger: {field}")

                self.state_machine.field_ready()
                self.state_machine.trigger_sent()
                attempt = self.state_machine.attempts
                self.status.emit("RUNNING", f"{self.static_profile.name} attempt {attempt} • trigger")
                if self.static_profile.reset_kind == "RUN_REINTERACT" and self.run_reinteract_pending:
                    self.status.emit("RUNNING", f"{self.static_profile.name} attempt {attempt} • waiting for Eon re-interact readiness")
                    self.last_trigger = self._trigger_run_reinteract(bridge, inputs)
                    self.run_reinteract_pending = False
                    self._log("STATIC RUN_REINTERACT TRIGGER", attempt=attempt, trigger=self.last_trigger)
                else:
                    self.last_trigger = self._trigger_static(bridge, inputs)
                self._log("STATIC TRIGGER", attempt=attempt, trigger=self.last_trigger)
                if not self.last_trigger.get("success"):
                    raise StaticSafetyHold(f"Static trigger failed: {self.last_trigger.get('reason') or self.last_trigger}")

                self.state_machine.battle_ready()
                self.status.emit("RUNNING", f"{self.static_profile.name} attempt {attempt} • reading PK6")
                self.last_boundary = core.wait_for_state2_for_pk6(core_read, timeout=15.0)
                if not self.last_boundary.get("ready_for_pk6"):
                    raise StaticSafetyHold(f"Static PK6 safety boundary failed: {self.last_boundary.get('status')}")

                raw = self._transport_read(
                    bridge, core.WILD_PK6_ADDR, core.PK6_STORED_SIZE, attempts=3, label="STATIC_PK6"
                )
                pk6 = core.decode_stored_pk6(raw, save_tid, save_sid)
                payload = self._pk6_payload(pk6)
                if self.static_profile.trigger_kind == "DEXNAV_TUTORIAL_POOCHYENA":
                    extras = self._decode_dexnav_tutorial_extras(core, raw)
                    payload.update(extras)
                    level = int(extras.get("level") or 0)
                    species = int(payload.get("species") or 0)
                    fang_name = str(extras.get("tutorial_fang_name") or "")
                    pk6_authoritative = (
                        bool(payload.get("valid"))
                        and bool(payload.get("checksum_valid", True))
                    )
                    if not pk6_authoritative:
                        raise StaticSafetyHold(
                            "DexNav tutorial PK6 was not authoritative enough to classify "
                            "as target vs accidental encounter"
                        )
                    target_identity = (
                        species == 261
                        and level == 5
                        and bool(extras.get("tutorial_fang_present"))
                    )
                    if not target_identity:
                        # A normal Route 101 encounter can interrupt the sneak.
                        # It is not the scripted target, so do not count it as a
                        # Static encounter and do not enter SAFETY_HOLD.  Never
                        # auto-reset over a RAM-confirmed shiny, though.
                        if bool(payload.get("is_shiny")):
                            self.last_pk6 = dict(payload)
                            self._log(
                                "DEXNAV TUTORIAL ACCIDENTAL SHINY HOLD",
                                attempt=attempt,
                                species=payload.get("species_name"),
                                species_id=species,
                                level=level,
                                moves=extras.get("move_ids"),
                                pid=payload.get("pid"),
                                xor=payload.get("shiny_xor"),
                            )
                            self.status.emit(
                                "SHINY HOLD",
                                f"Accidental shiny {payload.get('species_name') or species} encountered while sneaking • not resetting",
                            )
                            self._finish(
                                "SHINY_HOLD",
                                "Accidental shiny encountered during DexNav tutorial approach; protected from automatic reset",
                                support=True,
                            )
                            return

                        reason = (
                            "not scripted tutorial target: "
                            f"species={species} level={level} moves={extras.get('move_ids')}"
                        )
                        event = self.state_machine.accidental_encounter(payload, reason=reason)
                        self.last_pk6 = dict(payload)
                        self._log(
                            "DEXNAV TUTORIAL ACCIDENTAL ENCOUNTER",
                            attempt=attempt,
                            event=event,
                            species=payload.get("species_name"),
                            species_id=species,
                            level=level,
                            moves=extras.get("move_ids"),
                        )
                        self.status.emit(
                            "RESETTING",
                            f"Accidental {payload.get('species_name') or ('species ' + str(species))} • not tutorial Poochyena • resetting",
                        )
                        validator = lambda b: validate_saved_field_anchor(b, self.anchor)
                        ok, reset = run_reset_to_field_for_profile(
                            bridge,
                            inputs,
                            self._log,
                            gp,
                            validator,
                            use_code_ips=self.use_code_ips,
                        )
                        self.last_reset = reset
                        self._log(
                            "DEXNAV TUTORIAL ACCIDENTAL RESET RESULT",
                            attempt=attempt, ok=ok, result=reset,
                        )
                        if not ok:
                            raise StaticSafetyHold(
                                f"DexNav accidental encounter reset/field return failed: {reset.get('status')}"
                            )
                        self.session_resets += 1
                        self.lifetime["lifetime_resets"] = int(
                            self.lifetime.get("lifetime_resets", 0)
                        ) + 1
                        self._save_stats()
                        continue

                    # Make the special tutorial move first-class encounter data
                    # so the dashboard/history can display it visibly.
                    self.status.emit(
                        "RUNNING",
                        f"Tutorial Poochyena confirmed • {fang_name or 'Fang move detected'}",
                    )
                self.last_pk6 = dict(payload)
                event = self.state_machine.opponent(payload)
                self._log("STATIC PK6", attempt=attempt, event=event, species=payload.get("species_name"), pid=payload.get("pid"), ec=payload.get("ec"), shiny=payload.get("is_shiny"), xor=payload.get("shiny_xor"))

                if self.state_machine.state.value == "SAFETY_HOLD":
                    raise StaticSafetyHold(f"Static opponent validation failed: {event}")

                # Persist authoritative PK6 analytics before presenting it.
                # A subsequent Stop/reset cannot make a proven encounter vanish.
                self._record_encounter(payload)
                self.encounter.emit(dict(payload))
                self.last_seen_entry.emit(dict(payload))

                if payload.get("is_shiny"):
                    # Static policy: once PK6 proves the target is shiny, stop.
                    # Do not touch BAG, FIGHT, RUN, or any other battle control.
                    self.status.emit("SHINY HOLD", f"{self.static_profile.name} shiny confirmed by PK6")
                    self._log(
                        "STATIC SHINY HOLD",
                        policy="manual_capture_only",
                        species=payload.get("species_name"),
                        pid=payload.get("pid"),
                        ec=payload.get("ec"),
                        xor=payload.get("shiny_xor"),
                    )
                    self._finish(
                        "SHINY_HOLD",
                        f"Shiny {self.static_profile.name} confirmed; Static hunts never Auto Capture",
                        support=False,
                    )
                    return

                self._check_stop()
                if self.static_profile.reset_kind == "RUN_REINTERACT":
                    self.status.emit(
                        "RUNNING",
                        f"Non-shiny {self.static_profile.name} • RUN → field → re-interact",
                    )
                    reset = self._run_reinteract_return(bridge, battle_br, core)
                    self.last_reset = reset
                    self._log(
                        "STATIC RUN_REINTERACT COMPLETE",
                        attempt=attempt,
                        result=reset,
                    )
                    self.session_reinteracts += 1
                    self.lifetime["lifetime_reinteracts"] = int(
                        self.lifetime.get("lifetime_reinteracts", 0)
                    ) + 1
                    self.run_reinteract_pending = True
                else:
                    self.status.emit("RESETTING", f"Non-shiny {self.static_profile.name} • returning to saved tile")
                    validator = lambda b: validate_saved_field_anchor(b, self.anchor)
                    ok, reset = run_reset_to_field_for_profile(
                        bridge,
                        inputs,
                        self._log,
                        gp,
                        validator,
                        use_code_ips=self.use_code_ips,
                    )
                    self.last_reset = reset
                    self._log("STATIC RESET RESULT", attempt=attempt, ok=ok, result=reset)
                    if not ok:
                        raise StaticSafetyHold(f"Static reset/field return failed: {reset.get('status')}")
                    self.session_resets += 1
                    self.lifetime["lifetime_resets"] = int(
                        self.lifetime.get("lifetime_resets", 0)
                    ) + 1
                self._save_stats()
                # Both reset strategies return through field_ready() from
                # NONSHINY_RESET_REQUIRED. RUN_REINTERACT sends the next A only
                # after the next-loop saved-field authority check passes.

        except UserStop as exc:
            try:
                if inputs:
                    inputs.release_all()
            except Exception:
                pass
            self._finish(
                "STOPPED", str(exc),
                support=self.static_profile.trigger_kind == "SOARING_RIFT_MAPPER",
            )
        except Exception as exc:
            try:
                if inputs:
                    inputs.release_all()
            except Exception:
                pass
            reason = f"{type(exc).__name__}: {exc}"
            self._log("STATIC SAFETY HOLD", reason=reason, traceback=traceback.format_exc())
            self._finish("SAFETY_HOLD", reason, support=True)
        finally:
            try:
                if inputs:
                    inputs.close()
            except Exception:
                pass
