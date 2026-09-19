from __future__ import annotations

ALPHA_SAPPHIRE_TITLE_ID = "0x000400000011C500"
OMEGA_RUBY_TITLE_ID = "0x000400000011C400"
POKEMON_X_TITLE_ID = "0x0004000000055D00"
POKEMON_Y_TITLE_ID = "0x0004000000055E00"

PROFILES = {
    ALPHA_SAPPHIRE_TITLE_ID.lower(): {
        "key": "alpha_sapphire", "name": "Alpha Sapphire", "family": "oras",
        "title_id": ALPHA_SAPPHIRE_TITLE_ID, "title_id_int": int(ALPHA_SAPPHIRE_TITLE_ID, 16),
        "process": "sango-2", "starter_reset_profile": "alpha_sapphire_locked",
        "wild_profile": "oras_shared",
    },
    OMEGA_RUBY_TITLE_ID.lower(): {
        "key": "omega_ruby", "name": "Omega Ruby", "family": "oras",
        "title_id": OMEGA_RUBY_TITLE_ID, "title_id_int": int(OMEGA_RUBY_TITLE_ID, 16),
        "process": "sango-1", "starter_reset_profile": "omega_ruby_s2_proven",
        "wild_profile": "oras_shared",
    },
    POKEMON_X_TITLE_ID.lower(): {
        "key": "pokemon_x", "name": "Pokémon X", "family": "xy",
        "title_id": POKEMON_X_TITLE_ID, "title_id_int": int(POKEMON_X_TITLE_ID, 16),
        "process": "kujira-1", "starter_reset_profile": "xy_cro_gated", "wild_profile": "xy_initial",
    },
    POKEMON_Y_TITLE_ID.lower(): {
        "key": "pokemon_y", "name": "Pokémon Y", "family": "xy",
        "title_id": POKEMON_Y_TITLE_ID, "title_id_int": int(POKEMON_Y_TITLE_ID, 16),
        "process": "kujira-2", "starter_reset_profile": "xy_cro_gated", "wild_profile": "xy_initial",
    },
}


def normalize_title_id(value):
    if isinstance(value, int):
        return f"0x{value:016x}"
    s = str(value or "").strip().lower()
    if not s:
        return ""
    try:
        return f"0x{int(s, 16 if s.startswith('0x') else 10):016x}"
    except ValueError:
        return s


def profile_from_game_info(info):
    info = info or {}
    title_id = normalize_title_id(info.get("title_id", info.get("title_id_hex", "")))
    profile = PROFILES.get(title_id)
    if profile is None:
        return None
    process = info.get("process_name", info.get("process"))
    if process != profile["process"]:
        return None
    try:
        pid = int(info.get("pid", 0))
    except Exception:
        pid = 0
    if pid <= 0:
        return None
    if "status" in info and int(info.get("status", 0)) != 0:
        return None
    return dict(profile)
