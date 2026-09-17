from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import Iterable, Optional

BOX_SIZE = 0x88
PARTY_SIZE = 0xEC
DATA_OFFSET = 0x08
DATA_SIZE = 0x80
MAX_GEN4_SPECIES = 493

# pret/pokeheartgold src/pokemon.c::GetSubstruct
_BLOCK_OFFSETS = (
    (0x00, 0x20, 0x40, 0x60),
    (0x00, 0x20, 0x60, 0x40),
    (0x00, 0x40, 0x20, 0x60),
    (0x00, 0x60, 0x20, 0x40),
    (0x00, 0x40, 0x60, 0x20),
    (0x00, 0x60, 0x40, 0x20),
    (0x20, 0x00, 0x40, 0x60),
    (0x20, 0x00, 0x60, 0x40),
    (0x40, 0x00, 0x20, 0x60),
    (0x60, 0x00, 0x20, 0x40),
    (0x40, 0x00, 0x60, 0x20),
    (0x60, 0x00, 0x40, 0x20),
    (0x20, 0x40, 0x00, 0x60),
    (0x20, 0x60, 0x00, 0x40),
    (0x40, 0x20, 0x00, 0x60),
    (0x60, 0x20, 0x00, 0x40),
    (0x40, 0x60, 0x00, 0x20),
    (0x60, 0x40, 0x00, 0x20),
    (0x20, 0x40, 0x60, 0x00),
    (0x20, 0x60, 0x40, 0x00),
    (0x40, 0x20, 0x60, 0x00),
    (0x60, 0x20, 0x40, 0x00),
    (0x40, 0x60, 0x20, 0x00),
    (0x60, 0x40, 0x20, 0x00),
    (0x00, 0x20, 0x40, 0x60),
    (0x00, 0x20, 0x60, 0x40),
    (0x00, 0x40, 0x20, 0x60),
    (0x00, 0x60, 0x20, 0x40),
    (0x00, 0x40, 0x60, 0x20),
    (0x00, 0x60, 0x40, 0x20),
    (0x20, 0x00, 0x40, 0x60),
    (0x20, 0x00, 0x60, 0x40),
)

NATURES = (
    "Hardy", "Lonely", "Brave", "Adamant", "Naughty",
    "Bold", "Docile", "Relaxed", "Impish", "Lax",
    "Timid", "Hasty", "Serious", "Jolly", "Naive",
    "Modest", "Mild", "Quiet", "Bashful", "Rash",
    "Calm", "Gentle", "Sassy", "Careful", "Quirky",
)

HP_TYPES = (
    "Fighting", "Flying", "Poison", "Ground", "Rock", "Bug", "Ghost", "Steel",
    "Fire", "Water", "Grass", "Electric", "Psychic", "Ice", "Dragon", "Dark",
)


@dataclass(frozen=True)
class PK4:
    address: int
    pid: int
    checksum: int
    flags: int
    species: int
    held_item: int
    ot_id: int
    experience: int
    friendship: int
    ability: int
    language: int
    evs: tuple[int, int, int, int, int, int]
    moves: tuple[int, int, int, int]
    ivs: tuple[int, int, int, int, int, int]
    is_egg: bool
    has_nickname: bool
    gender: int
    form: int
    egg_location: int
    met_location: int
    nature: str
    shiny: bool
    shiny_value: int
    hidden_power_type: str
    hidden_power_power: int
    level: Optional[int]

    @property
    def identity(self) -> tuple[int, int, int]:
        return (self.species, self.pid, self.ot_id)


def _decrypt_words(data: bytes, seed: int) -> bytes:
    if len(data) != DATA_SIZE:
        raise ValueError(f"expected {DATA_SIZE} encrypted bytes, got {len(data)}")

    out = bytearray(DATA_SIZE)
    state = seed & 0xFFFFFFFF
    for i in range(0, DATA_SIZE, 2):
        state = (state * 1103515245 + 24691) & 0xFFFFFFFF
        key = (state >> 16) & 0xFFFF
        word = struct.unpack_from("<H", data, i)[0] ^ key
        struct.pack_into("<H", out, i, word)
    return bytes(out)


def _checksum(data: bytes) -> int:
    return sum(struct.unpack("<64H", data)) & 0xFFFF


def _hidden_power(ivs: tuple[int, int, int, int, int, int]) -> tuple[str, int]:
    hp, atk, defense, speed, spa, spd = ivs
    order = (hp, atk, defense, speed, spa, spd)
    type_bits = sum(((v & 1) << i) for i, v in enumerate(order))
    power_bits = sum((((v >> 1) & 1) << i) for i, v in enumerate(order))
    type_idx = (type_bits * 15) // 63
    power = (power_bits * 40) // 63 + 30
    return HP_TYPES[type_idx], power


