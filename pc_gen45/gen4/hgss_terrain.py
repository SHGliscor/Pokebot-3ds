from __future__ import annotations

from collections import deque

from .wild_navigation import GrassPatch, Position

MAIN_RAM_START = 0x02000000
MAIN_RAM_SIZE = 0x00400000
FIELD_SYSTEM_SIZE = 0x128

# FieldSystem offsets from pret/pokeheartgold include/field_system.h.
FS_LOCATION = 0x20
FS_MAP_MATRIX = 0x30
FS_TERRAIN_ATTRIBUTES = 0x5C
FS_RUNNING_FIELD_MAP = 0x68

# TerrainAttributes from pret/pokeheartgold include/terrain_attributes.h.
TERRAIN_MAP_INDEX_COUNT = 225
TERRAIN_DATA_OFFSET = 0xE2  # u8[225], one byte pad, then u16 terrain[]
TERRAIN_BLOCK_SIZE = 0x800
TERRAIN_BLOCK_COUNT_MAX = 16
TILES_PER_BLOCK = 32

# constants/metatile_behavior.h: 2 = tall grass, 3 = very tall grass.
GRASS_BEHAVIORS = frozenset({2, 3})


class TerrainReadError(RuntimeError):
    """Live HGSS field terrain could not be validated safely."""


def _in_main_ram(address: int, length: int = 1) -> bool:
    return (
        MAIN_RAM_START <= address
        and length >= 0
        and address + length <= MAIN_RAM_START + MAIN_RAM_SIZE
    )


def _u32(buffer: bytes, offset: int) -> int:
    return int.from_bytes(buffer[offset : offset + 4], "little")


