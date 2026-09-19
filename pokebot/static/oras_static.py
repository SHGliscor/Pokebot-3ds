from __future__ import annotations

"""ORAS static encounter profiles and fail-closed state authority.

The engine is intentionally save-position anchored.  For ordinary statics the
user saves directly in front of the target.  Each non-shiny cycle soft-resets,
returns through the existing proven ORAS reset route, and accepts the field only
when RAM proves the same saved zone/tile again.

Physical encounter triggers are kept deliberately small and explicit:
- INTERACT_A: bounded A presses while battle RAM is inactive.
- MENU_REVEAL: Spiritomb special case (open/close menu in the NW position).
- STEP_NORTH: Regigigas special case (save one tile south of the room centre).

Reset strategies are also explicit:
- SOFT_RESET_TO_SAVED_FIELD: ordinary static soft reset.
- RUN_REINTERACT: battle PK6 -> RUN -> prove saved field -> A re-interact.
  This is used by the Eon Ticket Southern Island Lati battle.

Hardware validation is tracked by trigger family as well as individual
targets. ORAS uses one shared runtime/choreography model, so hardware proof on
either Omega Ruby or Alpha Sapphire applies to the equivalent encounter path
in the other version unless the actual script/mechanic differs. The ordinary
ground Mirage/portal ring interaction is therefore shared across ORAS.
Dimensional Rifts and Storm Clouds remain separate Soaring trigger families and
must be mapped independently. Offline tests alone never promote choreography.
"""

from dataclasses import dataclass, field
from enum import Enum
import math
import struct
from typing import Any

from pokebot.common.oras_profiles import (
    BATTLE_STATE,
    BATTLE_INACTIVE,
    ZONE_ADDR,
    PRIMARY_BASE,
    SECONDARY_BASE,
)

BATTLE_ACTIVE = 0x00040001
BATTLE_TRANSITION = 0x00000000
FIELD_POSITION_EPSILON = 0.80
FIELD_SECONDARY_EPSILON = 1.25


class StaticState(str, Enum):
    IDLE = "IDLE"
    FIELD_READY = "FIELD_READY"
    TRIGGERED = "TRIGGERED"
    BATTLE_READY = "BATTLE_READY"
    OPPONENT_VALIDATED = "OPPONENT_VALIDATED"
    NONSHINY_RESET_REQUIRED = "NONSHINY_RESET_REQUIRED"
    SHINY_HOLD = "SHINY_HOLD"
    CAPTURED = "CAPTURED"
    SAFETY_HOLD = "SAFETY_HOLD"


@dataclass(frozen=True)
class StaticEncounterProfile:
    key: str
    name: str
    species: int
    games: tuple[str, ...] = ("alpha_sapphire", "omega_ruby")
    family: str = "static"
    shiny_locked: bool = False
    trigger_kind: str = "INTERACT_A"
    reset_kind: str = "SOFT_RESET_TO_SAVED_FIELD"
    choreography_status: str = "NEEDS_HARDWARE_VALIDATION"
    max_trigger_pulses: int = 4
    expected_level: int | None = 50
    location: str = ""
    expected_zone_id: int | None = None
    notes: str = ""
    required_party_species: tuple[int, ...] = ()


def _p(key: str, name: str, species: int, **kwargs) -> StaticEncounterProfile:
    return StaticEncounterProfile(key=key, name=name, species=species, **kwargs)


RING_SHARED_PROVEN_ORAS = "HARDWARE_PROVEN_SHARED_RING_ORAS"
# Backward-compatible alias retained for old stats/manifests.
RING_SHARED_PROVEN_AS = RING_SHARED_PROVEN_ORAS
RING_SHARED_OR_PARITY_PENDING = RING_SHARED_PROVEN_ORAS


