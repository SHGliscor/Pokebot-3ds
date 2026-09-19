from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math

from pokebot.common.pk6 import NATURE_NAMES

HP_TYPES = (
    "Fighting", "Flying", "Poison", "Ground", "Rock", "Bug", "Ghost", "Steel",
    "Fire", "Water", "Grass", "Electric", "Psychic", "Ice", "Dragon", "Dark",
)
IV_STATS = ("hp", "attack", "defense", "sp_attack", "sp_defense", "speed")
HP_ORDER = ("hp", "attack", "defense", "speed", "sp_attack", "sp_defense")

DEFAULT_TARGET = {
    "enabled": False,
    # Optional species allow-list. Empty means any species.
    "species": [],
    "nature": "Any",
    "shiny": "Any",          # Any / Shiny / Non-shiny
    "gender": "Any",         # Any / Male / Female / Genderless
    "hidden_power_type": "Any",
    # Gen VI fixes Hidden Power's base power at 60. It is displayed, not guessed.
    "hidden_power_power": 60,
    "ivs": {stat: {"min": 0, "max": 31} for stat in IV_STATS},
}

# Exact pre-hunt gender odds are currently needed for the locked Hoenn starters.
# Wild target matching does not depend on this table; only the odds projection does.
# If a future species database supplies a ratio, pass it into target_probability().
STARTER_GENDER_RATE = {
    252: {"Male": 7 / 8, "Female": 1 / 8, "Genderless": 0.0},
    255: {"Male": 7 / 8, "Female": 1 / 8, "Genderless": 0.0},
    258: {"Male": 7 / 8, "Female": 1 / 8, "Genderless": 0.0},
}


def normalize_target(data):
    out = deepcopy(DEFAULT_TARGET)
    if isinstance(data, dict):
        for key in ("enabled", "nature", "shiny", "gender", "hidden_power_type"):
            if key in data:
                out[key] = data[key]
        if isinstance(data.get("species"), (list, tuple, set)):
            out["species"] = list(data.get("species") or [])
        if isinstance(data.get("ivs"), dict):
            for stat in IV_STATS:
                raw = data["ivs"].get(stat)
                if isinstance(raw, dict):
                    out["ivs"][stat].update(raw)

    out["enabled"] = bool(out.get("enabled", False))
    cleaned_species = []
    for raw in out.get("species") or []:
        try:
            sid = int(raw)
        except Exception:
            continue
        if 1 <= sid <= 721 and sid not in cleaned_species:
            cleaned_species.append(sid)
    out["species"] = cleaned_species
    out["nature"] = str(out.get("nature") or "Any")
    if out["nature"] != "Any" and out["nature"] not in NATURE_NAMES:
        out["nature"] = "Any"

    out["shiny"] = str(out.get("shiny") or "Any")
    if out["shiny"] not in {"Any", "Shiny", "Non-shiny"}:
        out["shiny"] = "Any"

    out["gender"] = str(out.get("gender") or "Any")
    if out["gender"] not in {"Any", "Male", "Female", "Genderless"}:
        out["gender"] = "Any"

    out["hidden_power_type"] = str(out.get("hidden_power_type") or "Any")
    if out["hidden_power_type"] != "Any" and out["hidden_power_type"] not in HP_TYPES:
        out["hidden_power_type"] = "Any"
    out["hidden_power_power"] = 60

    for stat in IV_STATS:
        raw = out["ivs"].get(stat, {})
        try:
            lo = int(raw.get("min", 0))
        except Exception:
            lo = 0
        try:
            hi = int(raw.get("max", 31))
        except Exception:
            hi = 31
        lo = max(0, min(31, lo))
        hi = max(0, min(31, hi))
        if lo > hi:
            lo, hi = hi, lo
        out["ivs"][stat] = {"min": lo, "max": hi}
    return out


def has_active_constraints(criteria):
    """Return True only when Look for Target actually narrows the result set.

    An enabled-but-empty target must never be treated as a 1-in-1 match.
    This is a fail-closed guard shared by the UI, odds display and backend.
    """
    criteria = normalize_target(criteria)
    if criteria.get("species"):
        return True
    if criteria["nature"] != "Any":
        return True
    if criteria["shiny"] != "Any":
        return True
    if criteria["gender"] != "Any":
        return True
    if criteria["hidden_power_type"] != "Any":
        return True
    for stat in IV_STATS:
        rng = criteria["ivs"][stat]
        if int(rng["min"]) != 0 or int(rng["max"]) != 31:
            return True
    return False


