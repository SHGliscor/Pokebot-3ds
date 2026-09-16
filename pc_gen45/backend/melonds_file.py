from __future__ import annotations

import os
from pathlib import Path
import time
from typing import Iterable

from .backend import EmulatorBackend


class MelonDSFileBackend(EmulatorBackend):
    """
    Localhost-style IPC without sockets.

    Python writes one small command file atomically; melonDS Lua services it on
    the next emulated frame and writes a response file. Bulk reads are binary,
    so a full 4 MiB DS RAM snapshot does not get hex-expanded.
    """

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

        # Lua polls command.tsv every emulated frame. On Windows, replacing a
        # destination that Lua has open for the few microseconds needed to read
        # it can raise WinError 5. Retry the atomic publish instead of crashing.
        publish_deadline = time.monotonic() + min(self.timeout, 2.0)
        while True:
            try:
                os.replace(tmp, self.command_path)
                break
            except PermissionError:
                if time.monotonic() >= publish_deadline:
                    raise TimeoutError(
                        "Timed out publishing a command to the melonDS bridge "
                        "(Windows file-sharing collision)."
                    )
                time.sleep(0.001)

        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                raw = self.response_path.read_bytes()
            except (FileNotFoundError, PermissionError):
                # response.bin can also be momentarily locked while Lua is
                # replacing/truncating it on Windows.
                time.sleep(0.002)
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
        self._request("KEY", key, 1 if pressed else 0)

    def pulse(self, key: str, frames: int = 2) -> None:
        if not (1 <= frames <= 600):
            raise ValueError("pulse frames must be 1..600")
        self._request("PULSE", key, frames)

    def reset_input(self) -> None:
        self._request("RELEASE_ALL")

    def set_fast_forward(self, enabled: bool) -> None:
        self._request("FAST_FORWARD", 1 if enabled else 0)

    def reset_game(self) -> None:
        self._request("RESET")
