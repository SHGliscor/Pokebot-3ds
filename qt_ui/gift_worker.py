from __future__ import annotations

from pokebot.common.ability_names import ability_name
import json
import hashlib
import struct
import time
import traceback
import zipfile
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from pokebot.common.bridge import Bridge
from pokebot.common.acknowledged_input import AcknowledgedInput
from pokebot.common.oras_profiles import profile_from_game_info
from pokebot.common.oras_ram import read_trainer_ids, bounded_gate
from pokebot.common.reset_adapter import run_reset_to_field_for_profile
from pokebot.common.live_party import get_runtime_party_snapshot_for_bridge
from pokebot.common.species_names import SPECIES_NAMES
from pokebot.common.pk6 import NATURE_NAMES
from pokebot.common.evolution_prediction import predict_split_evolution
from pokebot.common.shiny_odds import (
    detect_oras_shiny_charm, resolve_shiny_odds, advance_phase_log_miss,
    cumulative_probability_from_log_miss, phase_log_miss_for_constant,
)
from pokebot.static.oras_static import read_saved_field_anchor, validate_any_loaded_field, validate_saved_field_anchor
from pokebot.gift.oras_gifts import get_gift_profile, find_new_gift, find_new_party_member, parsed_identity, FOSSIL_SPECIES

from .appdata_store import get_profile_paths, increment_species_shiny_total


class UserStop(RuntimeError):
    pass


class GiftSafetyHold(RuntimeError):
    pass


