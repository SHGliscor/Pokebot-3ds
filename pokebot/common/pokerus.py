from __future__ import annotations

from datetime import datetime

from pokebot.common.live_party import get_runtime_party_snapshot_for_bridge
from pokebot.common.evolution_prediction import predict_split_evolution


HIDDEN_POWER_TYPES = (
    "Fighting", "Flying", "Poison", "Ground",
    "Rock", "Bug", "Ghost", "Steel",
    "Fire", "Water", "Grass", "Electric",
    "Psychic", "Ice", "Dragon", "Dark",
)


def _hidden_power_from_ivs(ivs):
    try:
        bits = (
            (int(ivs["hp"]) & 1)
            + 2 * (int(ivs["attack"]) & 1)
            + 4 * (int(ivs["defense"]) & 1)
            + 8 * (int(ivs["speed"]) & 1)
            + 16 * (int(ivs["sp_attack"]) & 1)
            + 32 * (int(ivs["sp_defense"]) & 1)
        )
        return HIDDEN_POWER_TYPES[(bits * 15) // 63]
    except Exception:
        return "—"


def read_party_pokerus_snapshot(bridge):
    """Take one bounded six-slot party snapshot for post-battle Pokérus telemetry.

    This is deliberately not hunt authority. A failure here must never authorize
    or veto Run/HOLD/reset behaviour.
    """
    live = get_runtime_party_snapshot_for_bridge(bridge)
    parsed_party = list(live.get("ordered_parsed") or [])
    while len(parsed_party) < 6:
        parsed_party.append({})
    slots = []

    for slot_index, parsed in enumerate(parsed_party[:6]):
        valid = bool(parsed.get("valid") and parsed.get("checksum_valid"))
        if valid:
            evolution = predict_split_evolution(parsed.get("species", 0), parsed.get("ec"))
            slots.append({
                "slot": slot_index + 1,
                "valid": True,
                "species_id": int(parsed.get("species", 0) or 0),
                "species_name": str(parsed.get("species_name") or "Unknown"),
                "pid": str(parsed.get("pid") or ""),
                "ec": str(parsed.get("ec") or ""),
                "predicted_evolution": evolution.get("line") if evolution else None,
                "evolution_prediction": evolution,
                "nature": str(parsed.get("nature") or "—"),
                "gender": str(parsed.get("gender") or "—"),
                "shiny_xor": int(parsed.get("shiny_xor", 0) or 0),
                "is_shiny": bool(parsed.get("is_shiny")),
                "ivs": dict(parsed.get("ivs") or {}),
                "evs": dict(parsed.get("evs") or {}),
                "pokerus_state": int(parsed.get("pokerus_state", 0) or 0),
                "pokerus_days": int(parsed.get("pokerus_days", 0) or 0),
                "pokerus_strain": int(parsed.get("pokerus_strain", 0) or 0),
                "pokerus_status": str(parsed.get("pokerus_status") or "Unknown"),
            })
        else:
            slots.append({
                "slot": slot_index + 1,
                "valid": False,
                "species_id": 0,
                "species_name": "Empty",
                "pid": "",
                "ec": "",
                "predicted_evolution": None,
                "evolution_prediction": None,
                "nature": "—",
                "gender": "—",
                "shiny_xor": 0,
                "is_shiny": False,
                "ivs": {},
                "evs": {},
                "pokerus_state": 0,
                "pokerus_days": 0,
                "pokerus_strain": 0,
                "pokerus_status": "—",
            })

    return {
        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "slots": slots,
        "active_contagious_slots": [
            rec["slot"]
            for rec in slots
            if rec["valid"]
            and int(rec["pokerus_strain"]) > 0
            and int(rec["pokerus_days"]) > 0
        ],
    }


def compare_pokerus_snapshots(before, after):
    """Return newly infected party slots, or None when there is no new infection.

    A new infection is only accepted when the same valid Pokémon occupies the
    same party slot in both snapshots and transitions from strain 0 to non-zero.
    This avoids treating party changes or invalid reads as Pokérus.
    """
    if not isinstance(before, dict) or not isinstance(after, dict):
        return None

    before_slots = {
        int(rec.get("slot", 0)): rec
        for rec in (before.get("slots") or [])
        if isinstance(rec, dict)
    }
    after_slots = {
        int(rec.get("slot", 0)): rec
        for rec in (after.get("slots") or [])
        if isinstance(rec, dict)
    }

    newly_infected = []
    for slot in range(1, 7):
        old = before_slots.get(slot) or {}
        new = after_slots.get(slot) or {}
        if not (old.get("valid") and new.get("valid")):
            continue
        if (
            int(old.get("species_id", 0) or 0) != int(new.get("species_id", 0) or 0)
            or str(old.get("pid") or "") != str(new.get("pid") or "")
        ):
            continue
        if (
            int(old.get("pokerus_strain", 0) or 0) == 0
            and int(new.get("pokerus_strain", 0) or 0) > 0
        ):
            newly_infected.append({
                "slot": slot,
                "species_id": int(new.get("species_id", 0) or 0),
                "species_name": str(new.get("species_name") or "Pokémon"),
                "pid": str(new.get("pid") or ""),
                "strain": int(new.get("pokerus_strain", 0) or 0),
                "days": int(new.get("pokerus_days", 0) or 0),
                "status": str(new.get("pokerus_status") or "Infected"),
            })

    if not newly_infected:
        return None

    active_before = list(before.get("active_contagious_slots") or [])
    return {
        "detected": True,
        "time": after.get("time"),
        "source": (
            "NATURAL_POST_BATTLE"
            if not active_before
            else "PARTY_SPREAD_OR_NATURAL"
        ),
        "active_contagious_slots_before": active_before,
        "new_infections": newly_infected,
    }


def party_ui_payload(snapshot):
    """Convert a Pokérus snapshot into the dashboard's existing party schema."""
    payload = []
    for rec in (snapshot or {}).get("slots") or []:
        if rec.get("valid"):
            ivs = dict(rec.get("ivs") or {})
            payload.append({
                "slot": int(rec.get("slot", 0) or 0),
                "species": str(rec.get("species_name") or "Unknown"),
                "species_id": int(rec.get("species_id", 0) or 0),
                "nature": str(rec.get("nature") or "—"),
                "gender": str(rec.get("gender") or "—"),
                "pid": str(rec.get("pid") or "—"),
                "ec": str(rec.get("ec") or "—"),
                "predicted_evolution": rec.get("predicted_evolution"),
                "evolution_prediction": rec.get("evolution_prediction"),
                "sv": int(rec.get("shiny_xor", 0) or 0),
                "shiny": bool(rec.get("is_shiny")),
                "ivs": ivs,
                "evs": dict(rec.get("evs") or {}),
                "hidden_power": _hidden_power_from_ivs(ivs),
                "pokerus": str(rec.get("pokerus_status") or "Unknown"),
                "pokerus_days": int(rec.get("pokerus_days", 0) or 0),
                "pokerus_strain": int(rec.get("pokerus_strain", 0) or 0),
            })
        else:
            payload.append({
                "slot": int(rec.get("slot", 0) or 0),
                "species": "Empty",
                "species_id": 0,
                "nature": "—",
                "gender": "—",
                "pid": "—",
                "ec": "—",
                "predicted_evolution": None,
                "evolution_prediction": None,
                "sv": None,
                "shiny": False,
                "ivs": {},
                "evs": {},
                "hidden_power": "—",
                "pokerus": "—",
                "pokerus_days": 0,
                "pokerus_strain": 0,
            })
    return payload