def _ring(key: str, name: str, species: int, **kwargs) -> StaticEncounterProfile:
    """Ground Mirage/portal ring profile inheriting Zekrom's AS hardware proof."""
    games = tuple(kwargs.get("games", ("alpha_sapphire", "omega_ruby")))
    kwargs.setdefault("family", "ring_static")
    # ORAS is one shared runtime engine. Zekrom (AS) and Reshiram (OR)
    # independently proved the same ground-ring choreography, so version
    # exclusives inherit the shared proof without duplicate parity testing.
    kwargs.setdefault("choreography_status", RING_SHARED_PROVEN_ORAS)
    return _p(key, name, species, **kwargs)


def hardware_validation_label(profile: StaticEncounterProfile, game_key: str | None = None) -> str:
    """Human-readable, game-aware validation label for the dashboard."""
    status = str(profile.choreography_status or "")
    game_key = str(game_key or "")
    if profile.shiny_locked or status.startswith("LOCKED_"):
        return "Shiny locked / not huntable"
    if profile.trigger_kind == "SOARING_RIFT_MAPPER":
        if status.startswith("SOARING_RIFT_MANUAL_MAPPER_HARDWARE_PROVEN_AS"):
            return "Manual rift path proven — automation mapping"
        return "Dimensional Rift mapper — Soaring automation unfinished"
    if profile.trigger_kind == "SOARING_WEATHER_UNIMPLEMENTED":
        return "Storm Cloud mapper needed"
    if profile.trigger_kind == "DEXNAV_TUTORIAL_POOCHYENA":
        if game_key == "alpha_sapphire":
            return "Omega Ruby hardware proof — Alpha Sapphire parity pending"
        return "Hardware proven — Omega Ruby tutorial; automatic RAM-target Circle Pad sneak"
    if profile.reset_kind == "RUN_REINTERACT":
        if status.startswith("HARDWARE_PROVEN_"):
            return "Hardware proven — RUN → re-interact"
        return "RUN → re-interact hardware validation"
    if profile.family == "ring_static":
        return "Hardware proven — shared ORAS ring trigger"
    if status.startswith("HARDWARE_PROVEN_"):
        return "Hardware proven"
    return "Hardware validation needed"


