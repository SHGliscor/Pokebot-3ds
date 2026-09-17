from __future__ import annotations

import struct
import unittest

from backend.melonds_udp import MelonDSUDPBackend

X_ADDR = 0x1000
Z_ADDR = 0x1004
MAP_ADDR = 0x2000


class FakeBackend(MelonDSUDPBackend):
    def __init__(self, values):
        self.values = {key: list(value) for key, value in values.items()}
        self.requests = []
        self.released = False

    def _request(self, cmd, payload=b"", retries=3):
        self.requests.append((cmd, payload))
        return b""

    def read_block(self, address, length):
        values = self.values[address]
        value = values.pop(0) if len(values) > 1 else values[0]
        return int(value & 0xFFFF).to_bytes(2, "little")[:length]

    def reset_input(self):
        self.released = True


class GuardedStepTests(unittest.TestCase):
    def test_one_tile_step_uses_native_guard(self):
        backend = FakeBackend(
            {
                MAP_ADDR: [7, 7],
                X_ADDR: [10, 11],
                Z_ADDR: [20, 20],
            }
        )
        self.assertEqual(
            backend.guarded_step(
                "RIGHT",
                x_addr=X_ADDR,
                z_addr=Z_ADDR,
                map_addr=MAP_ADDR,
                expected_map=7,
                max_frames=90,
                timeout=0.1,
            ),
            (11, 20),
        )

        self.assertEqual(backend.requests[0][0], 10)
        bit, x_addr, z_addr, map_addr, expected, frames = struct.unpack(
            "<BIIIIH", backend.requests[0][1]
        )
        self.assertEqual(
            (bit, x_addr, z_addr, map_addr, expected, frames),
            (4, X_ADDR, Z_ADDR, MAP_ADDR, 7, 90),
        )
        self.assertFalse(backend.released)

    def test_signed_coordinate_crossing_is_one_tile(self):
        backend = FakeBackend(
            {
                MAP_ADDR: [7, 7],
                X_ADDR: [-1, 0],
                Z_ADDR: [20, 20],
            }
        )
        self.assertEqual(
            backend.guarded_step(
                "RIGHT",
                x_addr=X_ADDR,
                z_addr=Z_ADDR,
                map_addr=MAP_ADDR,
                expected_map=7,
                timeout=0.1,
            ),
            (0, 20),
        )

    def test_map_change_releases_and_fails(self):
        backend = FakeBackend(
            {
                MAP_ADDR: [7, 8],
                X_ADDR: [10, 10],
                Z_ADDR: [20, 20],
            }
        )
        with self.assertRaises(RuntimeError):
            backend.guarded_step(
                "LEFT",
                x_addr=X_ADDR,
                z_addr=Z_ADDR,
                map_addr=MAP_ADDR,
                expected_map=7,
                timeout=0.1,
            )
        self.assertTrue(backend.released)

    def test_multi_tile_jump_is_rejected(self):
        backend = FakeBackend(
            {
                MAP_ADDR: [7, 7],
                X_ADDR: [10, 12],
                Z_ADDR: [20, 20],
            }
        )
        with self.assertRaises(RuntimeError):
            backend.guarded_step(
                "RIGHT",
                x_addr=X_ADDR,
                z_addr=Z_ADDR,
                map_addr=MAP_ADDR,
                expected_map=7,
                timeout=0.1,
            )
        self.assertTrue(backend.released)


if __name__ == "__main__":
    unittest.main()
