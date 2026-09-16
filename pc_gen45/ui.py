from __future__ import annotations

import queue
import subprocess
import threading
import time
from pathlib import Path
import tkinter as tk
from tkinter import messagebox

from backend.melonds_udp import MelonDSUDPBackend
from pokebot_gen45 import (
    HG_STARTERS,
    _reach_hgss_starter_screen,
    _sound_target,
    _starter_set_identity,
    load_species_names,
)
from stats_store import StatsStore


BG = "#10131a"
PANEL = "#171c26"
PANEL_2 = "#1d2430"
BORDER = "#2b3545"
TEXT = "#eef3f8"
MUTED = "#8e9aab"
ACCENT = "#50b7ff"
GOOD = "#5bd68b"
WARN = "#f0b45a"
BAD = "#ff6d7a"
SHINY = "#ffd85a"

STARTER_IDS = (152, 155, 158)
STARTER_NAMES = {152: "Chikorita", 155: "Cyndaquil", 158: "Totodile"}


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _mon_dict(mon, names: dict[int, str]) -> dict:
    return {
        "species": mon.species,
        "name": names.get(mon.species, f"Species {mon.species}"),
        "pid": f"{mon.pid:08X}",
        "sv": mon.shiny_value,
        "nature": mon.nature,
        "ability": mon.ability,
        "ivs": list(mon.ivs),
        "hidden_power": f"{mon.hidden_power_type}/{mon.hidden_power_power}",
        "shiny": bool(mon.shiny),
    }


class StarterHuntWorker(threading.Thread):
    def __init__(
        self,
        events: queue.Queue,
        stop_event: threading.Event,
        target_species: set[int],
        *,
        headless: bool,
        mute_audio: bool,
    ) -> None:
        super().__init__(daemon=True)
        self.events = events
        self.stop_event = stop_event
        self.target_species = set(target_species)
        self.headless = headless
        self.mute_audio = mute_audio
        self.names = load_species_names()

    def emit(self, kind: str, **payload) -> None:
        self.events.put({"type": kind, **payload})

    def run(self) -> None:
        backend = MelonDSUDPBackend(timeout=2.0)
        display_disabled = False
        audio_disabled = False
        seen_sets: set[tuple[tuple[int, int, int], ...]] = set()
        duplicate_streak = 0
        base_hint = 0x022BBE84
        after_reset = False

        try:
            bridge = backend.ping()
            self.emit("connected", bridge=bridge)

            if self.headless:
                backend.set_display(False)
                display_disabled = True
                self.emit("presentation", display=False)
            if self.mute_audio:
                backend.set_audio(False)
                audio_disabled = True
                self.emit("presentation", audio=False)

            while not self.stop_event.is_set():
                jitter_boost = min(duplicate_streak * 0.40, 2.00)
                mons, resolved_base, source, cycle_seconds = _reach_hgss_starter_screen(
                    backend,
                    base_hint=base_hint,
                    timeout=45.0,
                    after_reset=after_reset,
                    reset_delay_min=0.0,
                    reset_delay_max=0.20,
                    input_interval=0.035,
                    boot_settle=0.35,
                    jitter_boost=jitter_boost,
                    stop_check=self.stop_event.is_set,
                )

                if source == "stopped" or self.stop_event.is_set():
                    break

                if mons is None:
                    backend.reset_input()
                    self.emit("safety", message="Starter screen was not reached before timeout.")
                    return

                base_hint = resolved_base
                identity = _starter_set_identity(mons)
                if identity in seen_sets:
                    duplicate_streak += 1
                    self.emit(
                        "duplicate",
                        streak=duplicate_streak,
                        seconds=cycle_seconds,
                    )
                else:
                    duplicate_streak = 0
                    seen_sets.add(identity)
                    mon_rows = [_mon_dict(mon, self.names) for mon in mons]
                    self.emit(
                        "set",
                        mons=mon_rows,
                        seconds=cycle_seconds,
                        address=f"0x{resolved_base:08X}",
                        source=source,
                    )

                    targets = [
                        mon for mon in mons
                        if mon.species in self.target_species and mon.shiny
                    ]
                    if targets:
                        backend.reset_input()
                        if display_disabled:
                            backend.set_display(True)
                            display_disabled = False
                        if audio_disabled:
                            backend.set_audio(True)
                            audio_disabled = False
                        self.emit(
                            "target",
                            mons=[_mon_dict(mon, self.names) for mon in targets],
                        )
                        _sound_target()
                        return

                if self.stop_event.is_set():
                    break

                backend.reset_input()
                backend.reset_game()
                self.emit("reset")
                after_reset = True

        except Exception as exc:
            self.emit("error", message=str(exc))
        finally:
            try:
                backend.reset_input()
            except Exception:
                pass
            if display_disabled:
                try:
                    backend.set_display(True)
                except Exception:
                    pass
            if audio_disabled:
                try:
                    backend.set_audio(True)
                except Exception:
                    pass
            self.emit("stopped")


