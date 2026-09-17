from __future__ import annotations

from .wild_navigation import GrassPatch, Position, UnsafeStep

# HeartGold/SoulSilver English RAM locations reused by PokeBot-NDS.
HG_EN_ANCHOR_PTR = 0x021D4158
HG_EN_TRAINER_X = 0x021DA6F4
HG_EN_TRAINER_Z = 0x021DA6FC
HG_MAP_HEADER_FROM_ANCHOR = -0x22DA4


def _read_u32(backend, address: int) -> int:
    return int.from_bytes(backend.read_block(address, 4), "little")


def _read_u16(backend, address: int) -> int:
    return int.from_bytes(backend.read_block(address, 2), "little")


def _read_s16(backend, address: int) -> int:
    value = _read_u16(backend, address)
    return value - 0x10000 if value & 0x8000 else value


def read_hgss_english_position(backend) -> tuple[Position, int]:
    """Return live HGSS map/X/Z plus the dynamic map-header address."""
    anchor = _read_u32(backend, HG_EN_ANCHOR_PTR)
    map_addr = anchor + HG_MAP_HEADER_FROM_ANCHOR

    # HGSS stores the map header as a u16. PokeBot-NDS likewise reads it with
    # mword(), while trainer coordinates are the signed low 16 bits of the
    # game's coordinate words. Reading wider would consume unrelated adjacent
    # state and can produce false map-change or multi-tile safety holds.
    map_id = _read_u16(backend, map_addr)
    x = _read_s16(backend, HG_EN_TRAINER_X)
    z = _read_s16(backend, HG_EN_TRAINER_Z)
    return Position(map_id, x, z), map_addr


class HGSSEnglishGrassLock:
    """
    Strict HGSS movement guard.

    No D-pad input is sent until the destination tile has already been proven
    to be part of the connected encounter patch containing the start tile.
    The native melonDS guard then releases the direction on the first X/Z tile
    transition, so an unthrottled emulator cannot run across multiple tiles.
    """

    def __init__(self, backend, encounter_tiles):
        self.backend = backend
        self.encounter_tiles = frozenset((int(x), int(z)) for x, z in encounter_tiles)
        self.patch: GrassPatch | None = None
        self.map_addr: int | None = None

    def start(self) -> Position:
        position, map_addr = read_hgss_english_position(self.backend)
        self.patch = GrassPatch.connected(
            position.map_id,
            self.encounter_tiles,
            (position.x, position.z),
        )
        self.map_addr = map_addr
        return position

    def _safety_fail(self, message: str):
        try:
            self.backend.reset_input()
        finally:
            raise UnsafeStep(message)

    def step(self, direction: str) -> Position:
        if self.patch is None or self.map_addr is None:
            raise RuntimeError("grass lock has not been started")

        current, current_map_addr = read_hgss_english_position(self.backend)
        if current_map_addr != self.map_addr or current.map_id != self.patch.map_id:
            self._safety_fail(
                f"map changed while grass lock was active: expected {self.patch.map_id}, "
                f"got {current.map_id}"
            )

        # This is the hard grass boundary. If the destination is not in the
        # verified patch, destination() raises before any controller input.
        destination = self.patch.destination(current, direction)

        x, z = self.backend.guarded_step(
            direction,
            x_addr=HG_EN_TRAINER_X,
            z_addr=HG_EN_TRAINER_Z,
            map_addr=self.map_addr,
            expected_map=self.patch.map_id,
        )

        actual, actual_map_addr = read_hgss_english_position(self.backend)
        if actual_map_addr != self.map_addr or actual != Position(self.patch.map_id, x, z):
            self._safety_fail("HGSS coordinate state changed unexpectedly after guarded step")
        if actual != destination:
            self._safety_fail(
                f"guarded step landed on {actual}, expected verified tile {destination}"
            )
        return actual
