from __future__ import annotations

from dataclasses import dataclass
from collections import deque

_DIRECTION_DELTAS = {
    "UP": (0, -1),
    "DOWN": (0, 1),
    "LEFT": (-1, 0),
    "RIGHT": (1, 0),
}


class UnsafeStep(RuntimeError):
    """Raised when movement would leave the verified encounter patch."""


@dataclass(frozen=True)
class Position:
    map_id: int
    x: int
    z: int


@dataclass(frozen=True)
class GrassPatch:
    """A connected set of verified walking-encounter tiles on one HGSS map."""

    map_id: int
    tiles: frozenset[tuple[int, int]]

    def __init__(self, map_id: int, tiles):
        object.__setattr__(self, "map_id", int(map_id))
        object.__setattr__(self, "tiles", frozenset((int(x), int(z)) for x, z in tiles))
        if not self.tiles:
            raise ValueError("grass patch must contain at least one tile")

    @classmethod
    def connected(cls, map_id: int, encounter_tiles, start: tuple[int, int]) -> "GrassPatch":
        """Keep only the encounter-tile component containing the player's start tile."""
        tiles = frozenset((int(x), int(z)) for x, z in encounter_tiles)
        start = (int(start[0]), int(start[1]))
        if start not in tiles:
            raise UnsafeStep(f"start tile {start} is not a verified encounter tile")

        found = {start}
        queue = deque([start])
        while queue:
            x, z = queue.popleft()
            for dx, dz in _DIRECTION_DELTAS.values():
                nxt = (x + dx, z + dz)
                if nxt in tiles and nxt not in found:
                    found.add(nxt)
                    queue.append(nxt)
        return cls(map_id, found)

    def contains(self, position: Position) -> bool:
        return position.map_id == self.map_id and (position.x, position.z) in self.tiles

    def destination(self, position: Position, direction: str) -> Position:
        """Return the destination only if one step remains inside this verified patch."""
        direction = direction.upper()
        if direction not in _DIRECTION_DELTAS:
            raise ValueError(f"unsupported direction: {direction}")
        if position.map_id != self.map_id:
            raise UnsafeStep(f"map changed from {self.map_id} to {position.map_id}")
        if (position.x, position.z) not in self.tiles:
            raise UnsafeStep(
                f"current tile {(position.x, position.z)} is outside the verified grass patch"
            )

        dx, dz = _DIRECTION_DELTAS[direction]
        dest = Position(position.map_id, position.x + dx, position.z + dz)
        if (dest.x, dest.z) not in self.tiles:
            raise UnsafeStep(
                f"blocked grass-edge step {direction}: {(dest.x, dest.z)} "
                "is not verified encounter terrain"
            )
        return dest

    def safe_directions(self, position: Position) -> tuple[str, ...]:
        """List every direction that keeps the next tile inside the verified patch."""
        if not self.contains(position):
            raise UnsafeStep(f"current position {position} is outside the verified grass patch")

        out = []
        for direction, (dx, dz) in _DIRECTION_DELTAS.items():
            if (position.x + dx, position.z + dz) in self.tiles:
                out.append(direction)
        return tuple(out)
