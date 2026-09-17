from __future__ import annotations

import unittest

from gen4.hgss_terrain import (
    FS_LOCATION,
    FS_MAP_MATRIX,
    FS_RUNNING_FIELD_MAP,
    FS_TERRAIN_ATTRIBUTES,
    HGSSTerrainReader,
    MAIN_RAM_SIZE,
    MAIN_RAM_START,
    TERRAIN_DATA_OFFSET,
    TerrainReadError,
)
from gen4.wild_navigation import Position


class FakeRAM:
    def __init__(self):
        self.memory = bytearray(MAIN_RAM_SIZE)
        self.base = MAIN_RAM_START
        self.fs = self.base + 0x1000
        self.location = self.base + 0x2000
        self.matrix = self.base + 0x3000
        self.terrain = self.base + 0x4000

        self.write_u32(self.fs + FS_LOCATION, self.location)
        self.write_u32(self.fs + FS_MAP_MATRIX, self.matrix)
        self.write_u32(self.fs + FS_TERRAIN_ATTRIBUTES, self.terrain)
        self.write_u32(self.fs + FS_RUNNING_FIELD_MAP, 1)
        self.write_u32(self.location, 33)

        # MapMatrix: width=2, height=1, matrix ID=0, then duplicate
        # MapMatrixData fields height=1, width=2.
        self.write_u8(self.matrix + 0, 2)
        self.write_u8(self.matrix + 1, 1)
        self.write_u8(self.matrix + 2, 0)
        self.write_u8(self.matrix + 3, 1)
        self.write_u8(self.matrix + 4, 2)

        # Matrix block 0 -> terrain block 0; matrix block 1 -> terrain block 1.
        self.write_u8(self.terrain + 0, 0)
        self.write_u8(self.terrain + 1, 1)

        self.set_behavior(0, 10, 10, 2)
        self.set_behavior(0, 11, 10, 3)
        self.set_behavior(0, 12, 10, 0)
        self.set_behavior(1, 5, 5, 2)

    def offset(self, address):
        return address - self.base

    def write_u8(self, address, value):
        self.memory[self.offset(address)] = value & 0xFF

    def write_u16(self, address, value):
        offset = self.offset(address)
        self.memory[offset : offset + 2] = int(value).to_bytes(2, "little")

    def write_u32(self, address, value):
        offset = self.offset(address)
        self.memory[offset : offset + 4] = int(value).to_bytes(4, "little")

    def set_behavior(self, block, x, z, behavior):
        address = (
            self.terrain
            + TERRAIN_DATA_OFFSET
            + block * 0x800
            + (z * 32 + x) * 2
        )
        self.write_u16(address, 0xAB00 | behavior)

    def read_block(self, address, length):
        offset = self.offset(address)
        return bytes(self.memory[offset : offset + length])


class HGSSTerrainTests(unittest.TestCase):
    def test_discovers_live_field_system_structurally(self):
        ram = FakeRAM()
        reader = HGSSTerrainReader(
            ram,
            expected_map=33,
            current_x=10,
            current_z=10,
        )
        self.assertEqual(reader.field_system_addr, ram.fs)

    def test_reads_live_tile_behavior_across_matrix_blocks(self):
        ram = FakeRAM()
        reader = HGSSTerrainReader(
            ram,
            expected_map=33,
            field_system_addr=ram.fs,
        )
        self.assertEqual(reader.tile_behavior(10, 10), 2)
        self.assertEqual(reader.tile_behavior(11, 10), 3)
        self.assertEqual(reader.tile_behavior(12, 10), 0)
        self.assertEqual(reader.tile_behavior(37, 5), 2)

    def test_connected_grass_patch_stops_at_non_grass(self):
        ram = FakeRAM()
        reader = HGSSTerrainReader(
            ram,
            expected_map=33,
            field_system_addr=ram.fs,
        )
        patch = reader.connected_grass_patch(Position(33, 10, 10))
        self.assertEqual(patch.tiles, frozenset({(10, 10), (11, 10)}))

    def test_rejects_map_mismatch_instead_of_guessing(self):
        ram = FakeRAM()
        with self.assertRaises(TerrainReadError):
            HGSSTerrainReader(
                ram,
                expected_map=34,
                field_system_addr=ram.fs,
            )


if __name__ == "__main__":
    unittest.main()
