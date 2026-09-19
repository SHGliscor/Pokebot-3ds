from __future__ import annotations

"""Small read-only Gen 6 CRO module locator.

The Pokebot bridge exposes QUERY/READ only.  Gen 6 loads UI/gameplay features
as CRO modules, so module presence is a useful state gate without fixed RAM
addresses.  This helper enumerates mapped process regions and reads only CRO
headers; it never writes game RAM and never sends controller input.
"""

import struct
import time
from typing import Iterable

CRO_SCAN_START = 0x00100000
CRO_SCAN_END = 0x04000000
MAX_QUERY_REGIONS = 1024
CRO_MAGICS = (b"CRO0", b"FIXD")

HDR_MAGIC = 0x80
HDR_NAME_REF = 0x84
HDR_FILE_SIZE = 0x90
HDR_BSS_SIZE = 0x94
HDR_MODULE_NAME_REF = 0xC0
HDR_MODULE_NAME_SIZE = 0xC4
HEADER_READ_SIZE = 0x50


def _as_int(value, default=0):
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except Exception:
            return default
    try:
        return int(value)
    except Exception:
        return default


def _query_region(bridge, address: int) -> dict:
    q = bridge.query(int(address))
    return {
        "status": _as_int(q.get("status"), -1),
        "base": _as_int(q.get("base")),
        "size": _as_int(q.get("size")),
        "perm": _as_int(q.get("perm")),
        "state": _as_int(q.get("state")),
        "page_flags": _as_int(q.get("page_flags")),
    }


def _resolve_ref(base: int, file_size: int, ref: int):
    ref = int(ref) & 0xFFFFFFFF
    if 0 < ref < file_size:
        return int(base) + ref
    if CRO_SCAN_START <= ref < CRO_SCAN_END:
        return ref
    return None


def _read_c_string(bridge, address: int, limit: int = 96):
    if not address:
        return None
    try:
        raw = bridge.read(int(address), max(1, min(128, int(limit))))
    except Exception:
        return None
    raw = raw.split(b"\0", 1)[0]
    if not raw:
        return None
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return None
    if not all(32 <= ord(ch) < 127 for ch in text):
        return None
    return text


def read_cro_header(bridge, base: int):
    try:
        raw = bridge.read(int(base) + HDR_MAGIC, HEADER_READ_SIZE)
    except Exception:
        return None
    if len(raw) != HEADER_READ_SIZE or raw[:4] not in CRO_MAGICS:
        return None

    def u32(off: int) -> int:
        return struct.unpack_from("<I", raw, off - HDR_MAGIC)[0]

    file_size = u32(HDR_FILE_SIZE)
    bss_size = u32(HDR_BSS_SIZE)
    module_name_size = u32(HDR_MODULE_NAME_SIZE)
    if not (0x1000 <= file_size <= 0x01000000) or not (0 < module_name_size <= 128):
        return None
    name_addr = _resolve_ref(base, file_size, u32(HDR_MODULE_NAME_REF))
    if name_addr is None:
        name_addr = _resolve_ref(base, file_size, u32(HDR_NAME_REF))
    name = _read_c_string(bridge, name_addr, max(32, module_name_size + 1))
    if not name:
        return None
    return {
        "module": name,
        "base": int(base),
        "base_hex": f"0x{int(base):08X}",
        "magic": raw[:4].decode("ascii", errors="replace"),
        "file_size": int(file_size),
        "file_size_hex": f"0x{int(file_size):X}",
        "bss_size": int(bss_size),
        "bss_size_hex": f"0x{int(bss_size):X}",
        "module_name_size": int(module_name_size),
    }


def locate_loaded_modules(bridge, wanted: Iterable[str]) -> dict[str, dict]:
    """Return requested currently-loaded CRO modules by exact module name."""
    targets = {str(x) for x in wanted if str(x)}
    if not targets:
        return {}
    found: dict[str, dict] = {}
    cursor = CRO_SCAN_START
    query_count = 0
    seen = set()

    while cursor < CRO_SCAN_END and query_count < MAX_QUERY_REGIONS and len(found) < len(targets):
        query_count += 1
        try:
            q = _query_region(bridge, cursor)
        except Exception:
            cursor += 0x1000
            continue
        base = int(q.get("base", 0))
        size = int(q.get("size", 0))
        if q.get("status") != 0 or size <= 0:
            cursor += 0x1000
            continue
        if base not in seen and CRO_SCAN_START <= base < CRO_SCAN_END:
            seen.add(base)
            rec = read_cro_header(bridge, base)
            if rec and rec.get("module") in targets:
                found[str(rec["module"])] = rec
        nxt = max(cursor + 0x1000, base + size)
        cursor = nxt if nxt > cursor else cursor + 0x1000
    return found


def locate_loaded_module(bridge, module_name: str):
    return locate_loaded_modules(bridge, (module_name,)).get(str(module_name))


def wait_for_module(bridge, module_name: str, *, present=True, timeout=10.0, interval=0.20, check_stop=None):
    started = time.monotonic()
    samples = []
    while time.monotonic() - started < float(timeout):
        if check_stop is not None:
            check_stop()
        rec = locate_loaded_module(bridge, module_name)
        now_present = rec is not None
        samples.append({
            "elapsed": round(time.monotonic() - started, 3),
            "present": now_present,
            "base": rec.get("base_hex") if rec else None,
        })
        if len(samples) > 20:
            samples.pop(0)
        if now_present == bool(present):
            return True, rec, samples
        time.sleep(max(0.02, float(interval)))
    return False, None, samples
