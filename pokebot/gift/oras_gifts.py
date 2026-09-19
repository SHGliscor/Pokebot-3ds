from __future__ import annotations

"""ORAS in-game Gift Pokémon profiles.

ORAS is treated as one shared runtime engine.  Version differences live only in
profile availability/species, not in duplicated automation branches.

The first production gift family is ``A_GIFT_TO_PARTY``: save facing the giver
(or gift Poké Ball) with at least one free party slot.  Pokebot soft-resets to
that exact saved field anchor, snapshots the existing party, advances a bounded
A dialogue, and treats a newly-populated checksum-valid PK6 in PARTY SLOT 2-6
as authority.  Slot 1 remains the existing lead; gifts that enter the party are
therefore detected by the same live PK6 reader already proven by Party Viewer.
Non-shiny gifts reset immediately; a shiny stops with no further input.

Fossil revival uses a separate ``FOSSIL_BATCH_5`` policy: begin with exactly
one existing lead in slot 1 and five empty party slots, revive into slots 2-6,
inspect every new checksum-valid PK6 immediately, HOLD on the first shiny, and
soft-reset only after all five revived Pokémon are confirmed non-shiny.

Choice menus and long scripted cutscenes remain visible but disabled until their
specific input choreography is mapped.  This does not affect the existing
opening Hoenn Starter engine.
"""

from dataclasses import dataclass
from typing import Iterable


# All fossil species Devon can revive in ORAS, including Jaw/Sail fossils
# transferred in from X/Y.  The mixed five-batch hunt accepts any of these
# checksum-valid PK6s in party slots 2-6.
FOSSIL_SPECIES = frozenset({
    138, 140, 142, 345, 347, 408, 410, 564, 566, 696, 698,
})
FOSSIL_ITEM_TO_SPECIES = {
    99: 345,   # Root Fossil -> Lileep
    100: 347,  # Claw Fossil -> Anorith
    101: 138,  # Helix Fossil -> Omanyte
    102: 140,  # Dome Fossil -> Kabuto
    103: 142,  # Old Amber -> Aerodactyl
    104: 410,  # Armor Fossil -> Shieldon
    105: 408,  # Skull Fossil -> Cranidos
    572: 564,  # Cover Fossil -> Tirtouga
    573: 566,  # Plume Fossil -> Archen
    710: 696,  # Jaw Fossil -> Tyrunt
    711: 698,  # Sail Fossil -> Amaura
}
FOSSIL_ITEM_NAMES = {
    99: "Root Fossil", 100: "Claw Fossil", 101: "Helix Fossil",
    102: "Dome Fossil", 103: "Old Amber", 104: "Armor Fossil",
    105: "Skull Fossil", 572: "Cover Fossil", 573: "Plume Fossil",
    710: "Jaw Fossil", 711: "Sail Fossil",
}


@dataclass(frozen=True)
class GiftPokemonProfile:
    key: str
    name: str
    species: int
    location: str
    games: tuple[str, ...] = ("alpha_sapphire", "omega_ruby")
    expected_level: int | None = None
    trigger_kind: str = "A_GIFT_TO_PARTY"
    automation_ready: bool = True
    shiny_locked: bool = False
    max_a_presses: int = 18
    notes: str = ""

    @property
    def display_name(self) -> str:
        return f"{self.name} — {self.location}"


def _p(key: str, name: str, species: int, location: str, **kwargs) -> GiftPokemonProfile:
    return GiftPokemonProfile(key=key, name=name, species=species, location=location, **kwargs)