# Resettable / battle-based ORAS statics.  Version exclusives are encoded in
# ``games`` so the worker fails before controller input on the wrong title.
STATIC_PROFILES: dict[str, StaticEncounterProfile] = {
    # Hoenn / fixed overworld
    "voltorb": _p("voltorb", "Voltorb", 100, expected_level=None, choreography_status="HARDWARE_PROVEN_SHARED_ORAS_2026_09_01", location="New Mauville / fixed event statics", notes="Save directly facing the event Voltorb. Five consecutive hardware cycles passed on ORAS shared choreography."),
    "electrode": _p("electrode", "Electrode", 101, expected_level=None, choreography_status="HARDWARE_PROVEN_SHARED_ORAS_2026_09_01", location="Team Magma/Aqua Hideout / fixed event statics", notes="Save directly facing the fake-item Electrode in the Team Magma/Aqua Hideout. Five consecutive hardware cycles passed on shared ORAS choreography."),
    "kecleon": _p("kecleon", "Kecleon", 352, expected_level=None, max_trigger_pulses=12, choreography_status="HARDWARE_PROVEN_SHARED_ORAS_2026_08_31", location="Routes 119/120 and fixed Devon Scope statics", notes="Save directly facing a battle-capable Kecleon; the bounded A budget advances the Devon Scope reveal/confirmation dialogue and stops immediately when battle RAM becomes active."),
    "spiritomb": _p("spiritomb", "Spiritomb", 442, trigger_kind="STEP_NORTH_MENU_REVEAL", max_trigger_pulses=1, choreography_status="HARDWARE_PROVEN_SHARED_ORAS_2026_09_01", location="Sea Mauville", notes="Save one tile south of the NW trigger position; worker uses UP, X, B, then up to four bounded A presses on the Shahhh! dialogue, checking battle RAM after each press. Five consecutive hardware cycles passed on shared ORAS choreography."),
    "poochyena_dexnav_tutorial": _p(
        "poochyena_dexnav_tutorial", "Poochyena", 261, expected_level=5,
        trigger_kind="DEXNAV_TUTORIAL_POOCHYENA", max_trigger_pulses=17,
        choreography_status="HARDWARE_PROVEN_OR_DEXNAV_TUTORIAL_FLUID_RAM_STEERING_2026_09_12",
        location="Route 101 (DexNav Tutorial)", expected_zone_id=23,
        notes=(
            "Save at the probed pre-tutorial position (world 1917,2349) before the scripted "
            "Route 101 DexNav Poochyena is consumed. Worker sends LEFT then adaptive fast A presses until RAM proves the DexNav target has spawned, "
            "RAM-proves the post-dialogue position (1791,2349), reads the HF89-probed live DexNav target coordinates, "
            "then re-steers partial Circle Pad corrections from player RAM until battle. Tutorial identity additionally requires "
            "Lv.5 and Thunder/Ice/Fire Fang."
        ),
    ),
    "regirock": _p("regirock", "Regirock", 377, expected_level=40, location="Desert Ruins", expected_zone_id=77, choreography_status="HARDWARE_PROVEN_2026_08_28"),
    "regice": _p("regice", "Regice", 378, expected_level=40, location="Island Cave", expected_zone_id=158, choreography_status="HARDWARE_PROVEN_2026_08_28"),
    "registeel": _p("registeel", "Registeel", 379, expected_level=40, location="Ancient Tomb", expected_zone_id=159, choreography_status="HARDWARE_PROVEN_2026_08_28"),
    "regigigas": _p("regigigas", "Regigigas", 486, trigger_kind="STEP_NORTH", max_trigger_pulses=1, location="Island Cave", expected_zone_id=158, choreography_status="HARDWARE_PROVEN_2026_08_29", notes="Save exactly one tile south of the centre trigger tile in Island Cave; worker sends one UP step, then bounded A presses to clear the hardware-proven post-step dialogue before battle."),
    "heatran": _ring("heatran", "Heatran", 485, location="Scorched Slab"),

    # Sea Mauville version exclusives
    "lugia": _ring("lugia", "Lugia", 249, games=("alpha_sapphire",), location="Sea Mauville", notes="Requires Tidal Bell."),
    "ho_oh": _ring("ho_oh", "Ho-Oh", 250, games=("omega_ruby",), location="Sea Mauville", notes="Requires Clear Bell."),

    # Eon Ticket battle: unlike ordinary statics, a non-shiny can be fled from
    # and immediately re-interacted with on Southern Island.  The normal story
    # Lati is a gift and is intentionally not represented by these profiles.
    "latias_eon": _p(
        "latias_eon", "Latias", 380, games=("omega_ruby",), expected_level=30,
        location="Southern Island (Eon Ticket)", reset_kind="RUN_REINTERACT",
        choreography_status="HARDWARE_PROVEN_SHARED_ORAS_RUN_REINTERACT_2026_08_31",
        notes="Eon Ticket battle only. Shared ORAS hardware-proven loop: RUN, prove saved field, press A to battle again; OR story Latios is a gift.",
    ),
    "latios_eon": _p(
        "latios_eon", "Latios", 381, games=("alpha_sapphire",), expected_level=30,
        location="Southern Island (Eon Ticket)", reset_kind="RUN_REINTERACT",
        choreography_status="HARDWARE_PROVEN_SHARED_ORAS_RUN_REINTERACT_2026_08_31",
        notes="Eon Ticket battle only. Shared ORAS hardware-proven loop: RUN, prove saved field, press A to battle again; AS story Latias is a gift.",
    ),

    # Nameless Cavern
    "uxie": _ring("uxie", "Uxie", 480, location="Nameless Cavern", notes="Time/party requirements must already be satisfied before saving."),
    "mesprit": _ring("mesprit", "Mesprit", 481, location="Nameless Cavern", notes="Time/party requirements must already be satisfied before saving."),
    "azelf": _ring("azelf", "Azelf", 482, location="Nameless Cavern", notes="Time/party requirements must already be satisfied before saving."),

    # Dimensional rifts
    "dialga": _p(
        "dialga", "Dialga", 483,
        games=("alpha_sapphire",), family="soaring_static",
        location="Dimensional Rift (Dewford takeoff)", expected_zone_id=8,
        trigger_kind="SOARING_RIFT_MAPPER", choreography_status="SOARING_RIFT_MANUAL_MAPPER_HARDWARE_PROVEN_AS_2026_08_29",
        required_party_species=(480, 481, 482),
        notes="Hardware-proven manual mapper: Dewford reset + Lake Trio + Y takeoff + manual rift entry + Dialga PK6. CH traces SkyTrip external writable state references to derive autonomous flight authority.",
    ),
    "palkia": _p(
        "palkia", "Palkia", 484,
        games=("omega_ruby",), family="soaring_static",
        location="Dimensional Rift (Dewford takeoff)", expected_zone_id=8,
        trigger_kind="SOARING_RIFT_MAPPER", choreography_status="SOARING_RIFT_MANUAL_MAPPER_HARDWARE_PROVEN_AS_2026_08_29",
        required_party_species=(480, 481, 482),
        notes="Shared ORAS manual Dimensional Rift path is proven; autonomous Soaring remains unfinished.",
    ),
    "giratina": _p(
        "giratina", "Giratina", 487,
        family="soaring_static", location="Dimensional Rift (Dewford takeoff)", expected_zone_id=8,
        trigger_kind="SOARING_RIFT_MAPPER", choreography_status="SOARING_RIFT_MAPPER_NEEDS_HARDWARE",
        required_party_species=(483, 484),
        notes="Mapper mode: requires Dialga and Palkia in party; save in Dewford Town.",
    ),

    # Crescent / Pathless / Trackless
    "cresselia": _ring("cresselia", "Cresselia", 488, location="Crescent Isle"),
    "cobalion": _ring("cobalion", "Cobalion", 638, location="Pathless Plain"),
    "terrakion": _ring("terrakion", "Terrakion", 639, location="Pathless Plain"),
    "virizion": _ring("virizion", "Virizion", 640, location="Pathless Plain"),
    "raikou": _ring("raikou", "Raikou", 243, location="Trackless Forest"),
    "entei": _ring("entei", "Entei", 244, location="Trackless Forest"),
    "suicune": _ring("suicune", "Suicune", 245, location="Trackless Forest"),

    # Storm clouds / Fabled Cave / Gnarled Den
    "tornadus": _p("tornadus", "Tornadus", 641, games=("omega_ruby",), family="soaring_static", trigger_kind="SOARING_WEATHER_UNIMPLEMENTED", choreography_status="SOARING_WEATHER_NEEDS_MAPPER", location="Storm Clouds"),
    "thundurus": _p("thundurus", "Thundurus", 642, games=("alpha_sapphire",), family="soaring_static", trigger_kind="SOARING_WEATHER_UNIMPLEMENTED", choreography_status="SOARING_WEATHER_NEEDS_MAPPER", location="Storm Clouds"),
    "landorus": _p("landorus", "Landorus", 645, family="soaring_static", trigger_kind="SOARING_WEATHER_UNIMPLEMENTED", choreography_status="SOARING_WEATHER_NEEDS_MAPPER", location="Storm Clouds"),
    "reshiram": _ring("reshiram", "Reshiram", 643, games=("omega_ruby",), location="Fabled Cave"),
    "zekrom": _ring("zekrom", "Zekrom", 644, games=("alpha_sapphire",), location="Fabled Cave"),
    "kyurem": _ring("kyurem", "Kyurem", 646, location="Gnarled Den"),
}


