from __future__ import annotations

AXES = {
    # Grass Walk/Run may start in an exact cardinal direction.  After the first
    # verified movement, the existing W6 planner may reverse on the same axis
    # to remain inside the authorised encounter component.
    "up": {
        "key": "up",
        "name": "↑ Up",
        "short": "Up",
        "directions": ("UP", "DOWN"),
        "initial_direction": "UP",
        "axis": "vertical",
    },
    "down": {
        "key": "down",
        "name": "↓ Down",
        "short": "Down",
        "directions": ("UP", "DOWN"),
        "initial_direction": "DOWN",
        "axis": "vertical",
    },
    "left": {
        "key": "left",
        "name": "← Left",
        "short": "Left",
        "directions": ("LEFT", "RIGHT"),
        "initial_direction": "LEFT",
        "axis": "horizontal",
    },
    "right": {
        "key": "right",
        "name": "→ Right",
        "short": "Right",
        "directions": ("LEFT", "RIGHT"),
        "initial_direction": "RIGHT",
        "axis": "horizontal",
    },
    # Legacy axis selectors remain for Cave/Surf methods whose validated
    # movement engines are axis-based rather than exact-direction-start based.
    "vertical": {
        "key": "vertical",
        "name": "Up ↕ Down",
        "short": "Up/Down",
        "directions": ("UP", "DOWN"),
        "axis": "vertical",
    },
    "horizontal": {
        "key": "horizontal",
        "name": "Left ↔ Right",
        "short": "Left/Right",
        "directions": ("LEFT", "RIGHT"),
        "axis": "horizontal",
    },
}

