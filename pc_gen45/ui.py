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
    load_ability_names,
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
ANTI = "#c58cff"

STARTER_IDS = (152, 155, 158)
STARTER_NAMES = {152: "Chikorita", 155: "Cyndaquil", 158: "Totodile"}


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _mon_dict(mon, names: dict[int, str], abilities: dict[int, str]) -> dict:
    return {
        "species": mon.species,
        "name": names.get(mon.species, f"Species {mon.species}"),
        "pid": f"{mon.pid:08X}",
        "sv": mon.shiny_value,
        "nature": mon.nature,
        "ability": abilities.get(mon.ability, f"Ability {mon.ability}"),
        "ability_id": mon.ability,
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
        self.abilities = load_ability_names()

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
                    mon_rows = [_mon_dict(mon, self.names, self.abilities) for mon in mons]
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
                            mons=[_mon_dict(mon, self.names, self.abilities) for mon in targets],
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
        self.root.geometry("1280x800")
        self.root.minsize(1120, 700)
        self.root.configure(bg=BG)

        self.package_root = Path(__file__).resolve().parent.parent
        self.sprite_root = self.package_root / "assets" / "sprites" / "hgss"
        self.emulator_path = self.package_root / "emulator" / "melonDS.exe"

        self.stats = StatsStore()
        self.abilities = load_ability_names()
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
        self.phase_target_seen = 0

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
        self.best_iv_sum_var = tk.StringVar(value="--")
        self.worst_iv_sum_var = tk.StringVar(value="--")
        self.best_sv_var = tk.StringVar(value="--")
        self.worst_sv_var = tk.StringVar(value="--")
        self.effective_rolls_var = tk.StringVar(value="0")
        self.odds_chance_var = tk.StringVar(value="0.00%")
        self.odds_eta_var = tk.StringVar(value="--")

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
        self._render_recent_shinies()
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
        root.pack(fill="both", expand=True, padx=8, pady=8)

        # Gen3-style overlay header: compact, informational, no oversized banner.
        header = tk.Frame(root, bg="#0d1720")
        header.pack(fill="x")

        self._label(
            header,
            "Pokebot Gen45 PC",
            font=("Segoe UI Semibold", 13),
            bg="#0d1720",
        ).pack(side="left", padx=(10, 8), pady=7)
        self._label(
            header,
            "HGSS • melonDS native bridge",
            font=("Segoe UI", 8),
            fg=MUTED,
            bg="#0d1720",
        ).pack(side="left", pady=7)

        self.status_badge = tk.Label(
            header,
            textvariable=self.status_var,
            bg=PANEL_2,
            fg=GOOD,
            font=("Segoe UI Semibold", 8),
            padx=9,
            pady=3,
        )
        self.status_badge.pack(side="right", padx=(5, 10), pady=5)
        self._label(
            header,
            textvariable=self.bridge_var,
            font=("Consolas", 7),
            fg=MUTED,
            bg="#0d1720",
        ).pack(side="right", pady=7)

        nav = tk.Frame(root, bg=BG)
        nav.pack(fill="x", pady=(5, 5))
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
                padx=10,
                pady=4,
                font=("Segoe UI Semibold", 7),
                cursor="hand2",
            )
            btn.pack(side="left", padx=(0, 3))
            self.nav_buttons[tab] = btn

        self.page_host = tk.Frame(root, bg=BG)
        self.page_host.pack(fill="both", expand=True)

        self.status_line = self._label(
            root,
            "Ready. Launch melonDS, load HeartGold, then start the hunt.",
            font=("Segoe UI", 7),
            fg=MUTED,
            bg=BG,
            anchor="w",
        )
        self.status_line.pack(fill="x", pady=(4, 0))

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
        page.grid_columnconfigure(0, weight=3)
        page.grid_columnconfigure(1, weight=5)
        page.grid_columnconfigure(2, weight=3)
        page.grid_rowconfigure(1, weight=1)

        # -----------------------------------------------------------------
        # Hunt Control
        # -----------------------------------------------------------------
        control = self._frame(page)
        control.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        self._section_title(control, "HUNT CONTROL", "HGSS").pack(
            fill="x", padx=9, pady=(7, 4)
        )

        info = tk.Frame(control, bg=PANEL)
        info.pack(fill="x", padx=9)

        def info_row(name: str, value: str):
            row = tk.Frame(info, bg=PANEL_2)
            row.pack(fill="x", pady=1)
            self._label(
                row, name.upper(), font=("Segoe UI Semibold", 7),
                fg=MUTED, bg=PANEL_2
            ).pack(side="left", padx=7, pady=4)
            self._label(
                row, value, font=("Segoe UI Semibold", 8), bg=PANEL_2
            ).pack(side="right", padx=7, pady=4)

        info_row("Game", "HeartGold")
        info_row("Mode", "Starters")
        info_row("Method", "3 Pokémon / reset")

        self._label(
            control, "TARGETS", font=("Segoe UI Semibold", 7), fg=MUTED
        ).pack(anchor="w", padx=10, pady=(6, 1))
        target_line = tk.Frame(control, bg=PANEL)
        target_line.pack(fill="x", padx=8)
        for species in STARTER_IDS:
            self._check(
                target_line,
                STARTER_NAMES[species],
                self.target_vars[species],
                bg=PANEL,
            ).pack(anchor="w")

        option_line = tk.Frame(control, bg=PANEL)
        option_line.pack(fill="x", padx=8, pady=(4, 1))
        self._check(option_line, "Headless display", self.headless_var).pack(anchor="w")
        self._check(option_line, "Mute game audio", self.mute_var).pack(anchor="w")

        actions = tk.Frame(control, bg=PANEL)
        actions.pack(fill="x", padx=8, pady=(6, 8))
        self.start_button = self._button(
            actions, "START", self._start_hunt, accent=True, compact=True
        )
        self.start_button.pack(side="left", fill="x", expand=True, padx=(0, 3))
        self.stop_button = self._button(
            actions, "STOP", self._stop_hunt, danger=True,
            compact=True, state="disabled"
        )
        self.stop_button.pack(side="left", fill="x", expand=True, padx=(3, 0))

        # -----------------------------------------------------------------
        # Current Encounter
        # -----------------------------------------------------------------
        current = self._frame(page)
        current.grid(row=0, column=1, sticky="nsew", padx=4)
        self._section_title(current, "CURRENT ENCOUNTER", "Live PK4").pack(
            fill="x", padx=9, pady=(7, 3)
        )
        self.current_cards_wrap = tk.Frame(current, bg=PANEL)
        self.current_cards_wrap.pack(fill="both", expand=True, padx=8, pady=(0, 7))

        # -----------------------------------------------------------------
        # Shiny Phase -- now owns all compact hunt telemetry.
        # -----------------------------------------------------------------
        phase = self._frame(page)
        phase.grid(row=0, column=2, sticky="nsew", padx=(4, 0))
        self._section_title(phase, "SHINY PHASE", "1 / 8,192 per Pokémon").pack(
            fill="x", padx=9, pady=(7, 3)
        )

        phase_grid = tk.Frame(phase, bg=PANEL)
        phase_grid.pack(fill="x", padx=8)

        phase_pairs = (
            ("PHASE SEEN", self.phase_seen_var),
            ("TIME", self.phase_var),
            ("ROLLS", self.effective_rolls_var),
            ("RATE", self.rate_var),
            ("CHANCE", self.odds_chance_var),
            ("ETA TO ODDS", self.odds_eta_var),
            ("LIFETIME SEEN", self.lifetime_seen_var),
            ("LIFETIME SHINIES", self.lifetime_shinies_var),
            ("BEST IV Σ", self.best_iv_sum_var),
            ("WORST IV Σ", self.worst_iv_sum_var),
            ("BEST SV", self.best_sv_var),
            ("WORST SV", self.worst_sv_var),
        )
        for idx, (name, var) in enumerate(phase_pairs):
            cell = tk.Frame(phase_grid, bg=PANEL_2)
            cell.grid(
                row=idx // 2, column=idx % 2, sticky="nsew",
                padx=2, pady=2
            )
            phase_grid.grid_columnconfigure(idx % 2, weight=1)
            self._label(
                cell, name, font=("Segoe UI Semibold", 6),
                fg=MUTED, bg=PANEL_2
            ).pack(pady=(3, 0))
            self._label(
                cell, textvariable=var, font=("Segoe UI Semibold", 9),
                bg=PANEL_2
            ).pack(pady=(0, 3))

        self.odds_canvas = tk.Canvas(
            phase, height=10, bg="#0a1118", highlightthickness=0
        )
        self.odds_canvas.pack(fill="x", padx=10, pady=(6, 1))
        self.odds_bar_rect = self.odds_canvas.create_rectangle(
            0, 0, 0, 10, fill=GOOD, outline=""
        )
        self.odds_bar_text = self._label(
            phase, "0 / 8,192 target rolls",
            font=("Segoe UI", 7), fg=MUTED
        )
        self.odds_bar_text.pack(anchor="w", padx=10, pady=(0, 4))

        self.phase_info = tk.Frame(phase, bg=PANEL)
        self.phase_info.pack(fill="x", padx=8, pady=(0, 7))
        self.phase_target_value = self._phase_info_row(
            "Targets", "Chikorita / Cyndaquil / Totodile"
        )
        self.phase_cycle_value = self._phase_info_row(
            "Last cycle", self.last_cycle_text
        )

        # -----------------------------------------------------------------
        # Lower half: Encounter Log | Recent Shinies.
        # Removing the stats strip gives both panels the reclaimed space.
        # -----------------------------------------------------------------
        lower = tk.Frame(page, bg=BG)
        lower.grid(row=1, column=0, columnspan=3, sticky="nsew", pady=(6, 0))
        lower.grid_columnconfigure(0, weight=1, uniform="lower")
        lower.grid_columnconfigure(1, weight=1, uniform="lower")
        lower.grid_rowconfigure(0, weight=1)

        encounter_log = self._frame(lower)
        encounter_log.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        encounter_log.grid_columnconfigure(0, weight=1)
        encounter_log.grid_rowconfigure(1, weight=1)
        self._section_title(
            encounter_log,
            "ENCOUNTER LOG",
            "Newest first"
        ).grid(row=0, column=0, sticky="ew", padx=9, pady=(7, 3))
        self.last_seen_wrap = tk.Frame(encounter_log, bg=PANEL)
        self.last_seen_wrap.grid(
            row=1, column=0, sticky="nsew", padx=7, pady=(0, 7)
        )

        recent_shiny = self._frame(lower)
        recent_shiny.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        recent_shiny.grid_columnconfigure(0, weight=1)
        recent_shiny.grid_rowconfigure(1, weight=1)
        self._section_title(
            recent_shiny,
            "RECENT SHINIES",
            "Persistent lifetime history"
        ).grid(row=0, column=0, sticky="ew", padx=9, pady=(7, 3))
        self.dashboard_shiny_wrap = tk.Frame(recent_shiny, bg=PANEL)
        self.dashboard_shiny_wrap.grid(
            row=1, column=0, sticky="nsew", padx=7, pady=(0, 7)
        )
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

        if not self.current_mons:
            wait = tk.Frame(self.current_cards_wrap, bg=PANEL_2)
            wait.pack(fill="both", expand=True)
            self._label(
                wait,
                "Waiting for starter set",
                font=("Segoe UI Semibold", 13),
                bg=PANEL_2,
            ).pack(pady=(24, 3))
            self._label(
                wait,
                "Chikorita, Cyndaquil and Totodile will all appear here once the RAM-valid set is ready.",
                font=("Segoe UI", 8),
                fg=MUTED,
                bg=PANEL_2,
                wraplength=520,
                justify="center",
            ).pack()
            mini = tk.Frame(wait, bg=PANEL_2)
            mini.pack(pady=10)
            for species in STARTER_IDS:
                img = self._sprite(species, False, small=True)
                lab = tk.Label(mini, image=img, bg=PANEL_2)
                lab.image = img
                lab.pack(side="left", padx=6)
            return

        # HG/SS exposes all three starters before selection. Treat the full
        # triplet as the current encounter and give every starter its own
        # complete telemetry card.
        cards = tk.Frame(self.current_cards_wrap, bg=PANEL)
        cards.pack(fill="both", expand=True)
        for col in range(3):
            cards.grid_columnconfigure(col, weight=1, uniform="starter")
        cards.grid_rowconfigure(0, weight=1)

        ordered = sorted(
            self.current_mons[:3],
            key=lambda mon: STARTER_IDS.index(int(mon.get("species", 0)))
            if int(mon.get("species", 0)) in STARTER_IDS else 99,
        )

        for col, mon in enumerate(ordered):
            shiny = bool(mon.get("shiny"))
            sv = int(mon.get("sv", 99999))
            border = SHINY if shiny else BORDER

            card = tk.Frame(
                cards,
                bg=PANEL_2,
                highlightthickness=2 if shiny else 1,
                highlightbackground=border,
            )
            card.grid(
                row=0,
                column=col,
                sticky="nsew",
                padx=(0 if col == 0 else 3, 0 if col == 2 else 3),
            )

            head = tk.Frame(card, bg=PANEL_2)
            head.pack(fill="x", padx=6, pady=(6, 2))

            img = self._sprite(int(mon["species"]), shiny)
            sprite = tk.Label(head, image=img, bg=PANEL_2)
            sprite.image = img
            sprite.pack()

            self._label(
                head,
                ("★ " if shiny else "") + str(mon.get("name", "Pokémon")),
                font=("Segoe UI Semibold", 10),
                fg=SHINY if shiny else TEXT,
                bg=PANEL_2,
            ).pack(pady=(0, 1))

            if shiny:
                self._label(
                    head,
                    "SHINY",
                    font=("Segoe UI Semibold", 7),
                    fg=SHINY,
                    bg=PANEL_2,
                ).pack()

            meta = tk.Frame(card, bg=PANEL_2)
            meta.pack(fill="x", padx=5, pady=(1, 3))
            meta_pairs = (
                ("PID", mon.get("pid", "-")),
                ("Nature", mon.get("nature", "-")),
                ("Ability", mon.get("ability", "-")),
                ("Hidden Power", mon.get("hidden_power", "-")),
                ("SV", mon.get("sv", "-")),
            )
            for idx, (name, value) in enumerate(meta_pairs):
                cell = tk.Frame(meta, bg="#152431")
                cell.grid(
                    row=idx,
                    column=0,
                    sticky="ew",
                    pady=1,
                )
                meta.grid_columnconfigure(0, weight=1)

                self._label(
                    cell,
                    name.upper(),
                    font=("Segoe UI Semibold", 6),
                    fg=MUTED,
                    bg="#152431",
                ).pack(side="left", padx=5, pady=3)

                value_fg = TEXT
                if name == "SV":
                    if sv < 8:
                        value_fg = GOOD
                    elif sv >= 65528:
                        value_fg = ANTI

                self._label(
                    cell,
                    str(value),
                    font=("Consolas", 7),
                    fg=value_fg,
                    bg="#152431",
                    anchor="e",
                ).pack(side="right", padx=5, pady=3)

            self._label(
                card,
                "IVS",
                font=("Segoe UI Semibold", 6),
                fg=MUTED,
                bg=PANEL_2,
            ).pack(anchor="w", padx=6, pady=(3, 1))

            ivrow = tk.Frame(card, bg=PANEL_2)
            ivrow.pack(fill="x", padx=4)

            iv_names = ("HP", "ATK", "DEF", "SPA", "SPD", "SPE")
            ivs = list(mon.get("ivs", []))
            while len(ivs) < 6:
                ivs.append(0)

            for idx, (name, value) in enumerate(zip(iv_names, ivs[:6])):
                cell = tk.Frame(ivrow, bg="#152431")
                cell.grid(row=0, column=idx, sticky="ew", padx=1)
                ivrow.grid_columnconfigure(idx, weight=1)

                self._label(
                    cell,
                    name,
                    font=("Segoe UI Semibold", 5),
                    fg=MUTED,
                    bg="#152431",
                ).pack(pady=(2, 0))

                iv_fg = GOOD if value == 31 else (BAD if value == 0 else TEXT)
                self._label(
                    cell,
                    str(value),
                    font=("Consolas", 8),
                    fg=iv_fg,
                    bg="#152431",
                ).pack(pady=(0, 2))

            foot = tk.Frame(card, bg=PANEL_2)
            foot.pack(fill="x", padx=6, pady=(4, 6))
            self._label(
                foot,
                f"IV SUM {sum(ivs[:6])}",
                font=("Consolas", 7),
                fg=BORDER,
                bg=PANEL_2,
            ).pack(side="left")
            self._label(
                foot,
                "RAM VALID",
                font=("Segoe UI Semibold", 6),
                fg=GOOD,
                bg=PANEL_2,
            ).pack(side="right")

    def _render_last_seen(self) -> None:
        for child in self.last_seen_wrap.winfo_children():
            child.destroy()

        headers = (
            "", "POKÉMON", "NATURE", "HP", "ATK", "DEF",
            "SPA", "SPD", "SPE", "SUM", "SV", "RESULT"
        )
        widths = (4, 14, 10, 4, 4, 4, 4, 4, 4, 5, 7, 8)
        head = tk.Frame(self.last_seen_wrap, bg="#0a141d")
        head.pack(fill="x")
        for col, (name, width) in enumerate(zip(headers, widths)):
            self._label(
                head,
                name,
                font=("Segoe UI Semibold", 6),
                fg=MUTED,
                bg="#0a141d",
                width=width,
                anchor="center" if col not in (1, 2) else "w",
            ).grid(row=0, column=col, padx=1, pady=3)

        recent = self.stats.snapshot().get("recently_seen", [])[:8]
        if not recent:
            self._label(
                self.last_seen_wrap,
                "No encounters yet.",
                font=("Segoe UI", 8),
                fg=MUTED,
                bg=PANEL,
            ).pack(anchor="w", padx=7, pady=12)
            return

        for mon in recent:
            shiny = bool(mon.get("shiny"))
            sv = int(mon.get("sv", 99999))
            anti = sv >= 65528
            row = tk.Frame(
                self.last_seen_wrap,
                bg=PANEL_2,
                highlightthickness=1 if shiny else 0,
                highlightbackground=SHINY,
            )
            row.pack(fill="x", pady=1)

            img = self._sprite(int(mon["species"]), shiny, small=True)
            lab = tk.Label(row, image=img, bg=PANEL_2, width=34)
            lab.image = img
            lab.grid(row=0, column=0, padx=1, pady=0)

            self._label(
                row,
                ("★ " if shiny else "") + str(mon.get("name", mon["species"])),
                font=("Segoe UI Semibold", 7),
                fg=SHINY if shiny else TEXT,
                bg=PANEL_2,
                width=14,
                anchor="w",
            ).grid(row=0, column=1, padx=1)

            self._label(
                row,
                str(mon.get("nature", "-")),
                font=("Segoe UI", 7),
                fg=MUTED,
                bg=PANEL_2,
                width=10,
                anchor="w",
            ).grid(row=0, column=2, padx=1)

            ivs = list(mon.get("ivs", []))
            while len(ivs) < 6:
                ivs.append(0)

            for i, val in enumerate(ivs[:6]):
                iv_fg = GOOD if val == 31 else (BAD if val == 0 else TEXT)
                self._label(
                    row,
                    str(val),
                    font=("Consolas", 7),
                    fg=iv_fg,
                    bg=PANEL_2,
                    width=4,
                ).grid(row=0, column=3 + i, padx=1)

            self._label(
                row,
                str(sum(ivs[:6])),
                font=("Consolas", 7),
                fg=BORDER,
                bg=PANEL_2,
                width=5,
            ).grid(row=0, column=9, padx=1)

            sv_fg = GOOD if shiny else (ANTI if anti else TEXT)
            self._label(
                row,
                str(mon.get("sv", "-")),
                font=("Consolas", 7),
                fg=sv_fg,
                bg=PANEL_2,
                width=7,
            ).grid(row=0, column=10, padx=1)

            result = "SHINY" if shiny else ("ANTI" if anti else "NORMAL")
            result_fg = SHINY if shiny else (ANTI if anti else MUTED)
            self._label(
                row,
                result,
                font=("Segoe UI Semibold", 6),
                fg=result_fg,
                bg=PANEL_2,
                width=8,
            ).grid(row=0, column=11, padx=1)

            tooltip = (
                f"{mon.get('name', '')} | PID {mon.get('pid', '-')} | "
                f"{mon.get('nature', '-')} | Ability {mon.get('ability', '-')} | "
                f"IVs {'/'.join(map(str, ivs[:6]))} | SUM {sum(ivs[:6])} | "
                f"SV {mon.get('sv', '-')} | HP {mon.get('hidden_power', '-')}"
            )
            for widget in row.winfo_children():
                widget.bind(
                    "<Enter>",
                    lambda _e, t=tooltip: self.status_line.configure(text=t),
                )
                widget.bind(
                    "<Leave>",
                    lambda _e: self.status_line.configure(text="Ready."),
                )
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
                ("Best IV sum", self.best_iv_sum_var),
                ("Worst IV sum", self.worst_iv_sum_var),
                ("Best SV", self.best_sv_var),
                ("Worst SV", self.worst_sv_var),
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
        targets = []
        if hasattr(self, "dashboard_shiny_wrap"):
            targets.append(self.dashboard_shiny_wrap)
        if hasattr(self, "recent_shiny_wrap"):
            targets.append(self.recent_shiny_wrap)
        if not targets:
            return

        shinies = self.stats.snapshot().get("recent_shinies", [])[:8]

        for wrap in targets:
            for child in wrap.winfo_children():
                child.destroy()

            if not shinies:
                self._label(
                    wrap,
                    "No shiny Pokémon recorded yet.",
                    font=("Segoe UI", 8),
                    fg=MUTED,
                    bg=PANEL,
                ).pack(anchor="w", padx=7, pady=12)
                continue

            for mon in shinies:
                row = tk.Frame(wrap, bg=PANEL_2)
                row.pack(fill="x", pady=1)

                img = self._sprite(int(mon["species"]), True, small=True)
                lab = tk.Label(row, image=img, bg=PANEL_2, width=36)
                lab.image = img
                lab.pack(side="left", padx=(2, 5))

                left = tk.Frame(row, bg=PANEL_2)
                left.pack(side="left", fill="x", expand=True, pady=3)
                self._label(
                    left,
                    f"★ {mon.get('name', mon['species'])}",
                    font=("Segoe UI Semibold", 8),
                    fg=SHINY,
                    bg=PANEL_2,
                ).pack(anchor="w")

                ability = mon.get("ability", "-")
                if isinstance(ability, int) or (isinstance(ability, str) and ability.isdigit()):
                    ability = self.abilities.get(int(ability), f"Ability {ability}")
                ivs = list(mon.get("ivs", []))
                iv_sum = sum(int(v) for v in ivs[:6]) if ivs else 0
                self._label(
                    left,
                    f"{mon.get('nature', '-')} • {ability} • IVΣ {iv_sum}",
                    font=("Segoe UI", 7),
                    fg=MUTED,
                    bg=PANEL_2,
                ).pack(anchor="w")

                right = tk.Frame(row, bg=PANEL_2)
                right.pack(side="right", padx=6, pady=3)
                self._label(
                    right,
                    f"SV {mon.get('sv', '-')}",
                    font=("Consolas", 8),
                    fg=GOOD,
                    bg=PANEL_2,
                ).pack(anchor="e")
                self._label(
                    right,
                    f"PID {mon.get('pid', '-')}",
                    font=("Consolas", 7),
                    fg=MUTED,
                    bg=PANEL_2,
                ).pack(anchor="e")
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
            # Presentation state is controlled from Hunt Control/Tools but is
            # intentionally not shown as dashboard telemetry.
            pass

        elif kind == "manual_presentation":
            if event["ok"]:
                state = "ON" if event["enabled"] else "OFF"
                label = "Display" if event["which"] == "display" else "Sound"
                self.status_line.configure(text=f"{label} {state}.", fg=MUTED)
            else:
                self.status_line.configure(text=event["message"], fg=BAD)

        elif kind == "set":
            mons = event["mons"]
            self.stats.record_set(mons)
            self.phase_target_seen += sum(
                1 for mon in mons
                if int(mon.get("species", 0)) in self.target_vars
                and self.target_vars[int(mon.get("species", 0))].get()
            )
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
        self.best_iv_sum_var.set(
            "--" if session.get("best_iv_sum") is None else str(session["best_iv_sum"])
        )
        self.worst_iv_sum_var.set(
            "--" if session.get("worst_iv_sum") is None else str(session["worst_iv_sum"])
        )
        self.best_sv_var.set(
            "--" if session.get("best_sv") is None else str(session["best_sv"])
        )
        self.worst_sv_var.set(
            "--" if session.get("worst_sv") is None else str(session["worst_sv"])
        )

    def _tick(self) -> None:
        now = time.monotonic()
        active = self.active_elapsed
        if self.active_started is not None:
            active += now - self.active_started

        phase = self.phase_elapsed
        if self.phase_started is not None:
            phase += now - self.phase_started

        session = self.stats.snapshot()["session"]
        seen = int(session["seen"])
        rate = seen / (max(1.0, active) / 3600.0) if active > 0 else 0.0
        self.rate_var.set(f"{rate:.1f} / hr")
        self.phase_var.set(_fmt_duration(phase))

        # Standard Gen-IV shiny roll: 1/8192 per eligible Pokémon.
        rolls = int(self.phase_target_seen)
        self.phase_seen_var.set(str(rolls))
        self.effective_rolls_var.set(str(rolls))
        chance = 1.0 - ((8191.0 / 8192.0) ** rolls)
        self.odds_chance_var.set(f"{chance * 100.0:.2f}%")

        selected_count = sum(1 for var in self.target_vars.values() if var.get())
        target_rate = 0.0
        if active > 0 and selected_count > 0:
            target_rate = rolls / (active / 3600.0)
        remaining = max(0, 8192 - rolls)
        if remaining == 0:
            eta = "AT ODDS"
        elif target_rate > 0:
            eta = _fmt_duration((remaining / target_rate) * 3600.0)
        else:
            eta = "--"
        self.odds_eta_var.set(eta)

        if hasattr(self, "odds_canvas"):
            width = max(1, self.odds_canvas.winfo_width())
            progress = min(1.0, rolls / 8192.0)
            self.odds_canvas.coords(self.odds_bar_rect, 0, 0, width * progress, 12)
            self.odds_bar_text.configure(
                text=f"{rolls:,} / 8,192 target rolls • {chance * 100.0:.2f}% chance"
            )

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