def hidden_power_from_ivs(ivs):
    values = [int((ivs or {}).get(stat, 0)) for stat in HP_ORDER]
    bits = sum((value & 1) << i for i, value in enumerate(values))
    return HP_TYPES[(bits * 15) // 63], 60


def _gender_label(value):
    value = str(value or "")
    return {"♂": "Male", "♀": "Female", "—": "Genderless"}.get(value, value)


def evaluate_target(pk6, criteria):
    criteria = normalize_target(criteria)
    hp_type, hp_power = hidden_power_from_ivs(pk6.get("ivs") or {})
    checks = {}

    if not criteria["enabled"]:
        return {
            "enabled": False,
            "match": False,
            "checks": {},
            "hidden_power_type": hp_type,
            "hidden_power_power": hp_power,
        }

    # Fail closed: simply ticking Enable with every field at Any/0-31 is not
    # a meaningful search target and must never HOLD the first valid Pokémon.
    if not has_active_constraints(criteria):
        return {
            "enabled": True,
            "match": False,
            "checks": {},
            "reason": "NO_TARGET_CRITERIA",
            "hidden_power_type": hp_type,
            "hidden_power_power": hp_power,
            "actual": {
                "species": int(pk6.get("species", 0) or 0),
                "nature": str(pk6.get("nature") or ""),
                "shiny": bool(pk6.get("is_shiny")),
                "gender": _gender_label(pk6.get("gender")),
                "ivs": dict(pk6.get("ivs") or {}),
            },
        }

    species_id = int(pk6.get("species", 0) or 0)
    checks["species"] = (not criteria.get("species") or species_id in criteria.get("species", []))

    nature = str(pk6.get("nature") or "")
    checks["nature"] = criteria["nature"] == "Any" or nature == criteria["nature"]

    shiny = bool(pk6.get("is_shiny"))
    checks["shiny"] = (
        criteria["shiny"] == "Any"
        or (criteria["shiny"] == "Shiny" and shiny)
        or (criteria["shiny"] == "Non-shiny" and not shiny)
    )

    gender = _gender_label(pk6.get("gender"))
    checks["gender"] = criteria["gender"] == "Any" or gender == criteria["gender"]

    checks["hidden_power"] = (
        criteria["hidden_power_type"] == "Any"
        or hp_type == criteria["hidden_power_type"]
    )

    ivs = pk6.get("ivs") or {}
    iv_checks = {}
    for stat in IV_STATS:
        value = int(ivs.get(stat, -1))
        rng = criteria["ivs"][stat]
        iv_checks[stat] = int(rng["min"]) <= value <= int(rng["max"])
    checks["ivs"] = all(iv_checks.values())

    return {
        "enabled": True,
        "match": all(checks.values()),
        "checks": checks,
        "iv_checks": iv_checks,
        "hidden_power_type": hp_type,
        "hidden_power_power": hp_power,
        "actual": {
            "species": species_id,
            "nature": nature,
            "shiny": shiny,
            "gender": gender,
            "ivs": dict(ivs),
        },
    }


def _parity_count(lo, hi, parity):
    return sum(1 for value in range(int(lo), int(hi) + 1) if (value & 1) == parity)


def iv_hidden_power_probability(criteria):
    """Exact joint IV + Hidden Power type probability for ordinary Gen VI IV generation.

    Hidden Power is derived from IV parity, so multiplying an IV-range probability by
    1/16 would be wrong for many filters. We sum the 64 parity patterns exactly.
    """
    criteria = normalize_target(criteria)
    ranges = criteria["ivs"]
    hp_target = criteria["hidden_power_type"]
    valid = 0
    total = 32 ** 6

    for parity_mask in range(64):
        ways = 1
        parity_by_stat = {}
        for i, stat in enumerate(HP_ORDER):
            parity = (parity_mask >> i) & 1
            parity_by_stat[stat] = parity
            rng = ranges[stat]
            ways *= _parity_count(rng["min"], rng["max"], parity)
        if ways == 0:
            continue
        bits = sum(parity_by_stat[stat] << i for i, stat in enumerate(HP_ORDER))
        hp_type = HP_TYPES[(bits * 15) // 63]
        if hp_target != "Any" and hp_type != hp_target:
            continue
        valid += ways
    return valid / total


def gender_probability_for_species(species, gender):
    gender = str(gender or "Any")
    if gender == "Any":
        return 1.0
    table = STARTER_GENDER_RATE.get(int(species or 0))
    if table is None:
        return None
    return float(table.get(gender, 0.0))


@dataclass(frozen=True)
class TargetOdds:
    probability: float | None
    exact: bool
    one_in: float | None
    expected_encounters: float | None
    note: str
    factors: dict


def target_probability(
    criteria,
    *,
    species=None,
    shiny_numerator=1,
    shiny_denominator=4096,
    shiny_probability=None,
    gender_probability=None,
):
    criteria = normalize_target(criteria)
    if not criteria["enabled"]:
        return TargetOdds(None, False, None, None, "Look for Target is off", {})
    if not has_active_constraints(criteria):
        return TargetOdds(None, False, None, None, "No target criteria configured", {})

    p = iv_hidden_power_probability(criteria)
    factors = {"ivs_hidden_power": p}

    if criteria["nature"] != "Any":
        p *= 1 / 25
        factors["nature"] = 1 / 25

    shiny_p = (
        float(shiny_probability)
        if shiny_probability is not None
        else float(shiny_numerator) / float(shiny_denominator)
    )
    shiny_p = max(0.0, min(1.0, shiny_p))
    if criteria["shiny"] == "Shiny":
        p *= shiny_p
        factors["shiny"] = shiny_p
    elif criteria["shiny"] == "Non-shiny":
        p *= (1.0 - shiny_p)
        factors["shiny"] = 1.0 - shiny_p

    exact = True
    note = "Exact for standard independent Gen VI IV/nature generation."
    if criteria["gender"] != "Any":
        gp = gender_probability
        if gp is None:
            gp = gender_probability_for_species(species, criteria["gender"])
        if gp is None:
            exact = False
            note = "Gender odds depend on the target species; pre-hunt total is not exact."
        else:
            gp = max(0.0, min(1.0, float(gp)))
            p *= gp
            factors["gender"] = gp

    if p <= 0:
        return TargetOdds(0.0, exact, math.inf, math.inf, note, factors)
    one_in = 1.0 / p
    return TargetOdds(p, exact, one_in, one_in, note, factors)


def format_one_in(value):
    if value is None:
        return "—"
    if math.isinf(value):
        return "Impossible"
    if value < 1000:
        return f"1 in {value:,.2f}".rstrip("0").rstrip(".")
    return f"1 in {value:,.0f}"


def format_duration(seconds):
    if seconds is None or math.isinf(seconds):
        return "—"
    seconds = max(0, int(round(seconds)))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def target_summary(criteria):
    criteria = normalize_target(criteria)
    parts = []
    if criteria.get("species"):
        from pokebot.common.species_names import SPECIES_NAMES
        names = [SPECIES_NAMES.get(int(s), f"Species #{s}") for s in criteria["species"]]
        if len(names) <= 3:
            parts.append("Species: " + ", ".join(names))
        else:
            parts.append("Species: " + ", ".join(names[:3]) + f" +{len(names)-3}")
    for stat, short in (
        ("hp", "HP"), ("attack", "Atk"), ("defense", "Def"),
        ("sp_attack", "SpA"), ("sp_defense", "SpD"), ("speed", "Spe"),
    ):
        rng = criteria["ivs"][stat]
        if rng["min"] == 0 and rng["max"] == 31:
            continue
        if rng["min"] == rng["max"]:
            parts.append(f"{short}={rng['min']}")
        else:
            parts.append(f"{short} {rng['min']}-{rng['max']}")
    if criteria["nature"] != "Any":
        parts.append(criteria["nature"])
    if criteria["shiny"] != "Any":
        parts.append(criteria["shiny"])
    if criteria["gender"] != "Any":
        parts.append(criteria["gender"])
    if criteria["hidden_power_type"] != "Any":
        parts.append(f"HP {criteria['hidden_power_type']} / 60")
    return " • ".join(parts) if parts else "Not configured — press Configure"