def parse_pk4(raw: bytes, *, address: int = 0) -> Optional[PK4]:
    if len(raw) < BOX_SIZE:
        return None

    pid, flags, checksum = struct.unpack_from("<IHH", raw, 0)

    # Only the three documented low flag bits are meaningful.
    if flags & 0xFFF8:
        return None
    if pid in (0, 0xFFFFFFFF) or checksum == 0:
        return None

    data = raw[DATA_OFFSET:DATA_OFFSET + DATA_SIZE]
    box_decrypted = bool(flags & 0x2)
    plain = data if box_decrypted else _decrypt_words(data, checksum)

    if _checksum(plain) != checksum:
        return None

    shuffle = (pid & 0x3E000) >> 13
    a_off, b_off, c_off, d_off = _BLOCK_OFFSETS[shuffle]

    block_a = plain[a_off:a_off + 0x20]
    block_b = plain[b_off:b_off + 0x20]

    species, held_item = struct.unpack_from("<HH", block_a, 0)
    if not (1 <= species <= MAX_GEN4_SPECIES):
        return None

    ot_id = struct.unpack_from("<I", block_a, 0x04)[0]
    exp = struct.unpack_from("<I", block_a, 0x08)[0]
    friendship = block_a[0x0C]
    ability = block_a[0x0D]
    language = block_a[0x0F]
    evs = tuple(block_a[0x10:0x16])

    moves = struct.unpack_from("<4H", block_b, 0x00)
    iv_word = struct.unpack_from("<I", block_b, 0x10)[0]
    ivs = (
        (iv_word >> 0) & 31,
        (iv_word >> 5) & 31,
        (iv_word >> 10) & 31,
        (iv_word >> 15) & 31,
        (iv_word >> 20) & 31,
        (iv_word >> 25) & 31,
    )
    is_egg = bool((iv_word >> 30) & 1)
    has_nickname = bool((iv_word >> 31) & 1)

    form_flags = block_b[0x18]
    gender = (form_flags >> 1) & 0x3
    form = (form_flags >> 3) & 0x1F
    egg_location = struct.unpack_from("<H", block_b, 0x1C)[0]
    met_location = struct.unpack_from("<H", block_b, 0x1E)[0]

    tid = ot_id & 0xFFFF
    sid = (ot_id >> 16) & 0xFFFF
    pid_lo = pid & 0xFFFF
    pid_hi = (pid >> 16) & 0xFFFF
    shiny_value = tid ^ sid ^ pid_lo ^ pid_hi
    shiny = shiny_value < 8

    hp_type, hp_power = _hidden_power(ivs)

    level: Optional[int] = None
    if len(raw) >= PARTY_SIZE:
        candidate_level = raw[0x8C]
        if 1 <= candidate_level <= 100:
            level = candidate_level

    return PK4(
        address=address,
        pid=pid,
        checksum=checksum,
        flags=flags,
        species=species,
        held_item=held_item,
        ot_id=ot_id,
        experience=exp,
        friendship=friendship,
        ability=ability,
        language=language,
        evs=evs,
        moves=tuple(moves),
        ivs=ivs,
        is_egg=is_egg,
        has_nickname=has_nickname,
        gender=gender,
        form=form,
        egg_location=egg_location,
        met_location=met_location,
        nature=NATURES[pid % 25],
        shiny=shiny,
        shiny_value=shiny_value,
        hidden_power_type=hp_type,
        hidden_power_power=hp_power,
        level=level,
    )


def scan_pk4(
    memory: bytes,
    *,
    base_address: int = 0x02000000,
    stride: int = 4,
    species: Optional[set[int]] = None,
) -> list[PK4]:
    """
    Scan a raw ARM9-RAM snapshot for checksum-valid Gen-4 Pokemon structures.

    The cheap header filters make this practical across the full 4 MiB DS main
    RAM image without using region-specific executable addresses.
    """
    found: list[PK4] = []
    limit = len(memory) - BOX_SIZE

    for off in range(0, max(0, limit + 1), stride):
        pid, flags, checksum = struct.unpack_from("<IHH", memory, off)

        if pid in (0, 0xFFFFFFFF) or checksum == 0:
            continue
        if flags & 0xFFF8:
            continue

        end = min(len(memory), off + PARTY_SIZE)
        mon = parse_pk4(memory[off:end], address=base_address + off)
        if mon is None:
            continue
        if species is not None and mon.species not in species:
            continue
        found.append(mon)

    return found


def newly_seen(before: Iterable[PK4], after: Iterable[PK4]) -> list[PK4]:
    old = {m.identity for m in before}
    return [m for m in after if m.identity not in old]
