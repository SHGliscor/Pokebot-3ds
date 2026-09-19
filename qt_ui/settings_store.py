
from __future__ import annotations

import ipaddress
import json
from copy import deepcopy
from pathlib import Path

from pokebot.common.target_filter import DEFAULT_TARGET, normalize_target

DEFAULTS = {
    "three_ds_ip": "192.168.0.28",
    "ram_bridge_port": 4952,
    "input_port": 4952,
    "bridge_timeout_s": 2.0,
    # Pokebot3DS-CFW ships hardware-validated ORAS 1.4 code.ips routes for
    # both Omega Ruby and Alpha Sapphire. Existing AppData settings still win.
    "use_code_ips": True,

    "auto_connection_test": True,
    "remember_selected_starter": True,
    "selected_starter": "torchic",

    "always_on_top": False,
    "download_oras_sprites": True,

    "shiny_sound_enabled": True,
    "shiny_sound_path": "assets/gen6_shiny_notification.wav",

    "auto_support_zip": True,
    # Hardware-proven Alpha Sapphire 1.4 action. When enabled, an unblocked
    # single wild shiny uses the bounded RAM-gated auto-catch/retry flow.
    "auto_throw_one_poke_ball_on_shiny": False,
    # "best" preserves automatic scoring. Any other valid value is an explicit
    # exact-Ball override for every non-starter hunt method.
    "capture_ball_override": "best",
    "raw_pk6_keep": 10,

    # Discord telemetry/notifications. These settings never participate in hunt authority.
    "discord_enabled": False,
    "discord_bot_token": "",
    "discord_guild_id": "",
    "discord_channel_id": "",
    "discord_auto_connect": False,
    "discord_presence_enabled": True,
    "discord_notify_shiny": True,
    "discord_notify_pokerus": True,
    "discord_notify_hunt_started": True,
    "discord_notify_hunt_stopped": True,
    "discord_notify_safety_hold": True,
    "discord_notify_milestones": True,
    "discord_phase_milestones": "100,500,1000,2048,4096",
    "discord_periodic_summary": False,
    "discord_summary_minutes": 60,

    # Personal Discord Rich Presence via the local desktop client. No user token is used.
    "discord_rpc_enabled": False,
    "discord_rpc_application_id": "",
    "discord_rpc_auto_connect": False,

    # Optional RAM-authoritative Look for Target mode. Disabled preserves the
    # existing shiny-hunt behaviour byte-for-byte at the decision boundary.
    "look_for_target": deepcopy(DEFAULT_TARGET),
}

STARTERS = {"random", "treecko", "torchic", "mudkip"}

def normalize(data):
    out = deepcopy(DEFAULTS)
    if isinstance(data, dict):
        out.update(data)

    try:
        ipaddress.ip_address(str(out["three_ds_ip"]).strip())
        out["three_ds_ip"] = str(out["three_ds_ip"]).strip()
    except Exception:
        out["three_ds_ip"] = DEFAULTS["three_ds_ip"]

    try:
        value = int(out["ram_bridge_port"])
    except Exception:
        value = DEFAULTS["ram_bridge_port"]
    out["ram_bridge_port"] = (
        value if 1 <= value <= 65535 else DEFAULTS["ram_bridge_port"]
    )

    # Pokebot-Luma uses one acknowledged bridge endpoint for both
    # read-only RAM and controller commands. Keep old AppData from pinning
    # the controller to the retired UDP/4950 InputRedirection transport.
    out["input_port"] = out["ram_bridge_port"]

    try:
        timeout = float(out["bridge_timeout_s"])
    except Exception:
        timeout = DEFAULTS["bridge_timeout_s"]
    out["bridge_timeout_s"] = min(10.0, max(0.25, timeout))

    if out.get("selected_starter") not in STARTERS:
        out["selected_starter"] = DEFAULTS["selected_starter"]

    allowed_ball_overrides = {
        "best", "poke ball", "great ball", "ultra ball", "net ball",
        "dive ball", "nest ball", "repeat ball", "timer ball",
        "luxury ball", "premier ball", "dusk ball", "heal ball",
        "quick ball", "master ball",
    }
    ball_override = str(out.get("capture_ball_override", "best") or "best").strip().lower()
    out["capture_ball_override"] = (
        ball_override if ball_override in allowed_ball_overrides else "best"
    )

    try:
        keep = int(out.get("raw_pk6_keep", DEFAULTS["raw_pk6_keep"]))
    except Exception:
        keep = DEFAULTS["raw_pk6_keep"]
    out["raw_pk6_keep"] = min(50, max(1, keep))

    for key in (
        "use_code_ips",
        "auto_connection_test",
        "remember_selected_starter",
        "always_on_top",
        "download_oras_sprites",
        "shiny_sound_enabled",
        "auto_support_zip",
        "auto_throw_one_poke_ball_on_shiny",
        "discord_enabled",
        "discord_auto_connect",
        "discord_presence_enabled",
        "discord_notify_shiny",
        "discord_notify_pokerus",
        "discord_notify_hunt_started",
        "discord_notify_hunt_stopped",
        "discord_notify_safety_hold",
        "discord_notify_milestones",
        "discord_periodic_summary",
        "discord_rpc_enabled",
        "discord_rpc_auto_connect",
    ):
        out[key] = bool(out.get(key, DEFAULTS[key]))

    out["shiny_sound_path"] = str(
        out.get("shiny_sound_path") or DEFAULTS["shiny_sound_path"]
    ).strip()

    for key in (
        "discord_bot_token", "discord_guild_id", "discord_channel_id",
        "discord_phase_milestones", "discord_rpc_application_id",
    ):
        out[key] = str(out.get(key, DEFAULTS[key]) or "").strip()

    out["look_for_target"] = normalize_target(out.get("look_for_target"))

    try:
        summary_minutes = int(out.get("discord_summary_minutes", DEFAULTS["discord_summary_minutes"]))
    except Exception:
        summary_minutes = DEFAULTS["discord_summary_minutes"]
    out["discord_summary_minutes"] = min(1440, max(5, summary_minutes))

    return out

def load_settings(path):
    path = Path(path)
    if not path.exists():
        return deepcopy(DEFAULTS)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return deepcopy(DEFAULTS)
    return normalize(data)

def save_settings(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = normalize(data)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean, indent=2), encoding="utf-8")
    tmp.replace(path)
    return clean
