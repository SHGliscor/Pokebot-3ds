from __future__ import annotations

"""Shared Alpha Sapphire live-party RAM authority (v0p43BO).

Hardware support capture on 2026-08-28 proved the actual idle-updating chain on
Alpha Sapphire 1.4:

    owner 0x08C6E760 -> +0x14 runtime party 0x08CFB1E0
    runtime party -> six PokemonParam* + count at +0x18
    PokemonParam +0x0C -> CoreParam* -> +0x08 encrypted PK6

v0p43BO validates that known owner first for an immediate low-read startup. If it is
not valid (different session/region/build), the D25 executable-derived 0x200-byte
safe scanner remains as fallback. The stale fixed 0x08CFB26C party and invalid
0x08C814BC copy are diagnostic history only and are never polled or displayed by
the idle Party Viewer.

Read-only: no game RAM writes and no controller input. Every bridge RAM read is
capped at 0x200 bytes.
"""

import os
import struct
import threading
import time
from datetime import datetime
from pathlib import Path

from pokebot.common.pk6 import parse_pk6
from pokebot.common.species_names import SPECIES_NAMES
from pokebot.common.evolution_prediction import predict_split_evolution

# ---------------------------------------------------------------------------
# ORAS v1.4 known structures
# ---------------------------------------------------------------------------
SAVE_BASE = 0x08C6E998
PARTY_SLOTS = 6
PK6_SIZE = 0xE8                                      # 232 bytes
BRIDGE_MAX_READ = 0x200                              # hard firmware contract
KNOWN_RUNTIME_OWNER = 0x08C6E760                     # hardware-proven AS 1.4

# Executable-derived runtime party chain.
OWNER_SAVE_PTR_OFF = 0x00
OWNER_PARTY_PTR_OFF = 0x14
OWNER_MIN_SIZE = 0x18
PARTY_PTRS_OFF = 0x00
PARTY_COUNT_OFF = 0x18
PARTY_OBJECT_SIZE = 0x1C
POKEMON_PARAM_CORE_OFF = 0x0C
CORE_STORED_OFF = 0x08

PTR_MIN = 0x08000000
PTR_MAX = 0x10000000

# D25/BO: 0x200-byte reads only. Priority starts near the save manager and the
# hardware-proven runtime-party neighborhood. The mapper is intentionally slow
# and is only a relocation fallback when the known owner does not validate.
SCAN_WINDOWS = (
    (0x08C6E000, 0x08C76000),  # narrow first pass around save/field manager
    (0x08C78000, 0x08C88000),  # PokePartySave/save runtime neighborhood
    (0x08CF0000, 0x08D10000),  # working-party / nearby heap neighborhood
    (0x08C60000, 0x08C6E000),
    (0x08C88000, 0x08C98000),
    (0x08D10000, 0x08D30000),
)
SCAN_READ = BRIDGE_MAX_READ
SCAN_STEP = SCAN_READ - 0x20                         # 32-byte overlap
SCAN_BUDGET_PER_TICK = 2                             # ~4 scan reads/sec at 500 ms UI tick
SCAN_RETRY_S = 30.0
SOURCE_FAILURE_LIMIT = 3

HP_TYPES = (
    "Fighting", "Flying", "Poison", "Ground", "Rock", "Bug", "Ghost", "Steel",
    "Fire", "Water", "Grass", "Electric", "Psychic", "Ice", "Dragon", "Dark",
)

_LOCK = threading.RLock()
_STATE: dict[tuple[str, int], dict] = {}


def _profile_root() -> Path:
    override = os.environ.get("POKEBOT_APPDATA_ROOT") or os.environ.get("POKEBOT_DATA_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata).expanduser().resolve() / "Pokebot-3DS"
    return Path.home() / ".pokebot-3ds"


def _log(line: str) -> None:
    try:
        path = _profile_root() / "logs" / "party_refresh.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().isoformat(timespec='milliseconds')} {line}\n")
    except Exception:
        pass


