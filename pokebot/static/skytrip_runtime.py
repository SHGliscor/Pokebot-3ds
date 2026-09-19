from __future__ import annotations

"""Read-only Alpha Sapphire 1.4 SkyTrip runtime-object mapper.

The first Dimensional Rift mapper proved that the normal ORAS overworld XYZ
copies stay anchored to Dewford while Latias/Latios is Soaring.  Soaring is
implemented by the runtime CRO module ``DllSkyTrip.cro``.  This module locates
that CRO through the bridge QUERY command, verifies its exact 1.4 identity and
one loader relocation, then performs a bounded pointer-graph walk seeded only
from SkyTrip's own data segment.

No game RAM writes are performed.  No controller input is performed here.
Every individual READ is <= 0x200 bytes, matching the CFW bridge limit.
"""

from collections import deque
import hashlib
import math
import struct
import time
from typing import Any, Callable


CMD_QUERY = 3
QUERY_INFO = struct.Struct("<IIIII")

CRO_SCAN_START = 0x00100000
CRO_SCAN_END = 0x04000000
MAX_QUERY_REGIONS = 1024
CRO_MAGICS = (b"CRO0", b"FIXD")

# Exact identity from the user's Alpha Sapphire 1.4 RomFS DllSkyTrip.cro.
SKYTRIP_MODULE = "DllSkyTrip"
SKYTRIP_FILE_SIZE = 0x12000
SKYTRIP_BSS_SIZE = 0x1A4
SKYTRIP_MODULE_NAME_SIZE = 0x0B
SKYTRIP_DATA_OFFSET = 0x11410
SKYTRIP_DATA_SIZE = 0x294
SKYTRIP_SEGMENT_TABLE_OFFSET = 0xF00C
SKYTRIP_SEGMENT_COUNT = 6
SKYTRIP_MAX_RUNTIME_SEGMENTS = 16
RUNTIME_HEADER_LAYOUT_OFFSET = 0xB0
RUNTIME_HEADER_LAYOUT_SIZE = 0x20

# CRO loader relocation sanity proof.  The MainProc constructor loads its vptr
# through the literal at file offset 0xBF40.  After CRO relocation that literal
# must contain module_base + 0xDFA0.
MAINPROC_VPTR_LITERAL_OFFSET = 0xBF40

# Hardware probe targets derived from the supplied 1.4 DllSkyTrip RTTI/vtables.
# Values are file-relative vptr addresses after the Itanium vtable prefix.
SKYTRIP_VPTR_OFFSETS = {
    "BuildModel": 0xDF10,
    "BMLegendPoint": 0xDF30,
    "BMShip": 0xDF50,
    "Camera": 0xDF70,
    "BMHikyo": 0xDF80,
    "MainProc": 0xDFA0,
    "BMEncount": 0xDFCC,
    "BaseProcess": 0xDFEC,
}

# Object snapshots are deliberately bounded. MainProc is known to initialise
# through +0x194, while Camera allocation is 0x80 bytes.
OBJECT_READ_LENGTHS = {
    "MainProc": 0x198,
    "Camera": 0x80,
    "BMEncount": 0x100,
    "BMLegendPoint": 0x100,
    "BMHikyo": 0x100,
    "BMShip": 0x100,
    "BuildModel": 0x100,
    "BaseProcess": 0x100,
}

HEAP_MIN = 0x08000000
HEAP_MAX = 0x10000000
READ_CHUNK = 0x200
POINTER_GRAPH_MAX_OBJECTS = 128
POINTER_GRAPH_MAX_DEPTH = 3
POINTERS_PER_OBJECT = 32
MAX_CANDIDATES_PER_CLASS = 8

# CE hardware proved the relocated SkyTrip .data singleton points into a large
# application heap, but the live RTTI objects were not necessarily reachable by
# following pointers from that singleton.  CF therefore performs one small,
# bounded exact-vptr sweep around the first proven heap anchor only when the
# pointer graph found nothing.  This is deliberately not a multi-megabyte heap
# scan: at most 0x10000 bytes / 128 bridge reads are inspected.
HEAP_ANCHOR_PROBE_BYTES = 0x10000
HEAP_ANCHOR_PROBE_MAX_READS = HEAP_ANCHOR_PROBE_BYTES // READ_CHUNK
HEAP_ANCHOR_PROBE_BACK = 0x2000
HEAP_ANCHOR_PROBE_MAX_SEEDS = 2
GRAPH_HEX_SNAPSHOTS = 8
STATE_FLOAT_ABS_MAX = 200000.0
STATE_DIFF_MAX_WORDS = 96

# CH/CI hardware-directed mapper: CG proved SkyTrip's own relocated .data/.bss do
# not contain live steering/position state. CH then proved the nominal CRO code
# span is not necessarily virtually contiguous. Scan actual relocated type-0/
# type-1 segments once for references into writable application memory, then
# trace a small ranked set of those externally-owned structures during flight.
EXTERNAL_CODE_SCAN_MAX = 0x12000
EXTERNAL_RAW_POINTER_MAX = 128
EXTERNAL_ACCEPTED_ROOT_MAX = 32
EXTERNAL_DIRECT_ROOT_MAX = 24
EXTERNAL_TRACE_TARGET_MAX = 16
EXTERNAL_ROOT_SNAPSHOT = 0x80
EXTERNAL_CHILD_SNAPSHOT = 0x80
EXTERNAL_CHILDREN_PER_ROOT = 1
EXTERNAL_STATE_DIFF_MAX_WORDS = 64
# CJ hardware correction: CI's literal filter only considered 0x08000000-
# 0x0FFFFFFF and accepted aliases into SkyTrip's own relocated .data/.bss as
# "external" roots.  External ownership is now defined structurally: a
# candidate must be an aligned userland pointer, QUERY-mapped, writable, and
# outside every live DllSkyTrip runtime segment.  Read-only bridge tables and
# pointers escaping the module-local mutable segments are followed with strict
# one-level/bounded budgets.
USERLAND_PTR_MIN = 0x00100000
USERLAND_PTR_MAX = 0x14000000
EXTERNAL_READONLY_BRIDGE_MAX = 16
EXTERNAL_BRIDGE_POINTERS_PER_ROOT = 8
EXTERNAL_LOCAL_SEGMENT_SCAN_MAX = 0x1000

# CK fast mapper.  Hardware has disproved the repeated local object/vptr and
# module .data/.bss motion hypotheses, so the hot path now uses a cached
# instruction-derived literal plan and a small set of genuine external roots.
FAST_PLAN_VERSION = 1
FAST_ARM_LITERAL_MAX = 96
FAST_ROOT_MAX = 16
FAST_TRACE_TARGET_MAX = 8
FAST_TRACE_SNAPSHOT = 0x60
FAST_MOTION_LOCK_SCORE = 36
FAST_MOTION_MIN_EVENTS = 4
FAST_MOTION_MIN_SPAN_S = 0.8
FAST_MOTION_MIN_DYNAMIC_OFFSETS = 2

HDR_MAGIC = 0x80
HDR_NAME_REF = 0x84
HDR_FILE_SIZE = 0x90
HDR_BSS_SIZE = 0x94
HDR_MODULE_NAME_REF = 0xC0
HDR_MODULE_NAME_SIZE = 0xC4
HEADER_READ_SIZE = 0x50


class SkyTripRuntimeError(RuntimeError):
    pass


def _hx(v: int | None) -> str | None:
    return None if v is None else f"0x{int(v) & 0xFFFFFFFF:08X}"


def _as_int(value, default=0) -> int:
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


def _query_region(br, address: int) -> dict[str, int]:
    if hasattr(br, "query"):
        q = br.query(int(address))
        return {
            "status": _as_int(q.get("status"), -1),
            "base": _as_int(q.get("base")),
            "size": _as_int(q.get("size")),
            "perm": _as_int(q.get("perm")),
            "state": _as_int(q.get("state")),
            "page_flags": _as_int(q.get("page_flags")),
        }

    r = br.request(CMD_QUERY, int(address), 0)
    status = _as_int(r.get("status"), -1)
    payload = r.get("payload") or b""
    if status != 0 or len(payload) != QUERY_INFO.size:
        return {"status": status, "base": 0, "size": 0, "perm": 0, "state": 0, "page_flags": 0}
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
    ref = int(ref) & 0xFFFFFFFF
    if 0 < ref < file_size:
        return int(base) + ref
    if CRO_SCAN_START <= ref < CRO_SCAN_END:
        return ref
    return None


def _read_c_string(br, address: int, limit: int = 64) -> str | None:
    if not address:
        return None
    try:
        raw = br.read(int(address), max(1, min(128, int(limit))))
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


def read_cro_header(br, base: int) -> dict[str, Any] | None:
    try:
        raw = br.read(int(base) + HDR_MAGIC, HEADER_READ_SIZE)
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
    name_ref = u32(HDR_MODULE_NAME_REF)
    name_addr = _resolve_ref(base, file_size, name_ref)
    if name_addr is None:
        name_addr = _resolve_ref(base, file_size, u32(HDR_NAME_REF))
    name = _read_c_string(br, name_addr, max(32, module_name_size + 1))
    if not name:
        return None
    return {
        "base": _hx(base),
        "base_int": int(base),
        "magic": raw[:4].decode("ascii", errors="replace"),
        "module": name,
        "file_size": int(file_size),
        "file_size_hex": _hx(file_size),
        "bss_size": int(bss_size),
        "bss_size_hex": _hx(bss_size),
        "module_name_size": int(module_name_size),
        "module_name_address": _hx(name_addr),
    }


def _is_skytrip(record: dict | None) -> bool:
    return bool(
        record
        and record.get("module") == SKYTRIP_MODULE
        and int(record.get("file_size", -1)) == SKYTRIP_FILE_SIZE
        and int(record.get("bss_size", -1)) == SKYTRIP_BSS_SIZE
        and int(record.get("module_name_size", -1)) == SKYTRIP_MODULE_NAME_SIZE
    )


def locate_skytrip_module(br) -> dict[str, Any]:
    """QUERY-enumerate loaded CROs and locate exact AS 1.4 DllSkyTrip."""
    started = time.monotonic()
    cursor = CRO_SCAN_START
    query_count = 0
    cro_count = 0
    target = None
    modules: list[dict[str, Any]] = []
    seen: set[int] = set()
    errors: list[dict[str, str]] = []

    while cursor < CRO_SCAN_END and query_count < MAX_QUERY_REGIONS:
        query_count += 1
        try:
            q = _query_region(br, cursor)
        except Exception as exc:
            errors.append({"address": _hx(cursor) or "", "error": f"{type(exc).__name__}: {exc}"})
            cursor += 0x1000
            continue
        base = int(q.get("base", 0))
        size = int(q.get("size", 0))
        if q.get("status") != 0 or size <= 0:
            cursor += 0x1000
            continue
        if base not in seen and CRO_SCAN_START <= base < CRO_SCAN_END:
            seen.add(base)
            rec = read_cro_header(br, base)
            if rec:
                cro_count += 1
                if len(modules) < 64:
                    modules.append({
                        "module": rec["module"], "base": rec["base"], "magic": rec["magic"],
                        "file_size_hex": rec["file_size_hex"], "bss_size_hex": rec["bss_size_hex"],
                    })
                if _is_skytrip(rec):
                    target = rec
                    break
        nxt = max(cursor + 0x1000, base + size)
        cursor = nxt if nxt > cursor else cursor + 0x1000

    return {
        "authority": "QUERY-enumerated loaded CRO + exact Alpha Sapphire 1.4 DllSkyTrip identity",
        "ram_writes": False,
        "present": bool(target),
        "target": target,
        "query_count": query_count,
        "cro_count": cro_count,
        "modules": modules,
        "errors": errors[:8],
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "bounded": query_count < MAX_QUERY_REGIONS or cursor >= CRO_SCAN_END,
    }


