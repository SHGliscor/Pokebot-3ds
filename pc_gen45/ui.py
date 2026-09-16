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


BG = "#0b121a"
PANEL = "#121e29"
PANEL_2 = "#182735"
BORDER = "#28d7f2"
TEXT = "#eef3f8"
MUTED = "#8e9aab"
ACCENT = "#38d27c"
GOOD = "#38d27c"
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
    """ORAS-style desktop dashboard for the PC Gen4/5 bot."""

    TABS = ("DASHBOARD", "HUNTS", "STATISTICS", "TOOLS", "SETTINGS", "TESTING & SUPPORT")

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Pokebot Gen45 PC")
        self.root.geometry("1520x900")
        self.root.minsize(1240, 760)
        self.root.configure(bg=BG)

        self.package_root = Path(__file__).resolve().parent.parent
        self.sprite_root = self.package_root / "assets" / "sprites" / "hgss"
        self.emulator_path = self.package_root / "emulator" / "melonDS.exe"

        self.stats = StatsStore()
        self.events: queue.Queue = queue.Queue()
        self.worker: StarterHuntWorker | None = None
        self.stop_event = threading.Event()
        self.sprite_cache: dict[tuple[int, bool, bool], tk.PhotoImage] = {}

        self.current_mode = "Starters"
        self.current_tab = "DASHBOARD"
        self.current_mons: list[dict] = []
        self.last_cycle_text = "Waiting for first starter set"
        self.last_hunt_status = "Idle"

        # ORAS-style active-session stopwatch: idle time is excluded.
        self.active_elapsed = 0.0
        self.active_started: float | None = None
        self.phase_elapsed = 0.0
        self.phase_started: float | None = None

        self.target_vars = {species: tk.BooleanVar(value=True) for species in STARTER_IDS}
        self.headless_var = tk.BooleanVar(value=True)
        self.mute_var = tk.BooleanVar(value=True)

        self.status_var = tk.StringVar(value="IDLE")
        self.bridge_var = tk.StringVar(value="Bridge: not tested")
        self.session_seen_var = tk.StringVar(value="0")
        self.session_resets_var = tk.StringVar(value="0")
        self.session_shinies_var = tk.StringVar(value="0")
        self.rate_var = tk.StringVar(value="0.0 / hr")
        self.phase_var = tk.StringVar(value="00:00")
        self.phase_seen_var = tk.StringVar(value="0")
        self.lifetime_seen_var = tk.StringVar(value="0")
        self.lifetime_shinies_var = tk.StringVar(value="0")
        self.lifetime_resets_var = tk.StringVar(value="0")

        self.pages: dict[str, tk.Frame] = {}
        self.nav_buttons: dict[str, tk.Button] = {}
        self.mode_buttons: dict[str, tk.Button] = {}

        self._build_shell()
        self._build_dashboard()
        self._build_hunts()
        self._build_statistics()
        self._build_tools()
        self._build_settings()
        self._build_testing_support()

        self._refresh_stats()
        self._render_current()
        self._render_last_seen()
        self._select_tab("DASHBOARD")
        self._select_mode("Starters")
        self._tick()
        self._poll_events()
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def run(self) -> None:
        self.root.mainloop()

    # ---------- shared UI helpers ----------

    def _frame(self, parent, **kwargs):
        return tk.Frame(
            parent,
            bg=kwargs.pop("bg", PANEL),
            highlightthickness=kwargs.pop("highlightthickness", 1),
            highlightbackground=kwargs.pop("highlightbackground", BORDER),
            **kwargs,
        )

    def _label(
        self,
        parent,
        text="",
        *,
        font=("Segoe UI", 10),
        fg=TEXT,
        bg=PANEL,
        **kwargs,
    ):
        return tk.Label(parent, text=text, font=font, fg=fg, bg=bg, **kwargs)

    def _button(
        self,
        parent,
        text,
        command,
        *,
        accent=False,
        danger=False,
        width=None,
        state="normal",
        compact=False,
    ):
        if danger:
            bg = BAD
            fg = "#13070a"
            active = "#ff8790"
        elif accent:
            bg = ACCENT
            fg = "#07140d"
            active = "#60e39a"
        else:
            bg = PANEL_2
            fg = TEXT
            active = "#22384a"

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
            disabledforeground="#607080",
            relief="flat",
            bd=0,
            padx=10 if compact else 15,
            pady=5 if compact else 9,
            font=("Segoe UI Semibold", 9 if compact else 10),
            cursor="hand2",
        )

    def _check(self, parent, text: str, variable: tk.BooleanVar, *, bg=PANEL):
        return tk.Checkbutton(
            parent,
            text=text,
            variable=variable,
            bg=bg,
            fg=TEXT,
            activebackground=bg,
            activeforeground=TEXT,
            selectcolor=PANEL_2,
            font=("Segoe UI", 9),
            bd=0,
            highlightthickness=0,
        )

    def _section_title(self, parent, title: str, subtitle: str | None = None):
        head = tk.Frame(parent, bg=PANEL)
        self._label(
            head,
            title,
            font=("Segoe UI Semibold", 11),
            fg=BORDER,
        ).pack(side="left")
        if subtitle:
            self._label(
                head,
                subtitle,
                font=("Segoe UI", 8),
                fg=MUTED,
            ).pack(side="right")
        return head

    def _stat_box(self, parent, name: str, variable: tk.StringVar, col: int):
        box = tk.Frame(parent, bg=PANEL_2, padx=12, pady=9)
        box.grid(row=0, column=col, sticky="nsew", padx=3)
        self._label(box, name.upper(), font=("Segoe UI Semibold", 7), fg=MUTED, bg=PANEL_2).pack()
        self._label(box, textvariable=variable, font=("Segoe UI Semibold", 14), bg=PANEL_2).pack(pady=(2, 0))

    def _sprite(self, species: int, shiny: bool, *, small: bool = False) -> tk.PhotoImage:
        key = (species, shiny, small)
        if key in self.sprite_cache:
            return self.sprite_cache[key]
        variant = "shiny" if shiny else "normal"
        path = self.sprite_root / variant / f"{species}.png"
        try:
            img = tk.PhotoImage(file=str(path))
            if small:
                img = img.subsample(2, 2)
        except Exception:
            img = tk.PhotoImage(width=40 if small else 80, height=40 if small else 80)
        self.sprite_cache[key] = img
        return img

    def _set_status(self, text: str, *, fg=GOOD, detail: str | None = None):
        self.status_var.set(text)
        self.status_badge.configure(fg=fg)
        if detail is not None:
            self.status_line.configure(text=detail, fg=MUTED if fg == GOOD else fg)

    # ---------- shell / navigation ----------

    def _build_shell(self) -> None:
        root = tk.Frame(self.root, bg=BG)
        root.pack(fill="both", expand=True, padx=12, pady=12)

        header = self._frame(root, bg="#0f1923")
        header.pack(fill="x")

        left = tk.Frame(header, bg="#0f1923")
        left.pack(side="left", padx=16, pady=11)
        self._label(
            left,
            "Pokebot Gen45 PC",
            font=("Segoe UI Semibold", 19),
            bg="#0f1923",
        ).pack(anchor="w")
        self._label(
            left,
            "HGSS • native melonDS RAM + controller bridge",
            font=("Segoe UI", 8),
            fg=MUTED,
            bg="#0f1923",
        ).pack(anchor="w")

        right = tk.Frame(header, bg="#0f1923")
        right.pack(side="right", padx=16, pady=9)
        self.status_badge = tk.Label(
            right,
            textvariable=self.status_var,
            bg=PANEL_2,
            fg=GOOD,
            font=("Segoe UI Semibold", 9),
            padx=13,
            pady=5,
        )
        self.status_badge.pack(anchor="e")
        self._label(
            right,
            textvariable=self.bridge_var,
            font=("Consolas", 8),
            fg=MUTED,
            bg="#0f1923",
        ).pack(anchor="e", pady=(3, 0))

        nav = tk.Frame(root, bg=BG)
        nav.pack(fill="x", pady=(9, 8))
        for tab in self.TABS:
            btn = tk.Button(
                nav,
                text=tab,
                command=lambda t=tab: self._select_tab(t),
                bg=PANEL,
                fg=MUTED,
                activebackground=PANEL_2,
                activeforeground=TEXT,
                relief="flat",
                bd=0,
                padx=14,
                pady=7,
                font=("Segoe UI Semibold", 8),
                cursor="hand2",
            )
            btn.pack(side="left", padx=(0, 4))
            self.nav_buttons[tab] = btn

        self.page_host = tk.Frame(root, bg=BG)
        self.page_host.pack(fill="both", expand=True)

        self.status_line = self._label(
            root,
            "Ready. Launch melonDS, load HeartGold, then start the hunt.",
            font=("Segoe UI", 8),
            fg=MUTED,
            bg=BG,
            anchor="w",
        )
        self.status_line.pack(fill="x", pady=(7, 0))

        for tab in self.TABS:
            frame = tk.Frame(self.page_host, bg=BG)
            frame.place(relx=0, rely=0, relwidth=1, relheight=1)
            self.pages[tab] = frame

    def _select_tab(self, tab: str) -> None:
        self.current_tab = tab
        self.pages[tab].tkraise()
        for name, btn in self.nav_buttons.items():
            if name == tab:
                btn.configure(bg=PANEL_2, fg=BORDER)
            else:
                btn.configure(bg=PANEL, fg=MUTED)

    # ---------- dashboard ----------

    def _build_dashboard(self) -> None:
        page = self.pages["DASHBOARD"]
        page.grid_columnconfigure(0, weight=7)
        page.grid_columnconfigure(1, weight=4)
        page.grid_rowconfigure(2, weight=1)

        current = self._frame(page)
        current.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        current.grid_columnconfigure(0, weight=1)
        self._section_title(current, "CURRENT ENCOUNTER", "HGSS three-starter buffer").grid(
            row=0, column=0, sticky="ew", padx=12, pady=(10, 6)
        )
        self.current_cards_wrap = tk.Frame(current, bg=PANEL)
        self.current_cards_wrap.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 10))
        for i in range(3):
            self.current_cards_wrap.grid_columnconfigure(i, weight=1)

        target = self._frame(page)
        target.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        self._section_title(target, "HUNT TARGET", "RAM-authoritative").pack(
            fill="x", padx=12, pady=(10, 5)
        )

        self._label(
            target,
            "HeartGold / SoulSilver Starters",
            font=("Segoe UI Semibold", 12),
        ).pack(anchor="w", padx=14, pady=(4, 2))
        self._label(
            target,
            "Checks all three starters before selection.",
            font=("Segoe UI", 8),
            fg=MUTED,
        ).pack(anchor="w", padx=14, pady=(0, 7))

        target_checks = tk.Frame(target, bg=PANEL)
        target_checks.pack(fill="x", padx=12)
        for species in STARTER_IDS:
            row = tk.Frame(target_checks, bg=PANEL_2)
            row.pack(fill="x", pady=2)
            shiny = self._sprite(species, True, small=True)
            img = tk.Label(row, image=shiny, bg=PANEL_2)
            img.image = shiny
            img.pack(side="left", padx=(6, 3), pady=2)
            self._check(row, STARTER_NAMES[species], self.target_vars[species], bg=PANEL_2).pack(
                side="left", fill="x", expand=True
            )

        opts = tk.Frame(target, bg=PANEL)
        opts.pack(fill="x", padx=14, pady=(7, 2))
        self._check(opts, "Headless display", self.headless_var).pack(anchor="w")
        self._check(opts, "Mute game audio", self.mute_var).pack(anchor="w")

        actions = tk.Frame(target, bg=PANEL)
        actions.pack(fill="x", padx=12, pady=(8, 12))
        self.start_button = self._button(actions, "START HUNT", self._start_hunt, accent=True)
        self.start_button.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.stop_button = self._button(
            actions, "STOP", self._stop_hunt, danger=True, state="disabled"
        )
        self.stop_button.pack(side="left", fill="x", expand=True, padx=(4, 0))

        stats = self._frame(page, highlightbackground="#274050")
        stats.grid(row=1, column=0, columnspan=2, sticky="ew", pady=10)
        for i in range(8):
            stats.grid_columnconfigure(i, weight=1)
        defs = (
            ("Session Seen", self.session_seen_var),
            ("Session Resets", self.session_resets_var),
            ("Session Shinies", self.session_shinies_var),
            ("Rate", self.rate_var),
            ("Phase Seen", self.phase_seen_var),
            ("Phase Time", self.phase_var),
            ("Lifetime Seen", self.lifetime_seen_var),
            ("Lifetime Shinies", self.lifetime_shinies_var),
        )
        for col, (name, var) in enumerate(defs):
            self._stat_box(stats, name, var, col)

        last_seen = self._frame(page)
        last_seen.grid(row=2, column=0, sticky="nsew", padx=(0, 6))
        last_seen.grid_columnconfigure(0, weight=1)
        last_seen.grid_rowconfigure(1, weight=1)
        self._section_title(last_seen, "LAST SEEN POKÉMON", "Newest first • max 7").grid(
            row=0, column=0, sticky="ew", padx=12, pady=(10, 5)
        )
        self.last_seen_wrap = tk.Frame(last_seen, bg=PANEL)
        self.last_seen_wrap.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))

        phase = self._frame(page)
        phase.grid(row=2, column=1, sticky="nsew", padx=(6, 0))
        self._section_title(phase, "SHINY PHASE", "Current session").pack(
            fill="x", padx=12, pady=(10, 6)
        )
        self.phase_info = tk.Frame(phase, bg=PANEL)
        self.phase_info.pack(fill="both", expand=True, padx=12, pady=(0, 10))
        self._phase_info_row("Mode", "HGSS Starters")
        self.phase_target_value = self._phase_info_row("Targets", "Chikorita / Cyndaquil / Totodile")
        self.phase_cycle_value = self._phase_info_row("Last cycle", self.last_cycle_text)
        self.phase_bridge_value = self._phase_info_row("Backend", "melonDS native UDP :4953")
        self.phase_display_value = self._phase_info_row("Display", "Headless when hunting")
        self.phase_audio_value = self._phase_info_row("Audio", "Muted when hunting")

    def _phase_info_row(self, name: str, value: str):
        row = tk.Frame(self.phase_info, bg=PANEL_2)
        row.pack(fill="x", pady=2)
        self._label(row, name.upper(), font=("Segoe UI Semibold", 7), fg=MUTED, bg=PANEL_2).pack(
            side="left", padx=8, pady=6
        )
        lab = self._label(row, value, font=("Segoe UI", 8), bg=PANEL_2)
        lab.pack(side="right", padx=8, pady=6)
        return lab

    def _render_current(self, mons: list[dict] | None = None) -> None:
        if mons is not None:
            self.current_mons = mons
        for child in self.current_cards_wrap.winfo_children():
            child.destroy()

        rows = self.current_mons
        if not rows:
            for idx, species in enumerate(STARTER_IDS):
                card = self._frame(
                    self.current_cards_wrap,
                    bg=PANEL_2,
                    highlightbackground="#274050",
                )
                card.grid(row=0, column=idx, sticky="nsew", padx=4)
                img = self._sprite(species, False)
                label = tk.Label(card, image=img, bg=PANEL_2)
                label.image = img
                label.pack(pady=(8, 2))
                self._label(
                    card,
                    STARTER_NAMES[species],
                    font=("Segoe UI Semibold", 10),
                    bg=PANEL_2,
                ).pack()
                self._label(
                    card,
                    "Waiting for live PK4",
                    font=("Segoe UI", 8),
                    fg=MUTED,
                    bg=PANEL_2,
                ).pack(pady=(2, 9))
            return

        for idx, mon in enumerate(rows[:3]):
            shiny = bool(mon.get("shiny"))
            card = self._frame(
                self.current_cards_wrap,
                bg=PANEL_2,
                highlightbackground=SHINY if shiny else BORDER,
                highlightthickness=2 if shiny else 1,
            )
            card.grid(row=0, column=idx, sticky="nsew", padx=4)

            img = self._sprite(int(mon["species"]), shiny)
            label = tk.Label(card, image=img, bg=PANEL_2)
            label.image = img
            label.pack(pady=(7, 0))

            name = ("★ " if shiny else "") + str(mon["name"])
            self._label(
                card,
                name,
                font=("Segoe UI Semibold", 11),
                fg=SHINY if shiny else TEXT,
                bg=PANEL_2,
            ).pack()

            ivs = "/".join(str(x) for x in mon.get("ivs", []))
            details = [
                f"PID {mon.get('pid', '-')}",
                f"SV {mon.get('sv', '-')}",
                f"{mon.get('nature', '-')}  •  Ability {mon.get('ability', '-')}",
                f"IVs {ivs}",
                f"Hidden Power {mon.get('hidden_power', '-')}",
            ]
            for line in details:
                self._label(
                    card,
                    line,
                    font=("Consolas", 8),
                    fg=MUTED,
                    bg=PANEL_2,
                ).pack()
            tk.Frame(card, bg=PANEL_2, height=7).pack()

    def _render_last_seen(self) -> None:
        for child in self.last_seen_wrap.winfo_children():
            child.destroy()

        headers = ("", "POKÉMON", "HP", "ATK", "DEF", "SPA", "SPD", "SPE", "SUM", "SV")
        widths = (4, 16, 5, 5, 5, 5, 5, 5, 6, 7)
        head = tk.Frame(self.last_seen_wrap, bg="#0e1822")
        head.pack(fill="x")
        for col, (name, width) in enumerate(zip(headers, widths)):
            self._label(
                head,
                name,
                font=("Segoe UI Semibold", 7),
                fg=MUTED,
                bg="#0e1822",
                width=width,
                anchor="center" if col != 1 else "w",
            ).grid(row=0, column=col, padx=1, pady=4)

        recent = self.stats.snapshot().get("recently_seen", [])[:7]
        if not recent:
            self._label(
                self.last_seen_wrap,
                "No Pokémon recorded yet.",
                font=("Segoe UI", 9),
                fg=MUTED,
                bg=PANEL,
            ).pack(anchor="w", padx=8, pady=16)
            return

        for mon in recent:
            shiny = bool(mon.get("shiny"))
            row = tk.Frame(
                self.last_seen_wrap,
                bg=PANEL_2,
                highlightthickness=1 if shiny else 0,
                highlightbackground=SHINY,
            )
            row.pack(fill="x", pady=1)

            img = self._sprite(int(mon["species"]), shiny, small=True)
            lab = tk.Label(row, image=img, bg=PANEL_2, width=40)
            lab.image = img
            lab.grid(row=0, column=0, padx=1, pady=1)

            self._label(
                row,
                ("★ " if shiny else "") + str(mon.get("name", mon["species"])),
                font=("Segoe UI Semibold", 8),
                fg=SHINY if shiny else TEXT,
                bg=PANEL_2,
                width=16,
                anchor="w",
            ).grid(row=0, column=1, padx=1)

            ivs = list(mon.get("ivs", []))
            while len(ivs) < 6:
                ivs.append(0)
            for i, val in enumerate(ivs[:6]):
                iv_fg = GOOD if val == 31 else (WARN if val >= 25 else TEXT)
                self._label(
                    row,
                    str(val),
                    font=("Consolas", 8),
                    fg=iv_fg,
                    bg=PANEL_2,
                    width=5,
                ).grid(row=0, column=2 + i, padx=1)

            self._label(
                row,
                str(sum(ivs[:6])),
                font=("Consolas", 8),
                fg=BORDER,
                bg=PANEL_2,
                width=6,
            ).grid(row=0, column=8, padx=1)
            self._label(
                row,
                str(mon.get("sv", "-")),
                font=("Consolas", 8),
                fg=SHINY if shiny else TEXT,
                bg=PANEL_2,
                width=7,
            ).grid(row=0, column=9, padx=1)

            tooltip = (
                f"{mon.get('name', '')} | {mon.get('nature', '-')} | PID {mon.get('pid', '-')} | "
                f"IVs {'/'.join(map(str, ivs[:6]))} | SUM {sum(ivs[:6])} | "
                f"SV {mon.get('sv', '-')} | HP {mon.get('hidden_power', '-')}"
            )
            row.bind("<Enter>", lambda _e, t=tooltip: self.status_line.configure(text=t))
            row.bind("<Leave>", lambda _e: self.status_line.configure(text="Ready."))

    # ---------- hunts page ----------

    def _build_hunts(self) -> None:
        page = self.pages["HUNTS"]
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1)

        selector = self._frame(page)
        selector.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self._section_title(selector, "HUNT MODES", "One shared dashboard").pack(
            fill="x", padx=12, pady=(10, 6)
        )

        modes = tk.Frame(selector, bg=PANEL)
        modes.pack(fill="x", padx=10, pady=(0, 10))
        for i, mode in enumerate(("Starters", "Wild", "Static")):
            modes.grid_columnconfigure(i, weight=1)
            card = self._frame(modes, bg=PANEL_2, highlightbackground="#274050")
            card.grid(row=0, column=i, sticky="nsew", padx=4)

            title = mode.upper()
            status = "IMPLEMENTED" if mode == "Starters" else "UI READY • BACKEND NEXT"
            self._label(card, title, font=("Segoe UI Semibold", 12), bg=PANEL_2).pack(
                anchor="w", padx=10, pady=(9, 2)
            )
            self._label(
                card,
                status,
                font=("Segoe UI Semibold", 7),
                fg=GOOD if mode == "Starters" else WARN,
                bg=PANEL_2,
            ).pack(anchor="w", padx=10)
            desc = {
                "Starters": "HGSS Chikorita / Cyndaquil / Totodile pre-selection RAM check.",
                "Wild": "Wild encounter mode shares Current Encounter and Last Seen.",
                "Static": "Static/gift targets share the same target and phase UI.",
            }[mode]
            self._label(
                card,
                desc,
                font=("Segoe UI", 8),
                fg=MUTED,
                bg=PANEL_2,
                wraplength=320,
                justify="left",
            ).pack(anchor="w", padx=10, pady=(4, 7))
            btn = self._button(card, f"Select {mode}", lambda m=mode: self._select_mode(m), compact=True)
            btn.pack(anchor="w", padx=10, pady=(0, 9))
            self.mode_buttons[mode] = btn

        starter = self._frame(page)
        starter.grid(row=1, column=0, sticky="nsew")
        self._section_title(starter, "STARTER TARGETS", "Multiple targets allowed").pack(
            fill="x", padx=12, pady=(10, 6)
        )

        grid = tk.Frame(starter, bg=PANEL)
        grid.pack(fill="x", padx=10)
        for i, species in enumerate(STARTER_IDS):
            grid.grid_columnconfigure(i, weight=1)
            card = self._frame(grid, bg=PANEL_2, highlightbackground="#274050")
            card.grid(row=0, column=i, sticky="nsew", padx=4)
            sprites = tk.Frame(card, bg=PANEL_2)
            sprites.pack(pady=(8, 3))
            for shiny in (False, True):
                img = self._sprite(species, shiny)
                lab = tk.Label(sprites, image=img, bg=PANEL_2)
                lab.image = img
                lab.pack(side="left", padx=4)
            self._check(card, STARTER_NAMES[species], self.target_vars[species], bg=PANEL_2).pack(
                anchor="center", pady=(0, 8)
            )

        options = tk.Frame(starter, bg=PANEL)
        options.pack(fill="x", padx=12, pady=10)
        self._check(options, "Headless display while hunting", self.headless_var).pack(side="left", padx=(0, 16))
        self._check(options, "Mute game audio while hunting", self.mute_var).pack(side="left")

        actions = tk.Frame(starter, bg=PANEL)
        actions.pack(fill="x", padx=12, pady=(0, 12))
        self.hunts_start_button = self._button(actions, "START HUNT", self._start_hunt, accent=True)
        self.hunts_start_button.pack(side="left", padx=(0, 6))
        self.hunts_stop_button = self._button(actions, "STOP", self._stop_hunt, danger=True, state="disabled")
        self.hunts_stop_button.pack(side="left")

    def _select_mode(self, mode: str) -> None:
        self.current_mode = mode
        for name, btn in self.mode_buttons.items():
            if name == mode:
                btn.configure(bg=BORDER, fg="#06151a", activebackground="#63e6f6")
            else:
                btn.configure(bg=PANEL_2, fg=TEXT, activebackground="#22384a")

        if mode == "Starters":
            enabled = self.worker is None
            self.start_button.configure(state="normal" if enabled else "disabled")
            self.hunts_start_button.configure(state="normal" if enabled else "disabled")
            self.status_line.configure(text="HGSS three-starter RAM hunter selected.", fg=MUTED)
        elif mode == "Wild":
            self.start_button.configure(state="disabled")
            self.hunts_start_button.configure(state="disabled")
            self.status_line.configure(
                text="Wild mode is laid out in the ORAS-style UI; Gen IV wild backend is next.",
                fg=WARN,
            )
        else:
            self.start_button.configure(state="disabled")
            self.hunts_start_button.configure(state="disabled")
            self.status_line.configure(
                text="Static mode is laid out in the ORAS-style UI; Gen IV static backend is next.",
                fg=WARN,
            )

    # ---------- statistics ----------

    def _build_statistics(self) -> None:
        page = self.pages["STATISTICS"]
        page.grid_columnconfigure(0, weight=1)
        page.grid_columnconfigure(1, weight=1)

        session = self._frame(page)
        session.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        self._section_title(session, "SESSION STATISTICS").pack(fill="x", padx=12, pady=(10, 8))
        self._stats_detail_rows(
            session,
            [
                ("Pokémon seen", self.session_seen_var),
                ("Resets", self.session_resets_var),
                ("Shinies", self.session_shinies_var),
                ("Encounter rate", self.rate_var),
                ("Phase seen", self.phase_seen_var),
                ("Phase time", self.phase_var),
            ],
        )

        life = self._frame(page)
        life.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        self._section_title(life, "LIFETIME STATISTICS").pack(fill="x", padx=12, pady=(10, 8))
        self._stats_detail_rows(
            life,
            [
                ("Pokémon seen", self.lifetime_seen_var),
                ("Resets", self.lifetime_resets_var),
                ("Shinies", self.lifetime_shinies_var),
            ],
        )
        self._label(
            life,
            "Lifetime values persist in Local AppData and are not cleared when the UI restarts.",
            font=("Segoe UI", 8),
            fg=MUTED,
            wraplength=520,
            justify="left",
        ).pack(anchor="w", padx=14, pady=12)

        recent = self._frame(page)
        recent.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(10, 0))
        self._section_title(recent, "RECENT SHINIES", "Persistent history will populate from live finds").pack(
            fill="x", padx=12, pady=(10, 8)
        )
        self.recent_shiny_wrap = tk.Frame(recent, bg=PANEL)
        self.recent_shiny_wrap.pack(fill="x", padx=12, pady=(0, 12))
        self._render_recent_shinies()

    def _stats_detail_rows(self, parent, pairs):
        for name, var in pairs:
            row = tk.Frame(parent, bg=PANEL_2)
            row.pack(fill="x", padx=12, pady=2)
            self._label(row, name, font=("Segoe UI", 9), fg=MUTED, bg=PANEL_2).pack(
                side="left", padx=9, pady=7
            )
            self._label(
                row,
                textvariable=var,
                font=("Segoe UI Semibold", 10),
                bg=PANEL_2,
            ).pack(side="right", padx=9, pady=7)

    def _render_recent_shinies(self) -> None:
        if not hasattr(self, "recent_shiny_wrap"):
            return
        for child in self.recent_shiny_wrap.winfo_children():
            child.destroy()
        shinies = [
            x for x in self.stats.snapshot().get("recently_seen", [])
            if x.get("shiny")
        ][:8]
        if not shinies:
            self._label(
                self.recent_shiny_wrap,
                "No shiny Pokémon recorded yet.",
                font=("Segoe UI", 9),
                fg=MUTED,
            ).pack(anchor="w", pady=10)
            return
        for mon in shinies:
            row = tk.Frame(self.recent_shiny_wrap, bg=PANEL_2)
            row.pack(fill="x", pady=2)
            img = self._sprite(int(mon["species"]), True, small=True)
            lab = tk.Label(row, image=img, bg=PANEL_2)
            lab.image = img
            lab.pack(side="left", padx=5)
            self._label(
                row,
                f"★ {mon.get('name', mon['species'])}",
                font=("Segoe UI Semibold", 9),
                fg=SHINY,
                bg=PANEL_2,
            ).pack(side="left")
            self._label(
                row,
                f"PID {mon.get('pid', '-')}  |  SV {mon.get('sv', '-')}  |  "
                f"{mon.get('nature', '-')}  |  IVs {'/'.join(map(str, mon.get('ivs', [])))}",
                font=("Consolas", 8),
                fg=MUTED,
                bg=PANEL_2,
            ).pack(side="right", padx=9)

    # ---------- tools/settings/support ----------

    def _build_tools(self) -> None:
        page = self.pages["TOOLS"]
        page.grid_columnconfigure(0, weight=1)
        page.grid_columnconfigure(1, weight=1)

        emu = self._frame(page)
        emu.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        self._section_title(emu, "MELONDS").pack(fill="x", padx=12, pady=(10, 8))
        self._button(emu, "Launch melonDS", self._launch_melonds, accent=True).pack(
            anchor="w", padx=12, pady=4
        )
        self._button(emu, "Test native bridge", self._test_bridge).pack(
            anchor="w", padx=12, pady=4
        )
        self._label(
            emu,
            "Native loopback bridge: 127.0.0.1:4953",
            font=("Consolas", 8),
            fg=MUTED,
        ).pack(anchor="w", padx=12, pady=(8, 12))

        control = self._frame(page)
        control.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        self._section_title(control, "PRESENTATION CONTROLS").pack(fill="x", padx=12, pady=(10, 8))
        row1 = tk.Frame(control, bg=PANEL)
        row1.pack(fill="x", padx=12, pady=4)
        self._button(row1, "Display OFF", lambda: self._manual_presentation("display", False), compact=True).pack(
            side="left", padx=(0, 4)
        )
        self._button(row1, "Display ON", lambda: self._manual_presentation("display", True), compact=True).pack(
            side="left"
        )
        row2 = tk.Frame(control, bg=PANEL)
        row2.pack(fill="x", padx=12, pady=4)
        self._button(row2, "Sound OFF", lambda: self._manual_presentation("audio", False), compact=True).pack(
            side="left", padx=(0, 4)
        )
        self._button(row2, "Sound ON", lambda: self._manual_presentation("audio", True), compact=True).pack(
            side="left"
        )

    def _build_settings(self) -> None:
        page = self.pages["SETTINGS"]
        settings = self._frame(page)
        settings.pack(fill="both", expand=True)
        self._section_title(settings, "HUNT SETTINGS", "Applied to the next hunt").pack(
            fill="x", padx=12, pady=(10, 8)
        )
        self._check(settings, "Disable melonDS display rendering while hunting", self.headless_var).pack(
            anchor="w", padx=14, pady=4
        )
        self._check(settings, "Mute melonDS game audio while hunting", self.mute_var).pack(
            anchor="w", padx=14, pady=4
        )
        self._label(
            settings,
            "Both are restored automatically on target, safety hold, Stop, window close or unexpected error.",
            font=("Segoe UI", 8),
            fg=MUTED,
            wraplength=760,
            justify="left",
        ).pack(anchor="w", padx=14, pady=(6, 12))

    def _build_testing_support(self) -> None:
        page = self.pages["TESTING & SUPPORT"]
        box = self._frame(page)
        box.pack(fill="both", expand=True)
        self._section_title(box, "TESTING & SUPPORT").pack(fill="x", padx=12, pady=(10, 8))

        items = [
            ("Backend", "Native melonDS UDP bridge"),
            ("Bridge address", "127.0.0.1:4953"),
            ("Target ROM", "Pokémon HeartGold Europe v10"),
            ("ROM SHA1", "EB47AB4BA0326AE842135F62C7EC68CF85C9785F"),
            ("Stats", str(self.stats.path)),
            ("Legacy console", str(self.package_root / "tools" / "Legacy-Console.bat")),
        ]
        for name, value in items:
            row = tk.Frame(box, bg=PANEL_2)
            row.pack(fill="x", padx=12, pady=2)
            self._label(row, name, font=("Segoe UI Semibold", 8), fg=MUTED, bg=PANEL_2).pack(
                side="left", padx=8, pady=7
            )
            self._label(row, value, font=("Consolas", 8), bg=PANEL_2).pack(
                side="right", padx=8, pady=7
            )

        actions = tk.Frame(box, bg=PANEL)
        actions.pack(fill="x", padx=12, pady=10)
        self._button(actions, "Open AppData folder", self._open_appdata, compact=True).pack(side="left")
        self._button(actions, "Test Bridge", self._test_bridge, compact=True).pack(side="left", padx=6)

    # ---------- actions ----------

    def _launch_melonds(self) -> None:
        if not self.emulator_path.exists():
            messagebox.showerror("melonDS not found", f"Missing:\n{self.emulator_path}")
            return
        try:
            subprocess.Popen([str(self.emulator_path)], cwd=str(self.emulator_path.parent))
            self.status_line.configure(text="melonDS launched.", fg=MUTED)
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

    def _manual_presentation(self, which: str, enabled: bool) -> None:
        def run():
            try:
                backend = MelonDSUDPBackend(timeout=1.0)
                if which == "display":
                    backend.set_display(enabled)
                else:
                    backend.set_audio(enabled)
                self.events.put({
                    "type": "manual_presentation",
                    "which": which,
                    "enabled": enabled,
                    "ok": True,
                })
            except Exception as exc:
                self.events.put({
                    "type": "manual_presentation",
                    "which": which,
                    "enabled": enabled,
                    "ok": False,
                    "message": str(exc),
                })
        threading.Thread(target=run, daemon=True).start()

    def _open_appdata(self) -> None:
        path = self.stats.path.parent
        try:
            os.startfile(str(path))
        except Exception as exc:
            messagebox.showerror("Could not open folder", str(exc))

    def _start_hunt(self) -> None:
        if self.worker is not None or self.current_mode != "Starters":
            return

        targets = {species for species, var in self.target_vars.items() if var.get()}
        if not targets:
            messagebox.showwarning("No targets", "Select at least one starter.")
            return

        target_text = " / ".join(STARTER_NAMES[x] for x in sorted(targets))
        self.phase_target_value.configure(text=target_text)

        self.stop_event = threading.Event()
        now = time.monotonic()
        self.active_started = now
        if self.phase_started is None:
            self.phase_started = now

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
        self.hunts_start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.hunts_stop_button.configure(state="normal")
        self._set_status("HUNTING", fg=GOOD, detail="Starting HGSS starter hunt…")

    def _stop_hunt(self) -> None:
        if self.worker is None:
            return
        self.stop_event.set()
        self.stop_button.configure(state="disabled")
        self.hunts_stop_button.configure(state="disabled")
        self._set_status(
            "STOPPING",
            fg=WARN,
            detail="Stopping hunt, releasing input and restoring display/audio…",
        )

    def _freeze_active_timers(self) -> None:
        now = time.monotonic()
        if self.active_started is not None:
            self.active_elapsed += now - self.active_started
            self.active_started = None
        if self.phase_started is not None:
            self.phase_elapsed += now - self.phase_started
            self.phase_started = None

    # ---------- event handling ----------

    def _handle_event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "connected":
            self.bridge_var.set(f"Bridge: {event['bridge']}")
            self.status_line.configure(text="Native bridge connected. Hunting.", fg=MUTED)

        elif kind == "bridge":
            if event["ok"]:
                self.bridge_var.set(f"Bridge: {event['value']}")
                self.status_line.configure(text="Native melonDS bridge is responding.", fg=MUTED)
            else:
                self.bridge_var.set("Bridge: offline")
                self.status_line.configure(text=event["value"], fg=BAD)

        elif kind == "presentation":
            if "display" in event:
                self.phase_display_value.configure(
                    text="OFF • headless" if not event["display"] else "ON"
                )
            if "audio" in event:
                self.phase_audio_value.configure(
                    text="MUTED" if not event["audio"] else "ON"
                )

        elif kind == "manual_presentation":
            if event["ok"]:
                state = "ON" if event["enabled"] else "OFF"
                label = "Display" if event["which"] == "display" else "Sound"
                self.status_line.configure(text=f"{label} {state}.", fg=MUTED)
                if event["which"] == "display":
                    self.phase_display_value.configure(text=state)
                else:
                    self.phase_audio_value.configure(
                        text="ON" if event["enabled"] else "MUTED"
                    )
            else:
                self.status_line.configure(text=event["message"], fg=BAD)

        elif kind == "set":
            mons = event["mons"]
            self.stats.record_set(mons)
            self.last_cycle_text = (
                f"{event['seconds']:.2f}s • {event['address']} • {event['source']}"
            )
            self.phase_cycle_value.configure(text=self.last_cycle_text)
            self._render_current(mons)
            self._refresh_stats()
            self._render_last_seen()
            self._render_recent_shinies()

        elif kind == "reset":
            self.stats.record_reset()
            self._refresh_stats()

        elif kind == "duplicate":
            self.status_line.configure(
                text=f"Duplicate starter set ignored for stats (streak {event['streak']}).",
                fg=WARN,
            )

        elif kind == "target":
            self._freeze_active_timers()
            mons = event["mons"]
            names = ", ".join(mon["name"] for mon in mons)
            self.last_hunt_status = "SHINY FOUND"
            self._set_status(
                "SHINY FOUND",
                fg=SHINY,
                detail=f"Target found: {names}. melonDS display and sound restored.",
            )
            self.phase_display_value.configure(text="ON • target found")
            self.phase_audio_value.configure(text="ON • target found")
            self._render_last_seen()
            self._render_recent_shinies()

        elif kind == "safety":
            self._freeze_active_timers()
            self.last_hunt_status = "Safety Hold"
            self._set_status("SAFETY HOLD", fg=WARN, detail=event["message"])

        elif kind == "error":
            self._freeze_active_timers()
            self.last_hunt_status = "Error"
            self._set_status("ERROR", fg=BAD, detail=event["message"])

        elif kind == "stopped":
            self._freeze_active_timers()
            self.worker = None
            enabled = self.current_mode == "Starters"
            self.start_button.configure(state="normal" if enabled else "disabled")
            self.hunts_start_button.configure(state="normal" if enabled else "disabled")
            self.stop_button.configure(state="disabled")
            self.hunts_stop_button.configure(state="disabled")
            if self.status_var.get() in {"HUNTING", "STOPPING"}:
                self._set_status(
                    "IDLE",
                    fg=GOOD,
                    detail="Hunt stopped. melonDS display/audio restored.",
                )
            self.phase_display_value.configure(text="ON")
            self.phase_audio_value.configure(text="ON")

    def _refresh_stats(self) -> None:
        data = self.stats.snapshot()
        session = data["session"]
        lifetime = data["lifetime"]
        self.session_seen_var.set(str(session["seen"]))
        self.session_resets_var.set(str(session["resets"]))
        self.session_shinies_var.set(str(session["shinies"]))
        self.phase_seen_var.set(str(session["seen"]))
        self.lifetime_seen_var.set(str(lifetime["seen"]))
        self.lifetime_shinies_var.set(str(lifetime["shinies"]))
        self.lifetime_resets_var.set(str(lifetime["resets"]))

    def _tick(self) -> None:
        now = time.monotonic()
        active = self.active_elapsed
        if self.active_started is not None:
            active += now - self.active_started

        phase = self.phase_elapsed
        if self.phase_started is not None:
            phase += now - self.phase_started

        seen = int(self.stats.snapshot()["session"]["seen"])
        rate = seen / (max(1.0, active) / 3600.0) if active > 0 else 0.0
        self.rate_var.set(f"{rate:.1f} / hr")
        self.phase_var.set(_fmt_duration(phase))

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