GIFT_PROFILES: dict[str, GiftPokemonProfile] = {
    # Direct gifts / eggs.  All are resolved through the shared live-party PK6
    # chain, so no title-specific RAM table is used.
    "wynaut_egg": _p("wynaut_egg", "Wynaut Egg", 360, "Lavaridge Town", expected_level=None,
                     notes="Save facing the old woman by the hot springs with a free party slot."),
    "togepi_egg": _p("togepi_egg", "Togepi Egg", 175, "Lavaridge Town", expected_level=None,
                     notes="Available after the story Groudon/Kyogre event. Save facing the old woman with a free party slot."),
    "castform": _p("castform", "Castform", 351, "Weather Institute", expected_level=30,
                  notes="Save facing the scientist after the Team Magma/Aqua event, with a free party slot."),
    "beldum": _p("beldum", "Beldum", 374, "Steven's House — Mossdeep City", expected_level=1,
                 notes="Post-Delta Episode gift Poké Ball. Save facing the Poké Ball with a free party slot."),
    "camerupt": _p("camerupt", "Camerupt", 323, "Battle Resort", expected_level=40,
                   notes="Gift from the former Team Magma grunt. Save facing the giver with a free party slot."),
    "sharpedo": _p("sharpedo", "Sharpedo", 319, "Battle Resort", expected_level=40,
                   notes="Gift from the former Team Aqua grunt. Save facing the giver with a free party slot."),


    # Production fossil hunt: revive whichever five valid fossils the Devon
    # menu offers, filling party slots 2-6.  Each revived PK6 is checked
    # immediately; any shiny HOLDs before another fossil is accepted.
    "fossil_batch": _p("fossil_batch", "Fossil Batch — Any 1–5 Fossils", 0, "Devon Corporation — Rustboro City", expected_level=20,
                        trigger_kind="FOSSIL_BATCH_5", max_a_presses=30,
                        notes="Start with one lead and empty party slots. Automatically revives however many supported fossils are available (1–5, capped at five), then resets after all are confirmed non-shiny. Fossils may be mixed species."),

    # Fossil revival batches.  ORAS is one shared engine: availability of a
    # fossil item may differ by version/acquisition route, but Devon can revive
    # any supported fossil present in the Bag.  The first hardware pass uses a
    # deliberately simple A-dialogue mapper; party PK6 is the only authority.
    "fossil_lileep": _p("fossil_lileep", "Lileep — Root Fossil", 345, "Devon Corporation — Rustboro City", expected_level=20,
                         trigger_kind="FOSSIL_TARGET_MENU_UNMAPPED", automation_ready=False, max_a_presses=30,
                         notes="Start with one lead + five empty party slots and at least five Root Fossils. Revive five before reset."),
    "fossil_anorith": _p("fossil_anorith", "Anorith — Claw Fossil", 347, "Devon Corporation — Rustboro City", expected_level=20,
                          trigger_kind="FOSSIL_TARGET_MENU_UNMAPPED", automation_ready=False, max_a_presses=30,
                          notes="Start with one lead + five empty party slots and at least five Claw Fossils. Revive five before reset."),
    "fossil_aerodactyl": _p("fossil_aerodactyl", "Aerodactyl — Old Amber", 142, "Devon Corporation — Rustboro City", expected_level=20,
                             trigger_kind="FOSSIL_TARGET_MENU_UNMAPPED", automation_ready=False, max_a_presses=30,
                             notes="Start with one lead + five empty party slots and at least five Old Ambers. Revive five before reset."),
    "fossil_kabuto": _p("fossil_kabuto", "Kabuto — Dome Fossil", 140, "Devon Corporation — Rustboro City", expected_level=20,
                         trigger_kind="FOSSIL_TARGET_MENU_UNMAPPED", automation_ready=False, max_a_presses=30,
                         notes="Start with one lead + five empty party slots and at least five Dome Fossils. Revive five before reset."),
    "fossil_omanyte": _p("fossil_omanyte", "Omanyte — Helix Fossil", 138, "Devon Corporation — Rustboro City", expected_level=20,
                          trigger_kind="FOSSIL_TARGET_MENU_UNMAPPED", automation_ready=False, max_a_presses=30,
                          notes="Start with one lead + five empty party slots and at least five Helix Fossils. Revive five before reset."),
    "fossil_shieldon": _p("fossil_shieldon", "Shieldon — Armor Fossil", 410, "Devon Corporation — Rustboro City", expected_level=20,
                           trigger_kind="FOSSIL_TARGET_MENU_UNMAPPED", automation_ready=False, max_a_presses=30,
                           notes="Start with one lead + five empty party slots and at least five Armor Fossils. Revive five before reset."),
    "fossil_cranidos": _p("fossil_cranidos", "Cranidos — Skull Fossil", 408, "Devon Corporation — Rustboro City", expected_level=20,
                           trigger_kind="FOSSIL_TARGET_MENU_UNMAPPED", automation_ready=False, max_a_presses=30,
                           notes="Start with one lead + five empty party slots and at least five Skull Fossils. Revive five before reset."),
    "fossil_archen": _p("fossil_archen", "Archen — Plume Fossil", 566, "Devon Corporation — Rustboro City", expected_level=20,
                         trigger_kind="FOSSIL_TARGET_MENU_UNMAPPED", automation_ready=False, max_a_presses=30,
                         notes="Start with one lead + five empty party slots and at least five Plume Fossils. Revive five before reset."),
    "fossil_tirtouga": _p("fossil_tirtouga", "Tirtouga — Cover Fossil", 564, "Devon Corporation — Rustboro City", expected_level=20,
                           trigger_kind="FOSSIL_TARGET_MENU_UNMAPPED", automation_ready=False, max_a_presses=30,
                           notes="Start with one lead + five empty party slots and at least five Cover Fossils. Revive five before reset."),

    "fossil_tyrunt": _p("fossil_tyrunt", "Tyrunt — Jaw Fossil", 696, "Devon Corporation — Rustboro City", expected_level=20,
                         trigger_kind="FOSSIL_TARGET_MENU_UNMAPPED", automation_ready=False, max_a_presses=30,
                         notes="Jaw Fossil is transferable from X/Y. Use Fossil Batch — Any 1–5 Fossils for the automated mixed batch."),
    "fossil_amaura": _p("fossil_amaura", "Amaura — Sail Fossil", 698, "Devon Corporation — Rustboro City", expected_level=20,
                         trigger_kind="FOSSIL_TARGET_MENU_UNMAPPED", automation_ready=False, max_a_presses=30,
                         notes="Sail Fossil is transferable from X/Y. Use Fossil Batch — Any 1–5 Fossils for the automated mixed batch."),

    # Later Birch choices are real gift hunts but use a choice UI.  Keep them
    # visible so the category is complete without pretending the input mapper
    # is already proven.
    "chikorita": _p("chikorita", "Chikorita", 152, "Route 101 — Professor Birch", expected_level=5,
                    trigger_kind="POSTGAME_BIRCH_STARTER", automation_ready=True, notes="Postgame Johto starter choice."),
    "cyndaquil": _p("cyndaquil", "Cyndaquil", 155, "Route 101 — Professor Birch", expected_level=5,
                    trigger_kind="POSTGAME_BIRCH_STARTER", automation_ready=True, notes="Postgame Johto starter choice."),
    "totodile": _p("totodile", "Totodile", 158, "Route 101 — Professor Birch", expected_level=5,
                   trigger_kind="POSTGAME_BIRCH_STARTER", automation_ready=True, notes="Postgame Johto starter choice."),
    "snivy": _p("snivy", "Snivy", 495, "Route 101 — Professor Birch", expected_level=5,
                trigger_kind="POSTGAME_BIRCH_STARTER", automation_ready=True, notes="Post-Delta Episode Unova starter choice."),
    "tepig": _p("tepig", "Tepig", 498, "Route 101 — Professor Birch", expected_level=5,
                trigger_kind="POSTGAME_BIRCH_STARTER", automation_ready=True, notes="Post-Delta Episode Unova starter choice."),
    "oshawott": _p("oshawott", "Oshawott", 501, "Route 101 — Professor Birch", expected_level=5,
                   trigger_kind="POSTGAME_BIRCH_STARTER", automation_ready=True, notes="Post-Delta Episode Unova starter choice."),
    "turtwig": _p("turtwig", "Turtwig", 387, "Route 101 — Professor Birch", expected_level=5,
                  trigger_kind="POSTGAME_BIRCH_STARTER", automation_ready=True, notes="Later postgame Sinnoh starter choice."),
    "chimchar": _p("chimchar", "Chimchar", 390, "Route 101 — Professor Birch", expected_level=5,
                   trigger_kind="POSTGAME_BIRCH_STARTER", automation_ready=True, notes="Later postgame Sinnoh starter choice."),
    "piplup": _p("piplup", "Piplup", 393, "Route 101 — Professor Birch", expected_level=5,
                 trigger_kind="POSTGAME_BIRCH_STARTER", automation_ready=True, notes="Later postgame Sinnoh starter choice."),

    # Scripted story gifts.  They are listed separately from the catchable Eon
    # Ticket statics.  Their long cutscene path needs its own mapper before the
    # bot can safely reset it unattended.
    "latios_story": _p("latios_story", "Latios (story gift)", 381, "Southern Island", games=("omega_ruby",), expected_level=30,
                      trigger_kind="SCRIPTED_STORY_GIFT", automation_ready=False, max_a_presses=0,
                      notes="Long scripted Southern Island gift sequence; not the Eon Ticket battle."),
    "latias_story": _p("latias_story", "Latias (story gift)", 380, "Southern Island", games=("alpha_sapphire",), expected_level=30,
                      trigger_kind="SCRIPTED_STORY_GIFT", automation_ready=False, max_a_presses=0,
                      notes="Long scripted Southern Island gift sequence; not the Eon Ticket battle."),

    # Cosplay Pikachu is intentionally not a shiny hunt target.
    "cosplay_pikachu": _p("cosplay_pikachu", "Cosplay Pikachu", 25, "Slateport City", expected_level=20,
                          trigger_kind="SHINY_LOCKED", automation_ready=False, shiny_locked=True, max_a_presses=0,
                          notes="Shiny-locked in ORAS."),
}


