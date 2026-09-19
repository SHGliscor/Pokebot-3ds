from __future__ import annotations

"""Alpha Sapphire live Party Viewer RAM authority (public compatibility mapper).

D20 still anchored discovery to the fixed PK6 block at 0x08CFB26C.  Hardware
proved that block can remain stale while the game is idle, so any mapper that
uses those identities as its search key is capable only of rediscovering stale
copies.

D21 uses the game executable's own C++ class identity instead:

* RTTI name: ``savedata::PokePartySave``
* base-game runtime vtable used by the object: 0x005E068C
* serialized data size returned by the class: 0x61C
* layout: six 0x104 UnionPokemon records + one 32-bit party count

The known save-backed instance is at 0x08C814B8 (data at 0x08C814BC).  That
instance is retained only as diagnostic comparison evidence because hardware
has shown it can lag the actual overworld party.  The idle mapper scans readable
process RAM for *other instances of the PokePartySave class*, validates their
PK6 records without using the stale party as a membership key, and promotes a
live instance.  The promoted object is then polled directly on every idle tick.

No game RAM writes are performed.  If no live object is mapped, the dashboard
shows an unmapped party instead of replaying stale Pokémon.
"""

import hashlib
import json
import struct
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from pokebot.common.bridge import Bridge
from pokebot.common.pk6 import parse_pk6
from pokebot.common.species_names import SPECIES_NAMES
from pokebot.common.evolution_prediction import predict_split_evolution
from .appdata_store import get_profile_paths

# ---------------------------------------------------------------------------
# Reverse-engineered Alpha Sapphire PokePartySave layout.
# ---------------------------------------------------------------------------
POKEPARTY_VPTR = 0x005E068C
POKEPARTY_OBJECT_SIZE = 0x620
POKEPARTY_DATA_OFFSET = 0x04
POKEPARTY_SLOT_STRIDE = 0x104
POKEPARTY_PK6_SIZE = 0xE8
POKEPARTY_SLOTS = 6
POKEPARTY_COUNT_OFFSET = POKEPARTY_DATA_OFFSET + POKEPARTY_SLOTS * POKEPARTY_SLOT_STRIDE  # 0x61C

# Known save-backed instance.  Hardware has shown this instance can be stale
# while idle, so it is never used as display authority in D21.
POKEPARTY_SAVE_OBJECT = 0x08C814B8
POKEPARTY_SAVE_BASE = POKEPARTY_SAVE_OBJECT + POKEPARTY_DATA_OFFSET
POKEPARTY_SAVE_COUNT = POKEPARTY_SAVE_OBJECT + POKEPARTY_COUNT_OFFSET

# Legacy fixed 0x1E4 party detail block.  Kept only for battle compatibility and
# diagnostics; the idle viewer no longer reads it as display authority.
PARTY_AUTH_BASE = 0x08CFB26C
PARTY_AUTH_STRIDE = 0x1E4
PARTY_CORE_SIZE = 0xE8
PARTY_SLOTS = 6

# Battle telemetry retained for horde compatibility.
BATTLE_STATE = 0x081FB478
BATTLE_ACTIVE = 0x00040001
BATTLE_PLAYER_POINTERS = 0x081FB58C
BATTLE_OBJECT_SPECIES_OFF = 0x0C
MAP_MIN = 0x00100000
MAP_MAX = 0x10000000

# Class-instance scan.  Runtime/save objects live in process data/heap, with the
# useful AS objects observed in 0x08xxxxxx.  We scan readable regions in 2 KiB
# slices and prioritize 0x08C00000..0x09000000 where save/field runtime objects
# live, then any remaining readable 0x08xxxxxx pages.
SCAN_START = 0x08000000
SCAN_END = 0x09000000
SCAN_PRIORITY_START = 0x08C00000
SCAN_READ = 0x800
SCAN_BUDGET = 48                    # <= 96 KiB discovery traffic per worker
SCAN_RESTART_S = 1.0
SOURCE_FAILURE_LIMIT = 3
MAX_CANDIDATES = 32

HP_TYPES = (
    "Fighting", "Flying", "Poison", "Ground", "Rock", "Bug", "Ghost", "Steel",
    "Fire", "Water", "Grass", "Electric", "Psychic", "Ice", "Dragon", "Dark",
)

