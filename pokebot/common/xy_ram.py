"""Pokémon X/Y Gen 6 RAM reference profile.

Initial addresses are independently documented by PokeReader and are deliberately
kept separate from the hardware-proven ORAS RAM module until verified on Pokebot.
PK6 parsing is shared with pokebot.common.pk6 (same 232-byte stored structure).
"""
from __future__ import annotations

import struct
from pokebot.common.pk6 import parse_pk6
from pokebot.common.species_names import SPECIES_NAMES

TRAINER_IDS = 0x08C79C3C
PARTY0 = 0x08CE1CF8
WILD0 = 0x081FF744
PARTY_STRIDE = 484
WILD_STRIDE = 484
PARTY_SLOTS = 6
PK6_SIZE = 232

# RNG/reference values retained for later XY work.
INITIAL_SEED = 0x08C52844
MT_STATE_INDEX = 0x08C52848
MT_START = 0x08C5284C
TINYMT_STATE = 0x08C52808
RADAR_CHAIN = 0x08D1B2B8


def read_trainer_ids(bridge):
    return struct.unpack("<HH", bridge.read(TRAINER_IDS, 4))


def read_party_raw(bridge):
    return [bridge.read(PARTY0 + slot * PARTY_STRIDE, PK6_SIZE) for slot in range(PARTY_SLOTS)]


def read_party_decoded(bridge):
    decoded = []
    for slot, raw in enumerate(read_party_raw(bridge), 1):
        p = parse_pk6(raw, SPECIES_NAMES)
        p["slot"] = slot
        decoded.append(p)
    return decoded


def read_wild_decoded(bridge, slot=0):
    raw = bridge.read(WILD0 + int(slot) * WILD_STRIDE, PK6_SIZE)
    p = parse_pk6(raw, SPECIES_NAMES)
    p["slot"] = int(slot)
    return p