class HGSSTerrainReader:
    """
    Read the currently loaded HGSS map's terrain attributes directly from RAM.

    This reproduces the data path used by GetMetatileBehavior(): FieldSystem ->
    MapMatrix chooses a 32x32 land-data block and TerrainAttributes supplies its
    1024 u16 tile attributes. The low byte is the metatile behavior.

    No ROM-specific hardcoded FieldSystem pointer is required. At startup the
    live FieldSystem is located by structural validation against the current map
    and map-matrix dimensions, then cached for the hunt.
    """

    def __init__(
        self,
        backend,
        *,
        expected_map: int,
        field_system_addr: int | None = None,
        current_x: int | None = None,
        current_z: int | None = None,
    ):
        self.backend = backend
        self.expected_map = int(expected_map)
        self._block_cache: dict[int, bytes] = {}
        self.field_system_addr = field_system_addr or self.discover_field_system(
            current_x=current_x,
            current_z=current_z,
        )
        self._load_layout()

    def _read(self, address: int, length: int) -> bytes:
        if not _in_main_ram(address, length):
            raise TerrainReadError(
                f"RAM read outside ARM9 main RAM: 0x{address:08X}+{length}"
            )
        data = self.backend.read_block(address, length)
        if len(data) != length:
            raise TerrainReadError(
                f"short RAM read at 0x{address:08X}: expected {length}, got {len(data)}"
            )
        return data

    def _read_u32(self, address: int) -> int:
        return int.from_bytes(self._read(address, 4), "little")

    def discover_field_system(
        self,
        *,
        current_x: int | None = None,
        current_z: int | None = None,
    ) -> int:
        """Find the retail FieldSystem without relying on a build-specific .bss symbol."""
        ram = self.backend.read_block(MAIN_RAM_START, MAIN_RAM_SIZE)
        if len(ram) != MAIN_RAM_SIZE:
            raise TerrainReadError(
                f"could not snapshot ARM9 main RAM: got {len(ram)} of {MAIN_RAM_SIZE} bytes"
            )

        def valid_pointer(pointer: int, length: int = 4) -> bool:
            return _in_main_ram(pointer, length)

        candidates: list[int] = []
        max_offset = MAIN_RAM_SIZE - FIELD_SYSTEM_SIZE

        for offset in range(0, max_offset, 4):
            # runningFieldMap is a BOOL. Requiring it to be exactly TRUE removes
            # almost every random pointer-shaped false positive before following
            # any nested pointers.
            if _u32(ram, offset + FS_RUNNING_FIELD_MAP) != 1:
                continue

            location = _u32(ram, offset + FS_LOCATION)
            matrix = _u32(ram, offset + FS_MAP_MATRIX)
            terrain = _u32(ram, offset + FS_TERRAIN_ATTRIBUTES)
            if not (
                valid_pointer(location, 20)
                and valid_pointer(matrix, 5)
                and valid_pointer(terrain, TERRAIN_DATA_OFFSET)
            ):
                continue

            location_offset = location - MAIN_RAM_START
            matrix_offset = matrix - MAIN_RAM_START
            if _u32(ram, location_offset) != self.expected_map:
                continue

            width = ram[matrix_offset]
            height = ram[matrix_offset + 1]
            if width < 1 or height < 1 or width * height > TERRAIN_MAP_INDEX_COUNT:
                continue

            # MapMatrix duplicates its dimensions in MapMatrixData. This is a
            # strong signature and makes a false FieldSystem match very unlikely.
            if ram[matrix_offset + 4] != width or ram[matrix_offset + 3] != height:
                continue

            if current_x is not None and not (0 <= current_x < width * TILES_PER_BLOCK):
                continue
            if current_z is not None and not (0 <= current_z < height * TILES_PER_BLOCK):
                continue

            candidates.append(MAIN_RAM_START + offset)

        if len(candidates) != 1:
            raise TerrainReadError(
                "expected exactly one live HGSS FieldSystem, "
                f"found {len(candidates)}; refusing to guess terrain geometry"
            )
        return candidates[0]

    def _load_layout(self) -> None:
        fs = self.field_system_addr
        if not _in_main_ram(fs, FIELD_SYSTEM_SIZE):
            raise TerrainReadError(f"invalid FieldSystem pointer: 0x{fs:08X}")

        location = self._read_u32(fs + FS_LOCATION)
        self.map_matrix = self._read_u32(fs + FS_MAP_MATRIX)
        self.terrain = self._read_u32(fs + FS_TERRAIN_ATTRIBUTES)

        if not (
            _in_main_ram(location, 20)
            and _in_main_ram(self.map_matrix, 5)
            and _in_main_ram(self.terrain, TERRAIN_DATA_OFFSET)
        ):
            raise TerrainReadError("invalid HGSS FieldSystem nested pointers")

        map_id = self._read_u32(location)
        if map_id != self.expected_map:
            raise TerrainReadError(
                f"FieldSystem map mismatch: expected {self.expected_map}, got {map_id}"
            )

        dimensions = self._read(self.map_matrix, 5)
        self.width = dimensions[0]
        self.height = dimensions[1]
        if (
            self.width < 1
            or self.height < 1
            or self.width * self.height > TERRAIN_MAP_INDEX_COUNT
        ):
            raise TerrainReadError(
                f"invalid HGSS map matrix dimensions {self.width}x{self.height}"
            )
        if dimensions[4] != self.width or dimensions[3] != self.height:
            raise TerrainReadError("HGSS map matrix duplicate dimensions disagree")

        self.block_map = self._read(self.terrain, TERRAIN_MAP_INDEX_COUNT)

    def tile_behavior(self, x: int, z: int) -> int:
        """Return the low-byte HGSS metatile behavior for one loaded-map coordinate."""
        x = int(x)
        z = int(z)
        if not (
            0 <= x < self.width * TILES_PER_BLOCK
            and 0 <= z < self.height * TILES_PER_BLOCK
        ):
            raise TerrainReadError(f"tile {(x, z)} is outside the loaded map matrix")

        block_x = x // TILES_PER_BLOCK
        block_z = z // TILES_PER_BLOCK
        matrix_index = block_z * self.width + block_x
        block_index = self.block_map[matrix_index]
        if block_index >= TERRAIN_BLOCK_COUNT_MAX:
            raise TerrainReadError(f"invalid terrain block index {block_index}")

        block = self._block_cache.get(block_index)
        if block is None:
            address = (
                self.terrain
                + TERRAIN_DATA_OFFSET
                + block_index * TERRAIN_BLOCK_SIZE
            )
            block = self._read(address, TERRAIN_BLOCK_SIZE)
            self._block_cache[block_index] = block

        local_x = x % TILES_PER_BLOCK
        local_z = z % TILES_PER_BLOCK
        offset = (local_z * TILES_PER_BLOCK + local_x) * 2
        return int.from_bytes(block[offset : offset + 2], "little") & 0xFF

    def is_grass(self, x: int, z: int) -> bool:
        return self.tile_behavior(x, z) in GRASS_BEHAVIORS

    def connected_grass_patch(self, start: Position) -> GrassPatch:
        """Flood-fill only the tall/very-tall grass connected to the start tile."""
        if start.map_id != self.expected_map:
            raise TerrainReadError(
                f"start map {start.map_id} differs from terrain map {self.expected_map}"
            )
        if not self.is_grass(start.x, start.z):
            behavior = self.tile_behavior(start.x, start.z)
            raise TerrainReadError(
                f"start tile {(start.x, start.z)} is not grass (behavior {behavior})"
            )

        found = {(start.x, start.z)}
        queue = deque(found)
        while queue:
            x, z = queue.popleft()
            for dx, dz in ((0, -1), (0, 1), (-1, 0), (1, 0)):
                neighbor = (x + dx, z + dz)
                if neighbor in found:
                    continue
                try:
                    grass = self.is_grass(*neighbor)
                except TerrainReadError:
                    grass = False
                if grass:
                    found.add(neighbor)
                    queue.append(neighbor)

        return GrassPatch(self.expected_map, found)