_LOCK = threading.RLock()
_LIVE_STATE: dict[tuple[str, int, int], dict] = {}


def _log(line: str) -> None:
    try:
        log_dir = get_profile_paths().root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "party_refresh.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().isoformat(timespec='milliseconds')} {line}\n")
    except Exception:
        pass


def _source_path() -> Path:
    return get_profile_paths().root / "party_live_pokepartysave_source.json"


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


def _parse_pid(value) -> int:
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


def _parse_ec(value) -> int:
    return _parse_pid(value)


def _hidden_power(ivs):
    hp = int(ivs.get("hp", 0)); atk = int(ivs.get("attack", 0)); de = int(ivs.get("defense", 0))
    spe = int(ivs.get("speed", 0)); spa = int(ivs.get("sp_attack", 0)); spd = int(ivs.get("sp_defense", 0))
    bits = (hp & 1) + 2*(atk & 1) + 4*(de & 1) + 8*(spe & 1) + 16*(spa & 1) + 32*(spd & 1)
    return HP_TYPES[(bits * 15) // 63]


def _empty_payload(slot: int, species: str = "Empty"):
    return {
        "slot": slot, "species": species, "species_id": 0, "nature": "—", "gender": "—",
        "pid": "—", "sv": None, "shiny": False, "ivs": {}, "evs": {}, "moves": [],
        "move_pp": [], "hidden_power": "—", "pokerus": "—", "pokerus_days": 0,
        "pokerus_strain": 0, "ec": "—", "predicted_evolution": None,
        "evolution_prediction": None,
    }


def _unmapped_payload():
    return [_empty_payload(i + 1, "Live party mapping…" if i == 0 else "Empty") for i in range(PARTY_SLOTS)]


def _payload_from_parsed(parsed_party):
    rows = []
    for slot_index in range(PARTY_SLOTS):
        parsed = parsed_party[slot_index] if slot_index < len(parsed_party) else {}
        if parsed.get("valid") and parsed.get("checksum_valid"):
            ivs = parsed.get("ivs") or {}
            evolution = predict_split_evolution(parsed.get("species", 0), parsed.get("ec"))
            rows.append({
                "slot": slot_index + 1,
                "species": parsed.get("species_name", f"Species #{parsed.get('species', 0)}"),
                "species_id": int(parsed.get("species", 0)),
                "nature": parsed.get("nature", "—"),
                "gender": parsed.get("gender", "—"),
                "pid": parsed.get("pid", "—"),
                "ec": parsed.get("ec", "—"),
                "predicted_evolution": evolution.get("line") if evolution else None,
                "evolution_prediction": evolution,
                "sv": int(parsed.get("shiny_xor", 0)),
                "shiny": bool(parsed.get("is_shiny")),
                "ivs": ivs,
                "evs": parsed.get("evs") or {},
                "moves": list(parsed.get("moves") or []),
                "move_pp": list(parsed.get("move_pp") or []),
                "hidden_power": _hidden_power(ivs),
                "pokerus": parsed.get("pokerus_status", "Unknown"),
                "pokerus_days": int(parsed.get("pokerus_days", 0)),
                "pokerus_strain": int(parsed.get("pokerus_strain", 0)),
            })
        else:
            rows.append(_empty_payload(slot_index + 1))
    return rows


def _identity_from_raw(raw: bytes, parsed: dict) -> tuple[int, int, int] | None:
    if len(raw) < 8 or not (parsed.get("valid") and parsed.get("checksum_valid")):
        return None
    species = int(parsed.get("species") or 0)
    if not (1 <= species <= 721):
        return None
    return (struct.unpack_from("<I", raw, 0)[0], _parse_pid(parsed.get("pid")), species)


def _identity_tuple_from_parsed(parsed: dict) -> tuple[int, int, int] | None:
    if not (parsed.get("valid") and parsed.get("checksum_valid")):
        return None
    species = int(parsed.get("species") or 0)
    if not (1 <= species <= 721):
        return None
    return (_parse_ec(parsed.get("ec")), _parse_pid(parsed.get("pid")), species)


def _snapshot_signature(parsed) -> tuple[tuple[int, int, int], ...]:
    out = []
    for mon in parsed:
        ident = _identity_tuple_from_parsed(mon)
        if ident is not None:
            out.append(ident)
    return tuple(out)


def _snapshot_hash(raws, count: int) -> str:
    h = hashlib.sha256()
    h.update(struct.pack("<I", int(count)))
    for raw in list(raws)[:int(count)]:
        h.update(raw)
    return h.hexdigest()


def _game_source_key(game_info) -> str:
    title = str((game_info or {}).get("title_id") or "unknown")
    name = str((game_info or {}).get("process_name") or "unknown")
    return f"{title}:{name}"


def _state_key(host, bridge_port, game_info):
    return (str(host), int(bridge_port), int((game_info or {}).get("pid", 0)))


def _load_source(game_info) -> int | None:
    try:
        data = json.loads(_source_path().read_text(encoding="utf-8"))
        row = (data.get("sources") or {}).get(_game_source_key(game_info))
        addr = _as_int((row or {}).get("object"), 0)
        if addr == POKEPARTY_SAVE_OBJECT:
            return None
        if SCAN_START <= addr < SCAN_END:
            return addr
    except Exception:
        pass
    return None


def _persist_source(game_info, address: int) -> None:
    path = _source_path()
    try:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        sources = data.get("sources")
        if not isinstance(sources, dict):
            sources = {}
        sources[_game_source_key(game_info)] = {
            "object": f"0x{int(address):08X}",
            "kind": "PokePartySave_live_instance",
            "updated": datetime.now().isoformat(timespec="seconds"),
        }
        data = {"version": 2, "sources": sources}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        _log(f"D21 source persist failed: {type(exc).__name__}: {exc}")


def _forget_source(game_info) -> None:
    path = _source_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        sources = data.get("sources") if isinstance(data, dict) else None
        if not isinstance(sources, dict):
            return
        if sources.pop(_game_source_key(game_info), None) is None:
            return
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps({"version": 2, "sources": sources}, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def _new_state(game_info):
    return {
        "source": _load_source(game_info),
        "source_failures": 0,
        "source_announced": False,
        "blocks": None,
        "scan_index": 0,
        "scan_cycles": 0,
        "scan_finished": 0.0,
        "candidates": {},
        "diagnostic": None,
        "battle_proof": None,
        "last_display_hash": None,
        "last_stale_signature": None,
        "search_announced": False,
    }


def _get_state(host, bridge_port, game_info):
    key = _state_key(host, bridge_port, game_info)
    with _LOCK:
        state = _LIVE_STATE.get(key)
        if state is None:
            state = _new_state(game_info)
            _LIVE_STATE[key] = state
            _log(
                "D21 LIVE PARTY state created; fixed 0x08CFB26C and save-backed "
                "0x08C814BC are diagnostic only"
            )
        return key, state


def _read_party_object(bridge, object_address: int) -> dict | None:
    """Validate one PokePartySave object and return its six live slots."""
    object_address = int(object_address)
    try:
        vptr = struct.unpack("<I", bridge.read(object_address, 4))[0]
        if vptr != POKEPARTY_VPTR:
            return None
        count = struct.unpack("<I", bridge.read(object_address + POKEPARTY_COUNT_OFFSET, 4))[0]
        if not (0 <= count <= POKEPARTY_SLOTS):
            return None
        raws = []
        parsed = []
        identities = []
        for slot in range(POKEPARTY_SLOTS):
            raw = bridge.read(
                object_address + POKEPARTY_DATA_OFFSET + slot * POKEPARTY_SLOT_STRIDE,
                POKEPARTY_PK6_SIZE,
            )
            raws.append(raw)
            if slot < count:
                mon = parse_pk6(raw, SPECIES_NAMES)
                ident = _identity_from_raw(raw, mon)
                if ident is None:
                    return None
                parsed.append(mon)
                identities.append(ident)
            else:
                parsed.append({})
        # Duplicate exact identities are not valid party members and normally
        # indicate we landed on non-party data despite the vptr match.
        if len(set(identities)) != len(identities):
            return None
        return {
            "object": object_address,
            "count": int(count),
            "raws": raws,
            "parsed": parsed,
            "identity_order": tuple(identities),
            "hash": _snapshot_hash(raws, count),
            "species": [x[2] for x in identities],
        }
    except Exception:
        return None


def _read_known_stale_snapshot(bridge) -> dict | None:
    return _read_party_object(bridge, POKEPARTY_SAVE_OBJECT)


def _query_scan_blocks(bridge):
    """Enumerate readable RAM, starting where AS save/runtime objects live.

    Starting at 0x08000000 made D20 vulnerable to spending its QUERY budget on
    unmapped gaps before ever reaching 0x08Cxxxxx. D21 walks the priority
    0x08C00000..0x09000000 range first, then lower 0x08xxxxxx RAM.
    """
    blocks = []
    for range_start, range_end in (
        (SCAN_PRIORITY_START, SCAN_END),
        (SCAN_START, SCAN_PRIORITY_START),
    ):
        cursor = range_start
        queries = 0
        while cursor < range_end and queries < 2048:
            queries += 1
            try:
                q = bridge.query(cursor)
            except Exception:
                cursor += 0x10000
                continue
            status = _as_int(q.get("status"), -1)
            base = _as_int(q.get("base"), 0)
            size = _as_int(q.get("size"), 0)
            perm = _as_int(q.get("perm"), 0)
            if status != 0 or size <= 0:
                cursor += 0x10000
                continue
            end = base + size
            if perm & 1:
                lo = max(base, range_start)
                hi = min(end, range_end)
                if hi > lo:
                    pos = lo & ~(SCAN_READ - 1)
                    if pos < lo:
                        pos += SCAN_READ
                    while pos + 4 <= hi:
                        blocks.append(pos)
                        pos += SCAN_READ
            cursor = max(cursor + 0x1000, end)
    blocks = list(dict.fromkeys(blocks))
    _log(f"D21 scan map: {len(blocks)} readable 2KiB blocks")
    return blocks


def _record_candidate(state, snapshot: dict):
    address = int(snapshot["object"])
    now = time.monotonic()
    row = state["candidates"].get(address)
    if row is None:
        row = {
            "address": address,
            "first_seen": now,
            "last_seen": now,
            "last_hash": snapshot["hash"],
            "last_signature": tuple(snapshot["identity_order"]),
            "changes": 0,
            "valid_reads": 1,
            "last_changed": 0.0,
        }
        state["candidates"][address] = row
        _log(
            f"D21 PokePartySave candidate object=0x{address:08X} "
            f"count={snapshot['count']} species={snapshot['species']}"
        )
    else:
        row["last_seen"] = now
        row["valid_reads"] = int(row.get("valid_reads", 0)) + 1
        if row.get("last_hash") != snapshot["hash"]:
            row["changes"] = int(row.get("changes", 0)) + 1
            row["last_changed"] = now
            _log(
                f"D21 candidate changed object=0x{address:08X} "
                f"changes={row['changes']} species={snapshot['species']}"
            )
        row["last_hash"] = snapshot["hash"]
        row["last_signature"] = tuple(snapshot["identity_order"])
    # Bound telemetry if a pathological scan ever finds many objects.
    if len(state["candidates"]) > MAX_CANDIDATES:
        ranked = sorted(
            state["candidates"].items(),
            key=lambda kv: (
                int(kv[1].get("changes", 0)),
                float(kv[1].get("last_seen", 0.0)),
            ),
            reverse=True,
        )[:MAX_CANDIDATES]
        state["candidates"] = dict(ranked)
    return row


def _promote_source(state, game_info, snapshot: dict, reason: str):
    address = int(snapshot["object"])
    if address == POKEPARTY_SAVE_OBJECT:
        return False
    old = state.get("source")
    state["source"] = address
    state["source_failures"] = 0
    state["last_display_hash"] = snapshot["hash"]
    if old != address:
        state["source_announced"] = False
        state["diagnostic"] = (
            f"PARTY LIVE OBJECT MAPPED: PokePartySave @ 0x{address:08X} • idle RAM authority"
        )
        _persist_source(game_info, address)
        _log(
            f"D21 SOURCE PROMOTED object=0x{address:08X} reason={reason} "
            f"count={snapshot['count']} species={snapshot['species']}"
        )
    return True


def _candidate_score(address: int, row: dict, stale_signature, proof_order):
    signature = tuple(row.get("last_signature") or ())
    score = 0
    # A non-save PokePartySave whose contents already disagree with the stale
    # save instance is the strongest immediate signal on the user's current
    # failure mode (old Grovyle vs actual party).
    if stale_signature is not None and signature != tuple(stale_signature):
        score += 1_000_000
    if proof_order is not None and signature == tuple(proof_order):
        score += 2_000_000
    score += int(row.get("changes", 0)) * 100_000
    score += min(int(row.get("valid_reads", 0)), 20) * 100
    # Prefer recently mutated runtime objects over long-lived clones.
    score += int(float(row.get("last_changed", 0.0)) * 10) % 100
    # Never prefer the known save object.
    if int(address) == POKEPARTY_SAVE_OBJECT:
        score -= 10_000_000
    return score


def _choose_candidate(bridge, state, game_info, stale_signature):
    proof_order = state.get("battle_proof")
    ranked = []
    for address, row in list(state.get("candidates", {}).items()):
        if int(address) == POKEPARTY_SAVE_OBJECT:
            continue
        snapshot = _read_party_object(bridge, int(address))
        if snapshot is None:
            continue
        _record_candidate(state, snapshot)
        ranked.append((_candidate_score(address, row, stale_signature, proof_order), snapshot, row))
    if not ranked:
        return None
    ranked.sort(key=lambda x: x[0], reverse=True)
    score, snapshot, row = ranked[0]

    # A candidate that differs from the known stale object, matches battle proof,
    # or has actually mutated while idle is proven enough to promote at once.
    sig = tuple(snapshot["identity_order"])
    if proof_order is not None and sig == tuple(proof_order):
        _promote_source(state, game_info, snapshot, "matches_exact_battle_party_proof")
        return snapshot
    if stale_signature is not None and sig != tuple(stale_signature):
        _promote_source(state, game_info, snapshot, "differs_from_known_stale_save_instance")
        return snapshot
    if int(row.get("changes", 0)) > 0:
        _promote_source(state, game_info, snapshot, "candidate_mutated_while_idle")
        return snapshot

    # If there is only one validated non-save class instance, provisionally use
    # it after two valid reads.  The mapper keeps monitoring all candidates and
    # will switch if another instance proves more live later.
    non_save = [a for a in state.get("candidates", {}) if int(a) != POKEPARTY_SAVE_OBJECT]
    if len(non_save) == 1 and int(row.get("valid_reads", 0)) >= 2:
        _promote_source(state, game_info, snapshot, "sole_valid_non_save_PokePartySave_instance")
        return snapshot
    return None


def _scan_for_party_objects(bridge, state, game_info, stale_signature):
    if state.get("blocks") is None:
        state["blocks"] = _query_scan_blocks(bridge)
        state["scan_index"] = 0
    blocks = list(state.get("blocks") or [])
    if not blocks:
        return None
    if int(state.get("scan_index", 0)) >= len(blocks):
        if time.monotonic() - float(state.get("scan_finished", 0.0)) < SCAN_RESTART_S:
            return _choose_candidate(bridge, state, game_info, stale_signature)
        state["scan_index"] = 0
        state["scan_cycles"] = int(state.get("scan_cycles", 0)) + 1

    pattern = struct.pack("<I", POKEPARTY_VPTR)
    end_index = min(len(blocks), int(state["scan_index"]) + SCAN_BUDGET)
    while int(state["scan_index"]) < end_index:
        address = blocks[int(state["scan_index"])]
        state["scan_index"] = int(state["scan_index"]) + 1
        try:
            data = bridge.read(address, SCAN_READ)
        except Exception:
            continue
        start = 0
        while True:
            off = data.find(pattern, start)
            if off < 0:
                break
            start = off + 1
            object_address = address + off
            if object_address & 3:
                continue
            snapshot = _read_party_object(bridge, object_address)
            if snapshot is None:
                continue
            _record_candidate(state, snapshot)

    chosen = _choose_candidate(bridge, state, game_info, stale_signature)
    if chosen is not None:
        return chosen

    if int(state["scan_index"]) >= len(blocks):
        state["scan_finished"] = time.monotonic()
        _log(
            f"D21 mapper cycle complete cycle={state.get('scan_cycles', 0)} "
            f"candidates={[f'0x{x:08X}' for x in state.get('candidates', {})]}"
        )
    return None


def _read_current_source(bridge, state, game_info, stale_signature):
    address = state.get("source")
    if not address:
        return None
    snapshot = _read_party_object(bridge, int(address))
    if snapshot is None:
        state["source_failures"] = int(state.get("source_failures", 0)) + 1
        if state["source_failures"] >= SOURCE_FAILURE_LIMIT:
            _log(f"D21 source invalidated object=0x{int(address):08X}")
            state["source"] = None
            state["source_failures"] = 0
            state["source_announced"] = False
            state["blocks"] = None
            state["scan_index"] = 0
            _forget_source(game_info)
        return None
    state["source_failures"] = 0
    row = _record_candidate(state, snapshot)

    # Keep looking at known alternate objects.  If the selected source stays
    # unchanged while another exact PokePartySave mutates or disagrees with the
    # stale save copy, move authority to the better live candidate.
    alternate = _choose_candidate(bridge, state, game_info, stale_signature)
    selected = state.get("source")
    if alternate is not None and int(alternate["object"]) == int(selected):
        return alternate
    if selected and int(selected) != int(address):
        switched = _read_party_object(bridge, int(selected))
        if switched is not None:
            return switched
    return snapshot


def remember_visible_order(host, bridge_port, game_info, identity_order, source="BATTLE_PLAYER_POINTERS"):
    """Keep exact battle order only as correlation evidence for the D21 mapper."""
    order = tuple((int(a), int(b), int(c)) for a, b, c in identity_order)
    _key, state = _get_state(host, bridge_port, game_info)
    with _LOCK:
        state["battle_proof"] = order
        _log(f"D21 battle proof source={source} species={[x[2] for x in order]}")
    return {"order": order, "source": source, "persisted": False}


def _legacy_authoritative_party(bridge):
    raws, parsed, identities = [], [], []
    for slot in range(PARTY_SLOTS):
        raw = bridge.read(PARTY_AUTH_BASE + slot * PARTY_AUTH_STRIDE, PARTY_CORE_SIZE)
        mon = parse_pk6(raw, SPECIES_NAMES)
        raws.append(raw); parsed.append(mon)
        ident = _identity_from_raw(raw, mon)
        if ident is not None:
            identities.append({"auth_index": slot, "ec": ident[0], "pid": ident[1], "species": ident[2]})
    return raws, parsed, identities


def get_live_party_snapshot(host, bridge_port, game_info, bridge, *, force_save_refresh=False):
    """Return the current party from a live PokePartySave class instance.

    The two historically stale fixed blocks are never emitted as an idle display
    fallback.  ``reorder_proven`` therefore also means *membership/detail source
    proven*, not merely slot permutation proven.
    """
    cache_key, state = _get_state(host, bridge_port, game_info)
    stale = _read_known_stale_snapshot(bridge)
    stale_signature = tuple(stale["identity_order"]) if stale is not None else None
    state["last_stale_signature"] = stale_signature

    snapshot = _read_current_source(bridge, state, game_info, stale_signature)

    scan_bridge = None
    if snapshot is None or state.get("source") is None:
        # Discovery uses a short timeout so mapper packet loss cannot freeze UI.
        scan_bridge = Bridge(host=str(host), port=int(bridge_port), timeout=0.20)
        snapshot = _scan_for_party_objects(scan_bridge, state, game_info, stale_signature)

    # Even after mapping, periodically advance discovery enough to notice a
    # reallocated/newer runtime instance.  This is bounded and class-signature
    # based, not a PK6-content search.
    if snapshot is not None and state.get("source") is not None:
        if state.get("blocks") is None or (
            time.monotonic() - float(state.get("scan_finished", 0.0)) >= SCAN_RESTART_S
            and int(state.get("scan_index", 0)) >= len(state.get("blocks") or [])
        ):
            scan_bridge = scan_bridge or Bridge(host=str(host), port=int(bridge_port), timeout=0.20)
            _scan_for_party_objects(scan_bridge, state, game_info, stale_signature)
            current = state.get("source")
            if current:
                newer = _read_party_object(bridge, int(current))
                if newer is not None:
                    snapshot = newer

    if snapshot is None or state.get("source") is None:
        if not state.get("search_announced"):
            state["search_announced"] = True
            state["diagnostic"] = (
                "PARTY LIVE MAPPER D21: locating active PokePartySave object • stale party hidden"
            )
        return {
            "payload": _unmapped_payload(),
            "parsed": [{} for _ in range(PARTY_SLOTS)],
            "ordered_parsed": [{} for _ in range(PARTY_SLOTS)],
            "identities": [],
            "identity_order": tuple(),
            "working_identity_order": stale_signature or tuple(),
            "save_identity_order": stale_signature,
            "source": "UNMAPPED_LIVE_POKEPARTYSAVE",
            "reorder_proven": False,
            "battle_active": False,
            "cache_key": cache_key,
            "live_source": None,
            "mapper": {
                "scan_index": int(state.get("scan_index", 0)),
                "scan_blocks": len(state.get("blocks") or []),
                "candidates": [f"0x{x:08X}" for x in state.get("candidates", {})],
                "scan_cycles": int(state.get("scan_cycles", 0)),
            },
        }

    source_address = int(snapshot["object"])
    payload = _payload_from_parsed(snapshot["parsed"])
    source_name = "LIVE_POKEPARTYSAVE_OBJECT"
    _log(
        f"D21 DISPLAY object=0x{source_address:08X} count={snapshot['count']} "
        f"species={snapshot['species']} stale_species={list(stale['species']) if stale else None}"
    )
    return {
        "payload": payload,
        "parsed": list(snapshot["parsed"]),
        "ordered_parsed": list(snapshot["parsed"]),
        "identities": [
            {"auth_index": i, "ec": a, "pid": b, "species": c}
            for i, (a, b, c) in enumerate(snapshot["identity_order"])
        ],
        "identity_order": tuple(snapshot["identity_order"]),
        "working_identity_order": tuple(snapshot["identity_order"]),
        "save_identity_order": stale_signature,
        "source": source_name,
        "reorder_proven": True,
        "battle_active": False,
        "cache_key": cache_key,
        "live_source": {
            "kind": "PokePartySave_live_instance",
            "object": source_address,
            "base": source_address + POKEPARTY_DATA_OFFSET,
            "stride": POKEPARTY_SLOT_STRIDE,
            "count_offset": POKEPARTY_COUNT_OFFSET,
        },
        "mapper": {
            "scan_index": int(state.get("scan_index", 0)),
            "scan_blocks": len(state.get("blocks") or []),
            "candidates": [f"0x{x:08X}" for x in state.get("candidates", {})],
            "scan_cycles": int(state.get("scan_cycles", 0)),
        },
    }


def build_live_party_payload(host, bridge_port, game_info, authoritative_bridge):
    snap = get_live_party_snapshot(host, bridge_port, game_info, authoritative_bridge)
    state = _LIVE_STATE.get(tuple(snap.get("cache_key") or ()))
    diagnostic = None
    if state is not None:
        with _LOCK:
            diagnostic = state.pop("diagnostic", None)
    if snap.get("reorder_proven") and state is not None and not state.get("source_announced"):
        state["source_announced"] = True
        live = snap.get("live_source") or {}
        diagnostic = diagnostic or (
            f"PARTY LIVE RAM D21: PokePartySave 0x{int(live.get('object', 0)):08X} • real-time idle party active"
        )
    return list(snap["payload"]), diagnostic


def build_party_payload(bridge):
    """Legacy API: never replay stale fixed party into the idle dashboard.

    Hunt-side callers that require exact live order use get_live_party_snapshot.
    This function intentionally returns the known save object only when it is
    asked outside the idle mapper for compatibility; ProbeWorker has been moved
    to build_live_party_payload in D21.
    """
    stale = _read_known_stale_snapshot(bridge)
    if stale is None:
        return _unmapped_payload()
    return _payload_from_parsed(stale["parsed"])


def probe_pokeparty_save_payload(bridge):
    """Expose the known save-backed object as diagnostic telemetry only."""
    snap = _read_known_stale_snapshot(bridge)
    if snap is None:
        return None
    return {
        "payload": _payload_from_parsed(snap["parsed"]),
        "identity_order": [
            {"ec": a, "pid": b, "species": c} for a, b, c in snap["identity_order"]
        ],
        "species": list(snap["species"]),
        "count": int(snap["count"]),
        "base": POKEPARTY_SAVE_BASE,
        "object": POKEPARTY_SAVE_OBJECT,
        "vptr": POKEPARTY_VPTR,
        "source": "POKEPARTY_SAVE_STALE_TELEMETRY",
    }


def _ordered_legacy_parsed(parsed, identities, identity_order):
    buckets: dict[tuple[int, int, int], list[dict]] = {}
    for item in identities:
        idx = int(item["auth_index"])
        key = (int(item["ec"]), int(item["pid"]), int(item["species"]))
        buckets.setdefault(key, []).append(parsed[idx])
    out = []
    for ident in identity_order:
        bucket = buckets.get(tuple(ident))
        if not bucket:
            return None
        out.append(bucket.pop(0))
    while len(out) < PARTY_SLOTS:
        out.append({})
    return out[:PARTY_SLOTS]


def probe_battle_party_payload(bridge):
    """Read active player order from the mapped battle pointer table.

    This compatibility probe still uses the fixed battle-synchronized PK6 block;
    it is not used by the idle Party Viewer as membership authority.
    """
    _raws, parsed, identities = _legacy_authoritative_party(bridge)
    if len(identities) < 2:
        return None
    auth_species = [int(x["species"]) for x in identities]
    if len(set(auth_species)) != len(auth_species):
        return None
    try:
        if struct.unpack("<I", bridge.read(BATTLE_STATE, 4))[0] != BATTLE_ACTIVE:
            return None
        ptrs = struct.unpack(
            "<" + "I" * len(identities),
            bridge.read(BATTLE_PLAYER_POINTERS, len(identities) * 4),
        )
        observed = []
        for ptr in ptrs:
            if not (MAP_MIN <= ptr < MAP_MAX):
                return None
            observed.append(struct.unpack("<H", bridge.read(ptr + BATTLE_OBJECT_SPECIES_OFF, 2))[0])
    except Exception:
        return None
    if Counter(observed) != Counter(auth_species):
        return None
    by_species = {int(item["species"]): item for item in identities}
    order = tuple(
        (int(by_species[int(species)]["ec"]), int(by_species[int(species)]["pid"]), int(species))
        for species in observed
    )
    ordered = _ordered_legacy_parsed(parsed, identities, order)
    if ordered is None:
        return None
    return {
        "payload": _payload_from_parsed(ordered),
        "identity_order": [{"ec": a, "pid": b, "species": c} for a, b, c in order],
        "identity_order_tuples": order,
        "species": list(observed),
        "order_indices": [int(by_species[int(s)]["auth_index"]) for s in observed],
        "source": "BATTLE_PLAYER_POINTERS",
    }


def visible_slot_for_identity(snapshot: dict, identity: tuple[int, int, int]) -> int | None:
    order = tuple(snapshot.get("identity_order") or ())
    try:
        return order.index(tuple(identity)) + 1
    except ValueError:
        return None

# ---------------------------------------------------------------------------
# Public runtime-party authority override.
# The legacy D21 PokePartySave mapper remains above only for historical/support
# compatibility.  All get_live_party_snapshot/build_live_party_payload callers
# below use the executable-derived live field pointer chain instead.
# ---------------------------------------------------------------------------
from pokebot.common.live_party import get_runtime_party_snapshot as _d23_runtime_party_snapshot


def get_live_party_snapshot(host, bridge_port, game_info, bridge, *, force_save_refresh=False):
    # game_info/force_save_refresh are retained in the public signature because
    # Horde and wild-worker call sites already pass them.  D25 needs neither:
    # the runtime pointer chain is validated from RAM on every read.
    return _d23_runtime_party_snapshot(host, int(bridge_port), bridge)


def build_live_party_payload(host, bridge_port, game_info, authoritative_bridge):
    snap = _d23_runtime_party_snapshot(host, int(bridge_port), authoritative_bridge)
    return list(snap.get("payload") or []), snap.get("diagnostic")
