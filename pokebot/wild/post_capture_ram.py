from __future__ import annotations

"""Read-only Alpha Sapphire 1.4 post-capture/Pokédex RAM authority.

ORAS implements the Pokédex UI in the runtime CRO module DllSangoZukan.cro.
Rather than guessing a screen flag or a fixed heap address, this module uses
Pokebot3DS-CFW's read-only QUERY command to enumerate mapped process regions,
recognises loaded CRO headers, resolves their runtime module names, and reports
whether DllSangoZukan is actually resident.

No game RAM writes are performed here.
"""

import struct
import time

CMD_QUERY = 3
QUERY_INFO = struct.Struct("<IIIII")

CRO_MAGIC_RAW = b"CRO0"
CRO_MAGIC_FIXED = b"FIXD"
CRO_MAGICS = (CRO_MAGIC_RAW, CRO_MAGIC_FIXED)

# 3DS user-process CROs are mapped in this address range.  The QUERY walk
# jumps over complete memory regions, so this is not a byte/page sweep.
CRO_SCAN_START = 0x00100000
CRO_SCAN_END = 0x04000000
MAX_QUERY_REGIONS = 1024

# Static identity taken from the supplied Alpha Sapphire 1.4 RomFS CRO.
POKEDEX_MODULE = "DllSangoZukan"
POKEDEX_CRO_FILE_SIZE = 0x39000
POKEDEX_CRO_BSS_SIZE = 0xF8
POKEDEX_MODULE_NAME_SIZE = 0x0E

# CRO0 header fields used below.
HDR_MAGIC = 0x80
HDR_NAME_REF = 0x84
HDR_NEXT = 0x88
HDR_PREV = 0x8C
HDR_FILE_SIZE = 0x90
HDR_BSS_SIZE = 0x94
HDR_MODULE_NAME_REF = 0xC0
HDR_MODULE_NAME_SIZE = 0xC4
HEADER_READ_SIZE = 0x50  # 0x80..0xCF


class PostCaptureRamError(RuntimeError):
    pass


def _hx(value: int) -> str:
    return f"0x{int(value) & 0xFFFFFFFF:08X}"


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


def _query_region(br, address: int) -> dict:
    """Normalise QUERY across the common Bridge and frozen Wild Bridge."""
    if hasattr(br, "query"):
        q = br.query(address)
        return {
            "status": _as_int(q.get("status"), -1),
            "base": _as_int(q.get("base")),
            "size": _as_int(q.get("size")),
            "perm": _as_int(q.get("perm")),
            "state": _as_int(q.get("state")),
            "page_flags": _as_int(q.get("page_flags")),
        }

    # The frozen Wild backend intentionally contains no QUERY convenience
    # method, but it exposes the same protocol request() primitive.
    r = br.request(CMD_QUERY, int(address), 0)
    status = _as_int(r.get("status"), -1)
    payload = r.get("payload") or b""
    if status != 0 or len(payload) != QUERY_INFO.size:
        return {
            "status": status,
            "base": 0,
            "size": 0,
            "perm": 0,
            "state": 0,
            "page_flags": 0,
        }
    base, size, perm, state, page_flags = QUERY_INFO.unpack(payload)
    return {
        "status": status,
        "base": base,
        "size": size,
        "perm": perm,
        "state": state,
        "page_flags": page_flags,
    }


def _resolve_ref(base: int, file_size: int, ref: int) -> int | None:
    """Resolve a CRO field that can be raw-relative or already fixed-up."""
    ref = int(ref) & 0xFFFFFFFF
    if 0 < ref < file_size:
        return base + ref
    if CRO_SCAN_START <= ref < CRO_SCAN_END:
        return ref
    return None


def _read_c_string(br, address: int, limit: int = 64) -> str | None:
    if not address:
        return None
    try:
        raw = br.read(address, max(1, min(128, int(limit))))
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


