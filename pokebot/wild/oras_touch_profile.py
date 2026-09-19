from __future__ import annotations

import json
import os
from pathlib import Path

# Hardware-proven coordinates already established by this project.
PROVEN_PARTY_FIELD_ACTION = {1: (119, 55)}
PROVEN_BATTLE_MOVE = {2: (246, 69)}
CALIBRATED_BATTLE_MOVE = {1: (74, 69), 2: (246, 69), 3: (74, 133), 4: (246, 133)}

# D25 party defaults plus hardware-framebuffer-calibrated battle move points.
# Slot 2 is additionally hardware-proven end-to-end. Slots 1/3/4 were measured
# from the 320x240 MOVE screen captured on real ORAS hardware in v0p43EF.
# Any explicit user calibration in %APPDATA%/Pokebot-3DS/oras_touch_profile.json overrides these.
DERIVED_PARTY_FIELD_ACTION = {
    1: (119, 55),
    2: (279, 55),
    3: (119, 119),
    4: (279, 119),
    5: (119, 183),
    6: (279, 183),
}
DERIVED_BATTLE_MOVE = dict(CALIBRATED_BATTLE_MOVE)
PROVEN_PARTY_OPEN = (60, 230)
PROVEN_SWEET_SCENT_MENU = (170, 104)
PROVEN_FIGHT = (158, 100)
# ORAS overworld PlayNav/save control on the 320x240 lower screen.
PROVEN_SAVE_MENU = (160, 220)


def profile_path(base_dir: Path) -> Path:
    override = os.environ.get("POKEBOT_APPDATA_ROOT") or os.environ.get("POKEBOT_DATA_ROOT")
    root = Path(override).expanduser().resolve() if override else (Path(os.environ["APPDATA"]).expanduser().resolve() / "Pokebot-3DS") if os.environ.get("APPDATA") else (Path.home() / ".pokebot-3ds")
    return root / "oras_touch_profile.json"


def default_profile() -> dict:
    return {
        "format": "pokebot3ds-oras-touch-profile-v1",
        "party_field_action": {str(k): list(v) for k, v in DERIVED_PARTY_FIELD_ACTION.items()},
        "battle_move": {str(k): list(v) for k, v in DERIVED_BATTLE_MOVE.items()},
        "save_menu": list(PROVEN_SAVE_MENU),
        "proven": {
            "party_field_action": [1],
            "battle_move": [2],
        },
        "derived": {
            "party_field_action": [2, 3, 4, 5, 6],
            "battle_move": [],
        },
        "calibrated": {
            "battle_move": [1, 3, 4],
        },
    }


def load_profile(base_dir: Path) -> dict:
    base = default_profile()
    path = profile_path(base_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return base
    for group in ("party_field_action", "battle_move"):
        values = raw.get(group)
        if isinstance(values, dict):
            for key, value in values.items():
                try:
                    slot = int(key)
                    x, y = int(value[0]), int(value[1])
                except Exception:
                    continue
                if 0 <= x < 320 and 0 <= y < 240:
                    base[group][str(slot)] = [x, y]
    proven = raw.get("proven") or {}
    for group in ("party_field_action", "battle_move"):
        extra = []
        for value in proven.get(group, []):
            try:
                extra.append(int(value))
            except Exception:
                pass
        base["proven"][group] = sorted(set(base["proven"][group] + extra))
    value = raw.get("save_menu")
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            x, y = int(value[0]), int(value[1])
        except Exception:
            x = y = -1
        if 0 <= x < 320 and 0 <= y < 240:
            base["save_menu"] = [x, y]
    return base


def save_profile(base_dir: Path, profile: dict) -> Path:
    path = profile_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(profile, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def get_party_field_action_xy(base_dir: Path, visible_slot: int):
    profile = load_profile(base_dir)
    value = profile.get("party_field_action", {}).get(str(int(visible_slot)))
    if value is None:
        return None
    return int(value[0]), int(value[1])


def get_battle_move_xy(base_dir: Path, move_slot: int):
    profile = load_profile(base_dir)
    value = profile.get("battle_move", {}).get(str(int(move_slot)))
    if value is None:
        return None
    return int(value[0]), int(value[1])


def get_save_menu_xy(base_dir: Path):
    profile = load_profile(base_dir)
    value = profile.get("save_menu") or list(PROVEN_SAVE_MENU)
    return int(value[0]), int(value[1])