GAMES = {
    "0x0004000000055d00": {
        "key": "pokemon_x",
        "name": "Pokémon X",
        "process": "kujira-1",
        "family": "xy",
        "wild_methods": (
            {
                "key": "run",
                "name": "Normal Wild (Run)",
                "detail": "X/Y bounded B+direction oscillation with authoritative wild PK6 read",
                "validation": "HARDWARE PROVEN HF57 • 3/3 COMPLETE LOOPS • REPEATED RUN TOUCH RETAINED",
                "requires_hid_pulse": True,
                "requires_hid_latch": False,
                "axes": ("up", "down", "left", "right", "vertical", "horizontal"),
            },
        ),
    },
    "0x0004000000055e00": {
        "key": "pokemon_y",
        "name": "Pokémon Y",
        "process": "kujira-2",
        "family": "xy",
        "wild_methods": (
            {
                "key": "run",
                "name": "Normal Wild (Run)",
                "detail": "X/Y bounded B+direction oscillation with authoritative wild PK6 read",
                "validation": "HARDWARE PROVEN HF57 • 3/3 COMPLETE LOOPS • REPEATED RUN TOUCH RETAINED",
                "requires_hid_pulse": True,
                "requires_hid_latch": False,
                "axes": ("up", "down", "left", "right", "vertical", "horizontal"),
            },
        ),
    },
    "0x000400000011c500": {
        "key": "alpha_sapphire",
        "name": "Alpha Sapphire",
        "process": "sango-2",
        "wild_methods": (
            {
                "key": "walk",
                "name": "Walk",
                "detail": "One-tile direction-only grass traversal",
                "validation": "SAFE W3C one-tile primitive",
                "requires_hid_pulse": True,
                "requires_hid_latch": False,
                "axes": ("up", "down", "left", "right"),
            },
            {
                "key": "run",
                "name": "Run",
                "detail": "Terrain-aware B+direction W6 movement",
                "validation": "VALIDATED 30/30 + 15/15",
                "requires_hid_pulse": True,
                "requires_hid_latch": False,
                "axes": ("up", "down", "left", "right"),
            },
            {
                "key": "acro_bunny",
                "name": "Acro Bike Bunny Hop",
                "detail": "Stationary continuous-B latch",
                "validation": "VALIDATED 10/10",
                "requires_hid_pulse": False,
                "requires_hid_latch": True,
                "axes": (),
            },
            {
                "key": "horde",
                "name": "Horde",
                "detail": "Sweet Scent or Honey stationary Horde loop; RAM-selected attacking moves",
                "validation": "5-SLOT RAM AUTHORITY PROVEN • SWEET SCENT + HONEY HARDWARE-PROVEN",
                "requires_hid_pulse": True,
                "requires_hid_latch": False,
                "axes": (),
            },
            {
                "key": "cave",
                "name": "Cave Walk",
                "detail": "RAM-verified two-tile cave oscillation; no grass mask required",
                "validation": "HARDWARE PROVEN • RUSTURF + GRANITE CAVE",
                "requires_hid_pulse": True,
                "requires_hid_latch": False,
                "axes": ("vertical", "horizontal"),
            },
            {
                "key": "cave_run",
                "name": "Cave Run",
                "detail": "On-foot live RAM-proven five-tile cave corridor, then bounded B+direction Run oscillation",
                "validation": "HARDWARE PROVEN • FIERY PATH 5/5 ON FOOT • LIVE 5-TILE CORRIDOR",
                "requires_hid_pulse": True,
                "requires_hid_latch": False,
                "axes": ("vertical", "horizontal"),
            },
            {
                "key": "cave_bunny",
                "name": "Cave Acro Bunny",
                "detail": "Stationary continuous-B Acro Bike latch with cave zone/grid authority",
                "validation": "HARDWARE PROVEN • FIERY PATH 5/5 • STATIONARY GRID",
                "requires_hid_pulse": False,
                "requires_hid_latch": True,
                "axes": (),
            },
            {
                "key": "surf",
                "name": "Surf / Ocean",
                "detail": "Already-Surfing fast directional sweeps RAM-bounded around a fixed anchor; no B input",
                "validation": "HARDWARE TEST • 600ms FAST SWEEP • MOUNTED GRID RECOVERY",
                "requires_hid_pulse": True,
                "requires_hid_latch": False,
                "axes": ("vertical", "horizontal"),
            },
            {
                "key": "fishing",
                "name": "Fishing",
                "detail": "Registered-rod Y cast with RAM state-5 immediate reel and state-10 no-bite recovery",
                "validation": "HARDWARE PROVEN v0p11 • STATE5 REEL • STATE10 MESSAGE RECOVERY • PK6 RAM AUTHORITY",
                "requires_hid_pulse": True,
                "requires_hid_latch": False,
                "axes": (),
            },
        ),
    },
    "0x000400000011c400": {
        "key": "omega_ruby",
        "name": "Omega Ruby",
        "process": "sango-1",
        "wild_methods": (
            {
                "key": "walk",
                "name": "Walk",
                "detail": "One-tile direction-only grass traversal",
                "validation": "SAFE W3C one-tile primitive",
                "requires_hid_pulse": True,
                "requires_hid_latch": False,
                "axes": ("up", "down", "left", "right"),
            },
            {
                "key": "run",
                "name": "Run",
                "detail": "Terrain-aware B+direction W6 movement",
                "validation": "VALIDATED 30/30 + 15/15",
                "requires_hid_pulse": True,
                "requires_hid_latch": False,
                "axes": ("up", "down", "left", "right"),
            },
            {
                "key": "acro_bunny",
                "name": "Acro Bike Bunny Hop",
                "detail": "Stationary continuous-B latch",
                "validation": "VALIDATED 10/10",
                "requires_hid_pulse": False,
                "requires_hid_latch": True,
                "axes": (),
            },
            {
                "key": "horde",
                "name": "Horde",
                "detail": "Sweet Scent or Honey stationary Horde loop; RAM-selected attacking moves",
                "validation": "5-SLOT RAM AUTHORITY PROVEN • SWEET SCENT + HONEY HARDWARE-PROVEN",
                "requires_hid_pulse": True,
                "requires_hid_latch": False,
                "axes": (),
            },
            {
                "key": "cave",
                "name": "Cave Walk",
                "detail": "RAM-verified two-tile cave oscillation; no grass mask required",
                "validation": "HARDWARE PROVEN • RUSTURF + GRANITE CAVE",
                "requires_hid_pulse": True,
                "requires_hid_latch": False,
                "axes": ("vertical", "horizontal"),
            },
            {
                "key": "cave_run",
                "name": "Cave Run",
                "detail": "On-foot live RAM-proven five-tile cave corridor, then bounded B+direction Run oscillation",
                "validation": "HARDWARE PROVEN • FIERY PATH 5/5 ON FOOT • LIVE 5-TILE CORRIDOR",
                "requires_hid_pulse": True,
                "requires_hid_latch": False,
                "axes": ("vertical", "horizontal"),
            },
            {
                "key": "cave_bunny",
                "name": "Cave Acro Bunny",
                "detail": "Stationary continuous-B Acro Bike latch with cave zone/grid authority",
                "validation": "HARDWARE PROVEN • FIERY PATH 5/5 • STATIONARY GRID",
                "requires_hid_pulse": False,
                "requires_hid_latch": True,
                "axes": (),
            },
            {
                "key": "surf",
                "name": "Surf / Ocean",
                "detail": "Already-Surfing fast directional sweeps RAM-bounded around a fixed anchor; no B input",
                "validation": "OMEGA RUBY HARDWARE VALIDATION REQUIRED • SHARED ORAS ENGINE",
                "requires_hid_pulse": True,
                "requires_hid_latch": False,
                "axes": ("vertical", "horizontal"),
            },
            {
                "key": "fishing",
                "name": "Fishing",
                "detail": "Registered-rod Y cast with RAM state-5 immediate reel and state-10 no-bite recovery",
                "validation": "OMEGA RUBY HARDWARE VALIDATION REQUIRED • FAIL-CLOSED RAM STATE ENGINE",
                "requires_hid_pulse": True,
                "requires_hid_latch": False,
                "axes": (),
            },
        ),
    },
}

WILD_METHODS = {
    method["key"]: method
    for game in GAMES.values()
    for method in game["wild_methods"]
}


def normalize_title_id(value):
    if isinstance(value, int):
        return f"0x{value:016x}"
    s = str(value or "").strip().lower()
    if not s:
        return ""
    if s.startswith("0x"):
        try:
            return f"0x{int(s, 16):016x}"
        except ValueError:
            return s
    try:
        return f"0x{int(s):016x}"
    except ValueError:
        return s


def game_from_probe(game_info):
    info = game_info or {}
    title_id = normalize_title_id(info.get("title_id", ""))
    game = GAMES.get(title_id)
    if game is None:
        return None
    process = info.get("process_name", info.get("process"))
    if process != game["process"]:
        return None
    return game


def method_available(meta, capability_flags, controller_ready=True):
    if not controller_ready:
        return False
    caps = int(capability_flags or 0)
    if meta.get("requires_hid_pulse") and not (caps & (1 << 0)):
        return False
    if meta.get("requires_hid_latch") and not (caps & (1 << 7)):
        return False
    return True


def run_axis_authorized(game_key, axis_key):
    # v0p32+: route/axis authorization is dynamic. The whole-game terrain DB
    # is re-read at Wild Start and the validated planner only emits a movement
    # pulse when the live grass corridor satisfies the proven W6 reserve.
    return game_key in {"alpha_sapphire", "omega_ruby"} and axis_key in AXES