class PokebotUI:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Pokebot Gen45 PC")
        self.root.geometry("1420x860")
        self.root.minsize(1180, 720)
        self.root.configure(bg=BG)

        self.package_root = Path(__file__).resolve().parent.parent
        self.sprite_root = self.package_root / "assets" / "sprites" / "hgss"
        self.emulator_path = self.package_root / "emulator" / "melonDS.exe"

        self.stats = StatsStore()
        self.events: queue.Queue = queue.Queue()
        self.worker: StarterHuntWorker | None = None
        self.stop_event = threading.Event()
        self.sprite_cache: dict[tuple[int, bool], tk.PhotoImage] = {}
        self.current_mode = "Starters"
        self.session_started = time.monotonic()
        self.hunt_started: float | None = None
        self.target_vars = {species: tk.BooleanVar(value=True) for species in STARTER_IDS}
        self.headless_var = tk.BooleanVar(value=True)
        self.mute_var = tk.BooleanVar(value=True)

        self.status_var = tk.StringVar(value="Idle")
        self.bridge_var = tk.StringVar(value="Bridge: not tested")
        self.session_seen_var = tk.StringVar(value="0")
        self.session_resets_var = tk.StringVar(value="0")
        self.session_shinies_var = tk.StringVar(value="0")
        self.rate_var = tk.StringVar(value="0.0 / hr")
        self.phase_var = tk.StringVar(value="00:00")
        self.lifetime_seen_var = tk.StringVar(value="0")
        self.lifetime_shinies_var = tk.StringVar(value="0")
        self.lifetime_resets_var = tk.StringVar(value="0")

        self._build()
        self._refresh_stats()
        self._render_recent()
        self._tick()
        self._poll_events()
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def run(self) -> None:
        self.root.mainloop()

    def _frame(self, parent, **kwargs):
        return tk.Frame(
            parent,
            bg=kwargs.pop("bg", PANEL),
            highlightthickness=kwargs.pop("highlightthickness", 1),
            highlightbackground=kwargs.pop("highlightbackground", BORDER),
            **kwargs,
        )

    def _label(self, parent, text="", *, font=("Segoe UI", 10), fg=TEXT, bg=PANEL, **kwargs):
        return tk.Label(parent, text=text, font=font, fg=fg, bg=bg, **kwargs)

    def _button(self, parent, text, command, *, accent=False, width=None, state="normal"):
        bg = ACCENT if accent else PANEL_2
        fg = "#09131c" if accent else TEXT
        active = "#75c8ff" if accent else "#293345"
        return tk.Button(
            parent,
            text=text,
            command=command,
            width=width,
            state=state,
            bg=bg,
            fg=fg,
            activebackground=active,
            activeforeground=fg,
            disabledforeground="#596476",
            relief="flat",
            bd=0,
            padx=14,
            pady=9,
            font=("Segoe UI Semibold", 10),
            cursor="hand2",
        )

    def _build(self) -> None:
        outer = tk.Frame(self.root, bg=BG)
        outer.pack(fill="both", expand=True, padx=14, pady=14)

        top = self._frame(outer, bg=PANEL)
        top.pack(fill="x", pady=(0, 12))

        title_box = tk.Frame(top, bg=PANEL)
        title_box.pack(side="left", padx=18, pady=13)
        self._label(
            title_box,
            "Pokebot Gen45 PC",
            font=("Segoe UI Semibold", 20),
        ).pack(anchor="w")
        self._label(
            title_box,
            "HeartGold / SoulSilver • native melonDS RAM + input",
            font=("Segoe UI", 9),
            fg=MUTED,
        ).pack(anchor="w")

        status_box = tk.Frame(top, bg=PANEL)
        status_box.pack(side="right", padx=18, pady=10)
        tk.Label(
            status_box,
            textvariable=self.status_var,
            bg=PANEL_2,
            fg=GOOD,
            font=("Segoe UI Semibold", 10),
            padx=12,
            pady=6,
        ).pack(anchor="e")
        self._label(
            status_box,
            textvariable=self.bridge_var,
            font=("Consolas", 9),
            fg=MUTED,
        ).pack(anchor="e", pady=(4, 0))

        body = tk.Frame(outer, bg=BG)
        body.pack(fill="both", expand=True)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        sidebar = self._frame(body, width=220)
        sidebar.grid(row=0, column=0, sticky="ns", padx=(0, 12))
        sidebar.grid_propagate(False)

        self._label(
            sidebar,
            "HUNT MODES",
            font=("Segoe UI Semibold", 9),
            fg=MUTED,
        ).pack(anchor="w", padx=16, pady=(18, 8))

        self.mode_buttons: dict[str, tk.Button] = {}
        for mode in ("Starters", "Wild", "Static"):
            btn = self._button(
                sidebar,
                mode,
                lambda m=mode: self._select_mode(m),
                width=19,
            )
            btn.pack(fill="x", padx=12, pady=4)
            self.mode_buttons[mode] = btn

        self._label(
            sidebar,
            "EMULATOR",
            font=("Segoe UI Semibold", 9),
            fg=MUTED,
        ).pack(anchor="w", padx=16, pady=(24, 8))

        self._button(sidebar, "Launch melonDS", self._launch_melonds, width=19).pack(fill="x", padx=12, pady=4)
        self._button(sidebar, "Test Bridge", self._test_bridge, width=19).pack(fill="x", padx=12, pady=4)

        opts = tk.Frame(sidebar, bg=PANEL)
        opts.pack(fill="x", padx=14, pady=(18, 0))
        self._check(opts, "Headless while hunting", self.headless_var).pack(anchor="w", pady=3)
        self._check(opts, "Mute game audio", self.mute_var).pack(anchor="w", pady=3)

        appdata = str(self.stats.path.parent)
        self._label(
            sidebar,
            "Stats saved to:",
            font=("Segoe UI Semibold", 8),
            fg=MUTED,
        ).pack(anchor="w", padx=16, pady=(24, 2))
        self._label(
            sidebar,
            appdata,
            font=("Segoe UI", 7),
            fg=MUTED,
            wraplength=185,
            justify="left",
        ).pack(anchor="w", padx=16)

        main = tk.Frame(body, bg=BG)
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(2, weight=1)

        current = self._frame(main)
        current.grid(row=0, column=0, sticky="ew")
        current.grid_columnconfigure(0, weight=1)

        current_header = tk.Frame(current, bg=PANEL)
        current_header.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 8))
        current_header.grid_columnconfigure(0, weight=1)
        self.current_title = self._label(
            current_header,
            "Current Hunt • HGSS Starters",
            font=("Segoe UI Semibold", 15),
        )
        self.current_title.grid(row=0, column=0, sticky="w")

        actions = tk.Frame(current_header, bg=PANEL)
        actions.grid(row=0, column=1, sticky="e")
        self.start_button = self._button(actions, "Start Hunt", self._start_hunt, accent=True)
        self.start_button.pack(side="left", padx=(0, 6))
        self.stop_button = self._button(actions, "Stop", self._stop_hunt, state="disabled")
        self.stop_button.pack(side="left")

        target_wrap = tk.Frame(current, bg=PANEL)
        target_wrap.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 16))
        for i, species in enumerate(STARTER_IDS):
            target_wrap.grid_columnconfigure(i, weight=1)
            card = self._starter_target_card(target_wrap, species)
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 5, 0 if i == 2 else 5))

        stats = self._frame(main)
        stats.grid(row=1, column=0, sticky="ew", pady=12)
        stats.grid_columnconfigure(tuple(range(8)), weight=1)

        stat_defs = (
            ("Session Seen", self.session_seen_var),
            ("Session Resets", self.session_resets_var),
            ("Session Shinies", self.session_shinies_var),
            ("Rate", self.rate_var),
            ("Phase", self.phase_var),
            ("Lifetime Seen", self.lifetime_seen_var),
            ("Lifetime Shinies", self.lifetime_shinies_var),
            ("Lifetime Resets", self.lifetime_resets_var),
        )
        for col, (name, var) in enumerate(stat_defs):
            cell = tk.Frame(stats, bg=PANEL, padx=10, pady=12)
            cell.grid(row=0, column=col, sticky="nsew")
            self._label(cell, name, font=("Segoe UI", 8), fg=MUTED).pack()
            self._label(cell, textvariable=var, font=("Segoe UI Semibold", 13)).pack(pady=(3, 0))

        recent_panel = self._frame(main)
        recent_panel.grid(row=2, column=0, sticky="nsew")
        recent_panel.grid_columnconfigure(0, weight=1)
        recent_panel.grid_rowconfigure(1, weight=1)

        rhead = tk.Frame(recent_panel, bg=PANEL)
        rhead.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 8))
        rhead.grid_columnconfigure(0, weight=1)
        self._label(rhead, "Recently Seen", font=("Segoe UI Semibold", 14)).grid(row=0, column=0, sticky="w")
        self.last_cycle_label = self._label(rhead, "No encounters this session", font=("Segoe UI", 9), fg=MUTED)
        self.last_cycle_label.grid(row=0, column=1, sticky="e")

        self.recent_wrap = tk.Frame(recent_panel, bg=PANEL)
        self.recent_wrap.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 14))
        for i in range(6):
            self.recent_wrap.grid_columnconfigure(i, weight=1)

        self.status_line = self._label(
            main,
            "Ready. Launch melonDS, load HeartGold, then start the hunt.",
            font=("Segoe UI", 9),
            fg=MUTED,
            bg=BG,
            anchor="w",
        )
        self.status_line.grid(row=3, column=0, sticky="ew", pady=(8, 0))

        self._select_mode("Starters")

    def _check(self, parent, text: str, variable: tk.BooleanVar):
        return tk.Checkbutton(
            parent,
            text=text,
            variable=variable,
            bg=PANEL,
            fg=TEXT,
            activebackground=PANEL,
            activeforeground=TEXT,
            selectcolor=PANEL_2,
            font=("Segoe UI", 9),
            bd=0,
            highlightthickness=0,
        )

    def _starter_target_card(self, parent, species: int) -> tk.Frame:
        card = self._frame(parent, bg=PANEL_2)
        name = STARTER_NAMES[species]
        top = tk.Frame(card, bg=PANEL_2)
        top.pack(fill="x", padx=10, pady=(9, 2))
        self._check(top, name, self.target_vars[species]).pack(side="left")
        self._label(top, f"#{species}", font=("Consolas", 8), fg=MUTED, bg=PANEL_2).pack(side="right")

        sprites = tk.Frame(card, bg=PANEL_2)
        sprites.pack(pady=(0, 8))
        normal = self._sprite(species, False)
        shiny = self._sprite(species, True)
        l1 = tk.Label(sprites, image=normal, bg=PANEL_2)
        l1.image = normal
        l1.pack(side="left", padx=6)
        l2 = tk.Label(sprites, image=shiny, bg=PANEL_2)
        l2.image = shiny
        l2.pack(side="left", padx=6)
        return card

    def _sprite(self, species: int, shiny: bool) -> tk.PhotoImage:
        key = (species, shiny)
        if key in self.sprite_cache:
            return self.sprite_cache[key]
        variant = "shiny" if shiny else "normal"
        path = self.sprite_root / variant / f"{species}.png"
        try:
            img = tk.PhotoImage(file=str(path))
        except Exception:
            img = tk.PhotoImage(width=80, height=80)
        self.sprite_cache[key] = img
        return img

    def _select_mode(self, mode: str) -> None:
        self.current_mode = mode
        for name, btn in self.mode_buttons.items():
            if name == mode:
                btn.configure(bg=ACCENT, fg="#09131c", activebackground="#75c8ff")
            else:
                btn.configure(bg=PANEL_2, fg=TEXT, activebackground="#293345")

        if mode == "Starters":
            self.current_title.configure(text="Current Hunt • HGSS Starters")
            self.start_button.configure(state="normal" if self.worker is None else "disabled")
            self.status_line.configure(text="HGSS three-starter RAM hunter ready.")
        elif mode == "Wild":
            self.current_title.configure(text="Current Hunt • Wild Encounters")
            self.start_button.configure(state="disabled")
            self.status_line.configure(text="Wild UI mode is ready; Gen IV wild encounter engine is the next backend to wire.")
        else:
            self.current_title.configure(text="Current Hunt • Static Encounters")
            self.start_button.configure(state="disabled")
            self.status_line.configure(text="Static UI mode is ready; Gen IV static encounter engine is the next backend to wire.")

    def _launch_melonds(self) -> None:
        if not self.emulator_path.exists():
            messagebox.showerror("melonDS not found", f"Missing:\n{self.emulator_path}")
            return
        try:
            subprocess.Popen([str(self.emulator_path)], cwd=str(self.emulator_path.parent))
            self.status_line.configure(text="melonDS launched.")
        except Exception as exc:
            messagebox.showerror("Could not start melonDS", str(exc))

    def _test_bridge(self) -> None:
        def run():
            try:
                value = MelonDSUDPBackend(timeout=1.0).ping()
                self.events.put({"type": "bridge", "ok": True, "value": value})
            except Exception as exc:
                self.events.put({"type": "bridge", "ok": False, "value": str(exc)})
        threading.Thread(target=run, daemon=True).start()

    def _start_hunt(self) -> None:
        if self.worker is not None:
            return
        if self.current_mode != "Starters":
            return

        targets = {species for species, var in self.target_vars.items() if var.get()}
        if not targets:
            messagebox.showwarning("No targets", "Select at least one starter.")
            return

        self.stop_event = threading.Event()
        self.hunt_started = time.monotonic()
        self.stats.start_hunt({
            "mode": "Starters",
            "game": "HeartGold",
            "targets": [STARTER_NAMES[x] for x in sorted(targets)],
        })

        self.worker = StarterHuntWorker(
            self.events,
            self.stop_event,
            targets,
            headless=self.headless_var.get(),
            mute_audio=self.mute_var.get(),
        )
        self.worker.start()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status_var.set("Hunting")
        self.status_line.configure(text="Starting HGSS starter hunt…")

    def _stop_hunt(self) -> None:
        if self.worker is None:
            return
        self.stop_event.set()
        self.status_var.set("Stopping…")
        self.stop_button.configure(state="disabled")
        self.status_line.configure(text="Stopping hunt and restoring melonDS display/audio…")

    def _handle_event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "connected":
            self.bridge_var.set(f"Bridge: {event['bridge']}")
            self.status_line.configure(text="Native bridge connected. Hunting.")
        elif kind == "bridge":
            if event["ok"]:
                self.bridge_var.set(f"Bridge: {event['value']}")
                self.status_line.configure(text="Native melonDS bridge is responding.")
            else:
                self.bridge_var.set("Bridge: offline")
                self.status_line.configure(text=event["value"])
        elif kind == "presentation":
            pass
        elif kind == "set":
            mons = event["mons"]
            self.stats.record_set(mons)
            self.last_cycle_label.configure(
                text=f"{event['seconds']:.2f}s • {event['address']} • {event['source']}"
            )
            self._refresh_stats()
            self._render_recent()
        elif kind == "reset":
            self.stats.record_reset()
            self._refresh_stats()
        elif kind == "duplicate":
            self.status_line.configure(
                text=f"Duplicate starter set ignored for stats (streak {event['streak']})."
            )
        elif kind == "target":
            mons = event["mons"]
            names = ", ".join(mon["name"] for mon in mons)
            self.status_var.set("SHINY FOUND")
            self.status_line.configure(
                text=f"Target found: {names}. melonDS display and sound restored.",
                fg=SHINY,
            )
            self._render_recent()
        elif kind == "safety":
            self.status_var.set("Safety Hold")
            self.status_line.configure(text=event["message"], fg=WARN)
        elif kind == "error":
            self.status_var.set("Error")
            self.status_line.configure(text=event["message"], fg=BAD)
        elif kind == "stopped":
            self.worker = None
            self.hunt_started = None
            self.start_button.configure(state="normal" if self.current_mode == "Starters" else "disabled")
            self.stop_button.configure(state="disabled")
            if self.status_var.get() in {"Hunting", "Stopping…"}:
                self.status_var.set("Idle")
                self.status_line.configure(text="Hunt stopped. melonDS display/audio restored.", fg=MUTED)

    def _refresh_stats(self) -> None:
        data = self.stats.snapshot()
        session = data["session"]
        lifetime = data["lifetime"]
        self.session_seen_var.set(str(session["seen"]))
        self.session_resets_var.set(str(session["resets"]))
        self.session_shinies_var.set(str(session["shinies"]))
        self.lifetime_seen_var.set(str(lifetime["seen"]))
        self.lifetime_shinies_var.set(str(lifetime["shinies"]))
        self.lifetime_resets_var.set(str(lifetime["resets"]))

    def _render_recent(self) -> None:
        for child in self.recent_wrap.winfo_children():
            child.destroy()

        recent = self.stats.snapshot().get("recently_seen", [])[:6]
        if not recent:
            empty = self._label(
                self.recent_wrap,
                "No Pokémon seen yet.",
                font=("Segoe UI", 10),
                fg=MUTED,
                bg=PANEL,
            )
            empty.grid(row=0, column=0, columnspan=6, pady=45)
            return

        for idx, mon in enumerate(recent):
            shiny = bool(mon.get("shiny"))
            card = self._frame(
                self.recent_wrap,
                bg=PANEL_2,
                highlightbackground=SHINY if shiny else BORDER,
            )
            card.grid(row=0, column=idx, sticky="nsew", padx=4, pady=4)

            img = self._sprite(int(mon["species"]), shiny)
            sprite = tk.Label(card, image=img, bg=PANEL_2)
            sprite.image = img
            sprite.pack(pady=(8, 0))

            name_fg = SHINY if shiny else TEXT
            self._label(
                card,
                ("★ " if shiny else "") + str(mon.get("name", mon["species"])),
                font=("Segoe UI Semibold", 9),
                fg=name_fg,
                bg=PANEL_2,
            ).pack()

            ivs = "/".join(str(x) for x in mon.get("ivs", []))
            lines = (
                f"PID {mon.get('pid', '-')}",
                f"SV {mon.get('sv', '-')}",
                f"{mon.get('nature', '-')} • Ability {mon.get('ability', '-')}",
                f"IVs {ivs}",
                f"HP {mon.get('hidden_power', '-')}",
            )
            for line in lines:
                self._label(
                    card,
                    line,
                    font=("Consolas", 7),
                    fg=MUTED,
                    bg=PANEL_2,
                ).pack()
            tk.Frame(card, bg=PANEL_2, height=7).pack()

    def _tick(self) -> None:
        data = self.stats.snapshot()
        seen = int(data["session"]["seen"])
        elapsed = max(1.0, time.monotonic() - self.session_started)
        rate = seen / (elapsed / 3600.0)
        self.rate_var.set(f"{rate:.1f} / hr")

        if self.hunt_started is not None:
            self.phase_var.set(_fmt_duration(time.monotonic() - self.hunt_started))
        else:
            self.phase_var.set("00:00")

        self.root.after(1000, self._tick)

    def _poll_events(self) -> None:
        try:
            while True:
                self._handle_event(self.events.get_nowait())
        except queue.Empty:
            pass
        self.root.after(75, self._poll_events)

    def _restore_emulator_best_effort(self) -> None:
        try:
            backend = MelonDSUDPBackend(timeout=0.25)
            backend.reset_input()
            backend.set_display(True)
            backend.set_audio(True)
        except Exception:
            pass

    def _close(self) -> None:
        if self.worker is not None:
            self.stop_event.set()
            self._restore_emulator_best_effort()
        self.root.destroy()


def main() -> int:
    app = PokebotUI()
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
