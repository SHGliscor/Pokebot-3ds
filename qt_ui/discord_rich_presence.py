from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from pokebot.common.shiny_odds import format_cumulative_percent

try:
    from pypresence import Presence  # type: ignore
except Exception:  # Keep the dashboard runnable before optional dependency install.
    Presence = None


class DiscordRichPresenceManager(QObject):
    """Personal-account Discord Rich Presence via the local Discord desktop client.

    This service is telemetry only. It never calls into hunt workers, never reads or
    writes 3DS RAM, and never authorises controller input/reset behaviour.
    """

    connection_changed = Signal(dict)
    event_emitted = Signal(dict)

    def __init__(self, profile_root: Path, parent=None):
        super().__init__(parent)
        self.profile_root = Path(profile_root)
        self.settings = {}
        self._connected = False
        self._connecting = False
        self._worker = None
        self._commands = queue.Queue()
        self._stop = threading.Event()
        self._rpc = None
        self._last_context = {}
        self._last_stats = {}
        self._last_status = "IDLE"
        self._hunt_active = False
        self._hunt_start_epoch = None
        self._last_payload = None
        self._last_sent_mono = 0.0
        self._minimum_update_s = 15.0

    @property
    def available(self):
        return Presence is not None

    @property
    def connected(self):
        return bool(self._connected)

    def apply_settings(self, settings):
        self.settings = dict(settings or {})

    def _connection(self, connected, detail, *, connecting=False):
        self.connection_changed.emit({
            "connected": bool(connected),
            "connecting": bool(connecting),
            "detail": str(detail),
            "dependency_available": self.available,
        })

    def _event(self, message, ok=True):
        self.event_emitted.emit({
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "type": "RICH PRESENCE",
            "message": str(message),
            "ok": bool(ok),
        })

    def _ensure_worker(self):
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="PokebotDiscordRPC",
            daemon=True,
        )
        self._worker.start()

    def connect_presence(self):
        self.apply_settings(self.settings)
        if self._connected or self._connecting:
            return
        if not self.settings.get("discord_rpc_enabled", False):
            self._connection(False, "Personal Rich Presence is disabled")
            return
        if Presence is None:
            self._connection(False, "pypresence is not installed. Install requirements.txt.")
            self._event("pypresence dependency missing", False)
            return
        app_id = str(self.settings.get("discord_rpc_application_id") or "").strip()
        if not app_id or not app_id.isdigit():
            self._connection(False, "Discord Application ID is missing or invalid")
            return
        self._connecting = True
        self._connection(False, "Connecting to local Discord desktop client…", connecting=True)
        self._ensure_worker()
        self._commands.put(("connect", app_id))

    def disconnect_presence(self):
        self._connecting = False
        self._commands.put(("disconnect", None))

    def shutdown(self):
        try:
            if self._worker is not None and self._worker.is_alive():
                self._commands.put(("shutdown", None))
                self._worker.join(timeout=1.5)
            else:
                self._close_rpc()
            self._stop.set()
        except Exception:
            pass

    def hunt_started(self, context):
        self._last_context = dict(context or {})
        self._last_stats = {}
        self._last_status = "STARTING"
        self._hunt_active = True
        self._hunt_start_epoch = int(time.time())
        self.update_presence(force=True)

    def hunt_finished(self, status, context=None):
        if context:
            self._last_context.update(context)
        self._last_status = str(status or "IDLE")
        self._hunt_active = False
        self.update_presence(force=True)

    def status_update(self, status, detail=None, context=None):
        if context:
            self._last_context.update(context)
        self._last_status = str(status or "IDLE")
        # Session finish will also call hunt_finished. Mark obvious terminal states
        # immediately so Discord does not keep an elapsed timer running on a HOLD.
        upper = self._last_status.upper()
        terminal = "SHINY" in upper or "HOLD" in upper
        if terminal:
            self._hunt_active = False
            self.update_presence(force=True)
        elif not self._hunt_active:
            # While idle, a newly detected game profile should immediately swap
            # the large artwork from generic Pokebot to the game/version image.
            self.update_presence(force=True)

    def stats_update(self, stats, context=None):
        self._last_stats = dict(stats or {})
        if context:
            self._last_context.update(context)
        self.update_presence()

    def encounter_update(self, encounter, context=None):
        if context:
            self._last_context.update(context)
        encounter = dict(encounter or {})
        # Random starter hunts report the actual current starter in each encounter.
        # Use it as display text only; the large image remains the GAME/version.
        if encounter.get("species_name"):
            self._last_context["encounter_species_name"] = encounter.get("species_name")
        if bool(encounter.get("is_shiny")):
            self._last_context["last_shiny_species"] = encounter.get("species_name")
        self.update_presence()

    def refresh(self):
        self.update_presence()

    def send_test_presence(self):
        if not self._connected:
            self._event("Test presence not sent: personal Rich Presence is not connected", False)
            return
        payload = self._build_payload(test=True)
        self._commands.put(("update_force", payload))
        self._event("Test Rich Presence queued", True)

    def update_presence(self, force=False):
        if not self._connected:
            return
        payload = self._build_payload()
        if not force and payload == self._last_payload:
            return
        self._last_payload = dict(payload)
        self._commands.put(("update_force" if force else "update", payload))

    @staticmethod
    def _game_asset(game):
        name = str(game or "").strip().casefold()
        if "alpha sapphire" in name:
            return "alpha_sapphire", "Pokémon Alpha Sapphire"
        if "omega ruby" in name:
            return "omega_ruby", "Pokémon Omega Ruby"
        return "pokebot3ds_cfw", str(game or "Pokebot3DS-CFW")

    @staticmethod
    def _rate(stats):
        try:
            rate = float(stats.get("rate"))
            if rate > 0:
                return rate
        except Exception:
            pass
        try:
            average = float(stats.get("average_time"))
            if average > 0:
                return 3600.0 / average
        except Exception:
            pass
        return 0.0

    def _build_payload(self, test=False):
        ctx = dict(self._last_context)
        stats = dict(self._last_stats)
        game = ctx.get("game") or stats.get("game") or "ORAS"
        large_image, large_text = self._game_asset(game)

        target = (
            ctx.get("target")
            or stats.get("target")
            or stats.get("target_species_name")
            or stats.get("starter")
            or ctx.get("encounter_species_name")
            or "Current Hunt"
        )
        if str(stats.get("hunt_mode") or "").casefold() == "random":
            target = "Random Starters"

        location = ctx.get("location_name") or stats.get("location_name")
        method = ctx.get("method") or stats.get("method_name") or stats.get("hunt_type")
        if not location and str(method or "").casefold() in ("starter", "starters"):
            game_cf = str(game or "").casefold()
            location = "Aquacorde Town" if ("pokémon x" in game_cf or "pokemon x" in game_cf or "pokémon y" in game_cf or "pokemon y" in game_cf) else "Route 101"
        location = location or ("Kalos" if "pokémon x" in str(game).casefold() or "pokémon y" in str(game).casefold() else "ORAS")

        try:
            seen = int(stats.get("phase_seen", stats.get("session_seen", 0)) or 0)
        except Exception:
            seen = 0
        try:
            shinies = int(stats.get("lifetime_shinies", 0) or 0)
        except Exception:
            shinies = 0
        rate = self._rate(stats)

        status_u = str(self._last_status or "").upper()
        if test:
            details = "Rich Presence test | Pokebot3DS-CFW"
            state = f"{location} | {game}"
        elif "SHINY" in status_u:
            shiny_target = ctx.get("last_shiny_species") or target
            details = f"✨ SHINY FOUND! | {shiny_target} | {seen:,} seen"
            state = f"{location} | {game}"
        elif "HOLD" in status_u:
            details = f"⚠ Safety HOLD | {target} | {seen:,} seen"
            state = f"{location} | {game}"
        elif not self._hunt_active and status_u in ("IDLE", "STOPPED", ""):
            details = "Ready for a hunt"
            state = str(game)
        else:
            parts = [str(target), f"{seen:,} ({shinies:,}✨)"]
            cumulative = stats.get("phase_cumulative_probability")
            if cumulative is not None:
                try:
                    parts.append(format_cumulative_percent(float(cumulative)))
                except Exception:
                    pass
            if rate > 0:
                parts.append(f"{rate:.0f}/h")
            details = " | ".join(parts)
            state = f"{location} | {game}"

        payload = {
            "details": details[:128],
            "state": state[:128],
            "large_image": large_image,
            "large_text": large_text[:128],
            "small_image": "pokebot3ds_cfw",
            "small_text": "Pokebot3DS-CFW",
        }
        if self._hunt_active and self._hunt_start_epoch:
            payload["start"] = int(self._hunt_start_epoch)
        return payload

    def _close_rpc(self):
        rpc = self._rpc
        self._rpc = None
        if rpc is not None:
            try:
                rpc.clear()
            except Exception:
                pass
            try:
                rpc.close()
            except Exception:
                pass
        was_connected = self._connected or self._connecting
        self._connected = False
        self._connecting = False
        self._last_payload = None
        if was_connected:
            self._connection(False, "Disconnected")

    def _send_payload(self, payload, force=False):
        if self._rpc is None or not self._connected:
            return
        now = time.monotonic()
        wait = self._minimum_update_s - (now - self._last_sent_mono)
        if not force and wait > 0:
            return
        try:
            self._rpc.update(**dict(payload or {}))
            self._last_sent_mono = time.monotonic()
        except Exception as exc:
            self._event(f"Presence update failed: {type(exc).__name__}: {exc}", False)
            # Do not throw into UI/hunt code. Treat a dead IPC connection as offline.
            self._close_rpc()

    def _worker_loop(self):
        while True:
            try:
                command, payload = self._commands.get(timeout=0.5)
            except queue.Empty:
                continue

            if command == "shutdown":
                self._close_rpc()
                return
            if command == "disconnect":
                self._close_rpc()
                self._event("Personal Rich Presence disconnected", True)
                continue
            if command == "connect":
                self._close_rpc()
                try:
                    rpc = Presence(str(payload))
                    rpc.connect()
                    self._rpc = rpc
                    self._connected = True
                    self._connecting = False
                    self._last_sent_mono = 0.0
                    self._connection(True, "Connected to your Discord desktop account")
                    self._event("Personal Rich Presence connected", True)
                    self._send_payload(self._build_payload(), force=True)
                except Exception as exc:
                    self._rpc = None
                    self._connected = False
                    self._connecting = False
                    self._connection(False, f"{type(exc).__name__}: {exc}")
                    self._event(f"Rich Presence connection failed: {type(exc).__name__}: {exc}", False)
                continue
            if command in ("update", "update_force"):
                self._send_payload(payload, force=(command == "update_force"))