def gift_profiles_for_game(game_key: str, *, include_locked: bool = True) -> tuple[GiftPokemonProfile, ...]:
    key = str(game_key or "")
    # Fossil Batch supersedes the old target-specific fossil selector entries.
    # Keep those backend profiles for compatibility/reference, but hide them
    # from the normal Gift UI.
    items: Iterable[GiftPokemonProfile] = (
        p for p in GIFT_PROFILES.values()
        if key in p.games and p.trigger_kind != "FOSSIL_TARGET_MENU_UNMAPPED"
    )
    if not include_locked:
        items = (p for p in items if not p.shiny_locked)
    order = {"A_GIFT_TO_PARTY": 0, "FOSSIL_BATCH_5": 1, "POSTGAME_BIRCH_STARTER": 2, "BIRCH_CHOICE_MAPPER": 3, "SCRIPTED_STORY_GIFT": 4, "SHINY_LOCKED": 5}
    return tuple(sorted(items, key=lambda p: (order.get(p.trigger_kind, 9), p.location.lower(), p.name.lower())))


def get_gift_profile(key: str) -> GiftPokemonProfile:
    k = str(key or "").strip().lower()
    if k not in GIFT_PROFILES:
        raise KeyError(f"unknown ORAS gift profile {key!r}")
    return GIFT_PROFILES[k]