# Vanilla ORAS story encounters that are explicitly not ordinary shiny-reset
# targets.  They remain visible to diagnostics but cannot instantiate a hunt.
SHINY_LOCKED_STATIC_PROFILES: dict[str, StaticEncounterProfile] = {
    "groudon": _p("groudon", "Groudon", 383, games=("omega_ruby",), shiny_locked=True, choreography_status="LOCKED_NOT_HUNTABLE", expected_level=45, location="Cave of Origin"),
    "kyogre": _p("kyogre", "Kyogre", 382, games=("alpha_sapphire",), shiny_locked=True, choreography_status="LOCKED_NOT_HUNTABLE", expected_level=45, location="Cave of Origin"),
    "rayquaza": _p("rayquaza", "Rayquaza", 384, shiny_locked=True, choreography_status="LOCKED_NOT_HUNTABLE", expected_level=70, location="Sky Pillar"),
    "deoxys": _p("deoxys", "Deoxys", 386, shiny_locked=True, choreography_status="LOCKED_NOT_HUNTABLE", expected_level=80, location="Sky Pillar"),
}


def static_profiles_for_game(game_key: str, *, include_locked: bool = False) -> tuple[StaticEncounterProfile, ...]:
    items = [p for p in STATIC_PROFILES.values() if game_key in p.games]
    if include_locked:
        items.extend(p for p in SHINY_LOCKED_STATIC_PROFILES.values() if game_key in p.games)
    return tuple(sorted(items, key=lambda p: (p.family, p.name.lower())))


