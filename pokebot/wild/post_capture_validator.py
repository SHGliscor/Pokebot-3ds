from __future__ import annotations

"""Compatibility entry point for post-capture validator/rescue tools.

v0p43AS hardware proved the Pokédex object-readiness/A-only retry guard.
v0p43AT promoted that guard into the live ``battle_bag_throw`` implementation.
v0p43AU adds hardware-proven 0x1735/0x1736 phase tolerance on the same Pokédex
outer object, so validator and rescue tools still call the same production routine.
No game RAM writes are performed.
"""

import pokebot.wild.battle_bag_throw as bagmod


def clear_post_capture_validator(br, core, *, check_stop, log) -> dict:
    result = bagmod.clear_post_capture_screens(
        br, core, check_stop=check_stop, log=log
    )
    result["validator_entrypoint"] = {
        "version": "v0p43AU",
        "authority": "production post-capture readiness logic: v0p43AS stable-object guard + v0p43AU hardware-proven 0x1735/0x1736 Pokédex phase tolerance",
        "live_hunt_modified": True,
        "ram_writes": False,
    }
    return result
