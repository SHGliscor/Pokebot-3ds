
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

BASE = Path(__file__).resolve().parent / "validated"


def _install_luma_input_transport(core):
    """Route frozen Wild backend input calls to the Pokebot-Luma acknowledged UDP/4952 bridge.

    RAM remains on the core's existing UDP/4952 request protocol.  This keeps
    the validated movement/state-machine source files unchanged on disk.
    """
    from pokebot.common.luma_input import INPUT_PORT, LumaInputTransport

    def _transport(self):
        obj = getattr(self, "_pokebot_luma_input", None)
        if obj is None:
            obj = LumaInputTransport(self.host, port=INPUT_PORT, timeout=min(float(self.timeout), 1.5))
            self._pokebot_luma_input = obj
        return obj

    def input_ping(self):
        info = _transport(self).input_ping()
        caps = int(info.get("capabilities", 0))
        return {
            "protocol_version": int(info.get("protocol", 1)),
            "capability_flags": caps,
            "capability_flags_hex": f"0x{caps:08X}",
            "runtime_flags": int(info.get("runtime_flags", 0)),
            "runtime_flags_hex": f"0x{int(info.get('runtime_flags', 0)):08X}",
            "neutral_hid": int(info.get("neutral_hid", 0xFFF)),
            "neutral_hid_hex": f"0x{int(info.get('neutral_hid', 0xFFF)):03X}",
            "max_hold_ms": int(info.get("max_hold_ms", 5000)),
            "max_settle_ms": int(info.get("max_settle_ms", 5000)),
            "hid_pulse": bool(caps & (1 << 0)),
            "touch_pulse": bool(caps & (1 << 6)),
            "hid_latch": bool(caps & (1 << 7)),
            "transport": "Pokebot-Luma acknowledged UDP 4952",
            "acknowledged": True,
        }

    def release_all(self):
        rec = _transport(self).release_all()
        return {
            "status": 0,
            "result": 0,
            "request_id": rec.get("sequence"),
            "raw_hid": rec.get("raw_hid", 0xFFF),
            "transport": "Pokebot-Luma acknowledged UDP 4952",
            "acknowledged": True,
        }

    def hid_pulse_no_retransmit(self, raw_hid, hold_ms, settle_ms):
        rec = _transport(self).pulse_raw(
            raw_hid,
            hold_ms=int(hold_ms),
            settle_ms=int(settle_ms),
            interval_ms=20,
        )
        seq = rec.get("sequence")
        terminal = {
            "completed": True,
            "terminal_state": 3,
            "samples": [],
            "transport": "Pokebot-Luma acknowledged UDP 4952 terminal completion",
        }
        return {
            "sequence_id": seq,
            "raw_hid": f"0x{int(raw_hid) & 0xFFF:03X}",
            "hold_ms": int(hold_ms),
            "settle_ms": int(settle_ms),
            "initial": {"state": 1, "acknowledged": True},
            "terminal": terminal,
            "completed": True,
        }

    def touch_pulse_no_retransmit(self, touch_state, hold_ms, settle_ms):
        rec = _transport(self).touch_pulse(
            touch_state,
            hold_ms=int(hold_ms),
            settle_ms=int(settle_ms),
            interval_ms=20,
        )
        seq = rec.get("sequence")
        return {
            "sequence_id": seq,
            "touch_state": f"0x{int(touch_state) & 0xFFFFFFFF:08X}",
            "hold_ms": int(hold_ms),
            "settle_ms": int(settle_ms),
            "initial": {"state": 1, "acknowledged": True},
            "terminal": {"completed": True, "terminal_state": 3, "samples": []},
            "completed": True,
            "response_timeout_recovery": False,
            "status_recovered": False,
            "touch_retransmitted": False,
        }

    def hid_latch_no_retransmit(self, raw_hid):
        rec = _transport(self).latch_raw(raw_hid)
        seq = rec.get("sequence")
        return {
            "sequence_id": seq,
            "raw_hid": f"0x{int(raw_hid) & 0xFFF:03X}",
            "initial": {"state": 2, "acknowledged": True},
            "status_recovery": {"active": True, "state": 2, "samples": []},
            "active": True,
            "response_timeout_recovery": False,
            "latch_retransmitted": False,
            "transport": "Pokebot-Luma retained remote HID state",
        }

    core.Bridge.input_ping = input_ping
    core.Bridge.release_all = release_all
    core.Bridge.hid_pulse_no_retransmit = hid_pulse_no_retransmit
    core.Bridge.touch_pulse_no_retransmit = touch_pulse_no_retransmit
    if hasattr(core.Bridge, "hid_latch_no_retransmit"):
        core.Bridge.hid_latch_no_retransmit = hid_latch_no_retransmit


def _load_file(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_validated_script(tag: str, folder: Path, script_name: str):
    core = _load_file(
        f"pokebot_wild_{tag}_core",
        folder / "causal_core_v0p14.py",
    )
    _install_luma_input_transport(core)
    terrain = _load_file(
        f"pokebot_wild_{tag}_terrain",
        folder / "route101_w6_mask.py",
    )

    # Preserve the validated source file byte-for-byte on disk. At runtime,
    # replace only its two local import statements with injected module objects
    # so walk and Acro can coexist in one Qt process without global module-name
    # collisions.
    source_path = folder / script_name
    source = source_path.read_text(encoding="utf-8")
    source = source.replace(
        "import causal_core_v0p14 as core",
        "core = __validated_core__",
        1,
    )
    source = source.replace(
        "import route101_w6_mask as terrain",
        "terrain = __validated_terrain__",
        1,
    )

    module = ModuleType(f"pokebot_wild_{tag}_script")
    module.__file__ = str(source_path)
    module.__dict__["__validated_core__"] = core
    module.__dict__["__validated_terrain__"] = terrain
    exec(compile(source, str(source_path), "exec"), module.__dict__)
    return module, core, terrain


def load_walk_v0p23():
    folder = BASE / "walk_v0p23"
    return _load_validated_script(
        "walk_v0p23",
        folder,
        "w6_unlimited_v0p23.py",
    )


def load_acro_v0p27():
    folder = BASE / "acro_v0p27"
    return _load_validated_script(
        "acro_v0p27",
        folder,
        "acro_bunny_latch_10_v0p27.py",
    )