def read_cro_header(br, base: int) -> dict | None:
    """Read and validate one mapped CRO header at *base*."""
    try:
        raw = br.read(base + HDR_MAGIC, HEADER_READ_SIZE)
    except Exception:
        return None
    if len(raw) != HEADER_READ_SIZE:
        return None

    magic = raw[:4]
    if magic not in CRO_MAGICS:
        return None

    def u32(header_off: int) -> int:
        return struct.unpack_from("<I", raw, header_off - HDR_MAGIC)[0]

    name_ref = u32(HDR_NAME_REF)
    next_ptr = u32(HDR_NEXT)
    prev_ptr = u32(HDR_PREV)
    file_size = u32(HDR_FILE_SIZE)
    bss_size = u32(HDR_BSS_SIZE)
    module_name_ref = u32(HDR_MODULE_NAME_REF)
    module_name_size = u32(HDR_MODULE_NAME_SIZE)

    if not (0x1000 <= file_size <= 0x01000000):
        return None
    if module_name_size == 0 or module_name_size > 128:
        return None

    name_addr = _resolve_ref(base, file_size, module_name_ref)
    if name_addr is None:
        name_addr = _resolve_ref(base, file_size, name_ref)
    name = _read_c_string(br, name_addr, max(32, module_name_size + 1))
    if not name:
        return None

    return {
        "base": _hx(base),
        "base_int": int(base),
        "magic": magic.decode("ascii", errors="replace"),
        "module": name,
        "module_name_address": _hx(name_addr),
        "file_size": file_size,
        "file_size_hex": _hx(file_size),
        "bss_size": bss_size,
        "bss_size_hex": _hx(bss_size),
        "next": _hx(next_ptr),
        "prev": _hx(prev_ptr),
        "name_ref": _hx(name_ref),
        "module_name_ref": _hx(module_name_ref),
        "module_name_size": module_name_size,
    }


def _is_pokedex_record(record: dict | None) -> bool:
    return bool(
        record
        and record.get("module") == POKEDEX_MODULE
        and int(record.get("file_size", -1)) == POKEDEX_CRO_FILE_SIZE
        and int(record.get("bss_size", -1)) == POKEDEX_CRO_BSS_SIZE
        and int(record.get("module_name_size", -1)) == POKEDEX_MODULE_NAME_SIZE
    )


def locate_pokedex_module(br) -> dict:
    """Perform one bounded QUERY walk and locate DllSangoZukan if loaded.

    Returns a compact evidence report. Absence is a valid result because a
    capture of an already-registered species need not open the Pokédex.
    """
    started = time.monotonic()
    cursor = CRO_SCAN_START
    query_count = 0
    cro_count = 0
    modules = []
    target = None
    seen_region_bases = set()
    errors = []

    while cursor < CRO_SCAN_END and query_count < MAX_QUERY_REGIONS:
        query_count += 1
        try:
            q = _query_region(br, cursor)
        except Exception as exc:
            errors.append(
                {"address": _hx(cursor), "error": f"{type(exc).__name__}: {exc}"}
            )
            # Fail closed for this region while still keeping the walk bounded.
            cursor += 0x1000
            continue

        base = int(q.get("base", 0))
        size = int(q.get("size", 0))
        if q.get("status") != 0 or size <= 0:
            # QUERY should normally describe free regions too. If it cannot,
            # advance one page rather than getting stuck.
            cursor += 0x1000
            continue

        end = base + size
        if base not in seen_region_bases and CRO_SCAN_START <= base < CRO_SCAN_END:
            seen_region_bases.add(base)
            record = read_cro_header(br, base)
            if record:
                cro_count += 1
                compact = {
                    "module": record["module"],
                    "base": record["base"],
                    "magic": record["magic"],
                    "file_size_hex": record["file_size_hex"],
                    "bss_size_hex": record["bss_size_hex"],
                }
                # Keep support evidence bounded; the target is always retained.
                if len(modules) < 64:
                    modules.append(compact)
                if _is_pokedex_record(record):
                    target = record
                    break

        next_cursor = max(cursor + 0x1000, end)
        if next_cursor <= cursor:
            next_cursor = cursor + 0x1000
        cursor = next_cursor

    elapsed = time.monotonic() - started
    return {
        "authority": "QUERY-enumerated loaded CRO header + Alpha Sapphire 1.4 DllSangoZukan identity",
        "ram_writes": False,
        "module": POKEDEX_MODULE,
        "present": bool(target),
        "target": target,
        "query_count": query_count,
        "cro_count": cro_count,
        "modules": modules,
        "errors": errors[:8],
        "elapsed_seconds": round(elapsed, 3),
        "scan_range": [_hx(CRO_SCAN_START), _hx(CRO_SCAN_END)],
        "bounded": query_count < MAX_QUERY_REGIONS or cursor >= CRO_SCAN_END,
    }


def verify_pokedex_module(br, target: dict | None) -> dict:
    """Fast direct verification after a prior locate_pokedex_module() hit."""
    base = _as_int((target or {}).get("base_int"))
    if not base:
        base = _as_int((target or {}).get("base"))
    record = read_cro_header(br, base) if base else None
    return {
        "present": _is_pokedex_record(record),
        "target": record if _is_pokedex_record(record) else None,
        "base": _hx(base) if base else None,
    }
