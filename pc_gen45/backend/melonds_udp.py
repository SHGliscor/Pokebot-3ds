from __future__ import annotations

import socket
import struct
import time

from .backend import EmulatorBackend


_KEY_BITS = {
    "A": 0,
    "B": 1,
    "SELECT": 2,
    "START": 3,
    "RIGHT": 4,
    "LEFT": 5,
    "UP": 6,
    "DOWN": 7,
    "R": 8,
    "L": 9,
    "X": 10,
    "Y": 11,
}


class MelonDSUDPBackend(EmulatorBackend):
    """Low-overhead native localhost bridge built directly into melonDS."""

    def __init__(self, host: str = "127.0.0.1", port: int = 4953, *, timeout: float = 2.0):
        self.addr = (host, port)
        self.timeout = timeout
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(timeout)
        self._seq = int(time.time() * 1000) & 0xFFFFFFFF

    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        return self._seq

    def _request(self, cmd: int, payload: bytes = b"", *, retries: int = 3) -> bytes:
        seq = self._next_seq()
        packet = b"PKB1" + struct.pack("<I", seq) + bytes([cmd]) + payload

        last_error: Exception | None = None
        for _ in range(retries):
            try:
                self.sock.sendto(packet, self.addr)
                while True:
                    data, _ = self.sock.recvfrom(65535)
                    if len(data) < 9 or data[:4] != b"PKR1":
                        continue
                    response_seq = struct.unpack_from("<I", data, 4)[0]
                    if response_seq != seq:
                        continue
                    if data[8] != 0:
                        detail = data[9:].decode("utf-8", errors="replace")
                        raise RuntimeError(f"melonDS native bridge error: {detail}")
                    return data[9:]
            except socket.timeout as exc:
                last_error = exc

        raise TimeoutError(
            "melonDS native Pokebot bridge did not answer on 127.0.0.1:4953. "
            "Use the Pokebot melonDS.exe from this package; no Lua script is required."
        ) from last_error

    def ping(self) -> str:
        return self._request(1).decode("utf-8", errors="replace")

    def read_block(self, address: int, length: int) -> bytes:
        if length < 1:
            raise ValueError("READ length must be positive")

        # Native bridge packets are intentionally small so localhost UDP stays
        # cheap and deterministic. Larger diagnostics are transparently chunked.
        out = bytearray()
        remaining = length
        current = address
        while remaining:
            chunk = min(4096, remaining)
            payload = struct.pack("<IH", current & 0xFFFFFFFF, chunk)
            data = self._request(2, payload)
            if len(data) != chunk:
                raise RuntimeError(f"short native READ: expected {chunk}, got {len(data)}")
            out.extend(data)
            current += chunk
            remaining -= chunk
        return bytes(out)

    def _key_bit(self, key: str) -> int:
        try:
            return _KEY_BITS[key.upper()]
        except KeyError as exc:
            raise ValueError(f"unsupported DS key: {key}") from exc

    def _read_u16(self, address: int) -> int:
        return int.from_bytes(self.read_block(address, 2), "little")

    def _read_s16(self, address: int) -> int:
        value = self._read_u16(address)
        return value - 0x10000 if value & 0x8000 else value

    def set_key(self, key: str, pressed: bool) -> None:
        self._request(3, bytes([self._key_bit(key), 1 if pressed else 0]))

    def pulse(self, key: str, frames: int = 2) -> None:
        if not (1 <= frames <= 600):
            raise ValueError("pulse frames must be 1..600")
        self._request(4, bytes([self._key_bit(key)]) + struct.pack("<H", frames))

    def reset_input(self) -> None:
        self._request(5)

    def reset_game(self) -> None:
        self._request(6)

    def set_fast_forward(self, enabled: bool) -> None:
        self._request(7, bytes([1 if enabled else 0]))

    def set_display(self, enabled: bool) -> None:
        self._request(8, bytes([1 if enabled else 0]))

    def set_audio(self, enabled: bool) -> None:
        self._request(9, bytes([1 if enabled else 0]))

    def guarded_step(
        self,
        key: str,
        *,
        x_addr: int,
        z_addr: int,
        map_addr: int,
        expected_map: int,
        max_frames: int = 120,
        timeout: float = 1.5,
    ) -> tuple[int, int]:
        """
        Move exactly one overworld tile with a native per-frame release guard.

        HGSS map IDs are u16 and overworld X/Z coordinates are signed s16.
        Command 10 keeps the requested D-pad direction held only until the first
        observed low-16-bit X/Z transition, a map change, or max_frames expiry.
        Python then verifies that the movement was exactly one tile and that the
        map did not change. This remains safe at unthrottled melonDS speeds
        because the release decision happens inside the emulator frame loop.
        """
        key = key.upper()
        if key not in {"UP", "DOWN", "LEFT", "RIGHT"}:
            raise ValueError("guarded_step only supports D-pad directions")
        if not (1 <= max_frames <= 600):
            raise ValueError("max_frames must be 1..600")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if not (0 <= expected_map <= 0xFFFF):
            raise ValueError("expected_map must fit HGSS u16 map header")

        start_map = self._read_u16(map_addr)
        start_x = self._read_s16(x_addr)
        start_z = self._read_s16(z_addr)
        if start_map != expected_map:
            raise RuntimeError(
                f"map changed before guarded step: expected {expected_map}, got {start_map}"
            )

        payload = struct.pack(
            "<BIIIIH",
            self._key_bit(key),
            x_addr & 0xFFFFFFFF,
            z_addr & 0xFFFFFFFF,
            map_addr & 0xFFFFFFFF,
            expected_map,
            max_frames,
        )
        self._request(10, payload)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current_map = self._read_u16(map_addr)
            current_x = self._read_s16(x_addr)
            current_z = self._read_s16(z_addr)

            if current_map != expected_map:
                self.reset_input()
                raise RuntimeError(
                    f"map changed during guarded step: expected {expected_map}, got {current_map}"
                )

            if current_x != start_x or current_z != start_z:
                distance = abs(current_x - start_x) + abs(current_z - start_z)
                if distance != 1:
                    self.reset_input()
                    raise RuntimeError(
                        "guarded step moved more than one tile: "
                        f"{(start_x, start_z)} -> {(current_x, current_z)}"
                    )
                return current_x, current_z

            time.sleep(0.001)

        self.reset_input()
        raise TimeoutError("guarded step timed out before a one-tile coordinate change")
