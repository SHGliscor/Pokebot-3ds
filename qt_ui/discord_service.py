from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

from pokebot.common.framebuffer import capture_top_screen
from pokebot.common.shiny_odds import format_cumulative_percent

try:
    import discord  # type: ignore
except Exception:  # Keep the dashboard runnable before optional dependency install.
    discord = None


class DiscordManager(QObject):
    """Non-authoritative Discord telemetry/notification bridge.

    Discord is deliberately isolated from hunt authority. All methods are best-effort:
    a Discord failure is surfaced to the Discord tab but never raises into the hunt path.
    """

    connection_changed = Signal(dict)
    event_emitted = Signal(dict)

    def __init__(self, profile_root: Path, parent=None):
        super().__init__(parent)
        self.profile_root = Path(profile_root)
        self.history_path = self.profile_root / "history" / "discord_events.json"
        self.settings = {}
        self._thread = None
        self._loop = None
        self._client = None
        self._connected = False
        self._connecting = False
        self._channel_id = None
        self._last_presence = None
        self._last_stats = {}
        self._last_context = {}
        self._last_encounter = {}
        self._last_phase_seen = {}
        self._sent_milestones = {}
        self._last_summary_mono = time.monotonic()
        self._shiny_dedup = set()
        self._history_lock = threading.Lock()
        self._framebuffer_lock = threading.Lock()
        self._framebuffer_threads = set()
        self._intentional_disconnect = False

    @property
    def available(self):
        return discord is not None

    @property
    def connected(self):
        return bool(self._connected)

    def apply_settings(self, settings):
        self.settings = dict(settings or {})
        try:
            self._channel_id = int(str(self.settings.get("discord_channel_id") or "").strip())
        except Exception:
            self._channel_id = None

    def _record_event(self, kind, message, ok=True):
        item = {
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "type": str(kind),
            "message": str(message),
            "ok": bool(ok),
        }
        try:
            with self._history_lock:
                self.history_path.parent.mkdir(parents=True, exist_ok=True)
                data = []
                if self.history_path.exists():
                    try:
                        loaded = json.loads(self.history_path.read_text(encoding="utf-8"))
                        if isinstance(loaded, list):
                            data = loaded
                    except Exception:
                        data = []
                data.append(item)
                data = data[-100:]
                tmp = self.history_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
                tmp.replace(self.history_path)
        except Exception:
            pass
        self.event_emitted.emit(item)

    def recent_events(self):
        try:
            data = json.loads(self.history_path.read_text(encoding="utf-8"))
            return data[-40:] if isinstance(data, list) else []
        except Exception:
            return []

    def connect_bot(self):
        if self._connected or self._connecting:
            return
        if self._thread is not None and self._thread.is_alive():
            self._connection(False, "Previous Discord connection is still closing")
            return
        self.apply_settings(self.settings)
        token = str(self.settings.get("discord_bot_token") or "").strip()
        if not self.settings.get("discord_enabled", False):
            self._connection(False, "Discord integration is disabled")
            return
        if discord is None:
            self._connection(False, "discord.py is not installed. Install requirements.txt.")
            self._record_event("CONNECTION", "discord.py dependency missing", False)
            return
        if not token:
            self._connection(False, "Bot token is empty")
            return
        if not self._channel_id:
            self._connection(False, "Channel ID is missing or invalid")
            return

        self._intentional_disconnect = False
        self._connecting = True
        self._connection(False, "Connecting…", connecting=True)
        self._thread = threading.Thread(
            target=self._run_client_thread,
            args=(token,),
            name="PokebotDiscord",
            daemon=True,
        )
        self._thread.start()

    def _run_client_thread(self, token):
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)

        intents = discord.Intents.none()
        intents.guilds = True
        client = discord.Client(intents=intents)
        self._client = client

        @client.event
        async def on_connect():
            # A TCP/WebSocket gateway connection is not yet a fully usable
            # Discord session. Keep the UI in CONNECTING until READY or RESUMED.
            if self._intentional_disconnect:
                return
            self._connecting = True
            self._connection(
                False,
                "Discord gateway connected — synchronizing session…",
                connecting=True,
            )

        @client.event
        async def on_ready():
            self._connecting = False
            self._connected = True
            user = str(client.user) if client.user else "Discord bot"
            self._connection(True, f"Connected as {user}", user=user)
            self._record_event("CONNECTION", f"Connected as {user}", True)
            await self._apply_presence_async(force=True)

        @client.event
        async def on_resumed():
            # discord.py dispatches RESUMED (not READY) when a transient gateway
            # interruption successfully resumes the existing session.  The old
            # manager cleared _connected in on_disconnect() but never restored it
            # here, leaving Pokebot permanently OFFLINE internally after a healthy
            # Discord resume.
            if self._intentional_disconnect:
                return
            self._connecting = False
            self._connected = True
            user = str(client.user) if client.user else "Discord bot"
            self._connection(True, f"Gateway session resumed as {user}", user=user)
            self._record_event("CONNECTION", f"Gateway session resumed as {user}", True)
            await self._apply_presence_async(force=True)

        @client.event
        async def on_disconnect():
            if self._intentional_disconnect:
                return
            # on_disconnect is expected to be transient when client.start() is
            # running with reconnect=True.  Do not present it as a terminal OFFLINE
            # state; discord.py will either READY a replacement session or RESUME
            # this one.  Delivery is paused while _connected is false.
            self._connected = False
            self._connecting = True
            self._connection(
                False,
                "Discord gateway interrupted — reconnecting automatically…",
                connecting=True,
            )
            self._record_event(
                "CONNECTION",
                "Gateway interrupted; automatic reconnect/resume in progress",
                False,
            )

        try:
            loop.run_until_complete(client.start(token, reconnect=True))
        except Exception as exc:
            self._connecting = False
            self._connected = False
            if not self._intentional_disconnect:
                self._connection(False, f"{type(exc).__name__}: {exc}")
                self._record_event(
                    "CONNECTION",
                    f"Connection failed: {type(exc).__name__}: {exc}",
                    False,
                )
        finally:
            try:
                if not client.is_closed():
                    loop.run_until_complete(client.close())
            except Exception as exc:
                # Teardown exceptions are not connection failures when the user or
                # application intentionally requested disconnect/shutdown.
                if not self._intentional_disconnect:
                    self._record_event(
                        "CONNECTION",
                        f"Discord shutdown warning: {type(exc).__name__}: {exc}",
                        False,
                    )
            # discord.py can queue event-handler tasks while closing the
            # gateway. Drain and cancel them before closing the private loop;
            # otherwise Python reports Client._run_event as never awaited
            # during application shutdown.
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception:
                pass
            self._connected = False
            self._connecting = False
            self._client = None
            self._loop = None
            try:
                loop.close()
            except Exception:
                pass

    def _connection(self, connected, detail, *, connecting=False, user=None):
        self.connection_changed.emit({
            "connected": bool(connected),
            "connecting": bool(connecting),
            "detail": str(detail),
            "user": user,
            "dependency_available": self.available,
            "channel_id": self._channel_id,
        })

    def disconnect_bot(self):
        self._intentional_disconnect = True
        self._connecting = False
        client, loop = self._client, self._loop
        if client is not None and loop is not None and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(client.close(), loop)
            except Exception:
                pass
        self._connected = False
        self._connection(False, "Disconnected")
        self._record_event("CONNECTION", "Disconnected by user", True)

    def shutdown(self):
        self._intentional_disconnect = True
        self._connecting = False
        client, loop, thread = self._client, self._loop, self._thread
        if client is not None and loop is not None and loop.is_running():
            coro = client.close()
            submitted = False
            try:
                future = asyncio.run_coroutine_threadsafe(coro, loop)
                submitted = True
                future.result(timeout=1.5)
            except Exception:
                if not submitted:
                    try:
                        coro.close()
                    except Exception:
                        pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.5)
        self._connected = False
        self._connection(False, "Disconnected")
        self._record_event("CONNECTION", "Disconnected by application shutdown", True)

    def _submit(self, coro, kind="DISCORD"):
        loop = self._loop
        if not self._connected or loop is None or not loop.is_running():
            try:
                coro.close()
            except Exception:
                pass
            return False
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            def done(fut):
                try:
                    fut.result()
                except Exception as exc:
                    self._record_event(kind, f"Delivery failed: {type(exc).__name__}: {exc}", False)
            future.add_done_callback(done)
            return True
        except Exception as exc:
            try:
                coro.close()
            except Exception:
                pass
            self._record_event(kind, f"Queue failed: {type(exc).__name__}: {exc}", False)
            return False

    async def _get_channel_async(self):
        if not self._channel_id:
            raise RuntimeError("Discord channel ID is not configured")
        channel = self._client.get_channel(self._channel_id)
        if channel is None:
            channel = await self._client.fetch_channel(self._channel_id)
        return channel

    async def _send_embed_async(self, embed_dict, image_path=None):
        channel = await self._get_channel_async()
        embed = discord.Embed.from_dict(embed_dict)
        if image_path:
            path = Path(image_path)
            if path.exists() and path.is_file():
                file = discord.File(str(path), filename=path.name)
                embed.set_image(url=f"attachment://{path.name}")
                await channel.send(embed=embed, file=file)
                return
        await channel.send(embed=embed)

    @staticmethod
    def _field(name, value, inline=True):
        return {"name": str(name), "value": str(value or "—"), "inline": bool(inline)}

    def _framebuffer_settings(self):
        host = str(self.settings.get("three_ds_ip") or "").strip()
        try:
            port = int(self.settings.get("ram_bridge_port", 4952))
        except Exception:
            port = 4952
        try:
            timeout = float(self.settings.get("bridge_timeout_s", 1.0))
        except Exception:
            timeout = 1.0
        return host, port, min(max(timeout, 0.5), 1.25)

    def _capture_path(self, prefix):
        folder = self.profile_root / "history" / "screenshots"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        return folder / f"{prefix}_{stamp}.png"

    def _discord_presentation_frame(self, source_path):
        """Create a Discord-only crop while preserving raw framebuffer evidence.

        The authoritative/raw 400x240 PNG remains untouched. This derived copy:
        - trims a uniform near-black bottom band when present;
        - removes a small 24px side margin and 6px top margin;
        - never feeds back into shiny detection or hunt control.
        """
        try:
            source = Path(source_path)
            image = QImage(str(source))
            if image.isNull():
                return source

            width = int(image.width())
            height = int(image.height())
            if width < 120 or height < 100:
                return source

            # Detect only a truly near-black bottom letterbox. Sample pixels
            # across each row; stop as soon as ordinary game content appears.
            def _row_is_black(y):
                step = max(1, width // 80)
                total = 0
                dark = 0
                for x in range(0, width, step):
                    c = image.pixelColor(x, y)
                    total += 1
                    if c.red() <= 14 and c.green() <= 14 and c.blue() <= 14:
                        dark += 1
                return total > 0 and (dark / total) >= 0.94

            bottom = height
            min_bottom = int(height * 0.68)
            y = height - 1
            while y >= min_bottom and _row_is_black(y):
                y -= 1
            if y < height - 1:
                bottom = min(height, y + 3)

            left = 24 if width >= 320 else 0
            right = width - 24 if width >= 320 else width
            top = 6 if height >= 180 else 0

            crop_w = max(1, right - left)
            crop_h = max(1, bottom - top)
            if crop_w < 100 or crop_h < 80:
                return source

            cropped = image.copy(left, top, crop_w, crop_h)
            out = source.with_name(source.stem + "_discord.png")
            if cropped.save(str(out), "PNG"):
                return out
        except Exception:
            pass
        return Path(source_path)

    def _queue_embed_with_framebuffer(
        self,
        embed,
        *,
        kind,
        prefix,
        existing_path=None,
        success_message=None,
    ):
        """Capture in a background thread, then queue Discord delivery.

        Screenshot failure is telemetry-only: the embed is still sent without an
        image. This function never feeds back into hunt authority.
        """
        if not self._connected:
            return False

        if existing_path:
            path = Path(existing_path)
            if path.exists() and path.is_file():
                discord_path = self._discord_presentation_frame(path)
                if self._submit(self._send_embed_async(embed, discord_path), kind):
                    self._record_event(
                        kind,
                        success_message
                        or f"{kind} queued with cropped 2DS/3DS screenshot "
                           f"(raw evidence preserved: {path.name})",
                        True,
                    )
                    return True

        def worker():
            image_path = None
            capture_detail = None
            try:
                host, port, timeout = self._framebuffer_settings()
                if not host:
                    raise RuntimeError("3DS IP is not configured")
                out = self._capture_path(prefix)
                with self._framebuffer_lock:
                    meta = capture_top_screen(
                        host,
                        out,
                        port=port,
                        timeout=timeout,
                    )
                raw_image_path = Path(meta["path"])
                image_path = str(self._discord_presentation_frame(raw_image_path))
                capture_detail = (
                    f"{meta['width']}x{meta['height']} raw framebuffer preserved; "
                    "Discord presentation crop attached"
                )
                fields = list(embed.get("fields") or [])
                fields.append(
                    self._field(
                        "2DS / 3DS Screenshot",
                        f"{meta['width']}×{meta['height']} live top screen",
                        inline=False,
                    )
                )
                embed["fields"] = fields
            except Exception as exc:
                capture_detail = (
                    "framebuffer screenshot unavailable: "
                    f"{type(exc).__name__}: {exc}"
                )

            if self._submit(self._send_embed_async(embed, image_path), kind):
                message = success_message or f"{kind} queued"
                if capture_detail:
                    message = f"{message} • {capture_detail}"
                self._record_event(kind, message, image_path is not None)

            self._framebuffer_threads.discard(threading.current_thread())

        thread = threading.Thread(
            target=worker,
            name=f"PokebotFramebuffer-{kind}",
            daemon=True,
        )
        self._framebuffer_threads.add(thread)
        thread.start()
        return True

    def send_test(self):
        if not self._connected:
            self._record_event("TEST", "Test message not sent: bot is not connected", False)
            return
        embed = {
            "title": "Pokebot3DS-CFW Discord Test",
            "description": (
                "Discord integration is connected. This test also requests a "
                "live screenshot from the 2DS/3DS top framebuffer."
            ),
            "color": 0x3BA55D,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {
                "text": "Framebuffer is presentation-only — RAM remains hunt authority."
            },
        }
        self._queue_embed_with_framebuffer(
            embed,
            kind="TEST",
            prefix="discord_test",
            success_message="Discord test queued",
        )

    def hunt_started(self, context):
        self._last_context = dict(context or {})
        self._last_stats = {}
        self._last_encounter = {}
        self._last_phase_seen.clear()
        self._sent_milestones.clear()
        self._last_summary_mono = time.monotonic()
        self._shiny_dedup.clear()
        if self.settings.get("discord_notify_hunt_started", True):
            self._send_hunt_state("Hunt Started", 0x3BA55D, context)
        self.update_presence(force=True)

    def hunt_finished(self, status, context=None):
        ctx = dict(self._last_context)
        ctx.update(context or {})
        status_u = str(status or "").upper()
        if "SHINY" in status_u:
            # Normally recent_shinies_update sends the richer persisted notification.
            # If that signal was unavailable, fall back to the held encounter once.
            if self._last_encounter and bool(self._last_encounter.get("is_shiny")):
                self._send_shiny(self._last_encounter)
        elif "HOLD" in status_u:
            if self.settings.get("discord_notify_safety_hold", True):
                self._send_hunt_state("Safety HOLD", 0xED4245, ctx, status=status)
        elif self.settings.get("discord_notify_hunt_stopped", True):
            self._send_hunt_state("Hunt Stopped", 0x747F8D, ctx, status=status)
        self.update_presence(status=status, force=True)

    def status_update(self, status, detail, context=None):
        if context:
            self._last_context.update(context)
        self._last_context["status"] = status
        self._last_context["status_detail"] = detail
        self.update_presence(status=status)

    def stats_update(self, stats, context=None):
        self._last_stats = dict(stats or {})
        if context:
            self._last_context.update(context)
        self._check_milestones()
        self.update_presence()

    def encounter_update(self, encounter, context=None):
        self._last_encounter = dict(encounter or {})
        if context:
            self._last_context.update(context)
        # A shiny notification is delayed until the persisted recent-shiny record
        # arrives. That record contains the completed phase length and proves the
        # encounter was committed before Discord is notified.
        self.update_presence()

    def recent_shinies_update(self, items):
        if not self._last_encounter or not bool(self._last_encounter.get("is_shiny")):
            return
        latest = (list(items or []) or [None])[0]
        if not isinstance(latest, dict):
            return
        encounter_pid = str(self._last_encounter.get("pokemon_pid") or self._last_encounter.get("pid") or "")
        recent_pid = str(latest.get("pid") or latest.get("pokemon_pid") or "")
        if encounter_pid and recent_pid and encounter_pid != recent_pid:
            return
        enriched = dict(self._last_encounter)
        if latest.get("phase_length") is not None:
            enriched["phase_length"] = latest.get("phase_length")
        for key in (
            "game", "location", "method", "target", "framebuffer_path"
        ):
            if latest.get(key) is not None:
                # The persisted shiny record is the committed post-authority
                # evidence. In particular, let its exact pre-Run framebuffer
                # path replace a transient None from the encounter signal.
                enriched[key] = latest.get(key)
        self._send_shiny(enriched)

    def pokerus_detected(self, payload, context=None):
        """Best-effort post-battle Pokérus notification.

        Pokérus telemetry is isolated from hunt authority exactly like Discord
        shiny presentation. A Discord failure never feeds back into the worker.
        """
        if not self.settings.get("discord_notify_pokerus", True):
            return False
        if not self._connected:
            return False

        event = dict(payload or {})
        ctx = dict(self._last_context)
        ctx.update(context or {})
        infections = list(event.get("new_infections") or [])
        if not infections:
            return False

        source = str(event.get("source") or "")
        source_text = (
            "Natural post-battle infection"
            if source == "NATURAL_POST_BATTLE"
            else "Party spread or natural infection"
        )

        fields = []
        for rec in infections:
            fields.append(self._field(
                f"Party Slot {int(rec.get('slot', 0) or 0)}",
                (
                    f"{rec.get('species_name', 'Pokémon')} • "
                    f"strain {int(rec.get('strain', 0) or 0)} • "
                    f"{int(rec.get('days', 0) or 0)} contagious day(s)"
                ),
                inline=False,
            ))

        if ctx.get("game"):
            fields.append(self._field("Game", ctx.get("game")))
        if ctx.get("location_name"):
            fields.append(self._field("Location", ctx.get("location_name")))
        if ctx.get("method"):
            fields.append(self._field("Method", ctx.get("method")))
        fields.append(self._field("Source", source_text, inline=False))

        embed = {
            "title": "🦠 POKÉRUS DETECTED",
            "description": (
                "A party Pokémon gained Pokérus after the battle. "
                "This was detected from a one-shot post-battle party RAM snapshot."
            ),
            "color": 0x57F287,
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {
                "text": "Pokebot3DS-CFW • post-battle party telemetry • hunt authority unchanged"
            },
        }
        if self._submit(self._send_embed_async(embed), "POKERUS"):
            names = ", ".join(
                str(rec.get("species_name") or "Pokémon")
                for rec in infections
            )
            self._record_event(
                "POKERUS",
                f"Pokérus notification queued: {names}",
                True,
            )
            return True
        return False

    def _parse_milestones(self):
        raw = str(self.settings.get("discord_phase_milestones") or "")
        values = set()
        for token in raw.replace(";", ",").split(","):
            try:
                value = int(token.strip())
                if value > 0:
                    values.add(value)
            except Exception:
                pass
        return sorted(values)

    def _check_milestones(self):
        if not self.settings.get("discord_notify_milestones", True):
            return
        try:
            current = int(self._last_stats.get("phase_seen", 0))
        except Exception:
            return
        phase_key = str(
            self._last_stats.get("starter")
            or self._last_stats.get("target_species_name")
            or self._last_stats.get("target")
            or "current"
        )
        previous = self._last_phase_seen.get(phase_key)
        self._last_phase_seen[phase_key] = current
        if previous is None:
            previous = max(0, current - 1)
        sent = self._sent_milestones.setdefault(phase_key, set())
        for milestone in self._parse_milestones():
            if milestone in sent:
                continue
            if previous < milestone <= current:
                sent.add(milestone)
                self._send_phase_summary(title=f"{phase_key} Phase Milestone — {milestone:,}")

    def maybe_periodic_summary(self):
        if not self.settings.get("discord_periodic_summary", False):
            return
        try:
            minutes = max(5, int(self.settings.get("discord_summary_minutes", 60)))
        except Exception:
            minutes = 60
        if time.monotonic() - self._last_summary_mono < minutes * 60:
            return
        self._last_summary_mono = time.monotonic()
        self._send_phase_summary(title="Periodic Hunt Summary")

    def _context_fields(self, context=None):
        ctx = dict(self._last_context)
        ctx.update(context or {})
        stats = self._last_stats
        fields = []
        game = ctx.get("game") or stats.get("game") or "ORAS"
        target = ctx.get("target") or stats.get("target") or stats.get("starter") or "Current Hunt"
        method = ctx.get("method") or stats.get("method_name") or stats.get("hunt_type") or "Starter"
        location = ctx.get("location_name") or stats.get("location_name")
        fields.extend([
            self._field("Game", game),
            self._field("Target", target),
            self._field("Method", method),
        ])
        if location:
            fields.append(self._field("Location", location))
        phase_seen = stats.get("phase_seen")
        if phase_seen is not None:
            fields.append(self._field("Phase Encounters", f"{int(phase_seen):,}"))
        cumulative = stats.get("phase_cumulative_probability")
        if cumulative is not None:
            try:
                fields.append(self._field("Cumulative Chance", format_cumulative_percent(float(cumulative))))
            except Exception:
                pass
        odds_text = stats.get("shiny_odds_display")
        if odds_text:
            fields.append(self._field("Shiny Odds", str(odds_text)))
        chain = stats.get("fishing_chain") or {}
        if isinstance(chain, dict) and chain.get("chain") is not None:
            try:
                fields.append(self._field("Fishing Chain", f"{int(chain.get('chain') or 0):,}"))
            except Exception:
                pass
        resets = stats.get("resets", stats.get("session_resets"))
        batches = stats.get("batches", stats.get("session_batches"))
        if batches is not None:
            try:
                fields.append(self._field("Resets / Batches", f"{int(resets or 0):,} / {int(batches or 0):,}"))
            except Exception:
                pass
        elif resets is not None:
            try:
                fields.append(self._field("Resets", f"{int(resets or 0):,}"))
            except Exception:
                pass
        rate = self._display_rate(stats)
        if rate:
            fields.append(self._field("Rate", rate))
        elapsed = stats.get("elapsed")
        if elapsed:
            fields.append(self._field("Elapsed", elapsed))
        return fields

    def _send_hunt_state(self, title, color, context=None, status=None):
        if not self._connected:
            return
        fields = self._context_fields(context)
        if status:
            fields.append(self._field("Status", status, inline=False))
        embed = {
            "title": f"Pokebot3DS-CFW — {title}",
            "color": int(color),
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if self._submit(self._send_embed_async(embed), "HUNT"):
            self._record_event("HUNT", title, True)

    def _send_phase_summary(self, title="Hunt Summary"):
        if not self._connected:
            return
        embed = {
            "title": f"Pokebot3DS-CFW — {title}",
            "color": 0x5865F2,
            "fields": self._context_fields(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if self._submit(self._send_embed_async(embed), "SUMMARY"):
            self._record_event("SUMMARY", title, True)

    def _send_shiny(self, enc):
        if not self.settings.get("discord_notify_shiny", True) or not self._connected:
            return
        key = (str(enc.get("pokemon_pid") or enc.get("pid")), int(enc.get("shiny_xor", -1)))
        if key in self._shiny_dedup:
            return
        self._shiny_dedup.add(key)

        ivs = dict(enc.get("ivs") or {})

        # PK6 authority uses canonical descriptive IV keys. Older UI payloads
        # used abbreviations, so accept both for presentation compatibility.
        def _iv_value(canonical, alias):
            if canonical in ivs:
                return ivs.get(canonical)
            return ivs.get(alias, "—")

        iv_text = (
            f"{_iv_value('hp','hp')}/{_iv_value('attack','atk')}/"
            f"{_iv_value('defense','def')}/{_iv_value('sp_attack','spa')}/"
            f"{_iv_value('sp_defense','spd')}/{_iv_value('speed','spe')}"
        )
        fields = self._context_fields({
            "target": enc.get("species_name") or self._last_context.get("target"),
            "location_name": enc.get("location_name") or self._last_context.get("location_name"),
            "method": enc.get("method_name") or enc.get("hunt_type") or self._last_context.get("method"),
            "game": enc.get("game") or self._last_context.get("game"),
        })
        if enc.get("phase_length") is not None:
            fields.append(self._field("Completed Phase", f"{int(enc.get('phase_length')):,} encounters"))
        fields.extend([
            self._field("PID", enc.get("pokemon_pid") or enc.get("pid")),
            self._field("Shiny XOR", enc.get("shiny_xor")),
            self._field("Nature", enc.get("nature")),
        ])
        if enc.get("predicted_evolution"):
            fields.append(self._field("Evolution", enc.get("predicted_evolution"), inline=False))
        fields.append(self._field("IVs HP/Atk/Def/SpA/SpD/Spe", iv_text, inline=False))
        blocklisted = bool(enc.get("shiny_blocked"))
        shiny_action = str(enc.get("shiny_action") or ("RUN" if blocklisted else "HOLD")).upper()
        if blocklisted or shiny_action == "RUN":
            shiny_description = (
                "RAM-authoritative shiny detection — species is on the live "
                "Shiny Blocklist, so Pokebot records it and automatically Runs."
            )
        elif shiny_action == "AUTO_CATCH":
            shiny_description = (
                "RAM-authoritative shiny detection — Auto-Capture is armed and "
                "the capture state machine is handling this Pokémon."
            )
        else:
            shiny_description = "RAM-authoritative shiny detection — hunt is in absolute HOLD."
        embed = {
            "title": (
                f"✨ SHINY {enc.get('species_name', 'Pokémon')} FOUND"
                + (" — BLOCKLIST RUN!" if blocklisted else "!")
            ),
            "description": shiny_description,
            "color": 0xFEE75C,
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "Pokebot3DS-CFW • RAM shiny authority"},
        }
        try:
            species_id = int(enc.get("species", 0) or 0)
        except Exception:
            species_id = 0
        if species_id > 0:
            embed["thumbnail"] = {
                "url": (
                    "https://raw.githubusercontent.com/PokeAPI/sprites/master/"
                    "sprites/pokemon/versions/generation-vi/"
                    f"omegaruby-alphasapphire/shiny/{species_id}.png"
                )
            }
        # Prefer the battle snapshot captured by the Wild worker. That is
        # essential for a blocklisted shiny because causal Run follows. Starter
        # and ordinary HOLD shinies fall back to a live framebuffer capture here.
        self._queue_embed_with_framebuffer(
            embed,
            kind="SHINY",
            prefix=(
                f"shiny_{int(enc.get('species', 0) or 0)}"
                if enc.get("species") is not None else "shiny"
            ),
            existing_path=enc.get("framebuffer_path"),
            success_message=(
                f"Shiny {enc.get('species_name', 'Pokémon')} notification queued"
            ),
        )

    def _display_rate(self, stats):
        try:
            rate = float(stats.get("rate", 0) or 0)
            if rate > 0:
                return f"{rate:.1f}/hr"
        except Exception:
            pass
        try:
            avg = float(stats.get("average_time", 0) or 0)
            if avg > 0:
                return f"{3600.0/avg:.1f}/hr"
        except Exception:
            pass
        return None

    def _presence_text(self, status=None):
        stats, ctx, enc = self._last_stats, self._last_context, self._last_encounter
        status_u = str(status or ctx.get("status") or "RUNNING").upper()
        if bool(enc.get("is_shiny")) and bool(enc.get("shiny_blocked")):
            target = enc.get("species_name") or ctx.get("target") or "Pokémon"
            return f"✨ Shiny {target} found — blocklist RUN"
        if "SHINY" in status_u or bool(enc.get("is_shiny")):
            target = enc.get("species_name") or ctx.get("target") or "Pokémon"
            return f"✨ Shiny {target} found — HOLD"
        if "HOLD" in status_u:
            return "Safety HOLD — no reset"
        target = ctx.get("target") or stats.get("target") or stats.get("starter") or "ORAS"
        try:
            seen = int(stats.get("phase_seen", 0) or 0)
        except Exception:
            seen = 0
        rate = self._display_rate(stats)
        parts = [f"Hunting {target}"]
        if seen:
            parts.append(f"{seen:,} seen")
        if rate:
            parts.append(rate)
        return " | ".join(parts)[:128]

    async def _apply_presence_async(self, force=False, status=None):
        if not self.settings.get("discord_presence_enabled", True):
            activity = None
            text = None
        else:
            text = self._presence_text(status)
            if not force and text == self._last_presence:
                return
            activity = discord.Game(name=text)
        try:
            await self._client.change_presence(activity=activity, status=discord.Status.online)
            self._last_presence = text
        except Exception as exc:
            self._record_event("PRESENCE", f"Presence update failed: {type(exc).__name__}: {exc}", False)

    def update_presence(self, status=None, force=False):
        if not self._connected:
            return
        self._submit(self._apply_presence_async(force=force, status=status), "PRESENCE")
