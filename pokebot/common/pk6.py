from __future__ import annotations

import hashlib
import struct

BLOCK_SIZE = 56
HEADER_SIZE = 8
BLOCK_ORDERS = (
    "ABCD","ABDC","ACBD","ACDB","ADBC","ADCB",
    "BACD","BADC","BCAD","BCDA","BDAC","BDCA",
    "CABD","CADB","CBAD","CBDA","CDAB","CDBA",
    "DABC","DACB","DBAC","DBCA","DCAB","DCBA",
)

NATURE_NAMES = (
    "Hardy","Lonely","Brave","Adamant","Naughty",
    "Bold","Docile","Relaxed","Impish","Lax",
    "Timid","Hasty","Serious","Jolly","Naive",
    "Modest","Mild","Quiet","Bashful","Rash",
    "Calm","Gentle","Sassy","Careful","Quirky",
)

def u16(data, off=0):
    return struct.unpack_from("<H", data, off)[0]

def u32(data, off=0):
    return struct.unpack_from("<I", data, off)[0]

def crypt_words(data, seed):
    out = bytearray(len(data))
    state = seed & 0xFFFFFFFF
    for off in range(0, len(data), 2):
        state = (0x41C64E6D * state + 0x6073) & 0xFFFFFFFF
        rand16 = (state >> 16) & 0xFFFF
        word = data[off] | (data[off + 1] << 8)
        word ^= rand16
        out[off] = word & 0xFF
        out[off + 1] = (word >> 8) & 0xFF
    return bytes(out)

def unshuffle_blocks(shuffled, ec):
    shift = ((ec >> 13) & 31) % 24
    order = BLOCK_ORDERS[shift]
    canonical = [b""] * 4
    for source_index, label in enumerate(order):
        start = source_index * BLOCK_SIZE
        canonical[ord(label) - ord("A")] = shuffled[start:start + BLOCK_SIZE]
    return b"".join(canonical)

def checksum(blocks):
    total = 0
    for off in range(0, len(blocks), 2):
        total = (total + u16(blocks, off)) & 0xFFFF
    return total

def parse_pk6(raw, species_names=None):
    ec = u32(raw, 0)
    dec = raw[:HEADER_SIZE] + unshuffle_blocks(crypt_words(raw[HEADER_SIZE:], ec), ec)

    sanity = u16(dec, 0x04)
    stored_checksum = u16(dec, 0x06)
    calc_checksum = checksum(dec[0x08:0xE8])
    species = u16(dec, 0x08)
    tid = u16(dec, 0x0C)
    sid = u16(dec, 0x0E)
    ability = dec[0x14]
    pid = u32(dec, 0x18)
    nature = dec[0x1C]
    gender_form = dec[0x1D]
    gender_code = (gender_form >> 1) & 0x03
    form = (gender_form >> 3) & 0x1F
    gender = {0: "♂", 1: "♀", 2: "—"}.get(gender_code, "—")

    # Gen 6 EVs / Pokérus state from decrypted Block A.
    ev_hp = dec[0x1E]
    ev_attack = dec[0x1F]
    ev_defense = dec[0x20]
    ev_speed = dec[0x21]
    ev_sp_attack = dec[0x22]
    ev_sp_defense = dec[0x23]
    pokerus_state = dec[0x2B]
    pokerus_days = pokerus_state & 0x0F
    pokerus_strain = (pokerus_state >> 4) & 0x0F
    if pokerus_strain == 0:
        pokerus_status = "Never infected"
    elif pokerus_days > 0:
        pokerus_status = f"Infected • strain {pokerus_strain} • {pokerus_days} day(s)"
    else:
        pokerus_status = f"Cured • strain {pokerus_strain}"

    # Gen 6 move IDs from decrypted Block B.
    moves = [
        u16(dec, 0x5A),
        u16(dec, 0x5C),
        u16(dec, 0x5E),
        u16(dec, 0x60),
    ]
    # Current PP and PP-Up counts live immediately after the four move IDs in
    # decrypted Block B.  These are read-only battle/preflight inputs for the
    # D19m safe move selector; starter/shiny identity semantics are unchanged.
    move_pp = [int(dec[0x62 + i]) for i in range(4)]
    move_pp_ups = [int(dec[0x66 + i]) for i in range(4)]

    shiny_xor = (tid ^ sid ^ (pid & 0xFFFF) ^ ((pid >> 16) & 0xFFFF)) & 0xFFFF
    iv_word = u32(dec, 0x74)

    valid = sanity == 0 and stored_checksum == calc_checksum and 1 <= species <= 721
    species_name = (
        species_names.get(species, f"Species {species}")
        if species_names
        else f"Species {species}"
    )

    return {
        "valid": valid,
        "checksum_valid": stored_checksum == calc_checksum,
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "ec": f"0x{ec:08X}",
        "sanity": f"0x{sanity:04X}",
        "checksum": f"0x{stored_checksum:04X}",
        "calculated_checksum": f"0x{calc_checksum:04X}",
        "species": species,
        "species_name": species_name,
        "tid": tid,
        "sid": sid,
        "pid": f"0x{pid:08X}",
        "shiny_xor": shiny_xor,
        "is_shiny": shiny_xor < 16,
        "nature": NATURE_NAMES[nature] if nature < len(NATURE_NAMES) else f"Nature {nature}",
        "gender": gender,
        "form": form,
        "ability_id": ability,
        "moves": moves,
        "move_pp": move_pp,
        "move_pp_ups": move_pp_ups,
        "evs": {
            "hp": ev_hp,
            "attack": ev_attack,
            "defense": ev_defense,
            "speed": ev_speed,
            "sp_attack": ev_sp_attack,
            "sp_defense": ev_sp_defense,
        },
        "pokerus_state": pokerus_state,
        "pokerus_days": pokerus_days,
        "pokerus_strain": pokerus_strain,
        "pokerus_status": pokerus_status,
        "ivs": {
            "hp": (iv_word >> 0) & 31,
            "attack": (iv_word >> 5) & 31,
            "defense": (iv_word >> 10) & 31,
            "speed": (iv_word >> 15) & 31,
            "sp_attack": (iv_word >> 20) & 31,
            "sp_defense": (iv_word >> 25) & 31,
        },
    }
