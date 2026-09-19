from __future__ import annotations

"""Shared shiny Auto-Capture dispatcher.

The hardware-proven single-battle and Horde capture engines remain unchanged.
This module centralises policy, identity locking and result shape so every hunt
family can call one backend without duplicating capture selection logic.
"""

from pokebot.wild import battle_bag_throw as bagmod
from pokebot.wild.horde_live_capture import live_auto_capture_single_shiny_horde

NORMAL_SINGLE_BATTLE_OWNER = 0x0852FC74
SUPPORTED_ORAS_AUTO_CAPTURE_GAMES = frozenset({"alpha_sapphire", "omega_ruby"})


def auto_capture_supported_for_game(game_key: str) -> bool:
    return str(game_key or "").strip().lower() in SUPPORTED_ORAS_AUTO_CAPTURE_GAMES


def auto_capture_test_eligible(*, game_key: str, horde_size: int, is_shiny: bool, target_match: bool) -> bool:
    """One-shot test is deliberately limited to an ordinary single encounter."""
    return bool(
        auto_capture_supported_for_game(game_key)
        and int(horde_size) == 1
        and not bool(is_shiny)
        and not bool(target_match)
    )


def _norm_hex(value) -> str:
    if isinstance(value, int):
        return f"0x{value & 0xFFFFFFFF:08X}"
    text = str(value or "").strip()
    if not text or text == "—":
        return ""
    try:
        return f"0x{int(text, 0) & 0xFFFFFFFF:08X}"
    except Exception:
        return text.upper()


def build_identity_lock(target: dict) -> dict:
    target = dict(target or {})
    species = int(target.get("species") or 0)
    pid = _norm_hex(target.get("pokemon_pid") or target.get("pid"))
    ec = _norm_hex(target.get("ec"))
    if not (1 <= species <= 721):
        raise RuntimeError(f"Auto-Capture identity lock has invalid species {species}")
    if not pid:
        raise RuntimeError("Auto-Capture identity lock is missing PID")
    if not ec:
        raise RuntimeError("Auto-Capture identity lock is missing EC")
    return {
        "species": species,
        "species_name": target.get("species_name"),
        "pid": pid,
        "ec": ec,
        "token": f"{ec}:{pid}:{species}",
        "authority": "checksum-valid opponent PK6 species+PID+EC",
    }


def run_shiny_auto_capture(
    core,
    br,
    *,
    opponent_set: dict,
    shiny: dict,
    horde_size: int,
    method_key: str,
    environment: str,
    ball_override: str,
    lead_pk6: dict | None = None,
    move_db: dict | None = None,
    base_dir=None,
    check_stop,
    log,
    game_key: str = "",
) -> dict:
    """Dispatch one RAM-confirmed shiny to the proven single/Horde backend.

    This function does not clear post-capture UI and does not resume movement;
    those remain explicit caller phases after ``result == CAPTURED``.
    """
    if game_key and not auto_capture_supported_for_game(game_key):
        raise RuntimeError(f"Auto-Capture is not enabled for game profile {game_key!r}")
    lock = build_identity_lock(shiny)
    log(
        "AUTO-CAPTURE IDENTITY LOCK: "
        f"{lock['species_name'] or lock['species']} EC={lock['ec']} PID={lock['pid']}"
    )

    if int(horde_size) == 5:
        report = live_auto_capture_single_shiny_horde(
            core,
            br,
            opponent_set,
            shiny,
            ball_override=ball_override,
            lead_pk6=lead_pk6,
            move_db=move_db or {},
            base_dir=base_dir,
            check_stop=check_stop,
            log=log,
        )
        backend = "HORDE_PROTECTED_SURVIVOR"
    elif int(horde_size) == 1:
        # Start from the hardware-proven normal owner as the zero-cost fast path.
        # The Bag thrower re-discovers the unique live dual-vptr owner after a
        # process reset if this historic address has relocated.
        bagmod.bind_runtime_owner(
            NORMAL_SINGLE_BATTLE_OWNER,
            locator="shared Auto-Capture single-battle owner",
        )
        report = bagmod.auto_catch_best_ball(
            core,
            br,
            target=shiny,
            method_key=method_key,
            environment=environment,
            ball_override=ball_override,
            check_stop=check_stop,
            log=log,
        )
        backend = "SINGLE_BATTLE_MULTI_BALL"
    else:
        raise RuntimeError(
            f"Auto-Capture only accepts one opponent or a five-slot Horde; got {horde_size}"
        )

    out = dict(report or {})
    out["identity_lock"] = lock
    out["capture_backend"] = backend
    out["shared_dispatch"] = "v0p43CM_ORAS"
    out["game_key"] = str(game_key or "") or None
    return out


def clear_captured_shiny_post_capture(
    br, core, *, check_stop, log, pre_capture_party_count: int | None = None
) -> dict:
    """Shared mapped Pokédex/nickname/Box recovery entry point."""
    return bagmod.clear_post_capture_screens(
        br, core, check_stop=check_stop, log=log,
        pre_capture_party_count=pre_capture_party_count,
    )