def get_static_profile(key: str) -> StaticEncounterProfile:
    k = str(key or "").strip().lower()
    if k in STATIC_PROFILES:
        return STATIC_PROFILES[k]
    if k in SHINY_LOCKED_STATIC_PROFILES:
        return SHINY_LOCKED_STATIC_PROFILES[k]
    raise KeyError(f"unknown ORAS static profile {key!r}")


def _identity(mon: dict) -> tuple[int, str, str]:
    species = int(mon.get("species") or 0)
    pid = str(mon.get("pid") or mon.get("pokemon_pid") or "").upper()
    ec = str(mon.get("ec") or "").upper()
    return species, pid, ec


def _u32(raw: bytes, off: int = 0) -> int:
    return struct.unpack_from("<I", raw, off)[0]


def _f32(raw: bytes, off: int = 0) -> float:
    return struct.unpack_from("<f", raw, off)[0]


def _grid(world: float) -> int:
    return int(round((float(world) - 9.0) / 18.0))


@dataclass(frozen=True)
class SavedFieldAnchor:
    zone: int
    grid_x: int
    grid_z: int
    world_x: float
    world_z: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "zone": self.zone,
            "grid": [self.grid_x, self.grid_z],
            "world": [self.world_x, self.world_z],
        }


def validate_any_loaded_field(bridge) -> dict:
    """Generic post-Continue overworld authority for Static bootstrap.

    Unlike :func:`validate_saved_field_anchor`, this deliberately does not care
    where the player was standing when the PC hunt was started.  It is used
    only as the final gate of the *first* soft-reset route.  Once the freshly
    loaded save reaches a stable overworld, the worker captures that position
    as the exact saved-field anchor and all later reset cycles must return to
    it.

    The reset route itself already requires the DllField module before this
    callback can win.  Here we add RAM proof that battle is inactive and the
    primary/secondary field coordinates are finite.  The secondary coordinate
    copy may still be zero/uninitialised on the already-proven OR reset path.
    """
    battle = _u32(bridge.read(BATTLE_STATE, 4))
    zone = _u32(bridge.read(ZONE_ADDR, 4)) & 0xFFFF
    p = bridge.read(PRIMARY_BASE, 12)
    s = bridge.read(SECONDARY_BASE, 12)
    x, z = _f32(p, 0), _f32(p, 8)
    sx, sz = _f32(s, 0), _f32(s, 8)
    primary_finite = math.isfinite(x) and math.isfinite(z)
    secondary_finite = math.isfinite(sx) and math.isfinite(sz)
    secondary_zero = secondary_finite and abs(sx) <= 0.01 and abs(sz) <= 0.01
    checks = {
        "battle_inactive": battle == BATTLE_INACTIVE,
        "primary_finite": primary_finite,
        "secondary_finite_or_uninitialized": secondary_finite or secondary_zero,
    }
    authority = all(checks.values())
    grid = [_grid(x), _grid(z)] if primary_finite else [None, None]
    return {
        "authority": authority,
        "authority_mode": "loaded_overworld_field" if authority else "failed",
        "checks": checks,
        "battle": f"0x{battle:08X}",
        "zone": zone,
        "grid": grid,
        "world_primary": [x, z],
        "world_secondary": [sx, sz],
    }


