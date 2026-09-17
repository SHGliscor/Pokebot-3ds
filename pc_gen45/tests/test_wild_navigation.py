from __future__ import annotations

import unittest

from gen4.wild_navigation import GrassPatch, Position, UnsafeStep


class GrassPatchTests(unittest.TestCase):
    def setUp(self):
        self.patch = GrassPatch(33, {(10, 10), (11, 10), (12, 10), (12, 11)})

    def test_only_allows_destination_inside_same_patch(self):
        position = Position(33, 11, 10)
        self.assertEqual(self.patch.destination(position, "LEFT"), Position(33, 10, 10))
        self.assertEqual(self.patch.destination(position, "RIGHT"), Position(33, 12, 10))
        with self.assertRaises(UnsafeStep):
            self.patch.destination(position, "UP")

    def test_rejects_wrong_map_or_start_outside_patch(self):
        with self.assertRaises(UnsafeStep):
            self.patch.destination(Position(34, 11, 10), "LEFT")
        with self.assertRaises(UnsafeStep):
            self.patch.destination(Position(33, 99, 99), "LEFT")

    def test_connected_component_excludes_disconnected_grass(self):
        tiles = {(1, 1), (2, 1), (3, 1), (20, 20)}
        patch = GrassPatch.connected(5, tiles, (2, 1))
        self.assertEqual(patch.tiles, frozenset({(1, 1), (2, 1), (3, 1)}))

    def test_safe_directions_never_include_non_grass(self):
        directions = self.patch.safe_directions(Position(33, 12, 10))
        self.assertEqual(set(directions), {"LEFT", "DOWN"})


if __name__ == "__main__":
    unittest.main()