class GiftHuntWorker(QObject):
    """Shared ORAS direct-gift shiny-reset engine.

    Direct gifts reset after every non-shiny. The fossil profile uses an
    adaptive mixed batch: start with one lead and empty party slots, revive the
    supported Devon fossils actually available in the Bag (1-5, capped at five),
    inspect each stable checksum-valid PK6 immediately, HOLD on the first shiny,
    and reset after every available fossil in that batch is confirmed non-shiny.
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

    def __init__(self, *, profile_key: str, host: str, base_dir, bridge_port: int = 4952,
                 input_port: int = 4952, timeout: float = 1.5,
                 auto_support_zip: bool = True, use_code_ips: bool = False):
        super().__init__()
        self.profile_key = str(profile_key or "").strip().lower()
        self.gift_profile = get_gift_profile(self.profile_key)
        self.host = str(host)
        self.base_dir = Path(base_dir)
        self.bridge_port = int(bridge_port)
        self.input_port = int(input_port)
        self.timeout = float(timeout)
        self.auto_support_zip = bool(auto_support_zip)
        self.use_code_ips = bool(use_code_ips)
        self.stop_requested = False
        self.started_monotonic = None
        self.read_count = 0
        self.game_profile = None
        self.anchor = None
        self.last_reset = None
        self.last_party_before = None
        self.last_party_after = None
        self.last_gift = None
        self.last_trigger = None
        self.last_batch = []
        self.session_seen = 0
        self.session_shinies = 0
        self.session_resets = 0
        self.session_batches = 0
        self.shiny_charm_state = {"detected": None, "status": "UNVERIFIED"}
        self.last_encounter_monotonic = None
        self._lifetime_time_committed = False

        paths = get_profile_paths()
        self.profile_paths = paths
        self.log_dir = paths.root / "logs"
        self.support_dir = paths.root / "support"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.support_dir.mkdir(parents=True, exist_ok=True)
        paths.stats_dir.mkdir(parents=True, exist_ok=True)
        paths.session_stats_dir.mkdir(parents=True, exist_ok=True)
        paths.encounters_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_path = self.log_dir / f"gift_{self.profile_key}_{stamp}.log"
        self.stats_path = paths.stats_dir / f"gift_{self.profile_key}.json"
        self.session_path = paths.session_stats_dir / f"gift_{self.profile_key}_latest.json"
        self.encounter_path = paths.encounters_dir / f"gift_{self.profile_key}.jsonl"
        self.batch_path = paths.encounters_dir / f"gift_{self.profile_key}_batches.jsonl"
        self.last_seen_path = paths.last_seen_path
        self.lifetime = self._load_stats()

    def _hardware_validation(self):
        if self.gift_profile.trigger_kind == "FOSSIL_BATCH_5":
            return "FOSSIL_MIXED_BATCH_PARTY_PATH_PROVEN"
        return "SHARED_ORAS_PARTY_PK6_GIFT_ENGINE"

    def _default_stats(self):
        return {
            "hunt_type": "Gift",
            "gift_key": self.profile_key,
            "target": self.gift_profile.name,
            "species": self.gift_profile.species,
            "location_name": self.gift_profile.location,
            "lifetime_seen": 0,
            "lifetime_shinies": 0,
            "phase_seen": 0,
            "last_phase_seen": 0,
            "phase_log_miss": None,
            "phase_cumulative_probability": 0.0,
            "last_phase_cumulative_probability": None,
            "lifetime_resets": 0,
            "lifetime_batches": 0,
            "fossil_lifetime_batches": 0,
            "fossil_lifetime_revived": 0,
            "fossil_batch_size_counts": {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0},
            "fossil_last_batch_size": None,
            "fossil_last_batch_species": [],
            "last_shiny": None,
            "highest_sv": None,
            "lowest_sv": None,
            "highest_iv_sum": None,
            "lowest_iv_sum": None,
            "lifetime_hunt_seconds": 0.0,
            "hardware_validation": self._hardware_validation(),
        }

    def _load_stats(self):
        data = self._default_stats()
        loaded = {}
        try:
            old = json.loads(self.stats_path.read_text(encoding="utf-8"))
            if isinstance(old, dict):
                loaded = old
                data.update(old)
        except Exception:
            pass
        # Current profile/build metadata is authoritative; old stats must not
        # keep obsolete labels such as "Any 5 Fossils".
        data["hunt_type"] = "Gift"
        data["gift_key"] = self.profile_key
        data["target"] = self.gift_profile.name
        data["species"] = self.gift_profile.species
        data["location_name"] = self.gift_profile.location
        data["hardware_validation"] = self._hardware_validation()
        counts = {str(i): 0 for i in range(1, 6)}
        if isinstance(data.get("fossil_batch_size_counts"), dict):
            for key in counts:
                counts[key] = int(data["fossil_batch_size_counts"].get(key, 0) or 0)
        data["fossil_batch_size_counts"] = counts
        if loaded.get("phase_log_miss") is None:
            odds = resolve_shiny_odds(
                game="ORAS", hunt_type="Gift", shiny_charm_present=None,
                shiny_charm_applies=False,
            )
            data["phase_log_miss"] = phase_log_miss_for_constant(
                int(data.get("phase_seen", 0) or 0), odds.probability
            )
        data["phase_cumulative_probability"] = cumulative_probability_from_log_miss(
            data.get("phase_log_miss", 0.0)
        )
        return data

    @staticmethod
    def _atomic_json(path: Path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        tmp.replace(path)

    def _log(self, text: str, **fields):
        stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
        suffix = " " + json.dumps(fields, sort_keys=True, default=str) if fields else ""
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
        self.status.emit("STOPPING", "Waiting for Gift safe boundary")

    def _pulse_a(self, inputs: AcknowledgedInput, hold_ms=90, settle_ms=170):
        self._check_stop()
        return inputs.pulse(("A",), hold_ms=int(hold_ms), resume_settle_ms=0,
                            packet_interval_ms=20, release_ms=int(settle_ms))

    # ------------------------------------------------------------------
    # v0p43DL — Omega Ruby Devon fossil RAM state machine
    #
    # Hardware-confirmed on Omega Ruby 1.4 from two marked runs:
    #
    #   UI classifier bytes:
    #     0x08C41778 / 0x08C4177A
    #       79/59 = scientist/field ready
    #       2E/0E = normal Devon dialogue
    #       6F/4F = Yes/No menu
    #       21/01 = fossil list / gift-received family
    #       3F/1F = post-gift nickname text handoff
    #
    #   fossil_scr.amx:
    #     live AMX table entry 40 at 0x005F47A4
    #     verified script pointer 0x005E2A19 on the tested 1.4 process
    #     observed descriptor pointer field at 0x08D5BA54
    #     observed owner at 0x08D5BA18
    #
    #   fossil owner:
    #     owner+0x04 = 3 on both Yes/No menus
    #     owner+0x5C = 0 before gift, 0x11 at received/gift state,
    #                  1 during nickname/return states
    #     owner+0x174 = live child pointer at received/gift state,
    #                   NULL at nickname state
    #
    #   0x081FB714 = 0x0008E6A0 after the nickname B returns control.
    #
    # The two Yes/No menus intentionally share 6F/4F.  We NEVER identify
    # them from that pair alone: owner+0x5C separates initial fossil Yes
    # (0) from nickname Yes/No (1).
    #
    # This path is Omega Ruby only until equivalent Alpha Sapphire hardware
    # traces are collected.  The legacy DI fossil path remains untouched for
    # Alpha Sapphire.
    # ------------------------------------------------------------------
    OR_FOSSIL_AMX_TABLE_ENTRY = 0x005F47A4  # table[40]
    OR_FOSSIL_EXPECTED_SCRIPT_INDEX = 40
    OR_FOSSIL_EXPECTED_HIT = 0x08D5BA54
    OR_FOSSIL_EXPECTED_OWNER = 0x08D5BA18
    OR_FOSSIL_SCRIPT_SCAN_START = 0x08D40000
    OR_FOSSIL_SCRIPT_SCAN_END = 0x08D70000

    OR_FOSSIL_UI_A = 0x08C41778
    OR_FOSSIL_UI_B = 0x08C4177A
    OR_FOSSIL_MENU_FLAG = 0x088031B2
    OR_FOSSIL_MENU_TEXT = 0x088031E8
    OR_FOSSIL_RETURN_FLAG = 0x081FB714

    OR_FOSSIL_FIELD_PAIR = (0x79, 0x59)
    OR_FOSSIL_DIALOGUE_PAIR = (0x2E, 0x0E)
    OR_FOSSIL_YESNO_PAIR = (0x6F, 0x4F)
    OR_FOSSIL_LIST_PAIR = (0x21, 0x01)
    OR_FOSSIL_NICKNAME_TEXT_PAIR = (0x3F, 0x1F)
    OR_FOSSIL_RETURN_FLAG_VALUE = 0x0008E6A0

    @staticmethod
    def _u16_read(bridge: Bridge, address: int) -> int:
        return struct.unpack("<H", bridge.read(int(address), 2))[0]

    @staticmethod
    def _u8_read(bridge: Bridge, address: int) -> int:
        return bridge.read(int(address), 1)[0]

    def _use_or_fossil_state_machine(self) -> bool:
        return (
            self.gift_profile.trigger_kind == "FOSSIL_BATCH_5"
            and str((self.game_profile or {}).get("key") or "") == "omega_ruby"
        )

    def _or_fossil_live_script_ptr(self, bridge: Bridge) -> int:
        ptr = self._u32_read(bridge, self.OR_FOSSIL_AMX_TABLE_ENTRY)
        # Verify the live table entry itself instead of trusting an offline VA.
        raw = bridge.read(ptr, 15)
        if raw[:14] != b"fossil_scr.amx":
            raise GiftSafetyHold(
                f"Omega Ruby fossil AMX table[40] did not resolve fossil_scr.amx: "
                f"ptr=0x{ptr:08X} raw={raw[:15]!r}"
            )
        return ptr

    def _or_fossil_owner_at_hit(self, bridge: Bridge, hit: int, script_ptr: int) -> int | None:
        try:
            if self._u32_read(bridge, int(hit)) != int(script_ptr):
                return None
            if self._u16_read(bridge, int(hit) - 2) != self.OR_FOSSIL_EXPECTED_SCRIPT_INDEX:
                return None
            return int(hit) - 0x3C
        except Exception:
            return None

    def _resolve_or_fossil_owner(self, bridge: Bridge, *, timeout_s: float = 3.0) -> int:
        """Resolve fossil_scr.amx owner; fast-path the twice-proven address, scan only as fallback."""
        script_ptr = self._or_fossil_live_script_ptr(bridge)
        deadline = time.monotonic() + max(0.25, float(timeout_s))
        last_scan = None

        while time.monotonic() < deadline:
            self._check_stop()

            owner = self._or_fossil_owner_at_hit(
                bridge, self.OR_FOSSIL_EXPECTED_HIT, script_ptr
            )
            if owner is not None:
                self._log(
                    "FOSSIL OR SCRIPT OWNER",
                    source="hardware_fast_path",
                    script_ptr=f"0x{script_ptr:08X}",
                    hit=f"0x{self.OR_FOSSIL_EXPECTED_HIT:08X}",
                    owner=f"0x{owner:08X}",
                )
                return owner

            # Fallback is intentionally bounded to the script-instance heap band
            # proven by the mapper.  It runs only if the deterministic fast path
            # is absent (for example after allocator relocation).
            needle = struct.pack("<I", script_ptr)
            addr = self.OR_FOSSIL_SCRIPT_SCAN_START
            found = []
            while addr < self.OR_FOSSIL_SCRIPT_SCAN_END:
                self._check_stop()
                n = min(0xC00, self.OR_FOSSIL_SCRIPT_SCAN_END - addr)
                try:
                    blob = bridge.read(addr, n)
                except Exception:
                    addr += n
                    continue
                at = 0
                while True:
                    rel = blob.find(needle, at)
                    if rel < 0:
                        break
                    hit = addr + rel
                    candidate = self._or_fossil_owner_at_hit(bridge, hit, script_ptr)
                    if candidate is not None:
                        found.append((hit, candidate))
                    at = rel + 1
                addr += n

            last_scan = found
            if len(found) == 1:
                hit, owner = found[0]
                self._log(
                    "FOSSIL OR SCRIPT OWNER",
                    source="bounded_fallback_scan",
                    script_ptr=f"0x{script_ptr:08X}",
                    hit=f"0x{hit:08X}",
                    owner=f"0x{owner:08X}",
                )
                return owner
            if len(found) > 1:
                raise GiftSafetyHold(
                    f"Omega Ruby fossil AMX owner was ambiguous: "
                    f"{[(f'0x{h:08X}', f'0x{o:08X}') for h, o in found]}"
                )
            time.sleep(0.08)

        raise GiftSafetyHold(
            f"Omega Ruby fossil AMX owner did not appear within {timeout_s:.2f}s; "
            f"last_scan={last_scan}"
        )

    def _or_fossil_state(self, bridge: Bridge, owner: int | None = None) -> dict:
        pair = (
            self._u8_read(bridge, self.OR_FOSSIL_UI_A),
            self._u8_read(bridge, self.OR_FOSSIL_UI_B),
        )
        menu_flag = self._u8_read(bridge, self.OR_FOSSIL_MENU_FLAG)
        return_flag = self._u32_read(bridge, self.OR_FOSSIL_RETURN_FLAG)

        owner4 = None
        owner5c = None
        owner174 = None
        if owner is not None:
            try:
                owner4 = self._u8_read(bridge, int(owner) + 0x04)
                owner5c = self._u8_read(bridge, int(owner) + 0x5C)
                owner174 = self._u32_read(bridge, int(owner) + 0x174)
            except Exception:
                owner4 = owner5c = owner174 = None

        # Mixed-fossil hardware proof: owner+0x5C is script-local data, not a
        # species-independent phase enum.  Tyrunt used 0x11 at RECEIVED_GIFT,
        # while Anorith used 0x07.  The durable invariant is zero before the
        # gift and non-zero after the gift; UI pair + owner+0x174 decide the
        # exact post-gift screen.
        postgift_owner = owner is not None and owner5c not in (None, 0)

        if pair == self.OR_FOSSIL_FIELD_PAIR:
            # Hardware batch trace 20260830_214742 proved a completed
            # nickname decline can return to the scientist with the normal
            # 79/59 field pair while 0x081FB714 remains 0x001A0DAC.  For
            # mixed fossils the post-gift owner marker may be any non-zero
            # value, so do not hard-code Tyrunt's value 1 here.
            if (
                return_flag == self.OR_FOSSIL_RETURN_FLAG_VALUE
                or (postgift_owner and menu_flag == 1)
            ):
                state = "RETURN_READY"
            else:
                state = "SCIENTIST_READY"
        elif pair == self.OR_FOSSIL_YESNO_PAIR:
            if owner4 == 3 and owner5c == 0:
                state = "FOSSIL_YES_NO"
            elif owner4 == 3 and postgift_owner:
                state = "NICKNAME_YES_NO"
            else:
                state = "YES_NO_UNCLASSIFIED"
        elif pair == self.OR_FOSSIL_LIST_PAIR:
            # RECEIVED_GIFT is the spawned child script pointer, not a
            # particular owner+0x5C value.  This is what lets Tyrunt (0x11),
            # Anorith (0x07), and the other Devon fossils share one batch.
            if postgift_owner and owner174 not in (None, 0):
                state = "RECEIVED_GIFT"
            elif owner5c == 0 and menu_flag == 0x10:
                state = "FOSSIL_LIST"
            else:
                state = "FOSSIL_PROGRESS"
        elif (
            pair == self.OR_FOSSIL_NICKNAME_TEXT_PAIR
            and postgift_owner
            and owner174 in (None, 0)
        ):
            state = "NICKNAME_TEXT"
        elif pair == self.OR_FOSSIL_DIALOGUE_PAIR:
            state = "DEVON_DIALOGUE"
        elif owner is not None and owner5c == 0:
            state = "FOSSIL_SCRIPT_PROGRESS"
        else:
            state = "UNKNOWN"

        # A small text window is a change detector only; production decisions
        # never depend on its language/content.
        try:
            text_window = bridge.read(self.OR_FOSSIL_MENU_TEXT, 0x40)
            text_fingerprint = hashlib.sha256(text_window).hexdigest()[:16]
        except Exception:
            text_fingerprint = None

        return {
            "state": state,
            "ui_a": pair[0],
            "ui_b": pair[1],
            "ui_pair": f"{pair[0]:02X}/{pair[1]:02X}",
            "menu_flag": menu_flag,
            "return_flag": f"0x{return_flag:08X}",
            "owner": f"0x{int(owner):08X}" if owner is not None else None,
            "owner4": owner4,
            "owner5c": owner5c,
            "owner174": f"0x{owner174:08X}" if owner174 is not None else None,
            "text_fingerprint": text_fingerprint,
        }

    @staticmethod
    def _or_fossil_state_signature(state: dict) -> tuple:
        return (
            state.get("state"),
            state.get("ui_a"),
            state.get("ui_b"),
            state.get("menu_flag"),
            state.get("return_flag"),
            state.get("owner4"),
            state.get("owner5c"),
            state.get("owner174"),
            state.get("text_fingerprint"),
        )

    def _wait_or_fossil_state(
        self,
        bridge: Bridge,
        owner: int | None,
        *,
        wanted: set[str],
        timeout_s: float,
        stable_reads: int = 2,
        label: str,
    ) -> dict:
        deadline = time.monotonic() + max(0.25, float(timeout_s))
        last = None
        candidate_sig = None
        streak = 0
        while time.monotonic() < deadline:
            self._check_stop()
            try:
                current = self._or_fossil_state(bridge, owner)
            except Exception as exc:
                last = {"error": f"{type(exc).__name__}: {exc}"}
                candidate_sig = None
                streak = 0
                time.sleep(0.06)
                continue
            last = current
            if current.get("state") in wanted:
                sig = self._or_fossil_state_signature(current)
                if sig == candidate_sig:
                    streak += 1
                else:
                    candidate_sig = sig
                    streak = 1
                if streak >= max(1, int(stable_reads)):
                    self._log(
                        "FOSSIL OR STATE READY",
                        label=label, wanted=sorted(wanted), stable_reads=streak, **current
                    )
                    return current
            else:
                candidate_sig = None
                streak = 0
            time.sleep(0.06)
        raise GiftSafetyHold(
            f"Omega Ruby fossil state timeout at {label}; "
            f"wanted={sorted(wanted)} last={last}"
        )

    @staticmethod
    def _or_fossil_dwell_signature(state: dict) -> tuple:
        """Authoritative fossil UI signature used for readiness dwell.

        The text fingerprint is intentionally excluded.  It is a diagnostic
        change detector only and hardware support trace 20260831_203654 proved
        that harmless text-window changes can occur while every authoritative
        NICKNAME_TEXT field remains unchanged.
        """
        return (
            state.get("state"),
            state.get("ui_a"),
            state.get("ui_b"),
            state.get("menu_flag"),
            state.get("return_flag"),
            state.get("owner4"),
            state.get("owner5c"),
            state.get("owner174"),
        )

    def _wait_or_fossil_state_dwell(
        self,
        bridge: Bridge,
        owner: int | None,
        *,
        wanted: set[str],
        dwell_s: float,
        timeout_s: float,
        label: str,
    ) -> dict:
        """Require one authoritative RAM state signature to remain stable.

        The Omega Ruby received-Pokemon state becomes visible in RAM before the
        message box is actually ready to consume A. Firmware HID completion is
        therefore not proof that the game accepted the input. This helper keeps
        the state machine RAM-authoritative while adding a bounded readiness
        dwell. Text-window fingerprints are diagnostic only and never reset the
        dwell timer.
        """
        deadline = time.monotonic() + max(float(dwell_s) + 0.5, float(timeout_s))
        authority_keys = (
            "state", "ui_a", "ui_b", "menu_flag", "return_flag",
            "owner4", "owner5c", "owner174",
        )
        candidate_sig = None
        candidate_state = None
        stable_since = None
        last = None
        last_text_fingerprint = None
        ignored_text_fingerprint_changes = 0
        while time.monotonic() < deadline:
            self._check_stop()
            try:
                current = self._or_fossil_state(bridge, owner)
            except Exception as exc:
                last = {"error": f"{type(exc).__name__}: {exc}"}
                if candidate_sig is not None:
                    self._log(
                        "FOSSIL OR STATE DWELL RESET",
                        label=label, reason="state_read_error",
                        error=last["error"],
                    )
                candidate_sig = None
                candidate_state = None
                stable_since = None
                last_text_fingerprint = None
                time.sleep(0.06)
                continue
            last = current

            text_fingerprint = current.get("text_fingerprint")
            if (
                last_text_fingerprint is not None
                and text_fingerprint != last_text_fingerprint
                and candidate_sig is not None
            ):
                ignored_text_fingerprint_changes += 1
            last_text_fingerprint = text_fingerprint

            if current.get("state") in wanted:
                sig = self._or_fossil_dwell_signature(current)
                now = time.monotonic()
                if sig != candidate_sig:
                    if candidate_sig is not None and candidate_state is not None:
                        changed_fields = {
                            key: {
                                "before": candidate_state.get(key),
                                "after": current.get(key),
                            }
                            for key in authority_keys
                            if candidate_state.get(key) != current.get(key)
                        }
                        self._log(
                            "FOSSIL OR STATE DWELL RESET",
                            label=label, reason="authoritative_signature_changed",
                            changed_fields=changed_fields,
                            previous_text_fingerprint=candidate_state.get("text_fingerprint"),
                            current_text_fingerprint=text_fingerprint,
                        )
                    candidate_sig = sig
                    candidate_state = dict(current)
                    stable_since = now
                elif stable_since is not None and (now - stable_since) >= float(dwell_s):
                    self._log(
                        "FOSSIL OR STATE DWELL READY",
                        label=label, wanted=sorted(wanted),
                        dwell_s=round(now - stable_since, 3),
                        ignored_text_fingerprint_changes=ignored_text_fingerprint_changes,
                        **current
                    )
                    return current
            else:
                if candidate_sig is not None:
                    self._log(
                        "FOSSIL OR STATE DWELL RESET",
                        label=label, reason="state_left_wanted",
                        previous_state=(candidate_state or {}).get("state"),
                        current_state=current.get("state"),
                    )
                candidate_sig = None
                candidate_state = None
                stable_since = None
            time.sleep(0.06)
        raise GiftSafetyHold(
            f"Omega Ruby fossil state dwell timeout at {label}; "
            f"wanted={sorted(wanted)} dwell_s={float(dwell_s):.2f} "
            f"ignored_text_fingerprint_changes={ignored_text_fingerprint_changes} last={last}"
        )

    def _wait_stable_or_fossil_gift(
        self,
        bridge: Bridge,
        baseline: set[tuple[int, str, str]],
        *,
        expected_slot: int,
        timeout_s: float,
        required_reads: int = 3,
    ):
        """Return only a repeatedly identical, trainer-owned, supported new fossil PK6.

        Transient/partially-written party data is ignored rather than being
        allowed to produce a false shiny HOLD.
        """
        deadline = time.monotonic() + max(0.15, float(timeout_s))
        trainer_tid, trainer_sid = read_trainer_ids(bridge)
        candidate_key = None
        streak = 0
        last = None
        last_snap = None

        while time.monotonic() < deadline:
            self._check_stop()
            try:
                snap = self._snapshot_party(bridge, allow_unmapped=True)
                last_snap = snap
                if str(snap.get("source")) == "UNMAPPED_D25":
                    time.sleep(0.08)
                    continue
                rows = list(snap.get("parsed") or [])
                idx = int(expected_slot) - 1
                mon = rows[idx] if 0 <= idx < len(rows) else None
                if not isinstance(mon, dict):
                    last = {"reason": "expected slot absent", "slot": expected_slot}
                    candidate_key = None
                    streak = 0
                    time.sleep(0.08)
                    continue

                ident = parsed_identity(mon)
                species = int(mon.get("species") or 0)
                tid = int(mon.get("tid") or 0)
                sid = int(mon.get("sid") or 0)
                candidate = dict(mon)
                candidate["party_slot"] = int(expected_slot)

                reason = None
                if ident is None or ident in baseline:
                    reason = "no new checksum-valid identity yet"
                elif species not in FOSSIL_SPECIES:
                    reason = f"transient unsupported species {species}"
                elif tid != int(trainer_tid) or sid != int(trainer_sid):
                    reason = (
                        f"transient trainer mismatch tid/sid={tid}/{sid} "
                        f"expected={trainer_tid}/{trainer_sid}"
                    )

                if reason is not None:
                    if ident is not None and ident not in baseline:
                        self._log(
                            "FOSSIL TRANSIENT PK6 IGNORED",
                            expected_slot=int(expected_slot), reason=reason,
                            species=species, pid=mon.get("pid"), ec=mon.get("ec"),
                            tid=tid, sid=sid, checksum=mon.get("checksum"),
                        )
                    last = {"reason": reason, "candidate": candidate}
                    candidate_key = None
                    streak = 0
                    time.sleep(0.08)
                    continue

                key = (
                    int(expected_slot), species,
                    str(mon.get("ec") or "").upper(),
                    str(mon.get("pid") or "").upper(),
                    tid, sid,
                    str(mon.get("checksum") or "").upper(),
                    str(mon.get("raw_sha256") or ""),
                )
                if key == candidate_key:
                    streak += 1
                else:
                    candidate_key = key
                    streak = 1

                self._log(
                    "FOSSIL STABLE PK6 SAMPLE",
                    expected_slot=int(expected_slot), streak=streak,
                    required_reads=int(required_reads), species=species,
                    pid=mon.get("pid"), ec=mon.get("ec"),
                    tid=tid, sid=sid, checksum=mon.get("checksum"),
                )
                if streak >= max(2, int(required_reads)):
                    self._log(
                        "FOSSIL STABLE PK6 AUTHORITY",
                        expected_slot=int(expected_slot), stable_reads=streak,
                        species=species, pid=mon.get("pid"), ec=mon.get("ec"),
                        shiny=bool(mon.get("is_shiny")), xor=mon.get("shiny_xor"),
                    )
                    return candidate, snap
                last = {"candidate": candidate, "streak": streak}
            except GiftSafetyHold:
                raise
            except Exception as exc:
                last = {"error": f"{type(exc).__name__}: {exc}"}
            time.sleep(0.08)
        return None, last_snap

    def _drain_or_fossil_exhausted_dialogue(
        self,
        bridge: Bridge,
        inputs: AcknowledgedInput,
        *,
        owner: int | None,
        batch_index: int,
        initial_dialogue_seen: bool = False,
        timeout_s: float = 8.0,
    ) -> dict | None:
        """Conservatively prove that Devon has no fossil left for this batch.

        This is used only after at least one fossil has already been revived in
        the current reset.  A valid exhaustion path must show ordinary Devon
        dialogue and then return to the scientist WITHOUT ever entering the
        fossil Yes/No or fossil-list states and without a new party PK6.

        The helper may press A only on the hardware-proven DEVON_DIALOGUE state.
        It never treats UNKNOWN or a menu state as exhaustion.
        """
        deadline = time.monotonic() + max(1.0, float(timeout_s))
        saw_dialogue = bool(initial_dialogue_seen)
        last = None
        field_sig = None
        field_streak = 0

        while time.monotonic() < deadline:
            self._check_stop()
            current = self._or_fossil_state(bridge, owner)
            last = current
            state = current.get("state")

            if state in {"FOSSIL_YES_NO", "FOSSIL_LIST", "FOSSIL_PROGRESS",
                         "FOSSIL_SCRIPT_PROGRESS", "RECEIVED_GIFT",
                         "NICKNAME_TEXT", "NICKNAME_YES_NO"}:
                return None

            if state == "DEVON_DIALOGUE":
                saw_dialogue = True
                field_sig = None
                field_streak = 0
                ack = self._pulse_a(inputs, hold_ms=180, settle_ms=220)
                self._log(
                    "FOSSIL OR EXHAUSTION DIALOGUE A",
                    batch_index=int(batch_index), ack=ack, **current
                )
                time.sleep(0.18)
                continue

            if state in {"SCIENTIST_READY", "RETURN_READY"} and saw_dialogue:
                sig = self._or_fossil_state_signature(current)
                if sig == field_sig:
                    field_streak += 1
                else:
                    field_sig = sig
                    field_streak = 1
                if field_streak >= 3:
                    self._log(
                        "FOSSIL OR BATCH EXHAUSTED",
                        batch_index=int(batch_index),
                        completed_fossils=int(batch_index) - 1,
                        stable_reads=field_streak,
                        **current,
                    )
                    return {
                        "success": False,
                        "batch_exhausted": True,
                        "completed_fossils": int(batch_index) - 1,
                        "reason": "Devon returned to scientist without offering another fossil",
                        "state": current,
                    }
            else:
                field_sig = None
                field_streak = 0

            time.sleep(0.06)

        self._log(
            "FOSSIL OR EXHAUSTION NOT PROVEN",
            batch_index=int(batch_index), saw_dialogue=bool(saw_dialogue), last=last,
        )
        return None

    def _trigger_or_fossil_state_machine(
        self,
        bridge: Bridge,
        inputs: AcknowledgedInput,
        baseline: set[tuple[int, str, str]],
        *,
        batch_index: int,
        expected_slot: int,
        allow_batch_exhausted: bool = False,
    ):
        """Drive one Devon revival up to stable PK6 authority.

        For batch members 2-5, this can also return ``batch_exhausted`` when
        Devon's RAM-authoritative event path proves there is no additional
        fossil available.  Exhaustion is deliberately impossible on member 1:
        a hunt started with zero fossils remains a safety hold rather than an
        endless reset loop.
        """
        presses = []
        owner = None
        saw_dialogue = False
        saw_offer = False

        start = self._wait_or_fossil_state(
            bridge, None,
            wanted={"SCIENTIST_READY", "RETURN_READY"},
            timeout_s=5.0, stable_reads=2,
            label=f"batch_{batch_index}_cycle_start",
        )

        self._log(
            "FOSSIL OR CYCLE START",
            batch_index=int(batch_index), expected_slot=int(expected_slot),
            allow_batch_exhausted=bool(allow_batch_exhausted), **start
        )

        # First A starts/restarts the Devon fossil interaction.
        ack = self._pulse_a(inputs, hold_ms=180, settle_ms=220)
        presses.append({"index": 1, "ack": ack, "state_before": start})

        try:
            owner = self._resolve_or_fossil_owner(bridge, timeout_s=3.5)
        except Exception as exc:
            # On a proven post-first-fossil cycle, absence of a fossil script
            # owner can be the "no fossils left" path.  We still require Devon
            # dialogue -> stable scientist return before accepting exhaustion.
            if allow_batch_exhausted:
                self._log(
                    "FOSSIL OR OWNER ABSENT ON POSSIBLE EXHAUSTION",
                    batch_index=int(batch_index), error=f"{type(exc).__name__}: {exc}"
                )
                exhausted = self._drain_or_fossil_exhausted_dialogue(
                    bridge, inputs, owner=None, batch_index=batch_index,
                    initial_dialogue_seen=False, timeout_s=8.0,
                )
                if exhausted is not None:
                    exhausted["presses"] = presses
                    exhausted["owner"] = None
                    exhausted["state_machine"] = "OR_FOSSIL_RAM_DYNAMIC_BATCH"
                    return exhausted, None, None
            raise

        max_presses = int(self.gift_profile.max_a_presses)
        for press_index in range(2, max_presses + 1):
            self._check_stop()

            mon, snap = self._wait_stable_or_fossil_gift(
                bridge, baseline, expected_slot=expected_slot,
                timeout_s=0.42, required_reads=3,
            )
            if mon is not None:
                return {
                    "success": True,
                    "accepted_press": press_index - 1,
                    "presses": presses,
                    "owner": f"0x{owner:08X}",
                    "state_machine": "OR_FOSSIL_RAM_DYNAMIC_BATCH",
                }, mon, snap

            state = self._or_fossil_state(bridge, owner)
            self._log(
                "FOSSIL OR PRE-GIFT STATE",
                batch_index=int(batch_index), expected_slot=int(expected_slot),
                press_index=int(press_index), saw_dialogue=bool(saw_dialogue),
                saw_offer=bool(saw_offer), **state
            )

            state_name = state.get("state")
            if state_name == "DEVON_DIALOGUE":
                saw_dialogue = True
            # Only an actual Yes/No prompt or fossil list proves that Devon
            # offered another fossil.  FOSSIL_PROGRESS is also used by the
            # hardware-proven no-fossils-left dialogue path, so treating it as
            # an offer breaks adaptive 1-4 fossil batches.
            if state_name in {"FOSSIL_YES_NO", "FOSSIL_LIST"}:
                saw_offer = True

            if state_name == "RECEIVED_GIFT":
                mon, snap = self._wait_stable_or_fossil_gift(
                    bridge, baseline, expected_slot=expected_slot,
                    timeout_s=5.0, required_reads=3,
                )
                if mon is None:
                    raise GiftSafetyHold(
                        f"Omega Ruby fossil received-state appeared but stable PK6 "
                        f"did not settle in expected slot {expected_slot}"
                    )
                return {
                    "success": True,
                    "accepted_press": press_index - 1,
                    "presses": presses,
                    "owner": f"0x{owner:08X}",
                    "state_machine": "OR_FOSSIL_RAM_DYNAMIC_BATCH",
                }, mon, snap

            if state_name in {"NICKNAME_TEXT", "NICKNAME_YES_NO"}:
                raise GiftSafetyHold(
                    f"Omega Ruby fossil reached nickname state before stable PK6 authority: {state}"
                )
            if state_name == "YES_NO_UNCLASSIFIED":
                raise GiftSafetyHold(
                    f"Omega Ruby fossil Yes/No state could not be classified safely: {state}"
                )

            # After at least one successful revival, Devon returning to the
            # scientist without ever offering Yes/No/list is the conservative
            # end-of-available-fossils signal.
            if (
                allow_batch_exhausted
                and saw_dialogue
                and not saw_offer
                and state_name in {"SCIENTIST_READY", "RETURN_READY"}
            ):
                exhausted = self._drain_or_fossil_exhausted_dialogue(
                    bridge, inputs, owner=owner, batch_index=batch_index,
                    initial_dialogue_seen=True, timeout_s=2.0,
                )
                if exhausted is not None:
                    exhausted["presses"] = presses
                    exhausted["owner"] = f"0x{owner:08X}"
                    exhausted["state_machine"] = "OR_FOSSIL_RAM_DYNAMIC_BATCH"
                    return exhausted, None, None

            # Before the gift, A is permitted only in the verified Devon/fossil
            # states.  On an exhaustion probe, stable scientist return is
            # handled above and never blindly A-spammed.
            allowed = state_name in {
                "DEVON_DIALOGUE", "FOSSIL_YES_NO", "FOSSIL_LIST",
                "FOSSIL_PROGRESS", "FOSSIL_SCRIPT_PROGRESS",
            }
            if not allowed:
                raise GiftSafetyHold(
                    f"Omega Ruby fossil pre-gift state is not A-safe: {state}"
                )
            if state.get("owner5c") not in (0, None):
                raise GiftSafetyHold(
                    f"Omega Ruby fossil pre-gift owner phase is not zero: {state}"
                )

            ack = self._pulse_a(inputs, hold_ms=180, settle_ms=220)
            presses.append({"index": press_index, "ack": ack, "state_before": state})
            time.sleep(0.18)

        return {
            "success": False,
            "presses": presses,
            "owner": f"0x{owner:08X}" if owner is not None else None,
            "state_machine": "OR_FOSSIL_RAM_DYNAMIC_BATCH",
            "reason": "Omega Ruby fossil RAM state machine exhausted bounded pre-gift A budget",
        }, None, None

    def _finish_or_fossil_nonshiny(
        self,
        bridge: Bridge,
        inputs: AcknowledgedInput,
        *,
        owner: int,
        batch_index: int,
        party_slot: int,
    ):
        """Hardware-proven post-gift choreography gated by RAM, not timers.

        Stable PK6 + non-shiny decision has already happened before entry.
        We wait for RECEIVED_GIFT, press A once, then handle the proven
        nickname text handoff (3F/1F + post-gift owner marker) with one A if
        needed.  Only after the nickname Yes/No selector is RAM-authoritative
        do we press B once, then require post-B RETURN_READY.
        """
        received = self._wait_or_fossil_state(
            bridge, owner,
            wanted={"RECEIVED_GIFT"},
            timeout_s=6.0, stable_reads=2,
            label=f"batch_{batch_index}_received",
        )
        self._log(
            "FOSSIL OR RECEIVED READY",
            batch_index=int(batch_index), party_slot=int(party_slot), **received
        )

        # Hardware support trace 20260830_213531 proved that RECEIVED_GIFT
        # appears ~0.8 s before the text box will actually consume A.  The
        # successful manual replay left ~5.4 s between gift-producing A and
        # the post-gift A.  Require the exact received-state signature to dwell
        # for 4.8 s before sending the ONE permitted A.
        received_ready = self._wait_or_fossil_state_dwell(
            bridge, owner,
            wanted={"RECEIVED_GIFT"},
            dwell_s=4.8, timeout_s=7.5,
            label=f"batch_{batch_index}_received_input_ready",
        )
        self._log(
            "FOSSIL OR RECEIVED INPUT READY",
            batch_index=int(batch_index), party_slot=int(party_slot), **received_ready
        )

        self._check_stop()
        a_ack = self._pulse_a(inputs, hold_ms=180, settle_ms=220)
        self._log(
            "FOSSIL OR RECEIVED A",
            batch_index=int(batch_index), party_slot=int(party_slot), ack=a_ack
        )

        # Hardware support trace 20260830_214242 proved there is a distinct
        # text-box state after the received-Pokemon A and BEFORE the nickname
        # Yes/No selector appears:
        #   NICKNAME_TEXT  = 3F/1F, owner+0x5C != 0
        # The game waits here for one more A to reveal the selector.  Accept
        # either state first so a faster/slower UI revision cannot cause a
        # duplicate A if the selector is already open.
        nickname_stage = self._wait_or_fossil_state(
            bridge, owner,
            wanted={"NICKNAME_TEXT", "NICKNAME_YES_NO"},
            timeout_s=8.0, stable_reads=2,
            label=f"batch_{batch_index}_nickname_stage",
        )
        self._log(
            "FOSSIL OR NICKNAME STAGE READY",
            batch_index=int(batch_index), party_slot=int(party_slot), **nickname_stage
        )

        if nickname_stage.get("state") == "NICKNAME_TEXT":
            # Require the exact text-box state to dwell before its ONE A.
            # This is deliberately state-guarded, not a blind repeated input.
            nickname_text_ready = self._wait_or_fossil_state_dwell(
                bridge, owner,
                wanted={"NICKNAME_TEXT"},
                dwell_s=1.5, timeout_s=4.0,
                label=f"batch_{batch_index}_nickname_text_input_ready",
            )
            self._log(
                "FOSSIL OR NICKNAME TEXT INPUT READY",
                batch_index=int(batch_index), party_slot=int(party_slot), **nickname_text_ready
            )

            self._check_stop()
            nickname_a_ack = self._pulse_a(inputs, hold_ms=180, settle_ms=220)
            self._log(
                "FOSSIL OR NICKNAME TEXT A",
                batch_index=int(batch_index), party_slot=int(party_slot), ack=nickname_a_ack
            )

            nickname = self._wait_or_fossil_state(
                bridge, owner,
                wanted={"NICKNAME_YES_NO"},
                timeout_s=6.0, stable_reads=2,
                label=f"batch_{batch_index}_nickname_yes_no",
            )
        else:
            nickname = nickname_stage

        self._log(
            "FOSSIL OR NICKNAME YESNO READY",
            batch_index=int(batch_index), party_slot=int(party_slot), **nickname
        )

        self._check_stop()
        b_ack = inputs.pulse(
            ("B",), hold_ms=180, resume_settle_ms=0,
            packet_interval_ms=20, release_ms=550
        )
        self._log(
            "FOSSIL OR NICKNAME B",
            batch_index=int(batch_index), party_slot=int(party_slot), ack=b_ack
        )

        returned = self._wait_or_fossil_state(
            bridge, owner,
            wanted={"RETURN_READY"},
            timeout_s=8.0, stable_reads=3,
            label=f"batch_{batch_index}_return_ready",
        )
        self._log(
            "FOSSIL OR RETURN READY",
            batch_index=int(batch_index), party_slot=int(party_slot), **returned
        )
        return {
            "transition": "OR_FOSSIL_RAM_RECEIVED_A_NICKNAME_B_RETURN_REARM",
            "received": received,
            "received_input_ready": received_ready,
            "nickname": nickname,
            "returned": returned,
            "a_ack": a_ack,
            "b_ack": b_ack,
        }

    FOSSIL_UI_FLOW_ADDR = 0x081FB390
    FOSSIL_UI_OUTER_PTR_ADDR = 0x081FB384
    FOSSIL_NICKNAME_READY_STABLE_SECONDS = 1.0

    @staticmethod
    def _u32_read(bridge: Bridge, address: int) -> int:
        return struct.unpack("<I", bridge.read(int(address), 4))[0]

    def _fossil_ui_signature(self, bridge: Bridge) -> tuple[int, int]:
        return (
            self._u32_read(bridge, self.FOSSIL_UI_FLOW_ADDR),
            self._u32_read(bridge, self.FOSSIL_UI_OUTER_PTR_ADDR),
        )

    def _wait_fossil_ui_stable(self, bridge: Bridge, *, stable_seconds: float, timeout_s: float, label: str):
        """Wait for one exact Devon UI flow/outer pair to settle before HID."""
        deadline = time.monotonic() + float(timeout_s)
        candidate = None
        stable_since = None
        last = None
        while time.monotonic() < deadline:
            self._check_stop()
            try:
                current = self._fossil_ui_signature(bridge)
            except Exception as exc:
                last = {"error": f"{type(exc).__name__}: {exc}"}
                candidate = None
                stable_since = None
                time.sleep(0.05)
                continue
            last = current
            # A nonzero outer is our minimal proof that a concrete UI object is live.
            if current[1] == 0:
                candidate = None
                stable_since = None
                time.sleep(0.05)
                continue
            if current != candidate:
                candidate = current
                stable_since = time.monotonic()
            elif stable_since is not None and time.monotonic() - stable_since >= float(stable_seconds):
                self._log(
                    "FOSSIL NICKNAME UI READY", label=label,
                    flow=f"0x{current[0]:08X}", outer=f"0x{current[1]:08X}",
                    stable_seconds=round(time.monotonic() - stable_since, 3),
                )
                return current
            time.sleep(0.05)
        raise GiftSafetyHold(f"fossil nickname UI never became input-ready; label={label} last={last}")

    def _decline_fossil_nickname(self, bridge: Bridge, inputs: AcknowledgedInput, *, batch_index: int, party_slot: int):
        """Clear Devon's post-revival nickname sequence using B only.

        Hardware through DH proved that the party PK6 becomes readable before
        Devon's generic flow=5 state uniquely identifies the nickname Yes/No
        prompt.  Treating that generic flow as the prompt caused both early-B
        and DOWN+A failures.  Auto-Capture already established the safe rule
        for nickname handling: A must be forbidden and B is the only decline
        input.  For fossils we therefore use a small bounded B-only clear:
        B advances any remaining post-revival text, B selects No when the
        nickname Yes/No prompt is reached, and any surplus B merely dismisses
        the trailing Devon text/does nothing once idle.

        A shiny fossil returns before this method, so no post-revival input is
        ever sent after a shiny PK6 is confirmed.
        """
        # Give the PK6/UI handoff a short arm interval before the first B.  Do
        # not use flow=5/outer as nickname-prompt authority; hardware proved it
        # is generic Devon dialogue state rather than a unique prompt marker.
        arm_seconds = 0.75
        time.sleep(arm_seconds)
        attempts = []
        for attempt in range(1, 5):
            self._check_stop()
            ack = inputs.pulse(("B",), hold_ms=180, resume_settle_ms=0,
                               packet_interval_ms=20, release_ms=550)
            attempts.append(ack)
            self._log(
                "FOSSIL POST-REVIVAL B-ONLY CLEAR",
                batch_index=int(batch_index), party_slot=int(party_slot),
                attempt=attempt, max_attempts=4, ack=ack,
                safety="Auto-Capture nickname policy: B only; A forbidden until clear completes",
            )
            time.sleep(0.45)
        settle_seconds = 0.65
        time.sleep(settle_seconds)
        self._log(
            "FOSSIL POST-REVIVAL B-ONLY CLEAR COMPLETE",
            batch_index=int(batch_index), party_slot=int(party_slot),
            attempts=len(attempts), arm_seconds=arm_seconds,
            settle_seconds=settle_seconds, next_action="START_NEXT_FOSSIL",
        )
        return {"attempts": attempts, "transition": "AUTO_CAPTURE_STYLE_B_ONLY_CLEAR"}

    def _snapshot_party(self, bridge: Bridge, *, allow_unmapped: bool = False) -> dict:
        self._check_stop()
        snap = get_runtime_party_snapshot_for_bridge(bridge)
        self.read_count += 1
        self.ram_reads.emit(self.read_count)
        if str(snap.get("source")) == "UNMAPPED_D25" and not allow_unmapped:
            raise GiftSafetyHold("live Party RAM authority is not mapped yet")
        return snap

    def _wait_for_party_authority(self, bridge: Bridge, *, timeout_s: float = 10.0) -> dict:
        """Allow the shared live-party mapper to revalidate after a game reset.

        The runtime party owner/pointers are process-local.  A soft reset can
        leave the shared cache pointing at the previous process for a few reads;
        the live_party layer deliberately needs several failed validations
        before invalidating that source.  Gift hunts must therefore give it a
        bounded remap window rather than treating the first post-reset
        UNMAPPED_D25 snapshot as a terminal safety failure.
        """
        deadline = time.monotonic() + max(0.5, float(timeout_s))
        polls = 0
        last = None
        while time.monotonic() < deadline:
            self._check_stop()
            polls += 1
            last = self._snapshot_party(bridge, allow_unmapped=True)
            if str(last.get("source")) != "UNMAPPED_D25":
                self._log(
                    "GIFT PARTY AUTHORITY READY",
                    polls=polls,
                    source=last.get("source"),
                    live_source=last.get("live_source"),
                    diagnostic=last.get("diagnostic"),
                )
                return last
            if polls == 1 or polls % 4 == 0:
                self._log(
                    "GIFT PARTY AUTHORITY REMAP",
                    polls=polls,
                    diagnostic=last.get("diagnostic"),
                    mapper=last.get("mapper"),
                )
            time.sleep(0.20)

        raise GiftSafetyHold(
            f"live Party RAM authority did not remap within {float(timeout_s):.1f}s; "
            f"last={dict((last or {}).get('mapper') or {})}"
        )

    def _wait_for_saved_field_authority(
        self,
        bridge: Bridge,
        anchor,
        *,
        timeout_s: float = 2.0,
    ) -> dict:
        """Allow post-reset field coordinates to settle without sending input."""
        deadline = time.monotonic() + max(0.25, float(timeout_s))
        polls = 0
        last = None

        while True:
            self._check_stop()
            polls += 1
            last = validate_saved_field_anchor(bridge, anchor)

            if bool(last.get("authority")):
                if polls > 1:
                    self._log(
                        "GIFT SAVED FIELD SETTLE READY",
                        polls=polls,
                        zone=last.get("zone"),
                        grid=last.get("grid"),
                        world_primary=last.get("world_primary"),
                    )
                return last

            if time.monotonic() >= deadline:
                self._log(
                    "GIFT SAVED FIELD SETTLE EXHAUSTED",
                    polls=polls,
                    zone=last.get("zone"),
                    grid=last.get("grid"),
                    world_primary=last.get("world_primary"),
                    checks=last.get("checks"),
                )
                return last

            if polls == 1:
                self._log(
                    "GIFT SAVED FIELD SETTLE",
                    polls=polls,
                    zone=last.get("zone"),
                    grid=last.get("grid"),
                    world_primary=last.get("world_primary"),
                    checks=last.get("checks"),
                )
            time.sleep(0.20)


    @staticmethod
    def _party_count(snap: dict) -> int:
        source = dict(snap.get("live_source") or {})
        return int(source.get("count", 0) or 0)

    @staticmethod
    def _baseline_identities(snap: dict) -> set[tuple[int, str, str]]:
        return {ident for mon in list(snap.get("parsed") or []) if (ident := parsed_identity(mon)) is not None}

    def _wait_for_gift(self, bridge: Bridge, baseline: set[tuple[int, str, str]], timeout_s=1.5):
        deadline = time.monotonic() + max(0.1, float(timeout_s))
        last = None
        while time.monotonic() < deadline:
            self._check_stop()
            try:
                snap = self._snapshot_party(bridge, allow_unmapped=True)
                last = snap
                if str(snap.get("source")) == "UNMAPPED_D25":
                    time.sleep(0.12)
                    continue
                rows = list(snap.get("parsed") or [])
                any_new = find_new_party_member(rows, baseline)
                if self.gift_profile.trigger_kind == "FOSSIL_BATCH_5":
                    if any_new is not None:
                        species = int(any_new.get("species") or 0)
                        if species not in FOSSIL_SPECIES:
                            raise GiftSafetyHold(
                                f"new party PK6 is not a Devon-revivable fossil species: "
                                f"species={species} slot={any_new.get('party_slot')}"
                            )
                        return any_new, snap
                else:
                    if any_new is not None and int(any_new.get("species") or 0) != int(self.gift_profile.species):
                        raise GiftSafetyHold(
                            f"new party PK6 species mismatch before target authority: expected {self.gift_profile.species}, "
                            f"got {any_new.get('species')} in slot {any_new.get('party_slot')}"
                        )
                    mon = find_new_gift(rows, baseline, self.gift_profile.species)
                    if mon is not None:
                        return mon, snap
            except GiftSafetyHold:
                raise
            except Exception as exc:
                self._log("GIFT PARTY POLL RETRY", error=f"{type(exc).__name__}: {exc}")
            time.sleep(0.12)
        return None, last

    POSTGAME_STARTER_SLOT = {
        "chikorita": 0, "cyndaquil": 1, "totodile": 2,
        "snivy": 0, "tepig": 1, "oshawott": 2,
        "turtwig": 0, "chimchar": 1, "piplup": 2,
    }
    POSTGAME_CHOOSER_SLOT = 3
    POSTGAME_MIDDLE_SLOT = 1
    POSTGAME_PROC_READY = 6
    POSTGAME_LOWER_SELECT = 2
    POSTGAME_LOWER_CONFIRM = 6
    POSTGAME_CHOOSER_DELAYS = (2.8, 0.45, 0.45, 0.55)
    POSTGAME_SLOT_DELAYS = (0.10, 0.20, 0.35)
    POSTGAME_CONFIRM_DELAYS = (0.20, 0.20, 0.25, 0.35)
    POSTGAME_STARTER_PULSE = dict(
        hold_ms=300, resume_settle_ms=220, packet_interval_ms=30, release_ms=120
    )

    @classmethod
    def _postgame_base_ready(cls, snap: dict) -> bool:
        return (
            snap["proc"]["vptr_matches"]
            and snap["proc"]["view_matches"]
            and snap["proc"]["completion_flag"] == 0
            and snap["proc"]["main_state"] == cls.POSTGAME_PROC_READY
            and snap["lower"]["vptr_matches"]
            and snap["lower"]["state"] == cls.POSTGAME_LOWER_SELECT
        )

    @classmethod
    def _postgame_chooser_ready(cls, snap: dict) -> bool:
        return cls._postgame_base_ready(snap) and snap["lower"]["selected_slot"] == cls.POSTGAME_CHOOSER_SLOT

    @classmethod
    def _postgame_middle_ready(cls, snap: dict) -> bool:
        return cls._postgame_base_ready(snap) and snap["lower"]["selected_slot"] == cls.POSTGAME_MIDDLE_SLOT

    @classmethod
    def _postgame_target_ready(cls, snap: dict, target_slot: int) -> bool:
        return cls._postgame_base_ready(snap) and snap["lower"]["selected_slot"] == int(target_slot)

    @classmethod
    def _postgame_confirm_ready(cls, snap: dict, target_slot: int) -> bool:
        ptr = int(snap["lower"]["confirm_pointer"], 16)
        return (
            snap["proc"]["vptr_matches"]
            and snap["proc"]["view_matches"]
            and snap["proc"]["completion_flag"] == 0
            and snap["proc"]["main_state"] == cls.POSTGAME_PROC_READY
            and snap["lower"]["vptr_matches"]
            and snap["lower"]["state"] == cls.POSTGAME_LOWER_CONFIRM
            and snap["lower"]["selected_slot"] == int(target_slot)
            and ptr != 0
        )

    def _trigger_postgame_birch_starter(
        self,
        bridge: Bridge,
        inputs: AcknowledgedInput,
        baseline: set[tuple[int, str, str]],
    ):
        target_slot = self.POSTGAME_STARTER_SLOT.get(self.profile_key)
        if target_slot is None:
            return {"success": False, "reason": f"unsupported postgame starter {self.profile_key}"}, None, None

        trace = []
        self._check_stop()
        ack = inputs.pulse(("DOWN",), **self.POSTGAME_STARTER_PULSE)
        trace.append({"stage": "house_down", "ack": ack})
        self._log("POSTGAME STARTER PRELUDE", stage="DOWN", target=self.gift_profile.name)

        for index in range(1, 6):
            self._check_stop()
            ack = inputs.pulse(("A",), **self.POSTGAME_STARTER_PULSE)
            trace.append({"stage": "prelude_a", "index": index, "ack": ack})
            self._log("POSTGAME STARTER PRELUDE", stage="A", press=index, total=5)

        inputs.pulse(("A",), **self.POSTGAME_STARTER_PULSE)
        ok, chooser = bounded_gate(
            bridge, self.POSTGAME_CHOOSER_DELAYS, self._postgame_chooser_ready,
            self._log, "POSTGAME_STARTER_CHOOSER_GATE",
        )
        if not ok:
            return {"success": False, "reason": "postgame starter chooser gate failed after DOWN + 5A prelude",
                    "stage": "chooser", "observations": chooser, "trace": trace}, None, None

        inputs.pulse(("A",), **self.POSTGAME_STARTER_PULSE)
        ok, middle = bounded_gate(
            bridge, self.POSTGAME_SLOT_DELAYS, self._postgame_middle_ready,
            self._log, "POSTGAME_STARTER_MIDDLE_GATE",
        )
        if not ok:
            return {"success": False, "reason": "postgame starter middle-slot gate failed",
                    "stage": "middle", "observations": middle, "trace": trace}, None, None

        if int(target_slot) == 0:
            inputs.pulse(("LEFT",), **self.POSTGAME_STARTER_PULSE)
            ok, target = bounded_gate(
                bridge, self.POSTGAME_SLOT_DELAYS,
                lambda s: self._postgame_target_ready(s, 0),
                self._log, "POSTGAME_STARTER_LEFT_GATE",
            )
        elif int(target_slot) == 2:
            inputs.pulse(("RIGHT",), **self.POSTGAME_STARTER_PULSE)
            ok, target = bounded_gate(
                bridge, self.POSTGAME_SLOT_DELAYS,
                lambda s: self._postgame_target_ready(s, 2),
                self._log, "POSTGAME_STARTER_RIGHT_GATE",
            )
        else:
            ok, target = True, middle

        if not ok:
            return {"success": False, "reason": f"postgame starter target slot {target_slot} gate failed",
                    "stage": "target", "observations": target, "trace": trace}, None, None

        inputs.pulse(("A",), **self.POSTGAME_STARTER_PULSE)
        ok, confirm = bounded_gate(
            bridge, self.POSTGAME_CONFIRM_DELAYS,
            lambda s: self._postgame_confirm_ready(s, target_slot),
            self._log, "POSTGAME_STARTER_CONFIRM_GATE",
        )
        if not ok:
            return {"success": False, "reason": "postgame starter confirmation gate failed",
                    "stage": "confirm", "observations": confirm, "trace": trace}, None, None

        confirm_ptr = int(confirm[-1]["lower"]["confirm_pointer"], 16)
        bridge.read(confirm_ptr, 4)

        inputs.pulse(("A",), **self.POSTGAME_STARTER_PULSE)
        mon, snap = self._wait_for_gift(bridge, baseline, timeout_s=6.0)
        if mon is None:
            return {"success": False, "reason": "postgame starter confirmed but target Party PK6 did not appear",
                    "stage": "party_pk6", "target_slot": target_slot, "trace": trace}, None, snap

        return {
            "success": True,
            "accepted_press": "starter_confirm",
            "target_slot": target_slot,
            "prelude": {"down": 1, "a": 5},
            "trace": trace,
        }, mon, snap

    def _trigger_gift(self, bridge: Bridge, inputs: AcknowledgedInput, baseline: set[tuple[int, str, str]]):
        presses = []
        for index in range(1, int(self.gift_profile.max_a_presses) + 1):
            ack = self._pulse_a(inputs)
            presses.append({"index": index, "ack": ack})
            mon, snap = self._wait_for_gift(bridge, baseline, timeout_s=1.15)
            if mon is not None:
                return {"success": True, "accepted_press": index, "presses": presses}, mon, snap
        return {"success": False, "presses": presses, "reason": "bounded A dialogue budget exhausted without new target party PK6"}, None, None

    def _payload(self, mon: dict, attempt: int) -> dict:
        species = int(mon.get("species") or 0)
        nature_id = int(mon.get("nature_id") or 0)
        evolution = predict_split_evolution(species, mon.get("ec"))
        return {
            **dict(mon),
            "hunt_type": "Gift",
            "gift_key": self.profile_key,
            "target": self.gift_profile.name,
            "location_name": self.gift_profile.location,
            "species": species,
            "species_name": SPECIES_NAMES.get(species, f"Species {species}"),
            "pokemon_pid": mon.get("pid"),
            "pid": mon.get("pid"),
            "nature": NATURE_NAMES[nature_id] if 0 <= nature_id < len(NATURE_NAMES) else f"Nature {nature_id}",
            "ability_id": pk6.get("ability_id"),
            "ability": ability_name(pk6.get("ability_id")),
            "attempt": int(attempt),
            "horde_size": 1,
            "shiny_action": "HOLD",
            "predicted_evolution": evolution.get("line") if evolution else None,
            "evolution_prediction": evolution,
            "hardware_validation": self._hardware_validation(),
        }

    def _reset_phase_extrema(self):
        for key in ("highest_sv", "lowest_sv", "highest_iv_sum", "lowest_iv_sum"):
            self.lifetime[key] = None

    def _load_last_seen(self):
        try:
            data = json.loads(self.last_seen_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []

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

    def _record_encounter(self, payload: dict):
        shiny = bool(payload.get("is_shiny"))
        sv = int(payload.get("shiny_xor") or 0)
        iv_sum = int(payload.get("iv_sum") or sum(int(v) for v in (payload.get("ivs") or {}).values()))
        now_mono = time.monotonic()
        duration_s = (max(0.0, now_mono - self.last_encounter_monotonic) if self.last_encounter_monotonic is not None
                      else (max(0.0, now_mono - self.started_monotonic) if self.started_monotonic else None))
        self.last_encounter_monotonic = now_mono
        self.session_seen += 1
        self.lifetime["lifetime_seen"] = int(self.lifetime.get("lifetime_seen", 0)) + 1
        self.lifetime["phase_seen"] = int(self.lifetime.get("phase_seen", 0)) + 1
        odds = resolve_shiny_odds(
            game=(self.game_profile or {}).get("name", "ORAS"),
            hunt_type="Gift",
            shiny_charm_present=(self.shiny_charm_state or {}).get("detected"),
            shiny_charm_applies=False,
        )
        self.lifetime["phase_log_miss"] = advance_phase_log_miss(
            self.lifetime.get("phase_log_miss", 0.0), odds.probability
        )
        self.lifetime["phase_cumulative_probability"] = cumulative_probability_from_log_miss(
            self.lifetime["phase_log_miss"]
        )
        payload["encounter_shiny_probability"] = odds.probability
        payload["phase_cumulative_probability"] = float(
            self.lifetime["phase_cumulative_probability"]
        )
        payload["shiny_odds_display"] = odds.display
        for key, value, fn in (("highest_sv", sv, max), ("lowest_sv", sv, min),
                               ("highest_iv_sum", iv_sum, max), ("lowest_iv_sum", iv_sum, min)):
            old = self.lifetime.get(key)
            self.lifetime[key] = value if old is None else fn(int(old), value)

        now = datetime.now().astimezone().isoformat(timespec="seconds")
        if shiny:
            self.session_shinies += 1
            phase = int(self.lifetime.get("phase_seen", 0))
            self.lifetime["lifetime_shinies"] = int(self.lifetime.get("lifetime_shinies", 0)) + 1
            self.lifetime["last_phase_seen"] = phase
            self.lifetime["last_phase_cumulative_probability"] = float(
                self.lifetime.get("phase_cumulative_probability", 0.0) or 0.0
            )
            self.lifetime["last_shiny"] = {
                "hunt_type": "Gift", "gift_key": self.profile_key, "target": self.gift_profile.name,
                "species": payload.get("species_name"), "species_id": payload.get("species"),
                "pid": payload.get("pid"), "ec": payload.get("ec"), "shiny_xor": sv,
                "ivs": dict(payload.get("ivs") or {}), "nature": payload.get("nature", "—"),
                "ability_id": payload.get("ability_id"),
                "ability": payload.get("ability", "—"),
                "gender": payload.get("gender", "—"),
                "location": self.gift_profile.location, "method": "Gift", "time": now,
            }
            increment_species_shiny_total(int(payload["species"]), payload.get("species_name"), found_time=now)
            self.lifetime["phase_seen"] = 0
            self.lifetime["phase_log_miss"] = 0.0
            self.lifetime["phase_cumulative_probability"] = 0.0
            self._reset_phase_extrema()
            self._append_recent_shiny(self.lifetime["last_shiny"])

        persisted = dict(payload)
        persisted.update({"time": now, "method": "Gift", "method_name": "Gift",
                          "game": (self.game_profile or {}).get("name"),
                          "location_name": self.gift_profile.location,
                          "duration_s": round(float(duration_s), 3) if duration_s is not None else None})
        history = self._load_last_seen()
        history.insert(0, persisted)
        self._atomic_json(self.last_seen_path, history[:7])
        try:
            with self.encounter_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(persisted, separators=(",", ":"), default=str) + "\n")
        except Exception as exc:
            self._log("GIFT ENCOUNTER LEDGER WRITE FAILED", error=f"{type(exc).__name__}: {exc}")
        self._save_stats()

    def _stats_payload(self):
        elapsed = max(0.0, time.monotonic() - self.started_monotonic) if self.started_monotonic else 0.0
        return {**dict(self.lifetime), "hunt_type": "Gift", "gift_key": self.profile_key,
                "target": self.gift_profile.name, "species": self.gift_profile.species,
                "location_name": self.gift_profile.location,
                "phase_index": int(self.lifetime.get("lifetime_shinies", 0)) + 1,
                "phase_seen": int(self.lifetime.get("phase_seen", 0)),
                "phase_log_miss": float(self.lifetime.get("phase_log_miss", 0.0) or 0.0),
                "phase_cumulative_probability": float(
                    self.lifetime.get("phase_cumulative_probability", 0.0) or 0.0
                ),
                "shiny_charm": dict(self.shiny_charm_state or {}),
                "shiny_charm_applies": False,
                "shiny_odds_display": resolve_shiny_odds(
                    game=(self.game_profile or {}).get("name", "ORAS"),
                    hunt_type="Gift", shiny_charm_present=(self.shiny_charm_state or {}).get("detected"),
                    shiny_charm_applies=False,
                ).display,
                "session_seen": self.session_seen,
                "resets": int(self.session_resets),
                "batches": int(self.session_batches),
                "average_time": (elapsed / self.session_seen) if self.session_seen else 0.0,
                "hardware_validation": "SHARED_ORAS_PARTY_PK6_GIFT_ENGINE"}

    def _save_stats(self):
        self._atomic_json(self.stats_path, self.lifetime)
        elapsed = max(0.0, time.monotonic() - self.started_monotonic) if self.started_monotonic else 0.0
        self._atomic_json(self.session_path, {
            "hunt_type": "Gift", "gift_key": self.profile_key, "target": self.gift_profile.name,
            "species": self.gift_profile.species, "location_name": self.gift_profile.location,
            "session_seen": self.session_seen, "session_shinies": self.session_shinies,
            "session_resets": self.session_resets, "session_batches": self.session_batches,
            "elapsed_seconds": elapsed, "saved_field_anchor": self.anchor.as_dict() if self.anchor else None,
            "last_trigger": self.last_trigger, "last_gift": self.last_gift, "last_reset": self.last_reset,
            "updated": datetime.now().astimezone().isoformat(timespec="seconds"),
        })
        self.stats.emit(self._stats_payload())

    def _record_fossil_batch_completion(self, batch_size: int):
        size = max(1, min(5, int(batch_size)))
        species = [str(x.get("species_name") or x.get("species") or "Unknown") for x in self.last_batch]
        self.session_batches += 1
        self.lifetime["lifetime_batches"] = int(self.lifetime.get("lifetime_batches", 0)) + 1
        self.lifetime["fossil_lifetime_batches"] = int(
            self.lifetime.get("fossil_lifetime_batches", 0)
        ) + 1
        self.lifetime["fossil_lifetime_revived"] = int(
            self.lifetime.get("fossil_lifetime_revived", 0)
        ) + size
        counts = self.lifetime.setdefault(
            "fossil_batch_size_counts", {str(i): 0 for i in range(1, 6)}
        )
        counts[str(size)] = int(counts.get(str(size), 0) or 0) + 1
        self.lifetime["fossil_last_batch_size"] = size
        self.lifetime["fossil_last_batch_species"] = species
        rec = {
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "hunt_type": "Gift",
            "gift_key": self.profile_key,
            "target": self.gift_profile.name,
            "batch_size": size,
            "species": species,
            "entries": list(self.last_batch),
            "result": "ALL_NON_SHINY_RESET",
        }
        try:
            with self.batch_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")
        except Exception as exc:
            self._log("FOSSIL BATCH LEDGER WRITE FAILED", error=f"{type(exc).__name__}: {exc}")

    def _support(self, status: str, reason: str):
        try:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            out = self.support_dir / f"Pokebot3DS-CFW_gift_{self.profile_key}_{stamp}_{status}.zip"
            summary = self.support_dir / f"gift_{self.profile_key}_{stamp}_summary.json"
            self._atomic_json(summary, {
                "status": status, "reason": reason, "profile": self.gift_profile.__dict__,
                "game_profile": self.game_profile, "saved_field_anchor": self.anchor.as_dict() if self.anchor else None,
                "last_party_before": self.last_party_before, "last_party_after": self.last_party_after,
                "last_trigger": self.last_trigger, "last_gift": self.last_gift, "last_batch": self.last_batch, "last_reset": self.last_reset,
            })
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in (self.log_path, self.stats_path, self.session_path, self.encounter_path, self.batch_path,
                          self.last_seen_path, self.profile_paths.recent_shinies_path, summary):
                    if p.exists():
                        zf.write(p, p.name)
            try:
                summary.unlink()
            except Exception:
                pass
            self.support_ready.emit(str(out))
        except Exception as exc:
            self._log("GIFT SUPPORT EXPORT FAILED", error=f"{type(exc).__name__}: {exc}")

    def _finish(self, status: str, reason: str, support=False):
        if not self._lifetime_time_committed and self.started_monotonic is not None:
            self.lifetime["lifetime_hunt_seconds"] = round(
                float(self.lifetime.get("lifetime_hunt_seconds", 0.0)) + max(0.0, time.monotonic() - self.started_monotonic), 3)
            self._lifetime_time_committed = True
        self._save_stats()
        self.status.emit(status, reason)
        self._log("GIFT SESSION FINISHED", status=status, reason=reason)
        if support and self.auto_support_zip:
            self._support(status, reason)
        self.session_finished.emit(status)

    @Slot()
    def run(self):
        self.started_monotonic = time.monotonic()
        bridge = None
        inputs = None
        try:
            p = self.gift_profile
            self.status.emit("STARTING", f"Gift {p.name} preflight")
            self._log("GIFT HUNT START", profile=p.__dict__)
            if p.shiny_locked:
                raise GiftSafetyHold(f"{p.name} is shiny-locked")
            if not p.automation_ready or p.trigger_kind not in {"A_GIFT_TO_PARTY", "FOSSIL_BATCH_5", "POSTGAME_BIRCH_STARTER"}:
                raise GiftSafetyHold(f"{p.name}: {p.trigger_kind} is listed but not automated yet")
            if self.bridge_port != 4952 or self.input_port != 4952:
                raise GiftSafetyHold("Gift hunts require unified Pokebot-Luma UDP 4952")

            bridge = Bridge(self.host, port=self.bridge_port, timeout=min(self.timeout, 1.5))
            inputs = AcknowledgedInput(self.host, port=self.input_port, timeout=min(self.timeout, 1.5))
            gi = bridge.game_info()
            gp = profile_from_game_info(gi)
            if not gp:
                raise GiftSafetyHold(f"Unsupported ORAS GAME_INFO: {gi}")
            self.game_profile = gp
            if gp["key"] not in p.games:
                raise GiftSafetyHold(f"{p.name} is not available in {gp['name']}")
            if gp["key"] == "omega_ruby" and not self.use_code_ips:
                raise GiftSafetyHold("Omega Ruby gift reset uses the shared proven code.ips reset route")
            ctrl = inputs.input_ping()
            inputs.release_all()
            self.shiny_charm_state = detect_oras_shiny_charm(bridge, game_key=gp["key"])
            self.connection.emit({"ram_ready": True, "input_ready": True, "controller_ready": True,
                                  "input_status": "Pokebot-Luma RAM + Input 4952: Ready", "game_info": gi,
                                  "game_profile": gp, "controller_info": ctrl, "shiny_charm": self.shiny_charm_state})

            self.status.emit("RESETTING", f"{p.name} bootstrap • resetting to saved gift position")
            ok, reset = run_reset_to_field_for_profile(bridge, inputs, self._log, gp, validate_any_loaded_field,
                                                       use_code_ips=self.use_code_ips)
            self.last_reset = dict(reset or {})
            if not ok:
                raise GiftSafetyHold(f"Gift bootstrap reset/load failed: {self.last_reset.get('status')}")
            self.anchor = read_saved_field_anchor(bridge)
            field = validate_saved_field_anchor(bridge, self.anchor)
            if not field.get("authority"):
                raise GiftSafetyHold(f"Gift saved-field anchor failed: {field}")
            self._log("GIFT SAVED FIELD ANCHOR", anchor=self.anchor.as_dict())
            self._save_stats()

            attempt = 0
            while True:
                self._check_stop()
                field = validate_saved_field_anchor(bridge, self.anchor)
                if not field.get("authority"):
                    raise GiftSafetyHold(f"Gift field moved/changed before trigger: {field}")
                before = self._wait_for_party_authority(bridge, timeout_s=10.0)
                self.last_party_before = {
                    "count": self._party_count(before), "source": before.get("source"),
                    "identities": list(before.get("identities") or []),
                }
                self.party.emit(list(before.get("payload") or []))
                before_count = self._party_count(before)
                if before_count < 1:
                    raise GiftSafetyHold("Gift hunt requires an existing lead Pokémon in party slot 1")

                if p.trigger_kind == "FOSSIL_BATCH_5":
                    if before_count != 1:
                        raise GiftSafetyHold(
                            f"Adaptive fossil batch requires exactly one lead and empty slots at batch start; "
                            f"party count={before_count}"
                        )
                    baseline = self._baseline_identities(before)
                    self.last_batch = []
                    current_count = before_count
                    batch_target = None
                    completed_nonshiny = 0

                    # Maximum five per reset because the party starts with one
                    # lead.  The actual target is discovered automatically:
                    # after 1-4 successful revivals, a conservative
                    # no-more-fossils event path ends the batch and triggers the
                    # reset.  No user setting is required.
                    for batch_index, expected_slot in enumerate(range(2, 7), start=1):
                        self._check_stop()
                        attempt += 1
                        self.status.emit(
                            "RUNNING",
                            f"Fossil revival {batch_index}/5 max • expecting party slot {expected_slot}",
                        )
                        if self._use_or_fossil_state_machine():
                            trigger, mon, after = self._trigger_or_fossil_state_machine(
                                bridge, inputs, baseline,
                                batch_index=batch_index, expected_slot=expected_slot,
                                allow_batch_exhausted=(completed_nonshiny >= 1),
                            )
                        else:
                            trigger, mon, after = self._trigger_gift(bridge, inputs, baseline)
                        self.last_trigger = trigger

                        if bool(trigger.get("batch_exhausted")):
                            if completed_nonshiny < 1:
                                raise GiftSafetyHold("Fossil batch started with no revivable fossils")
                            batch_target = completed_nonshiny
                            self._log(
                                "FOSSIL ADAPTIVE BATCH SIZE DISCOVERED",
                                batch_size=int(batch_target),
                                next_batch_index=int(batch_index),
                                reason=trigger.get("reason"),
                                batch=list(self.last_batch),
                            )
                            self.status.emit(
                                "RESETTING",
                                f"{batch_target}/{batch_target} available fossil"
                                f"{'s' if batch_target != 1 else ''} non-shiny • resetting batch",
                            )
                            break

                        if not trigger.get("success") or mon is None or after is None:
                            raise GiftSafetyHold(
                                trigger.get("reason")
                                or f"fossil revival {batch_index}/5 max did not produce target PK6"
                            )

                        after_count = self._party_count(after)
                        self.last_party_after = {
                            "count": after_count, "source": after.get("source"),
                            "identities": list(after.get("identities") or []),
                        }
                        self.party.emit(list(after.get("payload") or []))
                        if after_count != current_count + 1:
                            raise GiftSafetyHold(
                                f"fossil party count did not increase by exactly one at revival "
                                f"{batch_index}/5 max: before={current_count} after={after_count}"
                            )
                        gift_slot = int(mon.get("party_slot") or 0)
                        if gift_slot != expected_slot:
                            raise GiftSafetyHold(
                                f"fossil batch expected new PK6 in slot {expected_slot}, "
                                f"got slot {gift_slot or 'unknown'}"
                            )

                        payload = self._payload(mon, attempt)
                        payload["fossil_batch_index"] = batch_index
                        payload["fossil_batch_max"] = 5
                        payload["fossil_batch_size"] = batch_target
                        if not (payload.get("valid") and payload.get("checksum_valid")):
                            raise GiftSafetyHold("new fossil party PK6 failed validity/checksum authority")
                        if int(payload.get("species") or 0) not in FOSSIL_SPECIES:
                            raise GiftSafetyHold(
                                f"revived party PK6 is not a valid fossil species: {payload.get('species')}"
                            )
                        payload["target"] = f"Fossil Batch — {payload.get('species_name')}"
                        self.last_gift = dict(payload)
                        self.last_batch.append({
                            "batch_index": batch_index,
                            "party_slot": gift_slot,
                            "pid": payload.get("pid"),
                            "ec": payload.get("ec"),
                            "species": payload.get("species"),
                            "species_name": payload.get("species_name"),
                            "shiny": bool(payload.get("is_shiny")),
                            "shiny_xor": payload.get("shiny_xor"),
                        })
                        self._log(
                            "FOSSIL BATCH PK6",
                            batch_index=batch_index, batch_max=5,
                            party_slot=gift_slot,
                            species=payload.get("species_name"),
                            pid=payload.get("pid"), ec=payload.get("ec"),
                            shiny=payload.get("is_shiny"), xor=payload.get("shiny_xor"),
                            accepted_press=trigger.get("accepted_press"),
                        )
                        self._record_encounter(payload)
                        self.encounter.emit(dict(payload))
                        self.last_seen_entry.emit(dict(payload))

                        if payload.get("is_shiny"):
                            self.status.emit(
                                "SHINY HOLD",
                                f"{payload.get('species_name')} shiny confirmed in party slot "
                                f"{gift_slot} at revival {batch_index}",
                            )
                            self._finish(
                                "SHINY_HOLD",
                                f"Shiny {payload.get('species_name')} confirmed at fossil revival "
                                f"{batch_index}; no further fossil input sent",
                                support=False,
                            )
                            return

                        self.status.emit(
                            "RUNNING",
                            f"Fossil revival {batch_index} non-shiny • declining nickname",
                        )
                        if self._use_or_fossil_state_machine():
                            owner_hex = str(trigger.get("owner") or "")
                            try:
                                owner = int(owner_hex, 16)
                            except Exception as exc:
                                raise GiftSafetyHold(
                                    f"Omega Ruby fossil trigger did not retain a valid script owner: "
                                    f"{owner_hex!r} ({exc})"
                                )
                            self._finish_or_fossil_nonshiny(
                                bridge, inputs, owner=owner,
                                batch_index=batch_index, party_slot=gift_slot,
                            )
                        else:
                            self._decline_fossil_nickname(
                                bridge, inputs, batch_index=batch_index, party_slot=gift_slot
                            )

                        baseline = self._baseline_identities(after)
                        current_count = after_count
                        completed_nonshiny += 1

                        if batch_index == 5:
                            batch_target = 5
                            break

                    if batch_target is None:
                        batch_target = completed_nonshiny

                    if batch_target < 1 or batch_target > 5:
                        raise GiftSafetyHold(
                            f"adaptive fossil batch resolved invalid batch size {batch_target}"
                        )
                    if current_count != 1 + batch_target:
                        raise GiftSafetyHold(
                            f"adaptive fossil batch party count mismatch: target={batch_target} "
                            f"party_count={current_count}"
                        )
                    if completed_nonshiny != batch_target:
                        raise GiftSafetyHold(
                            f"adaptive fossil batch completion mismatch: target={batch_target} "
                            f"non_shiny={completed_nonshiny}"
                        )

                    self._check_stop()
                    self.status.emit(
                        "RESETTING",
                        f"{batch_target}/{batch_target} fossil revival"
                        f"{'s' if batch_target != 1 else ''} non-shiny • resetting entire batch",
                    )
                    self._log(
                        "FOSSIL BATCH RESET",
                        batch_size=int(batch_target), batch_max=5,
                        non_shiny=int(completed_nonshiny), batch=self.last_batch,
                    )
                    validator = lambda b: self._wait_for_saved_field_authority(b, self.anchor, timeout_s=2.0)
                    ok, reset = run_reset_to_field_for_profile(
                        bridge, inputs, self._log, gp, validator, use_code_ips=self.use_code_ips
                    )
                    self.last_reset = dict(reset or {})
                    if not ok:
                        raise GiftSafetyHold(
                            f"Fossil batch reset/field return failed: {self.last_reset.get('status')}"
                        )
                    self.session_resets += 1
                    self.lifetime["lifetime_resets"] = int(
                        self.lifetime.get("lifetime_resets", 0)
                    ) + 1
                    self._record_fossil_batch_completion(batch_target)
                    self._save_stats()
                    continue

                if before_count >= 6:
                    raise GiftSafetyHold("Gift hunt requires at least one free party slot before accepting the gift")
                baseline = self._baseline_identities(before)
                attempt += 1
                if p.trigger_kind == "POSTGAME_BIRCH_STARTER":
                    self.status.emit(
                        "RUNNING",
                        f"{p.name} attempt {attempt} • house DOWN + 5A → starter chooser",
                    )
                    trigger, mon, after = self._trigger_postgame_birch_starter(
                        bridge, inputs, baseline
                    )
                else:
                    self.status.emit("RUNNING", f"{p.name} attempt {attempt} • accepting gift")
                    trigger, mon, after = self._trigger_gift(bridge, inputs, baseline)
                self.last_trigger = trigger
                if not trigger.get("success") or mon is None or after is None:
                    raise GiftSafetyHold(trigger.get("reason") or "gift target PK6 did not appear")
                after_count = self._party_count(after)
                self.last_party_after = {
                    "count": after_count, "source": after.get("source"),
                    "identities": list(after.get("identities") or []),
                }
                self.party.emit(list(after.get("payload") or []))
                if after_count != before_count + 1:
                    raise GiftSafetyHold(
                        f"gift party count did not increase by exactly one: before={before_count} after={after_count}"
                    )
                gift_slot = int(mon.get("party_slot") or 0)
                if gift_slot not in range(2, 7):
                    raise GiftSafetyHold(f"gift PK6 authority must be party slot 2-6; got slot {gift_slot or 'unknown'}")
                payload = self._payload(mon, attempt)
                if not (payload.get("valid") and payload.get("checksum_valid")):
                    raise GiftSafetyHold("new gift party PK6 failed validity/checksum authority")
                if int(payload.get("species") or 0) != int(p.species):
                    raise GiftSafetyHold(f"gift species mismatch: expected {p.species}, got {payload.get('species')}")
                if p.expected_level is not None and int(payload.get("level") or p.expected_level) != int(p.expected_level):
                    level = int(payload.get("level") or 0)
                    if level and level != int(p.expected_level):
                        raise GiftSafetyHold(f"gift level mismatch: expected {p.expected_level}, got {level}")
                self.last_gift = dict(payload)
                self._log("GIFT PK6", attempt=attempt, species=payload.get("species_name"), pid=payload.get("pid"),
                          ec=payload.get("ec"), shiny=payload.get("is_shiny"), xor=payload.get("shiny_xor"),
                          party_slot=payload.get("party_slot"), accepted_press=trigger.get("accepted_press"))
                self._record_encounter(payload)
                self.encounter.emit(dict(payload))
                self.last_seen_entry.emit(dict(payload))

                if payload.get("is_shiny"):
                    self.status.emit("SHINY HOLD", f"{p.name} shiny confirmed from live party PK6")
                    self._finish("SHINY_HOLD", f"Shiny {p.name} confirmed; no further gift input sent", support=False)
                    return

                self._check_stop()
                self.status.emit("RESETTING", f"Non-shiny {p.name} • resetting to saved gift position")
                validator = lambda b: self._wait_for_saved_field_authority(b, self.anchor, timeout_s=2.0)
                ok, reset = run_reset_to_field_for_profile(bridge, inputs, self._log, gp, validator,
                                                           use_code_ips=self.use_code_ips)
                self.last_reset = dict(reset or {})
                if not ok:
                    raise GiftSafetyHold(f"Gift reset/field return failed: {self.last_reset.get('status')}")
                self.session_resets += 1
                self.lifetime["lifetime_resets"] = int(
                    self.lifetime.get("lifetime_resets", 0)
                ) + 1
                self._save_stats()

        except UserStop as exc:
            try:
                if inputs:
                    inputs.release_all()
            except Exception:
                pass
            self._finish("STOPPED", str(exc), support=False)
        except Exception as exc:
            try:
                if inputs:
                    inputs.release_all()
            except Exception:
                pass
            reason = f"{type(exc).__name__}: {exc}"
            self._log("GIFT SAFETY HOLD", reason=reason, traceback=traceback.format_exc())
            self._finish("SAFETY_HOLD", reason, support=True)
        finally:
            try:
                if inputs:
                    inputs.close()
            except Exception:
                pass
            try:
                if bridge:
                    bridge.close()
            except Exception:
                pass