def read_saved_field_anchor(bridge) -> SavedFieldAnchor:
    """Capture one field position that can be revalidated after soft reset."""
    battle = _u32(bridge.read(BATTLE_STATE, 4))
    if battle != BATTLE_INACTIVE:
        raise RuntimeError(f"Static start requires overworld; battle=0x{battle:08X}")
    zone = _u32(bridge.read(ZONE_ADDR, 4)) & 0xFFFF
    p = bridge.read(PRIMARY_BASE, 12)
    x, z = _f32(p, 0), _f32(p, 8)
    if not (math.isfinite(x) and math.isfinite(z)):
        raise RuntimeError("Static saved-field primary coordinates are not finite")
    return SavedFieldAnchor(zone, _grid(x), _grid(z), x, z)


def validate_saved_field_anchor(bridge, anchor: SavedFieldAnchor) -> dict:
    """Fail-closed same-save-position authority for reset return.

    The primary shared ORAS field coordinates are mandatory.  The secondary
    copy is accepted when it agrees, or when it is still zero/uninitialised as
    already observed on the proven OR reset path.
    """
    battle = _u32(bridge.read(BATTLE_STATE, 4))
    zone = _u32(bridge.read(ZONE_ADDR, 4)) & 0xFFFF
    p = bridge.read(PRIMARY_BASE, 12)
    s = bridge.read(SECONDARY_BASE, 12)
    x, z = _f32(p, 0), _f32(p, 8)
    sx, sz = _f32(s, 0), _f32(s, 8)
    finite = all(math.isfinite(v) for v in (x, z, sx, sz))
    primary_close = finite and abs(x - anchor.world_x) <= FIELD_POSITION_EPSILON and abs(z - anchor.world_z) <= FIELD_POSITION_EPSILON
    grid = (_grid(x), _grid(z)) if finite else (None, None)
    secondary_zero = finite and abs(sx) <= 0.01 and abs(sz) <= 0.01
    secondary_close = finite and abs(sx - anchor.world_x) <= FIELD_SECONDARY_EPSILON and abs(sz - anchor.world_z) <= FIELD_SECONDARY_EPSILON
    checks = {
        "battle_inactive": battle == BATTLE_INACTIVE,
        "same_zone": zone == anchor.zone,
        "primary_finite": finite,
        "same_grid": grid == (anchor.grid_x, anchor.grid_z),
        "primary_close": primary_close,
        "secondary_close_or_uninitialized": secondary_close or secondary_zero,
    }
    authority = all(checks.values())
    return {
        "authority": authority,
        "authority_mode": (
            "same_saved_field_dual_coordinate"
            if authority and secondary_close
            else "same_saved_field_primary_secondary_uninitialized"
            if authority and secondary_zero
            else "failed"
        ),
        "checks": checks,
        "battle": f"0x{battle:08X}",
        "zone": zone,
        "grid": list(grid),
        "world_primary": [x, z],
        "world_secondary": [sx, sz],
        "anchor": anchor.as_dict(),
    }


