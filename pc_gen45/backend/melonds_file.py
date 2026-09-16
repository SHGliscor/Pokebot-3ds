from __future__ import annotations

import os
from pathlib import Path
import time

from .backend import EmulatorBackend


class MelonDSFileBackend(EmulatorBackend):
    """
    Local file IPC between Python and the in-process melonDS Lua bridge.

    Python writes one small command file atomically; melonDS services it on
    the next emulated frame and writes a response file. Bulk reads are binary,
    so a full 4 MiB DS RAM snapshot does not get hex-expanded.
    """

    VALID_KEYS = {
        "A", "B", "X", "Y", "Left", "Right", "Up", "Down",
        "L", "R", "Select", "Start",
    }

    def __init__(self, ipc_dir: str | os.PathLike[str], *, timeout: float = 5.0):
        self.ipc_dir = Path(ipc_dir)
        self.ipc_dir.mkdir(parents=True, exist_ok=True)
        self.command_path = self.ipc_dir / "command.tsv"
        self.response_path = self.ipc_dir / "response.bin"
        self.timeout = timeout
        self._seq = int(time.time() * 1000) & 0x7FFFFFFF

    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0x7FFFFFFF
        return self._seq

    def _request(self, command: str, *args: object) -> tuple[list[str], bytes]:
        seq = self._next_seq()
        line = "\t".join([str(seq), command, *(str(x) for x in args)]) + "\n"

        tmp = self.command_path.with_suffix(".tmp")
        tmp.write_text(line, encoding="utf-8", newline="\n")
        os.replace(tmp, self.command_path)

        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                raw = self.response_path.read_bytes()
            except FileNotFoundError:
                time.sleep(0.005)
                continue

            nl = raw.find(b"\n")
            if nl < 0:
                time.sleep(0.002)
                continue

            try:
                header = raw[:nl].decode("utf-8").split("\t")
                response_seq = int(header[0])
            except (UnicodeDecodeError, ValueError, IndexError):
                time.sleep(0.002)
                continue

            if response_seq != seq:
                time.sleep(0.002)
                continue

            if len(header) < 2:
                raise RuntimeError(f"malformed melonDS response: {header!r}")
            if header[1] != "OK":
                detail = header[2] if len(header) > 2 else "unknown error"
                raise RuntimeError(f"melonDS bridge error: {detail}")

            payload = raw[nl + 1:]
            if len(header) >= 4 and header[2] == "BIN":
                expected = int(header[3])
                if len(payload) < expected:
                    time.sleep(0.002)
                    continue
                payload = payload[:expected]

            return header, payload

        raise TimeoutError(
            "melonDS Pokebot bridge did not answer. "
            "Make sure pokebot_bridge.lua is running and the IPC folder matches."
        )

    @classmethod
    def _key(cls, key: str) -> str:
        for valid in cls.VALID_KEYS:
            if valid.lower() == key.lower():
                return valid
        raise ValueError(f"unknown DS key: {key!r}")

    def ping(self) -> str:
        header, _ = self._request("PING")
        return header[2] if len(header) > 2 else "Pokebot-melonDS"

    def read_block(self, address: int, length: int) -> bytes:
        if not (1 <= length <= 0x400000):
            raise ValueError("READ length must be 1..0x400000")
        _, payload = self._request("READ", f"0x{address:08X}", length)
        if len(payload) != length:
            raise RuntimeError(f"short READ: expected {length}, got {len(payload)}")
        return payload

    def set_key(self, key: str, pressed: bool) -> None:
        self._request("KEY", self._key(key), 1 if pressed else 0)

    def pulse(self, key: str, frames: int = 2) -> None:
        if not (1 <= frames <= 600):
            raise ValueError("pulse frames must be 1..600")
        self._request("PULSE", self._key(key), frames)

    def reset_input(self) -> None:
        self._request("RELEASE_ALL")

    def touch(self, x: int, y: int) -> None:
        if not (0 <= x <= 255 and 0 <= y <= 191):
            raise ValueError("touch coordinates must be x=0..255, y=0..191")
        self._request("TOUCH", x, y)

    def release_touch(self) -> None:
        self._request("TOUCH_RELEASE")

    def reset_game(self) -> None:
        self._request("RESET")