def verify_skytrip_relocation(br, target: dict | None) -> dict[str, Any]:
    base = _as_int((target or {}).get("base_int")) or _as_int((target or {}).get("base"))
    if not base:
        return {"verified": False, "reason": "NO_MODULE_BASE"}
    literal_addr = base + MAINPROC_VPTR_LITERAL_OFFSET
    expected = base + SKYTRIP_VPTR_OFFSETS["MainProc"]
    try:
        observed = struct.unpack_from("<I", br.read(literal_addr, 4), 0)[0]
    except Exception as exc:
        return {
            "verified": False,
            "reason": f"READ_FAILED:{type(exc).__name__}:{exc}",
            "module_base": _hx(base),
            "literal_address": _hx(literal_addr),
            "expected_mainproc_vptr": _hx(expected),
        }
    return {
        "verified": observed == expected,
        "module_base": _hx(base),
        "literal_address": _hx(literal_addr),
        "observed": _hx(observed),
        "expected_mainproc_vptr": _hx(expected),
    }


def _is_heap_ptr(value: int) -> bool:
    return HEAP_MIN <= int(value) < HEAP_MAX and (int(value) & 3) == 0


def _extract_heap_ptrs(raw: bytes, *, max_items: int = POINTERS_PER_OBJECT) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    seen: set[int] = set()
    for off in range(0, max(0, len(raw) - 3), 4):
        v = struct.unpack_from("<I", raw, off)[0]
        if _is_heap_ptr(v) and v not in seen:
            seen.add(v)
            out.append((off, v))
            if len(out) >= max_items:
                break
    return out




def _nonzero_words(raw: bytes, *, max_items: int = 160) -> list[dict[str, Any]]:
    """Compact word view for support evidence without another hardware run."""
    rows: list[dict[str, Any]] = []
    for off in range(0, max(0, len(raw) - 3), 4):
        value = struct.unpack_from("<I", raw, off)[0]
        if not value:
            continue
        rows.append({"offset": f"0x{off:X}", "value": _hx(value)})
        if len(rows) >= max_items:
            break
    return rows


def _float_words(raw: bytes, *, max_items: int = 160) -> list[dict[str, Any]]:
    """Decode plausible aligned float state from a mutable runtime segment.

    This is diagnostic-only.  Values are not treated as navigation authority;
    they are recorded so repeated hardware traces can identify live position,
    heading, camera, speed, or encounter-state fields by their deltas.
    """
    rows: list[dict[str, Any]] = []
    for off in range(0, max(0, len(raw) - 3), 4):
        u32 = struct.unpack_from("<I", raw, off)[0]
        if not u32:
            continue
        value = struct.unpack_from("<f", raw, off)[0]
        if not math.isfinite(value):
            continue
        if abs(value) > STATE_FLOAT_ABS_MAX:
            continue
        # Filter tiny denormals/noise while retaining useful signed fractions.
        if 0.0 < abs(value) < 1.0e-6:
            continue
        rows.append({
            "offset": f"0x{off:X}",
            "u32": _hx(u32),
            "f32": float(value),
        })
        if len(rows) >= max_items:
            break
    return rows


def _word_diffs(before_hex: str | None, after_hex: str | None, *, max_items: int = STATE_DIFF_MAX_WORDS) -> list[dict[str, Any]]:
    try:
        before = bytes.fromhex(str(before_hex or ""))
        after = bytes.fromhex(str(after_hex or ""))
    except Exception:
        return []
    n = min(len(before), len(after))
    rows: list[dict[str, Any]] = []
    for off in range(0, max(0, n - 3), 4):
        old = struct.unpack_from("<I", before, off)[0]
        new = struct.unpack_from("<I", after, off)[0]
        if old == new:
            continue
        old_f = struct.unpack_from("<f", before, off)[0]
        new_f = struct.unpack_from("<f", after, off)[0]
        row = {
            "offset": f"0x{off:X}",
            "old": _hx(old),
            "new": _hx(new),
        }
        if math.isfinite(old_f) and abs(old_f) <= STATE_FLOAT_ABS_MAX:
            row["old_f32"] = float(old_f)
        if math.isfinite(new_f) and abs(new_f) <= STATE_FLOAT_ABS_MAX:
            row["new_f32"] = float(new_f)
        rows.append(row)
        if len(rows) >= max_items:
            break
    return rows


def diff_runtime_module_state(previous: dict | None, current: dict | None) -> dict[str, Any]:
    """Return exact aligned-word deltas for live SkyTrip .data and .bss."""
    previous = previous or {}
    current = current or {}
    data_changes = _word_diffs(previous.get("hex"), current.get("hex"))
    prev_bss = previous.get("bss") or {}
    cur_bss = current.get("bss") or {}
    bss_changes = _word_diffs(prev_bss.get("hex"), cur_bss.get("hex"))
    return {
        "changed": bool(data_changes or bss_changes),
        "data_change_count": len(data_changes),
        "bss_change_count": len(bss_changes),
        "data_changes": data_changes,
        "bss_changes": bss_changes,
    }


def _merge_candidate(
    candidates: dict[str, list[dict[str, Any]]],
    cls: str,
    *,
    address: int,
    vptr: int,
    found_in: int,
    offset: int,
    depth: int,
    via: str,
) -> bool:
    bucket = candidates.setdefault(cls, [])
    address_hex = _hx(address)
    if any(x.get("address") == address_hex for x in bucket):
        return False
    if len(bucket) >= MAX_CANDIDATES_PER_CLASS:
        return False
    bucket.append({
        "class": cls,
        "address": address_hex,
        "address_int": int(address),
        "vptr": _hx(vptr),
        "found_in": _hx(found_in),
        "offset": int(offset),
        "depth": int(depth),
        "via": str(via),
    })
    return True