@dataclass
class StaticEncounterStateMachine:
    profile: StaticEncounterProfile
    game_key: str
    state: StaticState = StaticState.IDLE
    attempts: int = 0
    last_identity: tuple[int, str, str] | None = None
    events: list[dict] = field(default_factory=list)

    def __post_init__(self):
        if self.profile.shiny_locked:
            self.state = StaticState.SAFETY_HOLD
            raise ValueError(f"{self.profile.name} is shiny-locked in vanilla ORAS")
        if self.game_key not in self.profile.games:
            raise ValueError(f"{self.profile.name} is not available for static hunting in {self.game_key}")

    def _event(self, kind: str, **payload) -> dict:
        rec = {"event": kind, "state": self.state.value, **payload}
        self.events.append(rec)
        return rec

    def field_ready(self) -> dict:
        if self.state not in {StaticState.IDLE, StaticState.NONSHINY_RESET_REQUIRED}:
            raise RuntimeError(f"field_ready invalid from {self.state.value}")
        self.state = StaticState.FIELD_READY
        return self._event("FIELD_READY")

    def trigger_sent(self) -> dict:
        if self.state != StaticState.FIELD_READY:
            raise RuntimeError(f"trigger_sent invalid from {self.state.value}")
        self.attempts += 1
        self.state = StaticState.TRIGGERED
        return self._event("TRIGGER_SENT", attempt=self.attempts)

    def battle_ready(self) -> dict:
        if self.state != StaticState.TRIGGERED:
            raise RuntimeError(f"battle_ready invalid from {self.state.value}")
        self.state = StaticState.BATTLE_READY
        return self._event("BATTLE_READY", attempt=self.attempts)

    def opponent(self, mon: dict) -> dict:
        if self.state != StaticState.BATTLE_READY:
            raise RuntimeError(f"opponent invalid from {self.state.value}")
        valid = bool(mon.get("valid") and mon.get("checksum_valid", True))
        ident = _identity(mon)
        if not valid:
            self.state = StaticState.SAFETY_HOLD
            return self._event("INVALID_PK6", identity=ident)
        if ident[0] != self.profile.species:
            self.state = StaticState.SAFETY_HOLD
            return self._event("WRONG_SPECIES", expected=self.profile.species, observed=ident[0], identity=ident)
        if not ident[1] or not ident[2]:
            self.state = StaticState.SAFETY_HOLD
            return self._event("INCOMPLETE_IDENTITY", identity=ident)
        if self.last_identity is not None and ident == self.last_identity:
            self.state = StaticState.SAFETY_HOLD
            return self._event("STALE_IDENTITY", identity=ident)
        self.last_identity = ident
        self.state = StaticState.OPPONENT_VALIDATED
        if bool(mon.get("is_shiny")):
            self.state = StaticState.SHINY_HOLD
            return self._event("SHINY_HOLD", identity=ident, attempt=self.attempts)
        self.state = StaticState.NONSHINY_RESET_REQUIRED
        return self._event("NONSHINY_RESET_REQUIRED", identity=ident, attempt=self.attempts)

    def accidental_encounter(self, mon: dict, *, reason: str = "tutorial identity mismatch") -> dict:
        """Reject a valid but non-target battle and return to the normal reset path.

        DexNav sneaking can naturally collide with an ordinary Route 101 wild
        encounter before reaching the scripted tutorial target.  That is not a
        corrupt/static-safety condition, so keep the attempt accounting but
        transition to NONSHINY_RESET_REQUIRED and let the worker soft-reset to
        the saved pre-tutorial field.
        """
        if self.state != StaticState.BATTLE_READY:
            raise RuntimeError(f"accidental_encounter invalid from {self.state.value}")
        ident = _identity(mon)
        self.state = StaticState.NONSHINY_RESET_REQUIRED
        return self._event(
            "ACCIDENTAL_ENCOUNTER_RESET",
            identity=ident,
            observed_species=ident[0],
            reason=str(reason),
            attempt=self.attempts,
        )

    def captured(self) -> dict:
        if self.state != StaticState.SHINY_HOLD:
            raise RuntimeError(f"captured invalid from {self.state.value}")
        self.state = StaticState.CAPTURED
        return self._event("CAPTURED", identity=self.last_identity)

    def snapshot(self) -> dict:
        return {
            "profile": self.profile.key,
            "name": self.profile.name,
            "species": self.profile.species,
            "game": self.game_key,
            "state": self.state.value,
            "attempts": self.attempts,
            "last_identity": self.last_identity,
            "events": list(self.events),
            "choreography_status": self.profile.choreography_status,
        }
