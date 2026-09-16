from __future__ import annotations

from abc import ABC, abstractmethod


class EmulatorBackend(ABC):
    @abstractmethod
    def ping(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def read_block(self, address: int, length: int) -> bytes:
        raise NotImplementedError

    @abstractmethod
    def set_key(self, key: str, pressed: bool) -> None:
        raise NotImplementedError

    @abstractmethod
    def pulse(self, key: str, frames: int = 2) -> None:
        raise NotImplementedError

    @abstractmethod
    def reset_input(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def set_fast_forward(self, enabled: bool) -> None:
        raise NotImplementedError

    @abstractmethod
    def reset_game(self) -> None:
        raise NotImplementedError