def _probe_heap_anchor_vptrs(
    br,
    seeds: list[tuple[int, int]],
    reverse: dict[int, str],
    candidates: dict[str, list[dict[str, Any]]],
    *,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Bounded local exact-vptr sweep around relocated SkyTrip heap anchors.

    CE hardware resolved .data+0x290 -> 0x08085988 but the pointer graph only
    reached 0x080859A8.  Runtime objects can be allocator siblings rather than
    children of that singleton.  Probe a maximum 64 KiB window around at most
    two unique anchors and classify only exact relocated SkyTrip vptr values.
    """
    started = time.monotonic()
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    total_reads = 0
    total_bytes = 0
    matches: list[dict[str, Any]] = []
    seen_windows: set[tuple[int, int]] = set()
    seen_seeds: set[int] = set()

    for seed_off, seed in seeds:
        seed = int(seed)
        if seed in seen_seeds or len(seen_seeds) >= HEAP_ANCHOR_PROBE_MAX_SEEDS:
            continue
        seen_seeds.add(seed)
        try:
            q = _query_region(br, seed)
        except Exception as exc:
            errors.append({"seed": _hx(seed), "error": f"QUERY:{type(exc).__name__}:{exc}"})
            continue
        qbase = int(q.get("base", 0) or 0)
        qsize = int(q.get("size", 0) or 0)
        qend = qbase + qsize
        if q.get("status") != 0 or qsize <= 0 or not (qbase <= seed < qend):
            rows.append({
                "seed": _hx(seed),
                "seed_offset": f"0x{seed_off:X}",
                "query": q,
                "status": "INVALID_QUERY_REGION",
            })
            continue

        start = max(qbase, (seed - HEAP_ANCHOR_PROBE_BACK) & ~(READ_CHUNK - 1))
        end = min(qend, start + HEAP_ANCHOR_PROBE_BYTES)
        if end <= start:
            continue
        window = (start, end)
        if window in seen_windows:
            continue
        seen_windows.add(window)
        row = {
            "seed": _hx(seed),
            "seed_offset": f"0x{seed_off:X}",
            "query": q,
            "start": _hx(start),
            "end": _hx(end),
            "requested_bytes": end - start,
            "reads": 0,
            "bytes_read": 0,
            "matches": [],
        }

        cursor = start
        while cursor < end and total_reads < HEAP_ANCHOR_PROBE_MAX_READS * HEAP_ANCHOR_PROBE_MAX_SEEDS:
            n = min(READ_CHUNK, end - cursor)
            try:
                raw = br.read(cursor, n)
            except Exception as exc:
                errors.append({"address": _hx(cursor), "length": n, "error": f"{type(exc).__name__}: {exc}"})
                cursor += n
                continue
            total_reads += 1
            total_bytes += len(raw)
            row["reads"] += 1
            row["bytes_read"] += len(raw)
            for off in range(0, max(0, len(raw) - 3), 4):
                word = struct.unpack_from("<I", raw, off)[0]
                cls = reverse.get(word)
                if not cls:
                    continue
                obj_addr = cursor + off
                added = _merge_candidate(
                    candidates, cls, address=obj_addr, vptr=word, found_in=cursor,
                    offset=off, depth=-1, via=f"heap_anchor_probe:{_hx(seed)}",
                )
                hit = {
                    "class": cls,
                    "address": _hx(obj_addr),
                    "vptr": _hx(word),
                    "chunk": _hx(cursor),
                    "offset": f"0x{off:X}",
                    "new_candidate": added,
                }
                row["matches"].append(hit)
                matches.append(hit)
            cursor += n
            if progress and row["reads"] in (1, 32, 64, 96, 128):
                progress({
                    "probe_reads": total_reads,
                    "probe_bytes": total_bytes,
                    "probe_seed": _hx(seed),
                    "probe_matches": len(matches),
                    "found": {name: len(items) for name, items in candidates.items() if items},
                })
        rows.append(row)
        if matches:
            # One positive local window is enough for the next timed trace.
            break

    return {
        "status": "FOUND" if matches else "NO_KNOWN_VPTRS_IN_LOCAL_WINDOW",
        "policy": "exact relocated SkyTrip vptrs only; <=64KiB per seed; <=2 seeds; read-only",
        "probe_bytes_per_seed": HEAP_ANCHOR_PROBE_BYTES,
        "max_reads_per_seed": HEAP_ANCHOR_PROBE_MAX_READS,
        "seeds_considered": len(seen_seeds),
        "reads": total_reads,
        "bytes_read": total_bytes,
        "windows": rows,
        "matches": matches[:64],
        "errors": errors[:32],
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "bounded": True,
    }


def _read_bounded(br, address: int, length: int) -> bytes:
    """Read an arbitrary small object using bridge-sized chunks."""
    if length < 0:
        raise ValueError("negative length")
    chunks = []
    done = 0
    while done < length:
        n = min(READ_CHUNK, length - done)
        chunks.append(br.read(int(address) + done, n))
        done += n
    return b"".join(chunks)


def _runtime_header_layout(br, base: int) -> dict[str, Any]:
    """Read CRO layout fields after RO has loaded/fixed the module.

    Dynamic CRO .data/.bss are not guaranteed to remain at file-relative
    addresses.  RO rewrites segment-table entries to the application-supplied
    runtime buffers, so these fields and that table are evidence, not an
    assumption that ``base + data_offset`` is still mapped.
    """
    try:
        raw = _read_bounded(br, base + RUNTIME_HEADER_LAYOUT_OFFSET, RUNTIME_HEADER_LAYOUT_SIZE)
        (code_off, code_size, data_off, data_size, name_off, name_size, seg_off, seg_num) = struct.unpack(
            "<8I", raw
        )
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "code_offset": _hx(code_off),
        "code_size": int(code_size),
        "data_offset": _hx(data_off),
        "data_size": int(data_size),
        "module_name_offset": _hx(name_off),
        "module_name_size": int(name_size),
        "segment_table_offset": _hx(seg_off),
        "segment_count": int(seg_num),
    }


def _resolve_runtime_address(base: int, raw_value: int, *, file_size: int = SKYTRIP_FILE_SIZE) -> int | None:
    """Resolve a CRO offset-or-runtime-address without guessing unmapped tails."""
    value = int(raw_value) & 0xFFFFFFFF
    if value == 0:
        return None
    # Loaded dynamic segment entries are absolute userland addresses.
    if 0x00100000 <= value < 0x14000000:
        return value
    # Header/table locations usually remain file-relative offsets.
    if 0 < value < int(file_size):
        return int(base) + value
    return None


def _runtime_segment_table(br, base: int, header: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    header = dict(header or _runtime_header_layout(br, base))
    if header.get("error"):
        return [{"error": header["error"]}]
    raw_table = _as_int(header.get("segment_table_offset"), SKYTRIP_SEGMENT_TABLE_OFFSET)
    count = int(header.get("segment_count", SKYTRIP_SEGMENT_COUNT) or SKYTRIP_SEGMENT_COUNT)
    count = max(0, min(SKYTRIP_MAX_RUNTIME_SEGMENTS, count))
    table_addr = _resolve_runtime_address(base, raw_table)
    if table_addr is None or count <= 0:
        return [{"error": "INVALID_RUNTIME_SEGMENT_TABLE", "raw_table": _hx(raw_table), "count": count}]
    try:
        raw = _read_bounded(br, table_addr, count * 12)
    except Exception as exc:
        return [{"error": f"{type(exc).__name__}: {exc}", "table_address": _hx(table_addr)}]
    rows = []
    for i in range(count):
        seg_value, size, kind = struct.unpack_from("<III", raw, i * 12)
        runtime_addr = _resolve_runtime_address(base, seg_value)
        q = None
        if runtime_addr:
            try:
                q = _query_region(br, runtime_addr)
            except Exception as exc:
                q = {"error": f"{type(exc).__name__}: {exc}"}
        rows.append({
            "index": i,
            "raw_offset_or_address": _hx(seg_value),
            "runtime_address": _hx(runtime_addr),
            "size": int(size),
            "kind": int(kind),
            "query": q,
        })
    return rows


def _runtime_segment(rows: list[dict[str, Any]], kind: int) -> dict[str, Any] | None:
    for row in rows:
        if isinstance(row, dict) and not row.get("error") and int(row.get("kind", -1)) == int(kind):
            if _as_int(row.get("runtime_address")):
                return row
    return None

def discover_skytrip_objects(
    br,
    target: dict | None,
    *,
    progress: Callable[[dict[str, Any]], None] | None = None,
    allow_anchor_probe: bool = True,
) -> dict[str, Any]:
    """Bounded pointer-graph discovery seeded from DllSkyTrip runtime data.

    This intentionally avoids a blind multi-megabyte heap sweep.  It starts at
    SkyTrip's own static data, follows only heap-looking pointers, and stops at
    a fixed object/depth budget.  Exact relocated vptrs classify candidates.
    """
    started = time.monotonic()
    base = _as_int((target or {}).get("base_int")) or _as_int((target or {}).get("base"))
    if not base:
        return {"status": "NO_MODULE_BASE", "candidates": {}, "elapsed_seconds": 0.0}

    expected_vptrs = {name: base + off for name, off in SKYTRIP_VPTR_OFFSETS.items()}
    reverse = {v: name for name, v in expected_vptrs.items()}

    runtime_header = _runtime_header_layout(br, base)
    segments = _runtime_segment_table(br, base, runtime_header)
    data_seg = _runtime_segment(segments, 2)
    bss_seg = _runtime_segment(segments, 3)

    # Critical hardware correction (v0p43CE): a loaded/fixed dynamic CRO has
    # its .data copied to an application-supplied buffer.  The original
    # file-relative data tail may be unmapped.  Seed discovery from the
    # relocated type-2 segment table entry, not base+SKYTRIP_DATA_OFFSET.
    data_address = _as_int((data_seg or {}).get("runtime_address"))
    if not data_address:
        raw_data_off = _as_int(runtime_header.get("data_offset"), SKYTRIP_DATA_OFFSET)
        data_address = _resolve_runtime_address(base, raw_data_off) or 0
    data_length = int((data_seg or {}).get("size", 0) or runtime_header.get("data_size", 0) or SKYTRIP_DATA_SIZE)
    if data_length <= 0 or data_length > 0x4000:
        data_length = SKYTRIP_DATA_SIZE
    # The AS 1.4 contract says .data is 0x294; never expand a mapper read past
    # the smaller runtime segment size and bridge-sized chunks remain enforced.
    data_length = min(data_length, SKYTRIP_DATA_SIZE)
    try:
        module_data = _read_bounded(br, data_address, data_length)
    except Exception as exc:
        region = None
        if data_address:
            try:
                region = _query_region(br, data_address)
            except Exception as qexc:
                region = {"error": f"{type(qexc).__name__}: {qexc}"}
        return {
            "status": "RUNTIME_DATA_READ_FAILED",
            "error": f"{type(exc).__name__}: {exc}",
            "module_base": _hx(base),
            "runtime_header": runtime_header,
            "runtime_segments": segments,
            "runtime_data_address": _hx(data_address),
            "runtime_data_length": data_length,
            "runtime_data_query": region,
            "candidates": {},
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }

    seeds = _extract_heap_ptrs(module_data, max_items=64)
    queue: deque[tuple[int, int, str]] = deque()
    for off, ptr in seeds:
        queue.append((ptr, 0, f"runtime_data+0x{off:X}"))

    # BSS is another application-supplied runtime buffer. Seed any heap
    # pointers from it as secondary evidence, again using the relocated table.
    bss_evidence = None
    if bss_seg:
        addr = _as_int(bss_seg.get("runtime_address"))
        size = int(bss_seg.get("size", 0) or 0)
        if addr and 0 < size <= 0x1000:
            try:
                bss_raw = _read_bounded(br, addr, size)
            except Exception:
                bss_raw = b""
            bss_ptrs = _extract_heap_ptrs(bss_raw, max_items=64)
            bss_evidence = {
                "address": _hx(addr),
                "length": len(bss_raw),
                "heap_pointers": [{"offset": f"0x{off:X}", "pointer": _hx(ptr)} for off, ptr in bss_ptrs],
                "hex": bss_raw.hex(),
                "nonzero_words": _nonzero_words(bss_raw),
                "sha256": hashlib.sha256(bss_raw).hexdigest(),
            }
            for off, ptr in bss_ptrs:
                queue.append((ptr, 0, f"runtime_bss+0x{off:X}"))

    seen: set[int] = set()
    objects_read = 0
    read_errors: list[dict[str, str]] = []
    graph_rows: list[dict[str, Any]] = []
    candidates: dict[str, list[dict[str, Any]]] = {name: [] for name in SKYTRIP_VPTR_OFFSETS}

    while queue and objects_read < POINTER_GRAPH_MAX_OBJECTS:
        address, depth, via = queue.popleft()
        address = int(address)
        if address in seen or not _is_heap_ptr(address):
            continue
        seen.add(address)
        try:
            raw = br.read(address, READ_CHUNK)
        except Exception as exc:
            read_errors.append({"address": _hx(address) or "", "error": f"{type(exc).__name__}: {exc}"})
            continue
        objects_read += 1
        row: dict[str, Any] = {
            "address": _hx(address), "depth": depth, "via": via,
            "sha256_16": hashlib.sha256(raw).hexdigest()[:16],
        }

        matches = []
        for off in range(0, len(raw) - 3, 4):
            word = struct.unpack_from("<I", raw, off)[0]
            cls = reverse.get(word)
            if not cls:
                continue
            obj_addr = address + off
            matches.append({"class": cls, "offset": f"0x{off:X}", "address": _hx(obj_addr), "vptr": _hx(word)})
            _merge_candidate(
                candidates, cls, address=obj_addr, vptr=word, found_in=address,
                offset=off, depth=depth, via=via,
            )
        if matches:
            row["vptr_matches"] = matches
        graph_ptrs = _extract_heap_ptrs(raw)
        if graph_ptrs:
            row["heap_pointers"] = [{"offset": f"0x{off:X}", "pointer": _hx(ptr)} for off, ptr in graph_ptrs]
        if len(graph_rows) < GRAPH_HEX_SNAPSHOTS:
            row["hex"] = raw.hex()
            row["nonzero_words"] = _nonzero_words(raw, max_items=64)

        if len(graph_rows) < 128:
            graph_rows.append(row)

        if depth < POINTER_GRAPH_MAX_DEPTH:
            for off, ptr in graph_ptrs:
                if ptr not in seen and len(queue) < POINTER_GRAPH_MAX_OBJECTS * 4:
                    queue.append((ptr, depth + 1, f"{_hx(address)}+0x{off:X}"))

        if progress and (objects_read == 1 or objects_read % 16 == 0):
            progress({
                "objects_read": objects_read,
                "queued": len(queue),
                "found": {name: len(rows) for name, rows in candidates.items() if rows},
            })

        # MainProc + Camera + one encounter/legend object is already enough to
        # make the live trace high-value. Continue a little longer only if the
        # queue is shallow, otherwise stop to keep takeoff latency bounded.
        if (
            candidates["MainProc"]
            and candidates["Camera"]
            and (candidates["BMEncount"] or candidates["BMLegendPoint"] or candidates["BMHikyo"])
            and objects_read >= 24
        ):
            break

    pointer_graph_candidates = {name: rows for name, rows in candidates.items() if rows}
    anchor_probe = {
        "status": "SKIPPED_POINTER_GRAPH_FOUND_OBJECTS" if pointer_graph_candidates else "NOT_RUN",
        "bounded": True,
    }
    if not pointer_graph_candidates and seeds and allow_anchor_probe:
        anchor_probe = _probe_heap_anchor_vptrs(br, seeds, reverse, candidates, progress=progress)
    elif not pointer_graph_candidates and seeds and not allow_anchor_probe:
        anchor_probe = {
            "status": "SKIPPED_AFTER_HARDWARE_EMPTY_LOCAL_PROBE",
            "policy": "CF hardware found no known vptrs in the 64KiB anchor window; retries use pointer graph only",
            "bounded": True,
        }

    compact_candidates = {name: rows for name, rows in candidates.items() if rows}
    return {
        "status": "FOUND" if compact_candidates else "NO_KNOWN_VPTR_OBJECTS",
        "module_base": _hx(base),
        "expected_vptrs": {name: _hx(v) for name, v in expected_vptrs.items()},
        "runtime_header": runtime_header,
        "runtime_segments": segments,
        "module_data": {
            "address": _hx(data_address),
            "source": "relocated_type2_segment" if data_seg else "header_data_offset_fallback",
            "length": data_length,
            "heap_seed_count": len(seeds),
            "heap_seeds": [{"offset": f"0x{off:X}", "pointer": _hx(ptr)} for off, ptr in seeds[:32]],
            "hex": module_data.hex(),
            "nonzero_words": _nonzero_words(module_data),
            "sha256": hashlib.sha256(module_data).hexdigest(),
        },
        "module_bss": bss_evidence,
        "anchor_probe": anchor_probe,
        "runtime_segments": segments,
        "objects_read": objects_read,
        "max_objects": POINTER_GRAPH_MAX_OBJECTS,
        "max_depth": POINTER_GRAPH_MAX_DEPTH,
        "queued_remaining": len(queue),
        "candidates": compact_candidates,
        "graph": graph_rows,
        "read_errors": read_errors[:32],
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "bounded": True,
    }


def choose_trace_objects(discovery: dict | None) -> list[dict[str, Any]]:
    """Select at most one candidate per useful class for the timed trace."""
    candidates = (discovery or {}).get("candidates") or {}
    order = ("MainProc", "Camera", "BMEncount", "BMLegendPoint", "BMHikyo", "BMShip", "BuildModel")
    selected: list[dict[str, Any]] = []
    seen_addr: set[int] = set()
    for cls in order:
        rows = candidates.get(cls) or []
        if not rows:
            continue
        row = rows[0]
        address = _as_int(row.get("address_int")) or _as_int(row.get("address"))
        if not address or address in seen_addr:
            continue
        seen_addr.add(address)
        selected.append({"class": cls, "address": _hx(address), "address_int": address, "length": OBJECT_READ_LENGTHS[cls]})
    return selected


def read_trace_objects(br, selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Read selected SkyTrip objects without dereferencing arbitrary pointers."""
    rows: list[dict[str, Any]] = []
    for obj in selected:
        cls = str(obj.get("class") or "Unknown")
        address = _as_int(obj.get("address_int")) or _as_int(obj.get("address"))
        length = max(4, min(0x200, int(obj.get("length") or 0x100)))
        row: dict[str, Any] = {"class": cls, "address": _hx(address), "length": length}
        try:
            raw = br.read(address, length)
            row["hex"] = raw.hex()
            row["sha256_16"] = hashlib.sha256(raw).hexdigest()[:16]
            if len(raw) >= 4:
                row["word0"] = _hx(struct.unpack_from("<I", raw, 0)[0])
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
        rows.append(row)
    return rows



def _bounded_region_snapshot(br, address: int, length: int) -> tuple[bytes, dict[str, Any]]:
    """Read within the QUERY region containing ``address``."""
    q = _query_region(br, int(address))
    if q.get("status") != 0:
        raise SkyTripRuntimeError(f"QUERY_FAILED:{q.get('status')}")
    base = int(q.get("base", 0))
    size = int(q.get("size", 0))
    if size <= 0 or not (base <= int(address) < base + size):
        raise SkyTripRuntimeError("INVALID_QUERY_REGION")
    available = (base + size) - int(address)
    n = max(0, min(int(length), int(available), READ_CHUNK))
    if n < 4:
        raise SkyTripRuntimeError("REGION_TAIL_TOO_SMALL")
    raw = br.read(int(address), n)
    return raw, q


def _read_runtime_scan_segment(br, address: int, length: int) -> tuple[bytes, list[dict[str, Any]]]:
    """Read one declared runtime segment without crossing an unmapped QUERY gap.

    The CRO runtime header's nominal code size is a file-layout quantity and can
    span padding/gaps that are not mapped after RO fixes the module.  Hardware
    on DllSkyTrip proved this: the type-0 executable segment ended before the
    next mapped type-1 segment, while a nominal contiguous read crossed an
    unmapped page and failed.  Runtime segment-table entries + QUERY boundaries
    are therefore authoritative for scanner reads.
    """
    cursor = int(address)
    remaining = max(0, int(length))
    if remaining <= 0:
        return b"", []

    chunks: list[bytes] = []
    regions: list[dict[str, Any]] = []
    while remaining > 0:
        q = _query_region(br, cursor)
        if q.get("status") != 0:
            raise SkyTripRuntimeError(f"QUERY_FAILED:{q.get('status')} at {_hx(cursor)}")
        base = int(q.get("base", 0) or 0)
        size = int(q.get("size", 0) or 0)
        perm = int(q.get("perm", 0) or 0)
        if size <= 0 or not (base <= cursor < base + size):
            raise SkyTripRuntimeError(f"INVALID_QUERY_REGION at {_hx(cursor)}")
        if not (perm & 0x1):
            raise SkyTripRuntimeError(f"NOT_READABLE at {_hx(cursor)}")
        available = (base + size) - cursor
        n = min(remaining, available)
        if n <= 0:
            raise SkyTripRuntimeError(f"EMPTY_QUERY_TAIL at {_hx(cursor)}")
        raw = _read_bounded(br, cursor, n)
        chunks.append(raw)
        regions.append({
            "address": _hx(cursor),
            "length": int(n),
            "query": q,
        })
        cursor += n
        remaining -= n
        if remaining > 0:
            # The next byte must itself be mapped/readable. Query it on the
            # next loop rather than assuming virtual contiguity.
            continue
    return b"".join(chunks), regions



def _is_userland_pointer(value: int) -> bool:
    value = int(value) & 0xFFFFFFFF
    return USERLAND_PTR_MIN <= value < USERLAND_PTR_MAX and (value & 3) == 0


def _address_in_ranges(address: int, ranges: list[dict[str, Any]]) -> bool:
    address = int(address)
    for row in ranges:
        start = int(row.get("start_int", 0) or 0)
        end = int(row.get("end_int", 0) or 0)
        if start <= address < end:
            return True
    return False


def _skytrip_runtime_ranges(
    runtime_segments: list[dict[str, Any]],
    target: dict | None,
) -> list[dict[str, Any]]:
    """Return exact live DllSkyTrip runtime ranges, not the whole shared heap.

    CI hardware put .data/.bss inside a ~12 MiB writable QUERY allocation that
    can also contain unrelated live objects.  Excluding that entire QUERY
    region would therefore hide the very objects we want.  Only the relocated
    CRO segments are module-local.  The type-3 BSS range is extended to the
    CRO-header/known 0x1A4 BSS size because hardware exposed a 0x184 runtime
    segment-table size while a module-local thunk/seed occupied the remaining
    tail through +0x1A4.
    """
    declared_bss = max(
        SKYTRIP_BSS_SIZE,
        int((target or {}).get("bss_size", 0) or 0),
    )
    ranges: list[dict[str, Any]] = []
    for row in runtime_segments:
        if not isinstance(row, dict) or row.get("error"):
            continue
        address = _as_int(row.get("runtime_address"))
        size = int(row.get("size", 0) or 0)
        kind = int(row.get("kind", -1))
        if not address or size <= 0:
            continue
        if kind == 3:
            size = max(size, declared_bss)
        ranges.append({
            "index": int(row.get("index", -1)),
            "kind": kind,
            "start": _hx(address),
            "start_int": int(address),
            "size": int(size),
            "end": _hx(address + size),
            "end_int": int(address + size),
        })
    ranges.sort(key=lambda row: (int(row["start_int"]), int(row.get("kind", -1))))
    return ranges


def _extract_userland_ptrs(
    raw: bytes,
    *,
    max_items: int = 32,
    exclude_ranges: list[dict[str, Any]] | None = None,
) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    seen: set[int] = set()
    excluded = list(exclude_ranges or [])
    for off in range(0, max(0, len(raw) - 3), 4):
        value = struct.unpack_from("<I", raw, off)[0]
        if not _is_userland_pointer(value):
            continue
        if _address_in_ranges(value, excluded) or value in seen:
            continue
        seen.add(value)
        out.append((off, value))
        if len(out) >= max_items:
            break
    return out


def _query_candidate(br, address: int) -> dict[str, Any] | None:
    """QUERY first so scalar/unmapped literals never trigger pointless READs."""
    try:
        q = _query_region(br, int(address))
    except Exception:
        return None
    if q.get("status") != 0:
        return None
    base = int(q.get("base", 0) or 0)
    size = int(q.get("size", 0) or 0)
    if size <= 0 or not (base <= int(address) < base + size):
        return None
    return q


def discover_skytrip_external_references(
    br,
    target: dict | None,
    *,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Find genuinely external writable state referenced by live DllSkyTrip.

    CI proved the QUERY-safe text/rodata reader, but its ownership filter was
    wrong: every accepted hardware root was an alias into SkyTrip's own
    relocated .data/.bss and the only delta was the already-known +0x0C init
    flag. CJ keeps the query-safe runtime-segment reader but changes the
    ownership model:

    * aligned pointers across the wider 3DS userland address space;
    * exact DllSkyTrip runtime segments are excluded (not the shared heap);
    * direct writable literals outside those segments are eligible;
    * code-referenced read-only bridge tables may yield one bounded writable
      pointer level;
    * relocated .data/.bss are scanned once for pointers that *escape* the
      module-local ranges into separately owned writable structures.

    No unreferenced heap sweep and no game-RAM writes are performed.
    """
    started = time.monotonic()
    base = _as_int((target or {}).get("base_int")) or _as_int((target or {}).get("base"))
    if not base:
        return {"status": "NO_MODULE_BASE", "trace_targets": [], "elapsed_seconds": 0.0}

    header = _runtime_header_layout(br, base)
    if header.get("error"):
        return {
            "status": "RUNTIME_HEADER_FAILED",
            "error": header["error"],
            "trace_targets": [],
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }

    nominal_code_address = _as_int(header.get("code_offset"))
    nominal_code_size = int(header.get("code_size", 0) or 0)
    if not nominal_code_address or nominal_code_size <= 0 or nominal_code_size > EXTERNAL_CODE_SCAN_MAX:
        return {
            "status": "INVALID_CODE_SEGMENT",
            "code_address": _hx(nominal_code_address),
            "code_size": nominal_code_size,
            "trace_targets": [],
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }

    runtime_segments = _runtime_segment_table(br, base, header)
    local_ranges = _skytrip_runtime_ranges(runtime_segments, target)
    scan_defs: list[dict[str, Any]] = []
    for row in runtime_segments:
        if not isinstance(row, dict) or row.get("error"):
            continue
        kind = int(row.get("kind", -1))
        if kind not in (0, 1):
            continue
        address = _as_int(row.get("runtime_address"))
        size = int(row.get("size", 0) or 0)
        if not address or size <= 0:
            continue
        scan_defs.append({
            "index": int(row.get("index", -1)),
            "kind": kind,
            "address": int(address),
            "size": int(size),
            "query": row.get("query"),
            "source": "runtime_segment_table",
        })

    # Compatibility fallback for older synthetic layouts. Clamp to the current
    # QUERY region so the CH cross-gap failure remains impossible.
    if not scan_defs:
        try:
            q = _query_region(br, nominal_code_address)
            qbase = int(q.get("base", 0) or 0)
            qsize = int(q.get("size", 0) or 0)
            available = max(0, (qbase + qsize) - nominal_code_address) if q.get("status") == 0 else 0
            fallback_size = min(nominal_code_size, available, EXTERNAL_CODE_SCAN_MAX)
        except Exception:
            q = None
            fallback_size = 0
        if fallback_size > 0:
            scan_defs.append({
                "index": -1,
                "kind": 0,
                "address": int(nominal_code_address),
                "size": int(fallback_size),
                "query": q,
                "source": "header_query_clamped_fallback",
            })

    if not scan_defs:
        return {
            "status": "NO_READABLE_CODE_SEGMENTS",
            "code_address": _hx(nominal_code_address),
            "nominal_code_size": nominal_code_size,
            "runtime_segments": runtime_segments,
            "local_runtime_ranges": local_ranges,
            "trace_targets": [],
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }

    total_declared = sum(int(x["size"]) for x in scan_defs)
    if total_declared > EXTERNAL_CODE_SCAN_MAX:
        budget = EXTERNAL_CODE_SCAN_MAX
        bounded_defs = []
        for row in scan_defs:
            if budget <= 0:
                break
            take = min(int(row["size"]), budget)
            if take <= 0:
                continue
            bounded = dict(row)
            bounded["size"] = take
            bounded_defs.append(bounded)
            budget -= take
        scan_defs = bounded_defs

    scanned_segments: list[dict[str, Any]] = []
    segment_blobs: list[tuple[dict[str, Any], bytes]] = []
    scan_errors: list[dict[str, Any]] = []
    for row in scan_defs:
        address = int(row["address"])
        size = int(row["size"])
        try:
            raw, query_chunks = _read_runtime_scan_segment(br, address, size)
        except Exception as exc:
            scan_errors.append({
                "index": row.get("index"),
                "kind": row.get("kind"),
                "address": _hx(address),
                "size": size,
                "error": f"{type(exc).__name__}: {exc}",
            })
            continue
        if not raw:
            continue
        meta = {
            "index": row.get("index"),
            "kind": row.get("kind"),
            "address": _hx(address),
            "address_int": address,
            "declared_size": size,
            "bytes_read": len(raw),
            "source": row.get("source"),
            "query_chunks": query_chunks,
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        scanned_segments.append(meta)
        segment_blobs.append((meta, raw))

    if not segment_blobs:
        return {
            "status": "CODE_SEGMENT_READ_FAILED",
            "code_address": _hx(nominal_code_address),
            "nominal_code_size": nominal_code_size,
            "runtime_segments": runtime_segments,
            "local_runtime_ranges": local_ranges,
            "scan_errors": scan_errors,
            "trace_targets": [],
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }

    # value -> absolute literal locations. Filter alignment and module ownership
    # before the raw-candidate budget so CI's local aliases cannot crowd out
    # genuinely external values.
    literal_locations: dict[int, list[dict[str, Any]]] = {}
    internal_literal_values: dict[int, int] = {}
    unaligned_userland_literals = 0
    total_scanned = 0
    code_hasher = hashlib.sha256()
    for meta, raw in segment_blobs:
        seg_address = int(meta["address_int"])
        seg_kind = int(meta.get("kind", -1))
        total_scanned += len(raw)
        code_hasher.update(raw)
        for off in range(0, max(0, len(raw) - 3), 4):
            value = struct.unpack_from("<I", raw, off)[0]
            if not (USERLAND_PTR_MIN <= value < USERLAND_PTR_MAX):
                continue
            if value & 3:
                unaligned_userland_literals += 1
                continue
            if _address_in_ranges(value, local_ranges):
                internal_literal_values[value] = internal_literal_values.get(value, 0) + 1
                continue
            bucket = literal_locations.setdefault(value, [])
            if len(bucket) < 16:
                bucket.append({
                    "address": seg_address + off,
                    "offset": off,
                    "segment_kind": seg_kind,
                    "segment_index": meta.get("index"),
                })

    raw_candidates = sorted(
        literal_locations.items(),
        key=lambda item: (
            -len(item[1]),
            item[1][0]["address"] if item[1] else 0,
            item[0],
        ),
    )[:EXTERNAL_RAW_POINTER_MAX]

    roots_by_address: dict[int, dict[str, Any]] = {}
    read_only_bridges: list[dict[str, Any]] = []
    query_errors: list[dict[str, Any]] = []

    def add_root(address: int, *, category: str, source: dict[str, Any]) -> bool:
        address = int(address)
        if not _is_userland_pointer(address) or _address_in_ranges(address, local_ranges):
            return False
        existing = roots_by_address.get(address)
        if existing is not None:
            existing.setdefault("sources", []).append(source)
            existing.setdefault("categories", [])
            if category not in existing["categories"]:
                existing["categories"].append(category)
            return True
        q = _query_candidate(br, address)
        if not q or not (int(q.get("perm", 0) or 0) & 0x2):
            return False
        try:
            raw, q = _bounded_region_snapshot(br, address, EXTERNAL_ROOT_SNAPSHOT)
        except Exception as exc:
            if len(query_errors) < 32:
                query_errors.append({"address": _hx(address), "error": f"{type(exc).__name__}: {exc}"})
            return False
        user_ptrs = _extract_userland_ptrs(raw, max_items=8, exclude_ranges=local_ranges)
        float_words = _float_words(raw, max_items=24)
        nonzero = _nonzero_words(raw, max_items=32)
        roots_by_address[address] = {
            "address": _hx(address),
            "address_int": address,
            "categories": [category],
            "sources": [source],
            "query": q,
            "snapshot_length": len(raw),
            "snapshot_hex": raw.hex(),
            "sha256_16": hashlib.sha256(raw).hexdigest()[:16],
            "external_pointers": [{"offset": f"0x{off:X}", "pointer": _hx(ptr)} for off, ptr in user_ptrs],
            # Compatibility field for existing support consumers; now contains
            # all aligned userland pointers rather than 0x08.. heap only.
            "heap_pointers": [{"offset": f"0x{off:X}", "pointer": _hx(ptr)} for off, ptr in user_ptrs],
            "float_words": float_words,
            "nonzero_words": nonzero,
        }
        return True

    # Direct code/rodata literal classification. QUERY before READ prevents the
    # invalid 0x0A00000x scalar literals seen in CI from producing read errors.
    for i, (address, locations) in enumerate(raw_candidates):
        q = _query_candidate(br, address)
        if not q:
            continue
        perm = int(q.get("perm", 0) or 0)
        literal_source = {
            "via": "skytrip_code_literal",
            "literal_count": len(locations),
            "literal_offsets": [
                f"seg{loc.get('segment_index')}:{loc.get('segment_kind')}+0x{int(loc.get('offset', 0)):X}"
                for loc in locations
            ],
            "code_literal_addresses": [_hx(int(loc["address"])) for loc in locations],
            "literal_sources": locations,
        }
        if perm & 0x2:
            add_root(address, category="direct_code_writable", source=literal_source)
        elif (perm & 0x1) and len(read_only_bridges) < EXTERNAL_READONLY_BRIDGE_MAX:
            try:
                bridge_raw, bridge_q = _bounded_region_snapshot(br, address, EXTERNAL_ROOT_SNAPSHOT)
            except Exception:
                bridge_raw = b""
                bridge_q = q
            if bridge_raw:
                bridge = {
                    "address": _hx(address),
                    "address_int": int(address),
                    "query": bridge_q,
                    "literal_count": len(locations),
                    "snapshot_length": len(bridge_raw),
                    "sha256_16": hashlib.sha256(bridge_raw).hexdigest()[:16],
                    "escaped_pointers": [],
                }
                for off, ptr in _extract_userland_ptrs(
                    bridge_raw,
                    max_items=EXTERNAL_BRIDGE_POINTERS_PER_ROOT,
                    exclude_ranges=local_ranges,
                ):
                    source = {
                        "via": "read_only_bridge_pointer",
                        "bridge_address": _hx(address),
                        "bridge_offset": f"0x{off:X}",
                        "code_literal_addresses": literal_source["code_literal_addresses"],
                    }
                    if add_root(ptr, category="read_only_bridge_escape", source=source):
                        bridge["escaped_pointers"].append({"offset": f"0x{off:X}", "pointer": _hx(ptr)})
                read_only_bridges.append(bridge)
        if progress and (i + 1) % 16 == 0:
            progress({
                "raw_candidates_checked": i + 1,
                "true_external_writable_roots": len(roots_by_address),
                "runtime_segments_scanned": len(scanned_segments),
                "bytes_scanned": total_scanned,
            })
        # Reserve root capacity for module-local escaped pointers, which are
        # stronger ownership evidence than a raw literal.
        if len(roots_by_address) >= EXTERNAL_DIRECT_ROOT_MAX:
            break

    # Scan module-local mutable segments once for pointers that escape the CRO.
    # This is the explicit replacement for CI's incorrect practice of tracing
    # local .data/.bss aliases themselves.
    local_segment_scans: list[dict[str, Any]] = []
    local_escaped_candidates = 0
    for local in local_ranges:
        if int(local.get("kind", -1)) not in (2, 3):
            continue
        address = int(local["start_int"])
        size = min(int(local.get("size", 0) or 0), EXTERNAL_LOCAL_SEGMENT_SCAN_MAX)
        if size < 4:
            continue
        try:
            raw = _read_bounded(br, address, size)
        except Exception as exc:
            local_segment_scans.append({
                "kind": local.get("kind"), "address": _hx(address), "size": size,
                "error": f"{type(exc).__name__}: {exc}",
            })
            continue
        scan_row = {
            "kind": local.get("kind"),
            "address": _hx(address),
            "size": len(raw),
            "sha256_16": hashlib.sha256(raw).hexdigest()[:16],
            "escaped_pointers": [],
        }
        for off, ptr in _extract_userland_ptrs(raw, max_items=32, exclude_ranges=local_ranges):
            local_escaped_candidates += 1
            source = {
                "via": "module_local_pointer_escape",
                "local_segment_kind": int(local.get("kind", -1)),
                "slot_address": _hx(address + off),
                "slot_offset": f"0x{off:X}",
            }
            if add_root(ptr, category="module_local_escape", source=source):
                scan_row["escaped_pointers"].append({"offset": f"0x{off:X}", "pointer": _hx(ptr)})
            if len(roots_by_address) >= EXTERNAL_ACCEPTED_ROOT_MAX:
                break
        local_segment_scans.append(scan_row)
        if len(roots_by_address) >= EXTERNAL_ACCEPTED_ROOT_MAX:
            break

    roots = list(roots_by_address.values())
    for row in roots:
        literal_count = 0
        for source in row.get("sources") or []:
            literal_count += int(source.get("literal_count", 0) or 0)
        row["literal_count"] = literal_count
        categories = row.get("categories") or []
        ownership_bonus = (
            (40 if "module_local_escape" in categories else 0)
            + (24 if "read_only_bridge_escape" in categories else 0)
            + (8 if "direct_code_writable" in categories else 0)
        )
        row["score"] = (
            literal_count * 4
            + len(row.get("external_pointers") or []) * 5
            + min(len(row.get("float_words") or []), 12)
            + ownership_bonus
        )
        # Keep old flattened fields when the root was a direct literal.
        direct_sources = [s for s in (row.get("sources") or []) if s.get("via") == "skytrip_code_literal"]
        row["code_literal_addresses"] = [a for s in direct_sources for a in (s.get("code_literal_addresses") or [])]
        row["literal_offsets"] = [a for s in direct_sources for a in (s.get("literal_offsets") or [])]
    roots.sort(key=lambda row: (-int(row.get("score", 0)), -int(row.get("literal_count", 0)), int(row.get("address_int", 0))))

    trace_targets: list[dict[str, Any]] = []
    seen_addresses: set[int] = set()

    def add_target(*, address: int, via: str, source: str, length: int, initial_raw: bytes | None = None, query: dict | None = None):
        if address in seen_addresses or len(trace_targets) >= EXTERNAL_TRACE_TARGET_MAX:
            return
        if not _is_userland_pointer(address) or _address_in_ranges(address, local_ranges):
            return
        try:
            if query is None:
                query = _query_candidate(br, address)
            if not query or not (int(query.get("perm", 0) or 0) & 0x2):
                return
            if initial_raw is None:
                initial_raw, query = _bounded_region_snapshot(br, address, length)
        except Exception:
            return
        seen_addresses.add(address)
        trace_targets.append({
            "id": f"ext{len(trace_targets):02d}",
            "address": _hx(address),
            "address_int": int(address),
            "length": len(initial_raw),
            "via": via,
            "source": source,
            "initial_hex": initial_raw.hex(),
            "initial_sha256_16": hashlib.sha256(initial_raw).hexdigest()[:16],
            "initial_external_pointers": [
                {"offset": f"0x{off:X}", "pointer": _hx(ptr)}
                for off, ptr in _extract_userland_ptrs(initial_raw, max_items=8, exclude_ranges=local_ranges)
            ],
            "initial_heap_pointers": [
                {"offset": f"0x{off:X}", "pointer": _hx(ptr)}
                for off, ptr in _extract_userland_ptrs(initial_raw, max_items=8, exclude_ranges=local_ranges)
            ],
            "initial_float_words": _float_words(initial_raw, max_items=24),
        })

    for root_index, row in enumerate(roots):
        if len(trace_targets) >= EXTERNAL_TRACE_TARGET_MAX:
            break
        address = int(row["address_int"])
        try:
            root_raw = bytes.fromhex(str(row.get("snapshot_hex") or ""))
        except Exception:
            root_raw = b""
        add_target(
            address=address,
            via="/".join(row.get("categories") or ["external_root"]),
            source=f"root#{root_index}",
            length=EXTERNAL_ROOT_SNAPSHOT,
            initial_raw=root_raw if root_raw else None,
            query=row.get("query"),
        )
        children_added = 0
        for ptr_row in row.get("external_pointers") or []:
            if children_added >= EXTERNAL_CHILDREN_PER_ROOT or len(trace_targets) >= EXTERNAL_TRACE_TARGET_MAX:
                break
            child = _as_int(ptr_row.get("pointer"))
            if not child or child == address:
                continue
            before = len(trace_targets)
            add_target(
                address=child,
                via=f"{_hx(address)}+{ptr_row.get('offset')}",
                source=f"root#{root_index}:child",
                length=EXTERNAL_CHILD_SNAPSHOT,
            )
            if len(trace_targets) > before:
                children_added += 1

    direct_count = sum(1 for row in roots if "direct_code_writable" in (row.get("categories") or []))
    local_escape_count = sum(1 for row in roots if "module_local_escape" in (row.get("categories") or []))
    bridge_escape_count = sum(1 for row in roots if "read_only_bridge_escape" in (row.get("categories") or []))
    return {
        "status": "FOUND_ESCAPED_EXTERNAL_REFS" if trace_targets else "NO_ESCAPED_EXTERNAL_REFS",
        "policy": "CJ: query-safe relocated type-0/type-1 scan; aligned 0x00100000-0x13FFFFFF pointers; exact SkyTrip runtime segments excluded; <=24 direct roots with capacity reserved for read-only/module-local escapes; <=32 total roots; <=16 traced roots/children; no heap sweep",
        "module_base": _hx(base),
        "runtime_header": header,
        "runtime_segments": runtime_segments,
        "local_runtime_ranges": local_ranges,
        "code_address": _hx(nominal_code_address),
        "nominal_code_size": nominal_code_size,
        "code_size": total_scanned,
        "code_sha256": code_hasher.hexdigest(),
        "scanned_segments": scanned_segments,
        "scan_errors": scan_errors,
        "aligned_userland_literals": len(literal_locations),
        # Compatibility key retained for support parsers from CI.
        "aligned_writable_range_literals": len(literal_locations),
        "unaligned_userland_literals_filtered": unaligned_userland_literals,
        "internal_runtime_literal_values_filtered": len(internal_literal_values),
        "internal_runtime_literal_occurrences_filtered": sum(internal_literal_values.values()),
        "raw_candidates_considered": len(raw_candidates),
        "direct_external_writable_roots": direct_count,
        "module_local_escaped_roots": local_escape_count,
        "read_only_bridge_escaped_roots": bridge_escape_count,
        "read_only_bridges": read_only_bridges,
        "local_segment_scans": local_segment_scans,
        "local_escaped_candidates": local_escaped_candidates,
        "writable_roots": roots,
        "trace_targets": trace_targets,
        "query_errors": query_errors,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "bounded": True,
    }



def _fast_segment_layout(runtime_segments: list[dict[str, Any]]) -> list[dict[str, int]]:
    rows: list[dict[str, int]] = []
    for row in runtime_segments or []:
        if not isinstance(row, dict) or row.get("error"):
            continue
        kind = int(row.get("kind", -1))
        if kind not in (0, 1):
            continue
        address = _as_int(row.get("runtime_address"))
        size = int(row.get("size", 0) or 0)
        if address and size > 0:
            rows.append({"index": int(row.get("index", -1)), "kind": kind, "size": size})
    rows.sort(key=lambda x: (x["index"], x["kind"]))
    return rows


def _fast_plan_valid(plan: dict | None, runtime_segments: list[dict[str, Any]]) -> bool:
    if not isinstance(plan, dict):
        return False
    if int(plan.get("version", 0) or 0) != FAST_PLAN_VERSION:
        return False
    if plan.get("module") != SKYTRIP_MODULE or int(plan.get("file_size", 0) or 0) != SKYTRIP_FILE_SIZE:
        return False
    if plan.get("segment_layout") != _fast_segment_layout(runtime_segments):
        return False
    slots = plan.get("literal_slots")
    return isinstance(slots, list) and bool(slots)


def _find_blob_for_address(segment_blobs: list[tuple[dict[str, Any], bytes]], address: int) -> tuple[dict[str, Any], bytes, int] | None:
    for meta, raw in segment_blobs:
        start = int(meta.get("address_int", 0) or 0)
        if start <= int(address) and int(address) + 4 <= start + len(raw):
            return meta, raw, int(address) - start
    return None


def _arm_ldr_literal_slots(segment_blobs: list[tuple[dict[str, Any], bytes]]) -> list[dict[str, Any]]:
    """Extract ARM-state PC-relative LDR literal slots from live type-0 text.

    This is intentionally a tiny static decoder, not a general disassembler.
    It recognizes the common ARM `LDR Rt,[PC,#(+/-)imm12]` form used to reach
    literal pools.  That cuts the candidate set from every aligned word in the
    CRO to only values the executable actually loads through PC-relative slots.
    """
    out: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for meta, raw in segment_blobs:
        if int(meta.get("kind", -1)) != 0:
            continue
        base = int(meta.get("address_int", 0) or 0)
        seg_index = int(meta.get("index", -1))
        for off in range(0, max(0, len(raw) - 3), 4):
            insn = struct.unpack_from("<I", raw, off)[0]
            # ARM single-data-transfer, immediate, pre-indexed, word load, Rn=PC.
            if (insn & 0x0F5F0000) != 0x051F0000:
                continue
            imm12 = insn & 0xFFF
            up = bool(insn & (1 << 23))
            pc = base + off + 8
            literal_address = pc + imm12 if up else pc - imm12
            located = _find_blob_for_address(segment_blobs, literal_address)
            if not located:
                continue
            lit_meta, _lit_raw, lit_off = located
            key = (int(lit_meta.get("index", -1)), int(lit_off))
            if key in seen:
                # Keep the plan compact; one literal slot can be used by many instructions.
                continue
            seen.add(key)
            out.append({
                "segment_index": int(lit_meta.get("index", -1)),
                "segment_kind": int(lit_meta.get("kind", -1)),
                "offset": int(lit_off),
                "instruction_segment_index": seg_index,
                "instruction_offset": int(off),
                "rt": int((insn >> 12) & 0xF),
            })
            if len(out) >= FAST_ARM_LITERAL_MAX:
                return out
    return out


def _legacy_pointer_slots_from_blobs(segment_blobs: list[tuple[dict[str, Any], bytes]]) -> list[dict[str, Any]]:
    """Fallback only when ARM literal decoding yields too little evidence."""
    slots: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for meta, raw in segment_blobs:
        for off in range(0, max(0, len(raw) - 3), 4):
            value = struct.unpack_from("<I", raw, off)[0]
            if not _is_userland_pointer(value):
                continue
            key = (int(meta.get("index", -1)), int(off))
            if key in seen:
                continue
            seen.add(key)
            slots.append({
                "segment_index": key[0],
                "segment_kind": int(meta.get("kind", -1)),
                "offset": key[1],
                "instruction_segment_index": -1,
                "instruction_offset": -1,
                "rt": -1,
                "fallback": True,
            })
            if len(slots) >= FAST_ARM_LITERAL_MAX:
                return slots
    return slots


def _read_fast_literal_slots(
    br,
    runtime_segments: list[dict[str, Any]],
    slots: list[dict[str, Any]],
    segment_blobs: list[tuple[dict[str, Any], bytes]] | None = None,
) -> list[dict[str, Any]]:
    """Resolve cached literal slots with <=0x200 grouped reads per segment bucket."""
    seg_by_index: dict[int, dict[str, Any]] = {}
    for row in runtime_segments or []:
        if isinstance(row, dict) and not row.get("error"):
            seg_by_index[int(row.get("index", -1))] = row

    # First-run path already has the full segment bytes in memory.
    if segment_blobs:
        blob_by_index = {int(meta.get("index", -1)): (meta, raw) for meta, raw in segment_blobs}
        rows = []
        for slot in slots:
            idx = int(slot.get("segment_index", -1))
            off = int(slot.get("offset", -1))
            pair = blob_by_index.get(idx)
            if not pair or off < 0 or off + 4 > len(pair[1]):
                continue
            value = struct.unpack_from("<I", pair[1], off)[0]
            seg = seg_by_index.get(idx) or {}
            address = _as_int(seg.get("runtime_address")) + off
            rows.append({**slot, "slot_address": int(address), "value": int(value)})
        return rows

    grouped: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for slot in slots:
        idx = int(slot.get("segment_index", -1))
        off = int(slot.get("offset", -1))
        seg = seg_by_index.get(idx)
        if not seg or off < 0 or off + 4 > int(seg.get("size", 0) or 0):
            continue
        bucket = off // READ_CHUNK
        grouped.setdefault((idx, bucket), []).append(slot)

    rows: list[dict[str, Any]] = []
    for (idx, bucket), bucket_slots in sorted(grouped.items()):
        seg = seg_by_index[idx]
        seg_address = _as_int(seg.get("runtime_address"))
        seg_size = int(seg.get("size", 0) or 0)
        start_off = bucket * READ_CHUNK
        length = min(READ_CHUNK, seg_size - start_off)
        if length < 4:
            continue
        try:
            raw = br.read(seg_address + start_off, length)
        except Exception:
            continue
        for slot in bucket_slots:
            rel = int(slot.get("offset", 0)) - start_off
            if rel < 0 or rel + 4 > len(raw):
                continue
            value = struct.unpack_from("<I", raw, rel)[0]
            rows.append({**slot, "slot_address": seg_address + int(slot.get("offset", 0)), "value": int(value)})
    return rows


def discover_skytrip_fast_references(
    br,
    target: dict | None,
    *,
    static_plan: dict | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """CK fast external-state mapper using cached instruction-derived slots.

    On a cache miss the actual runtime text/rodata segments are read once and a
    compact ARM literal plan is created.  On later runs the full ~56 KiB CRO
    scan is skipped: only the cached literal-pool buckets plus tiny mutable
    .data/.bss segments are read.  Exact module-local ranges remain excluded.
    """
    started = time.monotonic()
    base = _as_int((target or {}).get("base_int")) or _as_int((target or {}).get("base"))
    if not base:
        return {"status": "NO_MODULE_BASE", "trace_targets": [], "elapsed_seconds": 0.0}
    header = _runtime_header_layout(br, base)
    if header.get("error"):
        return {"status": "RUNTIME_HEADER_FAILED", "error": header.get("error"), "trace_targets": [], "elapsed_seconds": round(time.monotonic()-started, 3)}
    runtime_segments = _runtime_segment_table(br, base, header)
    local_ranges = _skytrip_runtime_ranges(runtime_segments, target)
    cache_hit = _fast_plan_valid(static_plan, runtime_segments)
    segment_blobs: list[tuple[dict[str, Any], bytes]] = []
    scanned_segments: list[dict[str, Any]] = []
    scan_errors: list[dict[str, Any]] = []

    plan = dict(static_plan) if cache_hit else None
    if not cache_hit:
        scan_defs = []
        for row in runtime_segments:
            if not isinstance(row, dict) or row.get("error") or int(row.get("kind", -1)) not in (0, 1):
                continue
            address = _as_int(row.get("runtime_address"))
            size = int(row.get("size", 0) or 0)
            if address and size > 0:
                scan_defs.append({"index": int(row.get("index", -1)), "kind": int(row.get("kind", -1)), "address": address, "size": size})
        budget = EXTERNAL_CODE_SCAN_MAX
        for row in scan_defs:
            if budget <= 0:
                break
            size = min(int(row["size"]), budget)
            try:
                raw, query_chunks = _read_runtime_scan_segment(br, int(row["address"]), size)
            except Exception as exc:
                scan_errors.append({"index": row["index"], "kind": row["kind"], "error": f"{type(exc).__name__}: {exc}"})
                continue
            meta = {"index": row["index"], "kind": row["kind"], "address": _hx(row["address"]), "address_int": int(row["address"]), "bytes_read": len(raw), "query_chunks": query_chunks, "sha256": hashlib.sha256(raw).hexdigest()}
            scanned_segments.append(meta)
            segment_blobs.append((meta, raw))
            budget -= len(raw)
        slots = _arm_ldr_literal_slots(segment_blobs)
        decoder = "arm_pc_relative_ldr"
        if not slots:
            slots = _legacy_pointer_slots_from_blobs(segment_blobs)
            decoder = "aligned_pointer_fallback"
        plan = {
            "version": FAST_PLAN_VERSION,
            "module": SKYTRIP_MODULE,
            "file_size": SKYTRIP_FILE_SIZE,
            "segment_layout": _fast_segment_layout(runtime_segments),
            "decoder": decoder,
            "literal_slots": slots,
            "created_from_runtime_base": _hx(base),
        }

    slots = list((plan or {}).get("literal_slots") or [])[:FAST_ARM_LITERAL_MAX]
    resolved_slots = _read_fast_literal_slots(br, runtime_segments, slots, segment_blobs=segment_blobs if not cache_hit else None)
    roots: dict[int, dict[str, Any]] = {}
    bridges: list[dict[str, Any]] = []

    def add_root(address: int, category: str, source: dict[str, Any]) -> bool:
        address = int(address)
        if not _is_userland_pointer(address) or _address_in_ranges(address, local_ranges):
            return False
        row = roots.get(address)
        if row is not None:
            row.setdefault("sources", []).append(source)
            if category not in row.setdefault("categories", []):
                row["categories"].append(category)
            return True
        if len(roots) >= FAST_ROOT_MAX:
            return False
        q = _query_candidate(br, address)
        if not q or not (int(q.get("perm", 0) or 0) & 0x2):
            return False
        try:
            raw, q = _bounded_region_snapshot(br, address, FAST_TRACE_SNAPSHOT)
        except Exception:
            return False
        ptrs = _extract_userland_ptrs(raw, max_items=6, exclude_ranges=local_ranges)
        roots[address] = {
            "address": _hx(address), "address_int": address, "categories": [category], "sources": [source],
            "query": q, "snapshot_hex": raw.hex(), "snapshot_length": len(raw),
            "external_pointers": [{"offset": f"0x{o:X}", "pointer": _hx(v)} for o, v in ptrs],
            "float_words": _float_words(raw, max_items=20),
        }
        return True

    # Strongest source first: values actually loaded by PC-relative LDR slots.
    by_value: dict[int, list[dict[str, Any]]] = {}
    for row in resolved_slots:
        value = int(row.get("value", 0) or 0)
        if not _is_userland_pointer(value) or value & 3 or _address_in_ranges(value, local_ranges):
            continue
        by_value.setdefault(value, []).append(row)
    ranked_values = sorted(by_value.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    for value, refs in ranked_values:
        q = _query_candidate(br, value)
        if not q:
            continue
        source = {"via": "arm_literal_slot", "slot_addresses": [_hx(int(r["slot_address"])) for r in refs], "reference_count": len(refs)}
        perm = int(q.get("perm", 0) or 0)
        if perm & 0x2:
            add_root(value, "direct_arm_literal_writable", source)
        elif (perm & 0x1) and len(bridges) < 8:
            try:
                raw, _ = _bounded_region_snapshot(br, value, FAST_TRACE_SNAPSHOT)
            except Exception:
                raw = b""
            escaped = []
            for off, ptr in _extract_userland_ptrs(raw, max_items=6, exclude_ranges=local_ranges):
                if add_root(ptr, "arm_literal_readonly_escape", {"via": "readonly_bridge", "bridge": _hx(value), "offset": f"0x{off:X}"}):
                    escaped.append({"offset": f"0x{off:X}", "pointer": _hx(ptr)})
            if escaped:
                bridges.append({"address": _hx(value), "escaped_pointers": escaped})
        if len(roots) >= FAST_ROOT_MAX:
            break

    # Escaped pointers from tiny module-local mutable segments are cheap and had
    # stronger ownership evidence than raw literals in CJ, so retain them.
    local_segment_scans = []
    for local in local_ranges:
        if int(local.get("kind", -1)) not in (2, 3):
            continue
        address = int(local["start_int"])
        size = min(int(local.get("size", 0) or 0), EXTERNAL_LOCAL_SEGMENT_SCAN_MAX)
        if size < 4:
            continue
        try:
            raw = _read_bounded(br, address, size)
        except Exception as exc:
            local_segment_scans.append({"address": _hx(address), "error": f"{type(exc).__name__}: {exc}"})
            continue
        escaped = []
        for off, ptr in _extract_userland_ptrs(raw, max_items=24, exclude_ranges=local_ranges):
            if add_root(ptr, "module_local_escape", {"via": "module_local_escape", "slot_address": _hx(address+off), "offset": f"0x{off:X}"}):
                escaped.append({"offset": f"0x{off:X}", "pointer": _hx(ptr)})
        local_segment_scans.append({"address": _hx(address), "kind": local.get("kind"), "size": len(raw), "escaped_pointers": escaped})

    root_rows = list(roots.values())
    for row in root_rows:
        cats = row.get("categories") or []
        row["score"] = (40 if "module_local_escape" in cats else 0) + (28 if "direct_arm_literal_writable" in cats else 0) + (20 if "arm_literal_readonly_escape" in cats else 0) + min(len(row.get("float_words") or []), 12) + len(row.get("external_pointers") or []) * 4
    root_rows.sort(key=lambda r: (-int(r.get("score", 0)), int(r.get("address_int", 0))))

    trace_targets = []
    seen: set[int] = set()
    def add_target(address: int, row: dict[str, Any], via: str, source: str):
        if len(trace_targets) >= FAST_TRACE_TARGET_MAX or address in seen:
            return
        if _address_in_ranges(address, local_ranges):
            return
        q = row.get("query") if row else None
        raw = b""
        try:
            if row and row.get("snapshot_hex"):
                raw = bytes.fromhex(row["snapshot_hex"])
            if not raw:
                raw, q = _bounded_region_snapshot(br, address, FAST_TRACE_SNAPSHOT)
        except Exception:
            return
        seen.add(address)
        trace_targets.append({"id": f"fast{len(trace_targets):02d}", "address": _hx(address), "address_int": address, "length": len(raw), "via": via, "source": source, "query": q, "initial_hex": raw.hex()})

    for idx, row in enumerate(root_rows):
        if len(trace_targets) >= FAST_TRACE_TARGET_MAX:
            break
        address = int(row["address_int"])
        add_target(address, row, "/".join(row.get("categories") or ["root"]), f"root#{idx}")
        # One child only if it is itself writable and external.
        for ptr in (row.get("external_pointers") or [])[:1]:
            child = _as_int(ptr.get("pointer"))
            if child and child not in seen:
                q = _query_candidate(br, child)
                if q and (int(q.get("perm", 0) or 0) & 0x2):
                    add_target(child, {"query": q}, f"{_hx(address)}+{ptr.get('offset')}", f"root#{idx}:child")
            break

    if progress:
        progress({"cache_hit": cache_hit, "literal_slots": len(slots), "resolved_slots": len(resolved_slots), "roots": len(root_rows), "trace_targets": len(trace_targets)})
    return {
        "status": "FOUND_FAST_EXTERNAL_REFS" if trace_targets else "NO_FAST_EXTERNAL_REFS",
        "policy": "CK cached ARM PC-relative literal plan; exact module ranges excluded; <=16 roots; <=8 trace targets; mutable local escapes retained; no heap sweep",
        "module_base": _hx(base), "runtime_header": header, "runtime_segments": runtime_segments, "local_runtime_ranges": local_ranges,
        "cache_status": "HIT" if cache_hit else "MISS_BUILT",
        "static_reference_plan": plan,
        "literal_decoder": (plan or {}).get("decoder"),
        "literal_slot_count": len(slots), "resolved_literal_slot_count": len(resolved_slots),
        "scanned_segments": scanned_segments, "scan_errors": scan_errors,
        "read_only_bridges": bridges, "local_segment_scans": local_segment_scans,
        "writable_roots": root_rows, "trace_targets": trace_targets,
        "elapsed_seconds": round(time.monotonic() - started, 3), "bounded": True,
    }


def build_fast_trace_batches(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group nearby fixed targets into <=0x200 reads without crossing QUERY regions."""
    items = []
    for target in list(targets or [])[:FAST_TRACE_TARGET_MAX]:
        address = _as_int(target.get("address_int")) or _as_int(target.get("address"))
        length = max(4, min(READ_CHUNK, int(target.get("length") or FAST_TRACE_SNAPSHOT)))
        q = target.get("query") or {}
        qbase, qsize = int(q.get("base", 0) or 0), int(q.get("size", 0) or 0)
        if not address or qsize <= 0 or not (qbase <= address and address + length <= qbase + qsize):
            # Unknown region -> leave as its own safe read.
            qbase, qsize = address, length
        items.append({"target": target, "address": address, "length": length, "region": (qbase, qsize)})
    items.sort(key=lambda x: (x["region"][0], x["address"]))
    batches = []
    for item in items:
        if batches:
            b = batches[-1]
            new_end = max(int(b["end"]), item["address"] + item["length"])
            if item["region"] == b["region"] and new_end - int(b["start"]) <= READ_CHUNK:
                b["end"] = new_end
                b["items"].append(item)
                continue
        batches.append({"region": item["region"], "start": item["address"], "end": item["address"] + item["length"], "items": [item]})
    for b in batches:
        b["length"] = int(b["end"]) - int(b["start"])
    return batches


def read_external_trace_targets_batched(br, targets: list[dict[str, Any]], batches: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Read CK targets with one bridge READ per nearby batch."""
    batches = batches or build_fast_trace_batches(targets)
    rows_by_id: dict[str, dict[str, Any]] = {}
    for batch in batches:
        start, length = int(batch["start"]), int(batch["length"])
        try:
            blob = br.read(start, length)
            error = None
        except Exception as exc:
            blob = b""
            error = f"{type(exc).__name__}: {exc}"
        for item in batch["items"]:
            target = item["target"]
            rid = str(target.get("id") or "")
            row = {"id": rid, "address": _hx(item["address"]), "length": item["length"], "via": target.get("via"), "source": target.get("source")}
            if error:
                row["error"] = error
            else:
                off = item["address"] - start
                raw = blob[off:off + item["length"]]
                row["hex"] = raw.hex()
                row["sha256_16"] = hashlib.sha256(raw).hexdigest()[:16]
            rows_by_id[rid] = row
    return [rows_by_id[str(t.get("id") or "")] for t in list(targets or [])[:FAST_TRACE_TARGET_MAX] if str(t.get("id") or "") in rows_by_id]


def update_fast_motion_scores(state: dict | None, delta: dict | None, elapsed_s: float) -> dict[str, Any]:
    """Rank repeatedly changing external fields and lock strong motion candidates.

    Physical Circle Pad input is not exposed by the current bridge, so CK does
    not claim direction labels.  It instead rejects one-shot initializers by
    requiring repeated, time-spanning changes across multiple offsets, with a
    bonus for plausible changing floats and adjacent float/vector fields.
    """
    state = state if isinstance(state, dict) else {}
    targets_state = state.setdefault("targets", {})
    for target in (delta or {}).get("targets") or []:
        rid = str(target.get("id") or "")
        if not rid:
            continue
        ts = targets_state.setdefault(rid, {"events": 0, "words": 0, "offsets": {}, "float_offsets": {}, "first_s": float(elapsed_s), "last_s": float(elapsed_s), "address": target.get("address"), "via": target.get("via")})
        ts["events"] += 1
        ts["last_s"] = float(elapsed_s)
        changes = target.get("word_changes") or []
        ts["words"] += len(changes)
        for change in changes:
            off = str(change.get("offset") or "")
            ts["offsets"][off] = int(ts["offsets"].get(off, 0)) + 1
            if "old_f32" in change and "new_f32" in change:
                try:
                    old_f, new_f = float(change["old_f32"]), float(change["new_f32"])
                    if math.isfinite(old_f) and math.isfinite(new_f) and abs(new_f - old_f) > 1e-6:
                        ts["float_offsets"][off] = int(ts["float_offsets"].get(off, 0)) + 1
                except Exception:
                    pass

    ranking = []
    for rid, ts in targets_state.items():
        dynamic = [off for off, n in ts.get("offsets", {}).items() if int(n) >= 2]
        float_dynamic = [off for off, n in ts.get("float_offsets", {}).items() if int(n) >= 2]
        float_ints = sorted(_as_int(off) for off in float_dynamic)
        vector_bonus = 0
        for i in range(len(float_ints)):
            near = sum(1 for j in range(i + 1, len(float_ints)) if 0 < float_ints[j] - float_ints[i] <= 0x10)
            if near >= 1:
                vector_bonus = 10
                break
        span = max(0.0, float(ts.get("last_s", 0.0)) - float(ts.get("first_s", 0.0)))
        score = int(ts.get("events", 0))*3 + min(int(ts.get("words", 0)), 24) + len(dynamic)*4 + len(float_dynamic)*6 + vector_bonus
        ranking.append({"id": rid, "address": ts.get("address"), "via": ts.get("via"), "score": score, "events": int(ts.get("events", 0)), "word_changes": int(ts.get("words", 0)), "dynamic_offsets": dynamic, "float_dynamic_offsets": float_dynamic, "span_s": round(span, 3), "vector_bonus": vector_bonus})
    ranking.sort(key=lambda r: (-int(r["score"]), -int(r["events"]), str(r["id"])))
    top = ranking[:4]
    locked = False
    winner = top[0] if top else None
    if winner:
        locked = (
            int(winner["score"]) >= FAST_MOTION_LOCK_SCORE
            and int(winner["events"]) >= FAST_MOTION_MIN_EVENTS
            and float(winner["span_s"]) >= FAST_MOTION_MIN_SPAN_S
            and len(winner["dynamic_offsets"]) >= FAST_MOTION_MIN_DYNAMIC_OFFSETS
        )
    state["ranking"] = ranking
    state["locked"] = bool(locked)
    state["winner"] = winner if locked else None
    return {"locked": bool(locked), "winner": winner if locked else None, "top_candidates": top, "state": state}

def read_external_trace_targets(br, targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Read the fixed CH/CI external-reference target set without dereferencing."""
    rows: list[dict[str, Any]] = []
    for target in list(targets or [])[:EXTERNAL_TRACE_TARGET_MAX]:
        address = _as_int(target.get("address_int")) or _as_int(target.get("address"))
        length = max(4, min(READ_CHUNK, int(target.get("length") or EXTERNAL_ROOT_SNAPSHOT)))
        row = {
            "id": str(target.get("id") or ""),
            "address": _hx(address),
            "length": length,
            "via": target.get("via"),
            "source": target.get("source"),
        }
        try:
            raw = br.read(address, length)
            row["hex"] = raw.hex()
            row["sha256_16"] = hashlib.sha256(raw).hexdigest()[:16]
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
        rows.append(row)
    return rows


def diff_external_trace_state(previous: list[dict[str, Any]] | None, current: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Return aligned-word deltas for the bounded CH/CI external target set."""
    prev_by_id = {str(row.get("id")): row for row in (previous or []) if row.get("id")}
    changes: list[dict[str, Any]] = []
    total_words = 0
    for row in current or []:
        rid = str(row.get("id") or "")
        prev = prev_by_id.get(rid)
        if not prev or row.get("error") or prev.get("error"):
            continue
        word_changes = _word_diffs(
            prev.get("hex"),
            row.get("hex"),
            max_items=max(1, EXTERNAL_STATE_DIFF_MAX_WORDS - total_words),
        )
        if not word_changes:
            continue
        total_words += len(word_changes)
        changes.append({
            "id": rid,
            "address": row.get("address"),
            "via": row.get("via"),
            "source": row.get("source"),
            "word_change_count": len(word_changes),
            "word_changes": word_changes,
        })
        if total_words >= EXTERNAL_STATE_DIFF_MAX_WORDS:
            break
    return {
        "changed": bool(changes),
        "target_change_count": len(changes),
        "word_change_count": total_words,
        "targets": changes,
    }


def runtime_contract() -> dict[str, Any]:
    """Compact offline contract included in support/build diagnostics."""
    return {
        "module": SKYTRIP_MODULE,
        "file_size": _hx(SKYTRIP_FILE_SIZE),
        "bss_size": _hx(SKYTRIP_BSS_SIZE),
        "module_name_size": SKYTRIP_MODULE_NAME_SIZE,
        "data_offset": _hx(SKYTRIP_DATA_OFFSET),
        "data_size": _hx(SKYTRIP_DATA_SIZE),
        "runtime_data_resolution": "relocated CRO segment-table type 2 (.data); type 3 (.bss)",
        "heap_anchor_probe": f"one initial read-only exact-vptr local sweep <=0x{HEAP_ANCHOR_PROBE_BYTES:X} bytes/seed; hardware-empty retries skip it",
        "live_state_trace": "CK fast external-state trace only; hardware-disproven repeated local object/.data/.bss probes removed from hot loop",
        "external_reference_mapper": "CK cached ARM PC-relative literal plan; <=16 roots; <=8 batched trace targets; motion scoring/early trace lock; no heap sweep",
        "fast_static_plan": "persistent module-identity/segment-layout cache; first run builds from runtime text, later runs read literal-pool buckets only",
        "mainproc_vptr_literal_offset": _hx(MAINPROC_VPTR_LITERAL_OFFSET),
        "vptr_offsets": {name: _hx(off) for name, off in SKYTRIP_VPTR_OFFSETS.items()},
        "ram_writes": False,
        "max_read": _hx(READ_CHUNK),
    }


def read_runtime_module_state(br, target: dict | None) -> dict[str, Any]:
    """Capture SkyTrip's relocated mutable .data and .bss during flight.

    CF hardware showed the sole .data heap-looking value points to a runtime
    code/thunk structure, not a normal RTTI object.  CG therefore traces both
    relocated mutable segments directly so hardware deltas can identify live
    Soaring position/camera/phase state without guessing object ownership.
    """
    base = _as_int((target or {}).get("base_int")) or _as_int((target or {}).get("base"))
    if not base:
        return {"error": "NO_MODULE_BASE"}
    header = _runtime_header_layout(br, base)
    segments = _runtime_segment_table(br, base, header)
    data_seg = _runtime_segment(segments, 2)
    bss_seg = _runtime_segment(segments, 3)

    address = _as_int((data_seg or {}).get("runtime_address"))
    if not address:
        address = _resolve_runtime_address(base, _as_int(header.get("data_offset"), SKYTRIP_DATA_OFFSET)) or 0
    length = int((data_seg or {}).get("size", 0) or header.get("data_size", 0) or SKYTRIP_DATA_SIZE)
    if length <= 0 or length > 0x4000:
        length = SKYTRIP_DATA_SIZE
    length = min(length, SKYTRIP_DATA_SIZE)

    try:
        raw = _read_bounded(br, address, length)
    except Exception as exc:
        return {
            "address": _hx(address),
            "length": length,
            "source": "relocated_type2_segment" if data_seg else "header_data_offset_fallback",
            "runtime_header": header,
            "runtime_segments": segments,
            "error": f"{type(exc).__name__}: {exc}",
        }

    heap_ptrs = _extract_heap_ptrs(raw, max_items=24)
    payload: dict[str, Any] = {
        "address": _hx(address),
        "length": length,
        "source": "relocated_type2_segment" if data_seg else "header_data_offset_fallback",
        "runtime_header": header,
        "runtime_segments": segments,
        "hex": raw.hex(),
        "sha256_16": hashlib.sha256(raw).hexdigest()[:16],
        "heap_pointers": [{"offset": f"0x{off:X}", "pointer": _hx(ptr)} for off, ptr in heap_ptrs],
        "nonzero_words": _nonzero_words(raw),
    }

    if bss_seg:
        bss_address = _as_int(bss_seg.get("runtime_address"))
        bss_length = int(bss_seg.get("size", 0) or 0)
        if bss_address and 0 < bss_length <= 0x4000:
            try:
                bss_raw = _read_bounded(br, bss_address, bss_length)
                payload["bss"] = {
                    "address": _hx(bss_address),
                    "length": len(bss_raw),
                    "source": "relocated_type3_segment",
                    "hex": bss_raw.hex(),
                    "sha256_16": hashlib.sha256(bss_raw).hexdigest()[:16],
                    "nonzero_words": _nonzero_words(bss_raw),
                    "float_words": _float_words(bss_raw),
                    "heap_pointers": [
                        {"offset": f"0x{off:X}", "pointer": _hx(ptr)}
                        for off, ptr in _extract_heap_ptrs(bss_raw, max_items=24)
                    ],
                }
            except Exception as exc:
                payload["bss"] = {
                    "address": _hx(bss_address),
                    "length": bss_length,
                    "source": "relocated_type3_segment",
                    "error": f"{type(exc).__name__}: {exc}",
                }

    return payload