def gift_validation_label(profile: GiftPokemonProfile) -> str:
    if profile.shiny_locked:
        return "Shiny locked / not huntable"
    if profile.automation_ready and profile.trigger_kind == "A_GIFT_TO_PARTY":
        return "Shared ORAS party-PK6 gift engine"
    if profile.automation_ready and profile.trigger_kind == "FOSSIL_BATCH_5":
        return "Adaptive mixed 1–5 fossil batch • party PK6 hardware path proven"
    if profile.trigger_kind == "FOSSIL_TARGET_MENU_UNMAPPED":
        return "Use Fossil Batch — Any 1–5 Fossils (target-specific Devon menu selection not mapped)"
    if profile.trigger_kind == "POSTGAME_BIRCH_STARTER":
        return "Postgame Birch starter • house bootstrap → proven starter chooser → Party PK6"
    if profile.trigger_kind == "BIRCH_CHOICE_MAPPER":
        return "Birch choice mapper needed"
    if profile.trigger_kind == "SCRIPTED_STORY_GIFT":
        return "Scripted gift mapper needed"
    return "Not yet wired"


def parsed_identity(mon: dict) -> tuple[int, str, str] | None:
    if not (mon.get("valid") and mon.get("checksum_valid")):
        return None
    species = int(mon.get("species") or 0)
    if species <= 0:
        return None
    return species, str(mon.get("pid") or "").upper(), str(mon.get("ec") or "").upper()


def find_new_party_member(parsed_party: list[dict], baseline: set[tuple[int, str, str]]) -> dict | None:
    """Return the first newly-added checksum-valid party PK6 from slots 2-6."""
    rows = list(parsed_party or [])
    for index in range(1, min(6, len(rows))):
        mon = rows[index]
        ident = parsed_identity(mon)
        if ident is None or ident in baseline:
            continue
        out = dict(mon)
        out["party_slot"] = index + 1
        return out
    return None


def find_new_gift(parsed_party: list[dict], baseline: set[tuple[int, str, str]], species: int) -> dict | None:
    """Return a newly-added target PK6 from party slots 2-6.

    ORAS always has an existing lead in slot 1. Direct gifts with free party
    capacity populate one of slots 2-6. We intentionally do not treat slot 1
    as Gift authority so a pre-existing target species in the lead can never
    be mistaken for the newly accepted gift.
    """
    target = int(species)
    mon = find_new_party_member(parsed_party, baseline)
    if mon is not None and int(mon.get("species") or 0) == target:
        return mon
    return None
