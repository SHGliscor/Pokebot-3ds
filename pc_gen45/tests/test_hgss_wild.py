from __future__ import annotations

import unittest

from gen4.hgss_wild import (
    HG_EN_ANCHOR_PTR,
    HG_EN_TRAINER_X,
    HG_EN_TRAINER_Z,
    HG_MAP_HEADER_FROM_ANCHOR,
    HGSSEnglishGrassLock,
)
from gen4.wild_navigation import Position, UnsafeStep


class FakeBackend:
    def __init__(self):
        self.anchor = 0x02200000
        self.map_id = 33
        self.x = 10
        self.z = 10
        self.steps = []
        self.released = False

    def read_block(self, address, length):
        if address == HG_EN_ANCHOR_PTR:
            value = self.anchor
        elif address == HG_EN_TRAINER_X:
            value = self.x
        elif address == HG_EN_TRAINER_Z:
            value = self.z
        elif address == self.anchor + HG_MAP_HEADER_FROM_ANCHOR:
            value = self.map_id
        else:
            raise AssertionError(hex(address))
        return int(value).to_bytes(4, "little")

    def guarded_step(self, key, **kwargs):
        self.steps.append((key, kwargs))
        if key == "RIGHT":
            self.x += 1
        elif key == "LEFT":
            self.x -= 1
        elif key == "UP":
            self.z -= 1
        elif key == "DOWN":
            self.z += 1
        return self.x, self.z

    def reset_input(self):
        self.released = True


class GrassLockTests(unittest.TestCase):
    def test_start_uses_connected_component_and_step_stays_inside(self):
        backend = FakeBackend()
        lock = HGSSEnglishGrassLock(backend, {(10, 10), (11, 10), (99, 99)})
        self.assertEqual(lock.start(), Position(33, 10, 10))
        self.assertEqual(lock.step("RIGHT"), Position(33, 11, 10))
        self.assertEqual(len(lock.patch.tiles), 2)

    def test_edge_step_is_blocked_before_any_input(self):
        backend = FakeBackend()
        lock = HGSSEnglishGrassLock(backend, {(10, 10), (11, 10)})
        lock.start()
        with self.assertRaises(UnsafeStep):
            lock.step("UP")
        self.assertEqual(backend.steps, [])

    def test_map_change_causes_safety_release(self):
        backend = FakeBackend()
        lock = HGSSEnglishGrassLock(backend, {(10, 10), (11, 10)})
        lock.start()
        backend.map_id = 34
        with self.assertRaises(UnsafeStep):
            lock.step("RIGHT")
        self.assertTrue(backend.released)


if __name__ == "__main__":
    unittest.main()