def _ptr(value: int) -> bool:
    value = int(value)
    return PTR_MIN <= value < PTR_MAX and (value & 0x3) == 0


def _read_exact(bridge, address: int, length: int) -> bytes:
    """Read an arbitrary range using only <=0x200-byte bridge transactions."""
    address = int(address)
    length = int(length)
    if length < 0:
        raise ValueError("negative RAM read length")
    out = bytearray()
    offset = 0
    while offset < length:
        size = min(BRIDGE_MAX_READ, length - offset)
        out.extend(bridge.read(address + offset, size))
        offset += size
    return bytes(out)


def _parse_int_hex(value) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 16)
        except Exception:
            try:
                return int(value, 0)
            except Exception:
                return 0
    try:
        return int(value or 0)
    except Exception:
        return 0


def _hidden_power(ivs: dict) -> str:
    hp = int(ivs.get("hp", 0)); atk = int(ivs.get("attack", 0)); de = int(ivs.get("defense", 0))
    spe = int(ivs.get("speed", 0)); spa = int(ivs.get("sp_attack", 0)); spd = int(ivs.get("sp_defense", 0))
    bits = (hp & 1) + 2*(atk & 1) + 4*(de & 1) + 8*(spe & 1) + 16*(spa & 1) + 32*(spd & 1)
    return HP_TYPES[(bits * 15) // 63]


def _empty_payload(slot: int, label: str = "Empty") -> dict:
    return {
        "slot": int(slot), "species": label, "species_id": 0,
        "nature": "—", "gender": "—", "pid": "—", "sv": None,
        "shiny": False, "ivs": {}, "evs": {}, "moves": [], "move_pp": [],
        "hidden_power": "—", "pokerus": "—", "pokerus_days": 0,
        "pokerus_strain": 0, "ec": "—",
        "predicted_evolution": None, "evolution_prediction": None,
    }


def unmapped_payload(label: str = "Mapping live party…") -> list[dict]:
    return [_empty_payload(i + 1, label if i == 0 else "Empty") for i in range(PARTY_SLOTS)]


def payload_from_parsed(parsed_party: list[dict]) -> list[dict]:
    out = []
    for i in range(PARTY_SLOTS):
        parsed = parsed_party[i] if i < len(parsed_party) else {}
        if parsed.get("valid") and parsed.get("checksum_valid"):
            ivs = dict(parsed.get("ivs") or {})
            evolution = predict_split_evolution(parsed.get("species", 0), parsed.get("ec"))
            out.append({
                "slot": i + 1,
                "species": parsed.get("species_name", f"Species #{parsed.get('species', 0)}"),
                "species_id": int(parsed.get("species", 0) or 0),
                "nature": parsed.get("nature", "—"),
                "gender": parsed.get("gender", "—"),
                "pid": parsed.get("pid", "—"),
                "ec": parsed.get("ec", "—"),
                "predicted_evolution": evolution.get("line") if evolution else None,
                "evolution_prediction": evolution,
                "sv": int(parsed.get("shiny_xor", 0) or 0),
                "shiny": bool(parsed.get("is_shiny")),
                "ivs": ivs,
                "evs": dict(parsed.get("evs") or {}),
                "moves": list(parsed.get("moves") or []),
                "move_pp": list(parsed.get("move_pp") or []),
                "hidden_power": _hidden_power(ivs),
                "pokerus": parsed.get("pokerus_status", "Unknown"),
                "pokerus_days": int(parsed.get("pokerus_days", 0) or 0),
                "pokerus_strain": int(parsed.get("pokerus_strain", 0) or 0),
            })
        else:
            out.append(_empty_payload(i + 1))
    return out


def _identity(raw: bytes, parsed: dict) -> tuple[int, int, int] | None:
    if len(raw) < 8 or not (parsed.get("valid") and parsed.get("checksum_valid")):
        return None
    species = int(parsed.get("species", 0) or 0)
    if not (1 <= species <= 721):
        return None
    return (struct.unpack_from("<I", raw, 0)[0], _parse_int_hex(parsed.get("pid")), species)


def _scan_blocks() -> list[int]:
    blocks: list[int] = []
    seen = set()
    for start, end in SCAN_WINDOWS:
        pos = int(start)
        while pos < int(end):
            if pos not in seen:
                seen.add(pos); blocks.append(pos)
            pos += SCAN_STEP
    return blocks


def _new_state() -> dict:
    return {
        "owner": None,
        "party": None,
        "blob_by_mon": {},
        "scan_blocks": _scan_blocks(),
        "scan_index": 0,
        "scan_finished_at": 0.0,
        "source_failures": 0,
        "mapped_announced": False,
        "last_signature": None,
        "last_count": None,
        "known_owner_checked": False,
        "unmapped_announced": False,
    }


def _get_state(host: str, port: int) -> dict:
    key = (str(host), int(port))
    with _LOCK:
        state = _STATE.get(key)
        if state is None:
            state = _new_state()
            _STATE[key] = state
            _log(
                "D25 state created; bridge read ceiling=0x200; validating hardware-proven runtime owner "
                "0x08C6E760 first; safe owner scan retained as fallback"
            )
        return state


def _resolve_blob_ptr(bridge, mon_ptr: int, state: dict) -> int | None:
    mon_ptr = int(mon_ptr)
    cached = int(state.get("blob_by_mon", {}).get(mon_ptr, 0) or 0)
    if _ptr(cached):
        return cached
    if not _ptr(mon_ptr):
        return None
    try:
        mon = _read_exact(bridge, mon_ptr, 0x10)
        core = struct.unpack_from("<I", mon, POKEMON_PARAM_CORE_OFF)[0]
        if not _ptr(core):
            return None
        core_head = _read_exact(bridge, core, 0x0C)
        stored = struct.unpack_from("<I", core_head, CORE_STORED_OFF)[0]
        if not _ptr(stored):
            return None
    except Exception:
        return None
    state.setdefault("blob_by_mon", {})[mon_ptr] = stored
    return stored


def _read_party_from_owner(bridge, owner: int, state: dict) -> dict | None:
    owner = int(owner)
    try:
        owner_head = _read_exact(bridge, owner, OWNER_MIN_SIZE)
        save_ptr = struct.unpack_from("<I", owner_head, OWNER_SAVE_PTR_OFF)[0]
        party_ptr = struct.unpack_from("<I", owner_head, OWNER_PARTY_PTR_OFF)[0]
    except Exception:
        return None
    if save_ptr != SAVE_BASE or not _ptr(party_ptr):
        return None

    try:
        party = _read_exact(bridge, party_ptr, PARTY_OBJECT_SIZE)
    except Exception:
        return None
    mon_ptrs = list(struct.unpack_from("<6I", party, PARTY_PTRS_OFF))
    count = int(party[PARTY_COUNT_OFF])
    if not (1 <= count <= PARTY_SLOTS):
        return None
    if any(not _ptr(p) for p in mon_ptrs[:count]):
        return None

    raws: list[bytes] = []
    parsed: list[dict] = []
    identities: list[tuple[int, int, int]] = []
    for slot in range(PARTY_SLOTS):
        if slot >= count:
            raws.append(b""); parsed.append({}); continue
        mon_ptr = int(mon_ptrs[slot])
        stored = _resolve_blob_ptr(bridge, mon_ptr, state)
        if stored is None:
            return None
        try:
            raw = _read_exact(bridge, stored, PK6_SIZE)
        except Exception:
            return None
        mon = parse_pk6(raw, SPECIES_NAMES)
        ident = _identity(raw, mon)
        if ident is None:
            # Object internals can be replaced in-place; drop cached leaf once.
            state.setdefault("blob_by_mon", {}).pop(mon_ptr, None)
            stored = _resolve_blob_ptr(bridge, mon_ptr, state)
            if stored is None:
                return None
            try:
                raw = _read_exact(bridge, stored, PK6_SIZE)
            except Exception:
                return None
            mon = parse_pk6(raw, SPECIES_NAMES)
            ident = _identity(raw, mon)
            if ident is None:
                return None
        raws.append(raw); parsed.append(mon); identities.append(ident)

    if len(identities) != count or len(set(identities)) != len(identities):
        return None
    return {
        "owner": owner, "party": party_ptr, "count": count,
        "mon_ptrs": tuple(mon_ptrs), "raws": raws, "parsed": parsed,
        "identity_order": tuple(identities), "species": tuple(x[2] for x in identities),
    }


def _candidate_offsets(data: bytes, block_addr: int):
    pattern = struct.pack("<I", SAVE_BASE)
    start = 0
    while True:
        off = data.find(pattern, start)
        if off < 0:
            break
        start = off + 1
        addr = int(block_addr) + off
        if (addr & 3) == 0 and off + OWNER_MIN_SIZE <= len(data):
            yield off, addr


def _scan_for_owner(bridge, state: dict) -> dict | None:
    now = time.monotonic()
    blocks = state["scan_blocks"]
    if state["scan_index"] >= len(blocks):
        if now - float(state.get("scan_finished_at", 0.0)) < SCAN_RETRY_S:
            return None
        state["scan_index"] = 0
        state["scan_finished_at"] = 0.0

    end = min(len(blocks), int(state["scan_index"]) + SCAN_BUDGET_PER_TICK)
    while state["scan_index"] < end:
        address = blocks[state["scan_index"]]
        state["scan_index"] += 1
        try:
            data = _read_exact(bridge, address, SCAN_READ)
        except Exception as exc:
            _log(f"D25 scan read failed 0x{address:08X}+0x{SCAN_READ:X}: {type(exc).__name__}: {exc}")
            continue
        for off, candidate in _candidate_offsets(data, address):
            party_ptr = struct.unpack_from("<I", data, off + OWNER_PARTY_PTR_OFF)[0]
            if not _ptr(party_ptr):
                continue
            snap = _read_party_from_owner(bridge, candidate, state)
            if snap is None:
                continue
            state["owner"] = int(candidate)
            state["party"] = int(snap["party"])
            state["source_failures"] = 0
            state["mapped_announced"] = False
            _log(
                f"D25 RUNTIME SOURCE MAPPED owner=0x{candidate:08X} party=0x{int(snap['party']):08X} "
                f"count={snap['count']} species={list(snap['species'])}"
            )
            return snap

    # Periodic progress breadcrumbs make it obvious that reads are succeeding.
    idx = int(state["scan_index"])
    if idx and (idx % 64) == 0:
        _log(f"D25 owner scan progress blocks={idx}/{len(blocks)} read_size=0x{SCAN_READ:X}")
    if state["scan_index"] >= len(blocks):
        state["scan_finished_at"] = time.monotonic()
        _log(f"D25 owner scan complete miss blocks={len(blocks)}; backing off {SCAN_RETRY_S:.0f}s")
    return None


def _result_from_snapshot(snap: dict, source: str, diagnostic: str | None, *, proven: bool) -> dict:
    parsed = list(snap.get("parsed") or [])
    while len(parsed) < PARTY_SLOTS:
        parsed.append({})
    identities = tuple(snap.get("identity_order") or ())
    return {
        "payload": payload_from_parsed(parsed),
        "parsed": parsed,
        "ordered_parsed": parsed,
        "identity_order": identities,
        "identities": [
            {"auth_index": i, "ec": a, "pid": b, "species": c}
            for i, (a, b, c) in enumerate(identities)
        ],
        "working_identity_order": identities,
        "save_identity_order": None,
        "source": source,
        "reorder_proven": bool(proven),
        "battle_active": False,
        "live_source": {
            "kind": source,
            "base": int(snap.get("party", 0)),
            "count": int(snap.get("count", 0)),
            "owner": int(snap.get("owner", 0)) if snap.get("owner") is not None else None,
        },
        "diagnostic": diagnostic,
    }


def get_runtime_party_snapshot(host: str, port: int, bridge) -> dict:
    state = _get_state(host, port)

    # 1) Reuse a mapped runtime owner with no fixed-copy polling.
    runtime = None
    owner = state.get("owner")
    if owner is not None:
        runtime = _read_party_from_owner(bridge, int(owner), state)
        if runtime is None:
            state["source_failures"] = int(state.get("source_failures", 0)) + 1
            if state["source_failures"] >= SOURCE_FAILURE_LIMIT:
                _log(f"D25 RUNTIME SOURCE INVALIDATED owner=0x{int(owner):08X}")
                state["owner"] = None
                state["party"] = None
                state["blob_by_mon"] = {}
                state["source_failures"] = 0
                state["mapped_announced"] = False
                state["known_owner_checked"] = False
        else:
            state["source_failures"] = 0

    # 2) Hardware-proven fast path. Validation is read-only and uses the exact
    # same structural checks as scanner-discovered owners.
    if runtime is None and state.get("owner") is None and not state.get("known_owner_checked"):
        state["known_owner_checked"] = True
        candidate = _read_party_from_owner(bridge, KNOWN_RUNTIME_OWNER, state)
        if candidate is not None:
            runtime = candidate
            state["owner"] = KNOWN_RUNTIME_OWNER
            state["party"] = int(candidate["party"])
            state["source_failures"] = 0
            state["mapped_announced"] = False
            _log(
                f"D25 KNOWN RUNTIME OWNER VALIDATED owner=0x{KNOWN_RUNTIME_OWNER:08X} "
                f"party=0x{int(candidate['party']):08X} count={candidate['count']} "
                f"species={list(candidate['species'])}"
            )
        else:
            _log("D25 known runtime owner 0x08C6E760 did not validate; starting safe scan fallback")

    # 3) Fallback mapper for a relocated owner. It uses only <=0x200 reads.
    if runtime is None and state.get("owner") is None:
        runtime = _scan_for_owner(bridge, state)

    if runtime is not None:
        signature = tuple(runtime["identity_order"])
        if signature != state.get("last_signature") or int(runtime["count"]) != state.get("last_count"):
            _log(
                f"D25 RUNTIME PARTY CHANGE owner=0x{int(runtime['owner']):08X} "
                f"party=0x{int(runtime['party']):08X} count={runtime['count']} "
                f"species={list(runtime['species'])}"
            )
            state["last_signature"] = signature
            state["last_count"] = int(runtime["count"])
        diag = None
        if not state.get("mapped_announced"):
            state["mapped_announced"] = True
            diag = (
                f"PARTY D25 LIVE: runtime owner 0x{int(runtime['owner']):08X} "
                f"→ party 0x{int(runtime['party']):08X}"
            )
        return _result_from_snapshot(runtime, "RUNTIME_POINTER_CHAIN", diag, proven=True)

    if not state.get("unmapped_announced"):
        state["unmapped_announced"] = True
        _log("D25 runtime party not mapped yet; stale fixed party copies are intentionally hidden")
    return {
        "payload": unmapped_payload("Mapping live runtime party…"),
        "parsed": [{} for _ in range(PARTY_SLOTS)],
        "ordered_parsed": [{} for _ in range(PARTY_SLOTS)],
        "identity_order": tuple(),
        "identities": [],
        "source": "UNMAPPED_D25",
        "reorder_proven": False,
        "live_source": None,
        "diagnostic": (
            f"PARTY D25: runtime owner not mapped; safe scan "
            f"{int(state.get('scan_index', 0))}/{len(state.get('scan_blocks') or [])}"
        ),
        "mapper": {
            "scan_index": int(state.get("scan_index", 0)),
            "scan_blocks": len(state.get("scan_blocks") or []),
        },
    }


def get_runtime_party_snapshot_for_bridge(bridge) -> dict:
    """Return the shared live-party snapshot using bridge identity for cache scoping."""
    host = str(getattr(bridge, "host", None) or getattr(bridge, "ip", None) or "bridge")
    port = int(getattr(bridge, "port", 4952) or 4952)
    return get_runtime_party_snapshot(host, port, bridge)
