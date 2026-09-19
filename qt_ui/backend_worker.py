
from __future__ import annotations

import importlib
import random
import csv
import json
import os
import threading
import struct
import time
from collections import Counter
import traceback
import zipfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QObject, Signal, Slot

from pokebot.common.bridge import Bridge
from pokebot.common.framebuffer import capture_top_screen
from pokebot.common.acknowledged_input import AcknowledgedInput
from pokebot.common.gen6_profiles import profile_from_game_info
from pokebot.common.reset_adapter import run_reset_to_bag_for_profile
from pokebot.common.xy_reset import run_reset_to_field as run_xy_reset_to_field
from .appdata_store import get_profile_paths, increment_species_shiny_total
from pokebot.common.live_party import get_runtime_party_snapshot, payload_from_parsed
from pokebot.common.pk6 import parse_pk6
from pokebot.common.ability_names import ability_name
from pokebot.common.pk6 import NATURE_NAMES
from pokebot.common.species_names import SPECIES_NAMES
from pokebot.common.shiny_odds import (
    detect_oras_shiny_charm, probability_for_rolls, advance_phase_log_miss,
    cumulative_probability_from_log_miss, phase_log_miss_for_constant,
)
from pokebot.common.target_filter import normalize_target, evaluate_target
from pokebot.common.oras_ram import BATTLE_STATE, PARTY_SLOTS, PK6_SIZE, read_party_snapshot
from pokebot.common.gen6_rng_tracker import read_live_locked_shiny_countdown
from pokebot.wild.world_authority import probe_world_position

STARTERS = {
    "treecko": {"name": "Treecko", "species": 252, "ability": "Overgrow", "family": "oras"},
    "torchic": {"name": "Torchic", "species": 255, "ability": "Blaze", "family": "oras"},
    "mudkip": {"name": "Mudkip", "species": 258, "ability": "Torrent", "family": "oras"},
    "chespin": {"name": "Chespin", "species": 650, "ability": "Overgrow", "family": "xy"},
    "fennekin": {"name": "Fennekin", "species": 653, "ability": "Blaze", "family": "xy"},
    "froakie": {"name": "Froakie", "species": 656, "ability": "Torrent", "family": "xy"},
}
VALIDATION = {
    "treecko": "LOCKED 10/10",
    "torchic": "LOCKED 10/10",
    "mudkip": "LOCKED 10/10",
    "chespin": "XY HARDWARE PROVEN • ACK TRIPLE-TOUCH 4952",
    "fennekin": "XY HARDWARE PROVEN • ACK TRIPLE-TOUCH 4952",
    "froakie": "XY HARDWARE PROVEN • ACK TRIPLE-TOUCH 4952",
}

# Full Gen 1-6 species-name authority is imported from
# pokebot.common.species_names for party-card display.
RAW_KEEP_LIMIT = 10

# Legacy D19-D22 party-mapper constants.  They are retained only because older
# support/probe helpers below still reference them.  D25 idle Party Viewer and
# Horde party telemetry do NOT use these fixed/save/mirror blocks; their live
# authority is qt_ui.runtime_party's field runtime pointer chain.
PARTY_AUTH_BASE = 0x08CFB26C
PARTY_AUTH_STRIDE = 0x1E4
PARTY_MIRROR_STRIDE = 0x104
PARTY_LIVE_STRIDES = (0x104, 0x1E4, 0xE8, 0x100, 0x108, 0x1F0, 0x200)
PARTY_BOX_BASE = 0x08C9E134
PARTY_BOX_SPAN = 31 * 30 * 0xE8
PARTY_SCAN_START = PARTY_BOX_BASE - 0x80000
PARTY_SCAN_END = PARTY_BOX_BASE + 0x80000
PARTY_SCAN_READ = 0x200
PARTY_SCAN_BLOCK_BUDGET = 256  # at most 128 KiB per 5 s idle tick
_PARTY_MIRROR_CACHE = {}
_PARTY_MIRROR_LOCK = threading.Lock()

HP_TYPES = (
    "Fighting", "Flying", "Poison", "Ground", "Rock", "Bug", "Ghost", "Steel",
    "Fire", "Water", "Grass", "Electric", "Psychic", "Ice", "Dragon", "Dark",
)

def format_elapsed(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def hidden_power_from_ivs(ivs):
    # PK6 parser dictionary order is hp, attack, defense, speed, sp_attack, sp_defense.
    hp = int(ivs.get("hp", 0))
    atk = int(ivs.get("attack", 0))
    de = int(ivs.get("defense", 0))
    spe = int(ivs.get("speed", 0))
    spa = int(ivs.get("sp_attack", 0))
    spd = int(ivs.get("sp_defense", 0))
    bits = (
        (hp & 1)
        + 2 * (atk & 1)
        + 4 * (de & 1)
        + 8 * (spe & 1)
        + 16 * (spa & 1)
        + 32 * (spd & 1)
    )
    return HP_TYPES[(bits * 15) // 63]



def _checksum_words(data):
    total = 0
    for off in range(0, len(data), 2):
        total = (total + struct.unpack_from("<H", data, off)[0]) & 0xFFFF
    return total


def _parse_decrypted_pk6(raw):
    """Parse the canonical decrypted 232-byte working PK6 representation.

    The volatile ORAS party/menu mirror may already contain the canonical PK6
    body in plaintext. This parser is dashboard telemetry only; encounter and
    shiny authority continue to use parse_pk6() on the proven encrypted block.
    """
    if len(raw) < 232:
        return {"valid": False, "checksum_valid": False, "species": 0}
    sanity = struct.unpack_from("<H", raw, 0x04)[0]
    stored_checksum = struct.unpack_from("<H", raw, 0x06)[0]
    calc_checksum = _checksum_words(raw[0x08:0xE8])
    species = struct.unpack_from("<H", raw, 0x08)[0]
    if not (sanity == 0 and stored_checksum == calc_checksum and 1 <= species <= 721):
        return {
            "valid": False,
            "checksum_valid": stored_checksum == calc_checksum,
            "species": species,
        }

    tid = struct.unpack_from("<H", raw, 0x0C)[0]
    sid = struct.unpack_from("<H", raw, 0x0E)[0]
    pid_int = struct.unpack_from("<I", raw, 0x18)[0]
    nature = raw[0x1C]
    gender_form = raw[0x1D]
    gender_code = (gender_form >> 1) & 0x03
    gender = {0: "♂", 1: "♀", 2: "—"}.get(gender_code, "—")
    pokerus_state = raw[0x2B]
    pokerus_days = pokerus_state & 0x0F
    pokerus_strain = (pokerus_state >> 4) & 0x0F
    if pokerus_strain == 0:
        pokerus_status = "Never infected"
    elif pokerus_days > 0:
        pokerus_status = f"Infected • strain {pokerus_strain} • {pokerus_days} day(s)"
    else:
        pokerus_status = f"Cured • strain {pokerus_strain}"
    shiny_xor = (tid ^ sid ^ (pid_int & 0xFFFF) ^ ((pid_int >> 16) & 0xFFFF)) & 0xFFFF
    iv_word = struct.unpack_from("<I", raw, 0x74)[0]
    return {
        "valid": True,
        "checksum_valid": True,
        "ec": f"0x{struct.unpack_from('<I', raw, 0)[0]:08X}",
        "species": species,
        "species_name": SPECIES_NAMES.get(species, f"Species {species}"),
        "tid": tid,
        "sid": sid,
        "pid": f"0x{pid_int:08X}",
        "shiny_xor": shiny_xor,
        "is_shiny": shiny_xor < 16,
        "nature": NATURE_NAMES[nature] if nature < len(NATURE_NAMES) else f"Nature {nature}",
        "gender": gender,
        "ability_id": raw[0x14],
        "evs": {
            "hp": raw[0x1E], "attack": raw[0x1F], "defense": raw[0x20],
            "speed": raw[0x21], "sp_attack": raw[0x22], "sp_defense": raw[0x23],
        },
        "pokerus_state": pokerus_state,
        "pokerus_days": pokerus_days,
        "pokerus_strain": pokerus_strain,
        "pokerus_status": pokerus_status,
        "ivs": {
            "hp": (iv_word >> 0) & 31,
            "attack": (iv_word >> 5) & 31,
            "defense": (iv_word >> 10) & 31,
            "speed": (iv_word >> 15) & 31,
            "sp_attack": (iv_word >> 20) & 31,
            "sp_defense": (iv_word >> 25) & 31,
        },
    }


def _parse_party_raw(raw, mode="encrypted"):
    if mode == "decrypted":
        return _parse_decrypted_pk6(raw)
    return parse_pk6(raw, SPECIES_NAMES)


def _party_signature(raw_party, mode="encrypted"):
    sig = []
    parsed = []
    for raw in raw_party:
        mon = _parse_party_raw(raw, mode)
        parsed.append(mon)
        if mon.get("valid") and mon.get("checksum_valid"):
            ec = struct.unpack_from("<I", raw, 0)[0]
            sig.append((ec, int(mon.get("species", 0))))
    return Counter(sig), parsed


def _payload_from_parsed_party(parsed_party):
    payload = []
    for slot_index, parsed in enumerate(parsed_party):
        if parsed.get("valid") and parsed.get("checksum_valid"):
            ivs = parsed.get("ivs") or {}
            payload.append({
                "slot": slot_index + 1,
                "species": parsed.get("species_name", f"Species #{parsed.get('species', 0)}"),
                "species_id": int(parsed.get("species", 0)),
                "nature": parsed.get("nature", "—"),
                "gender": parsed.get("gender", "—"),
                "pid": parsed.get("pid", "—"),
                "sv": int(parsed.get("shiny_xor", 0)),
                "shiny": bool(parsed.get("is_shiny")),
                "ivs": ivs,
                "evs": parsed.get("evs") or {},
                "hidden_power": hidden_power_from_ivs(ivs),
                "pokerus": parsed.get("pokerus_status", "Unknown"),
                "pokerus_days": int(parsed.get("pokerus_days", 0)),
                "pokerus_strain": int(parsed.get("pokerus_strain", 0)),
            })
        else:
            payload.append({
                "slot": slot_index + 1, "species": "Empty", "species_id": 0,
                "nature": "—", "gender": "—", "pid": "—", "sv": None,
                "shiny": False, "ivs": {}, "evs": {}, "hidden_power": "—",
                "pokerus": "—", "pokerus_days": 0, "pokerus_strain": 0,
            })
    return payload


def _read_party_at(bridge, base, stride):
    return [bridge.read(base + slot * stride, 232) for slot in range(6)]


def _valid_mirror_candidate(bridge, base, stride, auth_signature):
    stride = int(stride)
    if base < PARTY_SCAN_START or base + 5 * stride + 232 > PARTY_SCAN_END:
        return None
    if base == PARTY_AUTH_BASE and stride == PARTY_AUTH_STRIDE:
        return None
    if base < PARTY_BOX_BASE + PARTY_BOX_SPAN and base + 6 * stride > PARTY_BOX_BASE:
        return None
    try:
        raws = _read_party_at(bridge, base, stride)
    except Exception:
        return None
    for mode in ("decrypted", "encrypted"):
        sig, parsed = _party_signature(raws, mode)
        if sig and sig == auth_signature:
            order = []
            for raw, mon in zip(raws, parsed):
                if mon.get("valid") and mon.get("checksum_valid"):
                    order.append((struct.unpack_from("<I", raw, 0)[0], int(mon.get("species", 0))))
                else:
                    order.append(None)
            return {
                "kind": "pk6_copy", "base": base, "stride": stride,
                "mode": mode, "raws": raws, "parsed": parsed, "order": order,
            }
    return None


def _auth_order(auth_raw, auth_parsed):
    out = []
    for raw, mon in zip(auth_raw, auth_parsed):
        if mon.get("valid") and mon.get("checksum_valid"):
            out.append((struct.unpack_from("<I", raw, 0)[0], int(mon.get("species", 0))))
        else:
            out.append(None)
    return out


def _species_order(parsed):
    return [
        int(mon.get("species", 0)) if mon.get("valid") and mon.get("checksum_valid") else 0
        for mon in parsed
    ]


def _party_refresh_log(line):
    try:
        log_dir = get_profile_paths().root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().isoformat(timespec="seconds")
        with (log_dir / "party_refresh.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {line}\n")
    except Exception:
        pass


def _pointer_order_candidate(bridge, candidate_base, auth_raw, auth_parsed):
    slot_addresses = [PARTY_AUTH_BASE + i * PARTY_AUTH_STRIDE for i in range(6)]
    try:
        raw = bridge.read(candidate_base, 24)
    except Exception:
        return None
    values = list(struct.unpack("<6I", raw))
    if len(set(values)) != 6 or set(values) != set(slot_addresses):
        return None
    indices = [slot_addresses.index(v) for v in values]
    parsed = [auth_parsed[i] for i in indices]
    base_order = _auth_order(auth_raw, auth_parsed)
    order = [base_order[i] for i in indices]
    return {
        "kind": "pointer_order", "base": candidate_base, "stride": 4,
        "mode": "pointer-index", "parsed": parsed, "order_indices": indices,
        "order": order,
    }



def _read_order_indices(bridge, candidate_base, elem_size):
    """Read a six-entry party-order permutation.

    Supports 0-based [0..5] and 1-based [1..6] arrays stored as u8/u16/u32.
    This is dashboard-display telemetry only.
    """
    elem_size = int(elem_size)
    if elem_size not in (1, 2, 4):
        return None
    try:
        raw = bridge.read(int(candidate_base), 6 * elem_size)
    except Exception:
        return None
    if elem_size == 1:
        values = list(raw[:6])
    elif elem_size == 2:
        values = list(struct.unpack("<6H", raw[:12]))
    else:
        values = list(struct.unpack("<6I", raw[:24]))

    if set(values) == set(range(6)):
        return values, "zero-based"
    if set(values) == set(range(1, 7)):
        return [v - 1 for v in values], "one-based"
    return None


def _order_array_candidate(bridge, candidate_base, elem_size, auth_raw, auth_parsed):
    got = _read_order_indices(bridge, candidate_base, elem_size)
    if got is None:
        return None
    indices, encoding = got
    parsed = [auth_parsed[i] for i in indices]
    base_order = _auth_order(auth_raw, auth_parsed)
    order = [base_order[i] for i in indices]
    return {
        "kind": f"order_u{elem_size * 8}",
        "base": int(candidate_base),
        "stride": int(elem_size),
        "mode": encoding,
        "parsed": parsed,
        "order_indices": list(indices),
        "order": order,
    }


def _scan_order_arrays_in_block(data, address):
    """Find extremely-low-collision six-slot permutation arrays in one RAM block."""
    found = []
    specs = (
        (1, "<6B", 1),
        (2, "<6H", 2),
        (4, "<6I", 4),
    )
    for elem_size, fmt, align in specs:
        width = 6 * elem_size
        for off in range(0, len(data) - width + 1, align):
            try:
                values = list(struct.unpack_from(fmt, data, off))
            except struct.error:
                continue
            if set(values) == set(range(6)):
                indices, mode = values, "zero-based"
            elif set(values) == set(range(1, 7)):
                indices, mode = [v - 1 for v in values], "one-based"
            else:
                continue
            found.append({
                "base": int(address + off),
                "elem_size": int(elem_size),
                "indices": list(indices),
                "mode": mode,
            })
    return found


def _order_candidate_cluster_distance(base, state):
    """Distance to a validated PK6 live-copy anchor; None until one exists."""
    anchors = [int(k[0]) for k in state.get("candidates", {}).keys()]
    if not anchors:
        return None
    return min(abs(int(base) - anchor) for anchor in anchors)


def _best_live_order_candidate(scan_bridge, state, auth_raw, auth_parsed):
    """Choose a non-identity order array only when it clusters with a validated live PK6 copy."""
    identity = list(range(6))
    ranked = []
    for (base, elem_size), _meta in list(state.get("order_candidates", {}).items()):
        cand = _order_array_candidate(scan_bridge, base, elem_size, auth_raw, auth_parsed)
        if cand is None:
            state["order_candidates"].pop((base, elem_size), None)
            continue
        indices = list(cand.get("order_indices") or [])
        if indices == identity:
            continue
        distance = _order_candidate_cluster_distance(base, state)
        # Require correlation with a validated live PK6 copy. A six-value
        # permutation elsewhere in the 1 MiB save-buffer scan could belong to
        # an unrelated UI/state table; proximity makes this a party-specific
        # signal rather than a heuristic guess.
        if distance is None or distance > 0x4000:
            continue
        ranked.append((distance, elem_size, int(base), cand))
    if not ranked:
        return None
    ranked.sort(key=lambda x: (x[0], x[1], x[2]))
    return ranked[0][3]



def _order_candidate_debug(state, limit=8):
    rows = []
    anchors = [int(k[0]) for k in state.get("candidates", {}).keys()]
    for (base, elem_size), meta in state.get("order_candidates", {}).items():
        distance = min((abs(int(base) - a) for a in anchors), default=None)
        rows.append((
            0x7FFFFFFF if distance is None else int(distance),
            int(base),
            int(elem_size),
            list(meta.get("indices") or []),
            str(meta.get("mode") or ""),
        ))
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    shown = []
    for distance, base, elem_size, indices, mode in rows[: int(limit)]:
        d = "?" if distance == 0x7FFFFFFF else f"0x{distance:X}"
        shown.append(
            f"0x{base:08X}/u{elem_size*8}/{mode}/{indices}/d={d}"
        )
    return "[" + "; ".join(shown) + "]"


def _scan_block_addresses():
    first = PARTY_SCAN_START & ~(PARTY_SCAN_READ - 1)
    last = (PARTY_SCAN_END + PARTY_SCAN_READ - 1) & ~(PARTY_SCAN_READ - 1)
    blocks = list(range(first, last, PARTY_SCAN_READ))
    # The volatile copy has historically lived in the same save-buffer region as
    # the authoritative party. Search nearest that proven address first so the
    # common case is found in the first few idle ticks rather than after 1 MiB.
    blocks.sort(key=lambda a: abs((a + PARTY_SCAN_READ // 2) - PARTY_AUTH_BASE))
    return blocks


def _get_mirror_state(cache_key, auth_signature):
    with _PARTY_MIRROR_LOCK:
        state = _PARTY_MIRROR_CACHE.get(cache_key)
        sig_key = tuple(sorted(auth_signature.items()))
        if state is None or state.get("auth_signature") != sig_key:
            state = {
                "auth_signature": sig_key,
                "base": None,
                "mode": None,
                "blocks": _scan_block_addresses(),
                "scan_index": 0,
                "hits": {},
                "candidates": {},
                "pointer_candidates": {},
                "order_candidates": {},
                "failed_at": 0.0,
                "announced": False,
                "search_announced": False,
                "miss_announced": False,
            }
            _PARTY_MIRROR_CACHE[cache_key] = state
        return state


def _locate_live_party_mirror(scan_bridge, cache_key, auth_signature, known_ecs, auth_raw, auth_parsed):
    state = _get_mirror_state(cache_key, auth_signature)
    auth_order = _auth_order(auth_raw, auth_parsed)

    # Revalidate all previously discovered candidates on every idle refresh.
    # Prefer a valid source whose order differs from the stale authoritative
    # order; that is exactly the signal manual in-game reordering creates.
    valid_candidates = []
    for (base, stride), _meta in list(state.get("candidates", {}).items()):
        candidate = _valid_mirror_candidate(scan_bridge, base, stride, auth_signature)
        if candidate is None:
            state["candidates"].pop((base, stride), None)
            continue
        valid_candidates.append(candidate)
    for base in list(state.get("pointer_candidates", {})):
        candidate = _pointer_order_candidate(scan_bridge, base, auth_raw, auth_parsed)
        if candidate is None:
            state["pointer_candidates"].pop(base, None)
            continue
        valid_candidates.append(candidate)

    # A manual party reorder can be represented by a separate six-entry order
    # vector while both PK6 arrays remain in physical/save order. Hardware
    # evidence from v0p42V showed exactly that symptom: authoritative and the
    # validated 0x104 copy both stayed [Azelf,Tropius,...] while the game UI
    # showed Tropius first. Prefer a correlated non-identity order vector before
    # falling back to copied-PK6 order.
    order_candidate = _best_live_order_candidate(
        scan_bridge, state, auth_raw, auth_parsed
    )
    if order_candidate is not None:
        order_candidate["cached"] = True
        return order_candidate, state

    differing = [c for c in valid_candidates if c.get("order") != auth_order]
    if differing:
        differing.sort(key=lambda c: (0 if c.get("kind") == "pk6_copy" else 1, abs(int(c.get("base", 0)) - PARTY_AUTH_BASE)))
        differing[0]["cached"] = True
        return differing[0], state

    # After a complete miss, pause before restarting the finite progressive scan.
    if state["scan_index"] >= len(state["blocks"]):
        if valid_candidates:
            valid_candidates.sort(key=lambda c: abs(int(c.get("base", 0)) - PARTY_AUTH_BASE))
            valid_candidates[0]["cached"] = True
            return valid_candidates[0], state
        if time.monotonic() - float(state.get("failed_at", 0.0)) < 30.0:
            return None, state
        state["scan_index"] = 0
        state["hits"] = {}

    patterns = {struct.pack("<I", ec): ec for ec in known_ecs if ec != 0}
    target_count = sum(auth_signature.values())
    slot_addresses = [PARTY_AUTH_BASE + i * PARTY_AUTH_STRIDE for i in range(6)]
    slot_address_set = set(slot_addresses)
    budget_end = min(len(state["blocks"]), state["scan_index"] + PARTY_SCAN_BLOCK_BUDGET)

    while state["scan_index"] < budget_end:
        address = state["blocks"][state["scan_index"]]
        state["scan_index"] += 1
        try:
            data = scan_bridge.read(address, PARTY_SCAN_READ)
        except Exception:
            continue

        # Search for a separate six-slot order vector. A 6-entry permutation
        # of 0..5 (or 1..6), especially as u16/u32, has negligible accidental
        # collision probability. We record all candidates now but only APPLY a
        # non-identity one if it clusters within 0x4000 of a fully validated
        # live PK6 copy.
        for meta in _scan_order_arrays_in_block(data, address):
            key = (int(meta["base"]), int(meta["elem_size"]))
            state["order_candidates"][key] = {
                "mode": meta["mode"],
                "indices": list(meta["indices"]),
            }

        # Cheap pointer-order discovery. If ORAS represents a temporary menu
        # reorder as six pointers back to the fixed party slots, this finds it
        # without assuming a copied-PK6 stride. Exact six-address set matching
        # keeps false positives negligible.
        for off in range(0, len(data) - 3, 4):
            value = struct.unpack_from("<I", data, off)[0]
            if value not in slot_address_set:
                continue
            hit_addr = address + off
            for pos in range(6):
                candidate_base = hit_addr - pos * 4
                if candidate_base in state["pointer_candidates"]:
                    continue
                cand = _pointer_order_candidate(scan_bridge, candidate_base, auth_raw, auth_parsed)
                if cand is not None:
                    state["pointer_candidates"][candidate_base] = True
                    if cand.get("order") != auth_order:
                        return cand, state

        # Locate copies by Encryption Constant, but do not assume one stride.
        new_hits = []
        for pattern, ec in patterns.items():
            start = 0
            while True:
                off = data.find(pattern, start)
                if off < 0:
                    break
                hit = address + off
                start = off + 1
                if (hit & 3) != 0:
                    continue
                if PARTY_AUTH_BASE <= hit < PARTY_AUTH_BASE + 6 * PARTY_AUTH_STRIDE:
                    continue
                if PARTY_BOX_BASE <= hit < PARTY_BOX_BASE + PARTY_BOX_SPAN:
                    continue
                if state["hits"].get(hit) == ec:
                    continue
                state["hits"][hit] = ec
                new_hits.append((hit, ec))

        if target_count >= 2 and new_hits:
            hit_items = list(state["hits"].items())
            strides = set(PARTY_LIVE_STRIDES)
            # Infer additional 4-byte-aligned slot strides directly from EC hit
            # spacing. This covers a live representation we have not pre-guessed.
            for hit, ec in new_hits:
                for other, other_ec in hit_items:
                    if other == hit or other_ec == ec:
                        continue
                    diff = abs(hit - other)
                    for gap in range(1, 6):
                        if diff % gap:
                            continue
                        stride = diff // gap
                        if 0xE0 <= stride <= 0x400 and (stride & 3) == 0:
                            strides.add(stride)

            for hit, _ec in new_hits:
                for stride in strides:
                    for slot_index in range(6):
                        candidate_base = hit - slot_index * stride
                        key = (candidate_base, stride)
                        if key in state["candidates"]:
                            continue
                        # Require all occupied authoritative identities to land
                        # on the candidate lattice before spending six READs.
                        lattice = [state["hits"].get(candidate_base + j * stride) for j in range(6)]
                        if sum(v is not None for v in lattice) < target_count:
                            continue
                        candidate = _valid_mirror_candidate(
                            scan_bridge, candidate_base, stride, auth_signature
                        )
                        if candidate is None:
                            continue
                        state["candidates"][key] = {"mode": candidate["mode"]}
                        correlated_order = _best_live_order_candidate(
                            scan_bridge, state, auth_raw, auth_parsed
                        )
                        if correlated_order is not None:
                            return correlated_order, state
                        if candidate.get("order") != auth_order:
                            return candidate, state

    # A validated PK6 live copy may have been discovered on a previous block,
    # while its adjacent order vector was discovered later in this tick.
    correlated_order = _best_live_order_candidate(
        scan_bridge, state, auth_raw, auth_parsed
    )
    if correlated_order is not None:
        return correlated_order, state

    if state["scan_index"] >= len(state["blocks"]):
        state["failed_at"] = time.monotonic()

    # Use any fully validated copy if one exists even when its order currently
    # matches authoritative. It will be revalidated on the next idle tick and
    # can then expose a later manual reorder immediately.
    valid_candidates = []
    for (base, stride), _meta in state.get("candidates", {}).items():
        c = _valid_mirror_candidate(scan_bridge, base, stride, auth_signature)
        if c is not None:
            valid_candidates.append(c)
    if valid_candidates:
        valid_candidates.sort(key=lambda c: abs(int(c.get("base", 0)) - PARTY_AUTH_BASE))
        return valid_candidates[0], state
    return None, state


def build_live_party_payload(host, bridge_port, game_info, authoritative_bridge):
    """Return (payload, diagnostic) for dashboard-only idle party telemetry."""
    auth_raw = read_party_snapshot(authoritative_bridge)
    auth_signature, auth_parsed = _party_signature(auth_raw, "encrypted")
    auth_payload = _payload_from_parsed_party(auth_parsed)
    auth_species = _species_order(auth_parsed)
    if not auth_signature:
        return auth_payload, None

    # One-member parties cannot be meaningfully reordered, so there is no need
    # to scan a MiB of RAM for a display mirror.
    if sum(auth_signature.values()) < 2:
        return auth_payload, None

    pid = int((game_info or {}).get("pid", 0))
    cache_key = (str(host), int(bridge_port), pid)
    known_ecs = [ec for (ec, _species), count in auth_signature.items() for _ in range(count)]

    # Mirror discovery is display-only and progressive. A short timeout prevents
    # a lost scan packet from holding the idle worker for seconds; authoritative
    # reads above still use CountingBridge's normal bounded retry protection.
    scan_bridge = Bridge(host=host, port=int(bridge_port), timeout=0.30)
    candidate, state = _locate_live_party_mirror(
        scan_bridge, cache_key, auth_signature, known_ecs, auth_raw, auth_parsed
    )
    if candidate is None:
        diagnostic = None
        if not state.get("search_announced"):
            state["search_announced"] = True
            diagnostic = (
                "PARTY LIVE REFRESH: locating ORAS overworld party mirror "
                "in bounded idle-only RAM slices…"
            )
        elif (
            state.get("scan_index", 0) >= len(state.get("blocks", ()))
            and not state.get("miss_announced")
        ):
            state["miss_announced"] = True
            diagnostic = (
                "PARTY LIVE REFRESH: mirror not located in this pass; "
                "using authoritative party until the bounded rescan."
            )
        _party_refresh_log(
            f"AUTH species={auth_species} source=fallback scan={state.get('scan_index',0)}/{len(state.get('blocks',()))} "
            f"pk6_candidates={len(state.get('candidates',{}))} pointer_candidates={len(state.get('pointer_candidates',{}))} "
            f"order_candidates={len(state.get('order_candidates',{}))} order_near={_order_candidate_debug(state)}"
        )
        return auth_payload, diagnostic

    payload = _payload_from_parsed_party(candidate["parsed"])
    live_species = _species_order(candidate["parsed"])
    _party_refresh_log(
        f"AUTH species={auth_species} LIVE species={live_species} kind={candidate.get('kind')} "
        f"base=0x{int(candidate.get('base',0)):08X} stride=0x{int(candidate.get('stride',0)):X} mode={candidate.get('mode')} "
        f"order_indices={candidate.get('order_indices')} "
        f"scan={state.get('scan_index',0)}/{len(state.get('blocks',()))} "
        f"order_near={_order_candidate_debug(state)}"
    )
    diagnostic = None
    if not state.get("announced"):
        state["announced"] = True
        diagnostic = (
            f"PARTY LIVE REFRESH: overworld mirror located at "
            f"0x{candidate['base']:08X} ({candidate['mode']}, stride 0x{int(candidate.get('stride', 0)):X}, {candidate.get('kind','candidate')})."
        )
    return payload, diagnostic


def _read_party_snapshot_bulk(bridge):
    """Read the same ORAS fixed party slots as hunt startup in one RAM request.

    The six PK6 records live at PARTY_AUTH_BASE with PARTY_AUTH_STRIDE spacing.
    Only the first PK6_SIZE bytes of each record are decoded.  Reading the
    containing span in one request reduces idle traffic from six logical RAM
    reads per refresh to one while preserving the exact hunt-start data source.
    """
    span = ((PARTY_SLOTS - 1) * PARTY_AUTH_STRIDE) + PK6_SIZE
    # D25 transport contract: normal bridge reads are capped at 0x200 bytes.
    # Keep this legacy helper safe even though idle Party Viewer now routes
    # through qt_ui.runtime_party.
    chunks = []
    offset = 0
    while offset < span:
        size = min(0x200, span - offset)
        chunks.append(bridge.read(PARTY_AUTH_BASE + offset, size))
        offset += size
    raw = b"".join(chunks)
    return [
        raw[(slot * PARTY_AUTH_STRIDE):(slot * PARTY_AUTH_STRIDE) + PK6_SIZE]
        for slot in range(PARTY_SLOTS)
    ]


def build_direct_idle_party_payload(bridge):
    """Direct idle Party Viewer snapshot using the hunt-start RAM authority.

    No mapper, persisted party order, battle pointer table, or hunt state is
    involved.  This deliberately mirrors the data source used by
    read_party_snapshot() when a Wild hunt starts, but batches the contiguous
    address span into one read for low idle traffic.
    """
    try:
        raw_party = _read_party_snapshot_bulk(bridge)
    except Exception:
        # Compatibility fallback: preserve the exact proven hunt-start reader
        # if a firmware build ever rejects the larger contiguous request.
        raw_party = read_party_snapshot(bridge)
    parsed_party = [parse_pk6(raw_slot, SPECIES_NAMES) for raw_slot in raw_party]
    return _payload_from_parsed_party(parsed_party)


def build_party_payload(bridge):
    """One bounded six-slot authoritative party snapshot for existing telemetry."""
    raw_party = read_party_snapshot(bridge)
    parsed_party = [parse_pk6(raw_slot, SPECIES_NAMES) for raw_slot in raw_party]
    return _payload_from_parsed_party(parsed_party)


class CountingBridge(Bridge):
    def __init__(
        self,
        host,
        port=4952,
        timeout=2.0,
        read_callback=None,
        transport_retry_callback=None,
    ):
        super().__init__(host=host, port=port, timeout=timeout)
        self.read_count = 0
        self._read_callback = read_callback
        self._transport_retry_callback = transport_retry_callback

    def read(self, address, length):
        # Bounded UDP retransmission for a lost RAM-read packet.
        #
        # This is transport recovery, not state polling: the caller still
        # performs one logical RAM read. Most addresses retain the proven
        # single retransmission. The 4-byte ORAS battle-state gate gets one
        # extra retransmission because hardware evidence showed two
        # consecutive lost replies after an otherwise valid starter confirm.
        max_attempts = 3 if (int(address) == BATTLE_STATE and int(length) == 4) else 2
        for transport_attempt in range(1, max_attempts + 1):
            try:
                raw = super().read(address, length)
                break
            except TimeoutError as exc:
                if self._transport_retry_callback:
                    self._transport_retry_callback(
                        address=address,
                        length=length,
                        attempt=transport_attempt,
                        max_attempts=max_attempts,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                if transport_attempt >= max_attempts:
                    raise
                time.sleep(0.25 if transport_attempt == 1 else 0.50)

        self.read_count += 1
        if self._read_callback:
            self._read_callback(self.read_count)
        return raw

    def game_info(self):
        # GAME_INFO is an idempotent transport query. Support evidence from a
        # 349-encounter clean run showed a single lost reply at the pre-reset
        # GAME_INFO gate. Retransmit the same logical query once on TimeoutError.
        for transport_attempt in range(1, 3):
            try:
                return super().game_info()
            except TimeoutError as exc:
                if self._transport_retry_callback:
                    self._transport_retry_callback(
                        address=0,
                        length=0,
                        attempt=transport_attempt,
                        max_attempts=2,
                        error=f"GAME_INFO {type(exc).__name__}: {exc}",
                    )
                if transport_attempt >= 2:
                    raise
                time.sleep(0.25)

class PartyRefreshWorker(QObject):
    """One bounded party snapshot for idle dashboard telemetry.

    This worker is only scheduled by MainWindow while no hunt or connection
    probe is active. It never authorizes input and never changes hunt state.
    """
    party = Signal(list)
    diagnostic = Signal(str)
    # D19d: idle dashboard RAM traffic must be visible in the same status
    # counter as hunt-side RAM reads. This signal emits a DELTA, not an
    # absolute worker-local count, because a fresh PartyRefreshWorker is
    # created for every idle tick.
    ram_reads = Signal(int)
    finished = Signal()

    def __init__(self, host, bridge_port=4952, timeout=1.0, game_profile=None):
        super().__init__()
        self.host = host
        self.bridge_port = int(bridge_port)
        self.timeout = float(timeout)
        self.game_profile = dict(game_profile or {})
        self._last_read_count = 0
        self._stop_requested = threading.Event()

    def request_stop(self):
        self._stop_requested.set()

    def _emit_idle_read_delta(self, count):
        count = int(count)
        delta = max(0, count - int(self._last_read_count))
        self._last_read_count = count
        if delta:
            self.ram_reads.emit(delta)

    @Slot()
    def run(self):
        try:
            if self._stop_requested.is_set():
                return
            # Use the same bounded transport wrapper as hunt-side telemetry.
            # D25 resolves the field runtime party and batches PK6 reads where
            # addresses are close; discovery is finite and backs off on a miss.
            bridge = CountingBridge(
                host=self.host,
                port=self.bridge_port,
                timeout=self.timeout,
                read_callback=self._emit_idle_read_delta,
            )
            if self.game_profile.get("family") == "xy":
                from pokebot.common.xy_ram import read_party_decoded
                parsed = read_party_decoded(bridge)
                if self._stop_requested.is_set():
                    return
                self.party.emit(payload_from_parsed(parsed))
            else:
                # D25: read the executable-derived ORAS field runtime party
                # pointer chain. The stale fixed/save blocks are never emitted.
                snap = get_runtime_party_snapshot(self.host, self.bridge_port, bridge)
                if self._stop_requested.is_set():
                    return
                diagnostic = snap.get("diagnostic")
                if diagnostic:
                    self.diagnostic.emit(str(diagnostic))
                self.party.emit(list(snap.get("payload") or []))
        except Exception as exc:
            # Display telemetry only: a missed refresh must never disturb the
            # dashboard or alter hunt authority. Keep one visible breadcrumb so
            # party-refresh failures are no longer silently swallowed forever.
            self.diagnostic.emit(
                f"PARTY IDLE REFRESH: {type(exc).__name__}: {exc}"
            )
        finally:
            self.finished.emit()


class RngRefreshWorker(QObject):
    """Persistent passive Gen-6 nearest-shiny telemetry worker.

    HF85 keeps one background worker alive across overworld, battle, run-away,
    and field re-entry.  Earlier builds created a fresh QThread every 750 ms;
    a busy battle RAM loop could leave one short-lived worker waiting long
    enough that the UI stopped scheduling useful follow-up samples.

    This worker is dashboard-only: it reads RAM, never sends controller input,
    and never feeds hunt decisions.  Transient bridge contention is retried on
    the next tick rather than terminating the telemetry thread.
    """
    rng = Signal(dict)
    diagnostic = Signal(str)
    ram_reads = Signal(int)
    finished = Signal()

    def __init__(self, host, bridge_port=4952, timeout=0.75, game_profile=None,
                 tracking_state=None, refresh_interval_s=0.50):
        super().__init__()
        self.host = host
        self.bridge_port = int(bridge_port)
        self.timeout = float(timeout)
        self.game_profile = dict(game_profile or {})
        self.tracking_state = dict(tracking_state or {})
        if isinstance(self.tracking_state.get("state"), list):
            self.tracking_state["state"] = list(self.tracking_state["state"])
        self.refresh_interval_s = max(0.20, float(refresh_interval_s))
        self._last_read_count = 0
        self._stop_requested = threading.Event()
        self._last_error = None

    def request_stop(self):
        self._stop_requested.set()

    def _emit_read_delta(self, count):
        count = int(count)
        delta = max(0, count - int(self._last_read_count))
        self._last_read_count = count
        if delta:
            self.ram_reads.emit(delta)

    @Slot()
    def run(self):
        try:
            family = str(self.game_profile.get("family") or "").lower()
            if family not in {"oras", "xy"}:
                self.rng.emit({
                    "available": False,
                    "reason": "unsupported game family for RNG telemetry",
                })
                return

            bridge = CountingBridge(
                host=self.host,
                port=self.bridge_port,
                timeout=self.timeout,
                read_callback=self._emit_read_delta,
            )

            while not self._stop_requested.is_set():
                tick_started = time.monotonic()
                try:
                    payload = read_live_locked_shiny_countdown(
                        bridge,
                        family=family,
                        tracking_state=self.tracking_state,
                        max_advances=250_000,
                        # Battle/transition bursts can consume substantially
                        # more than a quiet field tick; keep enough local
                        # catch-up headroom to preserve the locked target.
                        max_catchup=32_768,
                    )
                    if self._stop_requested.is_set():
                        break
                    payload = dict(payload or {})
                    if payload.get("available"):
                        next_state = payload.get("tracking_state")
                        if isinstance(next_state, dict):
                            self.tracking_state = dict(next_state)
                            if isinstance(next_state.get("state"), list):
                                self.tracking_state["state"] = list(next_state["state"])
                        payload["telemetry_worker"] = "persistent"
                        self._last_error = None
                        self.rng.emit(payload)
                    else:
                        # An unavailable sample may be a genuine reset boundary.
                        # Keep the last good UI target visible and retry; a later
                        # coherent sample will either catch up or reacquire.
                        reason = str(payload.get("reason") or "RAM sample unavailable")
                        if reason != self._last_error:
                            self.diagnostic.emit(f"RNG TRACKER RETRY: {reason}")
                            self._last_error = reason
                except Exception as exc:
                    # Battle workers can briefly dominate the single 4952
                    # bridge.  A telemetry timeout is not fatal and must not
                    # freeze future countdown updates.
                    message = f"{type(exc).__name__}: {exc}"
                    if message != self._last_error:
                        self.diagnostic.emit(f"RNG TRACKER RETRY: {message}")
                        self._last_error = message

                elapsed = time.monotonic() - tick_started
                wait_s = max(0.02, self.refresh_interval_s - elapsed)
                self._stop_requested.wait(wait_s)
        finally:
            self.finished.emit()


class ProbeWorker(QObject):
    result = Signal(dict)
    finished = Signal()

    def __init__(self, host, bridge_port=4952, input_port=4952, timeout=2.0, use_code_ips=False):
        super().__init__()
        self.host = host
        self.bridge_port = int(bridge_port)
        self.input_port = int(input_port)
        self.timeout = float(timeout)
        self.use_code_ips = bool(use_code_ips)

    @Slot()
    def run(self):
        payload = {
            "ram_ready": False,
            "controller_ready": False,
            "input_status": f"Pokebot-Luma acknowledged input UDP {self.input_port}: Not tested",
            "host": self.host,
        }
        controller = None
        try:
            bridge = Bridge(
                host=self.host,
                port=self.bridge_port,
                timeout=self.timeout,
            )
            t0 = time.monotonic()
            gi = bridge.game_info()
            payload.setdefault("latency_ms", {})["game_info"] = round((time.monotonic()-t0)*1000, 1)
            payload["game_info"] = gi
            game_profile = profile_from_game_info(gi)
            payload["game_profile"] = game_profile
            payload["ram_ready"] = game_profile is not None
            if payload["ram_ready"]:
                family = game_profile.get("family", "oras")
                if family == "xy":
                    from pokebot.common.xy_ram import read_trainer_ids as read_xy_trainer_ids, read_party_decoded
                    try:
                        tid, sid = read_xy_trainer_ids(bridge)
                        payload["trainer_ids"] = {"tid": int(tid), "sid": int(sid), "source": "XY_POKEREADER_REFERENCE"}
                    except Exception as id_exc:
                        payload["trainer_ids"] = {"error": f"{type(id_exc).__name__}: {id_exc}"}
                    try:
                        xy_party = read_party_decoded(bridge)
                        payload["party"] = payload_from_parsed(xy_party)
                        payload["xy_party_probe"] = {
                            "address": "0x08CE1CF8", "stride": 484,
                            "valid_slots": [p.get("slot") for p in xy_party if p.get("valid")],
                            "all_slots": [{"slot": p.get("slot"), "valid": p.get("valid"), "species": p.get("species"), "checksum_valid": p.get("checksum_valid")} for p in xy_party],
                        }
                    except Exception as party_exc:
                        payload["party_error"] = f"{type(party_exc).__name__}: {party_exc}"
                    payload["shiny_charm"] = {"status": "UNMAPPED_XY", "present": None}
                else:
                    try:
                        ids_raw = bridge.read(0x08C81340, 4)
                        tid, sid = struct.unpack("<HH", ids_raw)
                        payload["trainer_ids"] = {"tid": int(tid), "sid": int(sid)}
                    except Exception as id_exc:
                        payload["trainer_ids"] = {"error": f"{type(id_exc).__name__}: {id_exc}"}

                    try:
                        # D25: connection result and idle viewer share the same
                        # live field runtime pointer-chain authority.
                        live_party = get_runtime_party_snapshot(self.host, self.bridge_port, bridge)
                        payload["party"] = list(live_party.get("payload") or [])
                        if live_party.get("diagnostic"):
                            payload["party_diagnostic"] = str(live_party.get("diagnostic"))
                    except Exception as party_exc:
                        payload["party_error"] = f"{type(party_exc).__name__}: {party_exc}"

                    # Unified ORAS 1.4 live Key Items pocket. Read-only telemetry.
                    payload["shiny_charm"] = detect_oras_shiny_charm(
                        bridge, game_key=game_profile.get("key")
                    )

                # Tools-only read-only diagnostics. This never authorizes input.
                try:
                    battle_raw = bridge.read(0x081FB478, 4)
                    battle = struct.unpack("<I", battle_raw)[0]
                    payload["battle_state"] = {
                        "value": battle,
                        "hex": f"0x{battle:08X}",
                        "active": battle == 0x00040001,
                    }
                    if battle == 0x00040001:
                        wild_raw = bridge.read(0x081FFA6C, 232)
                        payload["wild_opponent"] = parse_pk6(wild_raw, SPECIES_NAMES)
                except Exception as diag_exc:
                    payload["battle_diag_error"] = f"{type(diag_exc).__name__}: {diag_exc}"

                # Whole-game read-only location/terrain telemetry. This is
                # display/preflight information only; Wild Start re-reads and
                # re-authorizes the position before any gameplay input.
                try:
                    world_db = (
                        Path(__file__).resolve().parents[1]
                        / "pokebot" / "wild" / "world"
                        / "alpha_sapphire_grass_runtime.sqlite"
                    )
                    payload["world_location"] = probe_world_position(
                        bridge, world_db
                    )
                except Exception as world_exc:
                    payload["world_location"] = {
                        "resolved": False,
                        "error": f"{type(world_exc).__name__}: {world_exc}",
                    }

            controller = AcknowledgedInput(
                self.host,
                port=self.input_port,
                timeout=min(self.timeout, 1.5),
            )
            t0 = time.monotonic()
            info = controller.input_ping()
            neutral = controller.release_all()
            payload.setdefault("latency_ms", {})["controller_ping"] = round((time.monotonic()-t0)*1000, 1)
            payload["controller_info"] = info
            payload["controller_neutral"] = neutral
            payload["controller_ready"] = True
            payload["input_status"] = (
                f"Pokebot-Luma acknowledged input UDP {self.input_port}: Configured "
                "(neutral state sent; gameplay authority is RAM-gated)"
            )
        except Exception as exc:
            payload["error"] = f"{type(exc).__name__}: {exc}"
            if payload.get("ram_ready"):
                payload["input_status"] = (
                    f"Pokebot-Luma acknowledged input UDP {self.input_port}: Not Ready"
                )
        finally:
            if controller is not None:
                controller.close()
        self.result.emit(payload)
        self.finished.emit()

class ControllerToolWorker(QObject):
    result = Signal(dict)
    finished = Signal()

    def __init__(self, host, port, button=None, touch=False, timeout=1.5):
        super().__init__()
        self.host=str(host); self.port=int(port); self.button=button; self.touch=bool(touch); self.timeout=float(timeout)

    @Slot()
    def run(self):
        started=time.monotonic()
        try:
            if self.touch:
                from pokebot.wild.validated_loader import load_walk_v0p23
                _, core, _ = load_walk_v0p23()
                br=core.Bridge(self.host, timeout=self.timeout)
                caps=br.input_ping()
                if not caps.get("touch_pulse"):
                    raise RuntimeError("Bridge does not advertise native touch pulse")
                # Use the exact hardware-proven native touch encoding. Command 9 is never retransmitted.
                touch_state=core.RUN_TOUCH_STATE
                out=br.touch_pulse_no_retransmit(touch_state, 80, 100)
                detail={"mode":"touch","screen_xy":list(core.RUN_TOUCH_XY),"completed":bool(out.get("completed")),"sequence_id":out.get("sequence_id")}
            else:
                ctl=AcknowledgedInput(self.host, port=self.port, timeout=self.timeout)
                try:
                    ctl.input_ping()
                    out=ctl.pulse([self.button],hold_ms=90,resume_settle_ms=0,packet_interval_ms=0,release_ms=100)
                    detail={"mode":"button","button":self.button,"state":out.get("state_name"),"sequence":out.get("sequence")}
                finally:
                    try: ctl.release_all()
                    except Exception: pass
                    ctl.close()
            detail["latency_ms"]=round((time.monotonic()-started)*1000,1); detail["ok"]=True
            self.result.emit(detail)
        except Exception as exc:
            self.result.emit({"ok":False,"error":f"{type(exc).__name__}: {exc}","latency_ms":round((time.monotonic()-started)*1000,1)})
        self.finished.emit()


class HuntWorker(QObject):
    log_line = Signal(str)
    status = Signal(str, str)
    connection = Signal(dict)
    encounter = Signal(dict)
    last_seen_entry = Signal(dict)
    party = Signal(list)
    stats = Signal(dict)
    recent_shinies = Signal(list)
    ram_reads = Signal(int)
    support_ready = Signal(str)
    session_finished = Signal(str)

    def __init__(
        self,
        starter_key,
        host,
        base_dir,
        bridge_port=4952,
        input_port=4952,
        timeout=2.0,
        auto_support_zip=True,
        raw_pk6_keep=10,
        use_code_ips=False,
        target_criteria=None,
    ):
        super().__init__()
        self.requested_starter_key = str(starter_key)
        self.random_mode = self.requested_starter_key == "random"
        # Random remains an ORAS-only mode. XY uses explicit Kalos starter
        # selection while the first hardware choreography is being proven.
        self.starter_key = "torchic" if self.random_mode else self.requested_starter_key
        if self.starter_key not in STARTERS:
            raise ValueError(f"Unknown starter: {self.starter_key}")
        self.meta = STARTERS[self.starter_key]
        self.starter_family = "oras" if self.random_mode else self.meta.get("family", "oras")
        self.random_selection_counts = {"treecko": 0, "torchic": 0, "mudkip": 0}
        # Fair-random bag: each shuffled block of three contains every Hoenn
        # starter exactly once. This prevents one starter being starved by a
        # long independent-random streak while preserving random order.
        self.random_starter_bag = []
        self.host = host
        self.bridge_port = int(bridge_port)
        self.input_port = int(input_port)
        self.timeout = float(timeout)
        self.auto_support_zip = bool(auto_support_zip)
        self.raw_pk6_keep = max(1, min(50, int(raw_pk6_keep)))
        self.requested_use_code_ips = bool(use_code_ips)
        # XY never requires code.ips. Ignore the ORAS setting for XY instead
        # of making users toggle a global option when switching games.
        self.use_code_ips = bool(use_code_ips) if self.starter_family == "oras" else False
        self.target_criteria = normalize_target(target_criteria)
        self.reset_policy = (
            "xy_cro_gated_no_code_ips" if self.starter_family == "xy"
            else ("code_ips_direct" if self.use_code_ips else "legacy_comm_error")
        )
        self.base_dir = Path(base_dir)
        self.stop_event = threading.Event()
        self.inputs = None
        self.started_mono = None
        self.started_iso = None
        self.current_attempt = 0
        self.attempts = []
        self.events = []
        self.durations = []
        self.phase_high_sv = None
        self.phase_low_sv = None
        self.session_high_iv_sum = None
        self.session_low_iv_sum = None
        self.session_high_iv_record = None
        self.session_low_iv_record = None
        self.session_high_sv_record = None
        self.session_low_sv_record = None
        self.session_iv_stat_min = {
            "hp": None, "attack": None, "defense": None,
            "sp_attack": None, "sp_defense": None, "speed": None,
        }
        self.session_iv_stat_max = dict(self.session_iv_stat_min)

        # Build-independent persistent profile.
        profile = get_profile_paths()

        # HF70 multi-3DS: named console instances must not share starter logs
        # or raw PK6 evidence. Normal single-instance launches retain the
        # legacy build-local runtime paths for backwards compatibility.
        if os.environ.get("POKEBOT_MULTI_INSTANCE") == "1":
            self.runtime = profile.root
            self.log_dir = profile.root / "logs"
            self.raw_dir = profile.root / "raw" / self.requested_starter_key
        else:
            self.runtime = self.base_dir / "runtime"
            self.log_dir = self.runtime / "logs"
            self.raw_dir = self.runtime / "raw" / self.requested_starter_key
        for d in (self.runtime, self.log_dir, self.raw_dir):
            d.mkdir(parents=True, exist_ok=True)
        profile.root.mkdir(parents=True, exist_ok=True)
        profile.stats_dir.mkdir(parents=True, exist_ok=True)
        profile.history_dir.mkdir(parents=True, exist_ok=True)
        profile.encounters_dir.mkdir(parents=True, exist_ok=True)
        profile.session_stats_dir.mkdir(parents=True, exist_ok=True)
        profile.screenshots_dir.mkdir(parents=True, exist_ok=True)

        self.stats_dir = profile.stats_dir
        self.screenshots_dir = profile.screenshots_dir
        self.shiny_path = profile.recent_shinies_path
        self.last_seen_path = profile.last_seen_path
        self.encounter_jsonl_path = profile.encounters_dir / f"{starter_key}.jsonl"
        self.encounter_csv_path = profile.encounters_dir / f"{starter_key}.csv"
        self.session_stats_path = profile.session_stats_dir / f"{starter_key}_latest.json"

        self.stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_path = self.log_dir / f"Pokebot3DS-CFW_{self.requested_starter_key}_{self.stamp}.log"
        self.session_path = self.log_dir / f"Pokebot3DS-CFW_{self.requested_starter_key}_session_{self.stamp}.json"
        self.random_session_stats_path = profile.session_stats_dir / "random_latest.json"
        self.stats_path = self.stats_dir / f"{self.starter_key}.json"

        self.lifetime = self._load_stats()
        self.starter_module = importlib.import_module(
            f"pokebot.starters.{self.starter_key}"
        )

    def _activate_starter_context(self, key):
        if key not in STARTERS:
            raise ValueError(f"Unknown starter context: {key}")
        self.starter_key = key
        self.meta = STARTERS[key]
        self.stats_path = self.stats_dir / f"{key}.json"
        profile = get_profile_paths()
        self.encounter_jsonl_path = profile.encounters_dir / f"{key}.jsonl"
        self.encounter_csv_path = profile.encounters_dir / f"{key}.csv"
        self.session_stats_path = profile.session_stats_dir / f"{key}_latest.json"
        self.lifetime = self._load_stats()
        self.starter_module = importlib.import_module(f"pokebot.starters.{key}")


    def _pick_random_starter(self):
        """Return the next fair-random Hoenn starter.

        A fresh bag contains each starter exactly once and is shuffled with
        Python's system-seeded PRNG. Once exhausted, a new bag is shuffled.
        This keeps the sequence random while guaranteeing all three starters
        occur in every three completed selections.
        """
        if not self.random_starter_bag:
            self.random_starter_bag = ["treecko", "torchic", "mudkip"]
            random.shuffle(self.random_starter_bag)
        return self.random_starter_bag.pop()

    def _random_display_name(self):
        return "Random Starters" if self.random_mode else self.meta["name"]

    def _save_random_session_summary(self, final_status, reason):
        if not self.random_mode:
            return
        payload = {
            "mode": "Random Starters",
            "session_started": self.started_iso,
            "session_elapsed_s": round(self._elapsed(), 3),
            "session_seen": self._session_seen(),
            "selection_counts": dict(self.random_selection_counts),
            "selection_policy": "SHUFFLED_BAG_3_EACH_STARTER_ONCE_PER_BLOCK",
            "highest_iv_sum": self.session_high_iv_sum,
            "lowest_iv_sum": self.session_low_iv_sum,
            "highest_sv": self.phase_high_sv,
            "lowest_sv": self.phase_low_sv,
            "final_status": final_status,
            "reason": reason,
            "note": "Per-starter lifetime stats and encounter ledgers remain authoritative.",
        }
        tmp = self.random_session_stats_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.random_session_stats_path)

    def request_stop(self):
        self.stop_event.set()
        # Pokebot3DS-CFW STOP neutralizes the Pokebot-Luma controller
        # immediately without blocking the Qt UI thread.
        threading.Thread(
            target=self._emergency_release_controller,
            name="Pokebot3DS-CFW-ReleaseAll",
            daemon=True,
        ).start()

    def _emergency_release_controller(self):
        controller = None
        try:
            controller = AcknowledgedInput(
                self.host,
                port=self.input_port,
                timeout=min(0.75, self.timeout),
            )
            controller.release_all()
        except Exception:
            pass
        finally:
            if controller is not None:
                controller.close()

    def _emit_read_count(self, count):
        self.ram_reads.emit(int(count))

    def _log_transport_retry(
        self,
        address,
        length,
        attempt,
        max_attempts,
        error,
    ):
        message = (
            "GAME_INFO_TRANSPORT_RETRY"
            if int(address) == 0 and int(length) == 0
            else "RAM_READ_TRANSPORT_RETRY"
        )
        fields = {
            "attempt": int(attempt),
            "max_attempts": int(max_attempts),
            "error": error,
        }
        if message == "RAM_READ_TRANSPORT_RETRY":
            fields.update(
                address=f"0x{int(address):08X}",
                length=int(length),
            )
        self._log(
            message,
            **fields,
        )
        return

    def _log(self, message, **fields):
        stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
        event = {"timestamp": stamp, "message": message, **fields}
        self.events.append(event)

        if fields:
            short_fields = []
            for key, value in fields.items():
                if key in ("traceback", "observations", "route_trace"):
                    continue
                short_fields.append(f"{key}={value}")
            suffix = " | " + " ".join(short_fields) if short_fields else ""
        else:
            suffix = ""

        line = f"[{stamp[11:23]}] {message}{suffix}"
        self.log_line.emit(line)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, default=str) + "\n")

    def _load_stats(self):
        default = {
            "starter": self.meta["name"],
            "species": self.meta["species"],
            "lifetime_seen": 0,
            "lifetime_shinies": 0,
            "completed_sessions": 0,
            "phase_seen": 0,
            "last_phase_seen": 0,
            "phase_log_miss": None,
            "phase_cumulative_probability": 0.0,
            "last_phase_cumulative_probability": None,
            "last_shiny": None,
            "last_result": None,
            "best_shiny_xor": None,
            "highest_shiny_xor": None,
            "lifetime_highest_iv_sum": None,
            "lifetime_lowest_iv_sum": None,
            "lifetime_highest_sv": None,
            "lifetime_lowest_sv": None,
            "highest_iv_record": None,
            "lowest_iv_record": None,
            "highest_sv_record": None,
            "lowest_sv_record": None,
            "last_session_summary": None,
        }
        data = {}
        if self.stats_path.exists():
            try:
                loaded = json.loads(self.stats_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except Exception:
                pass
        merged = dict(default)
        merged.update(data)
        # Profile metadata belongs to this build, not to stale JSON.
        merged["starter"] = self.meta["name"]
        merged["species"] = self.meta["species"]
        if data.get("phase_log_miss") is None:
            p = probability_for_rolls(1)
            merged["phase_log_miss"] = phase_log_miss_for_constant(
                int(merged.get("phase_seen", 0) or 0), p
            )
        merged["phase_cumulative_probability"] = cumulative_probability_from_log_miss(
            merged.get("phase_log_miss", 0.0)
        )
        return merged

    def _save_stats(self, result=None, end_session=False):
        self.lifetime["starter"] = self.meta["name"]
        self.lifetime["species"] = self.meta["species"]
        if result is not None:
            self.lifetime["last_result"] = result
        if self.started_iso:
            self.lifetime["last_session_started"] = self.started_iso
        if end_session:
            self.lifetime["completed_sessions"] = int(
                self.lifetime.get("completed_sessions", 0)
            ) + 1
            self.lifetime["last_session_ended"] = (
                datetime.now().astimezone().isoformat(timespec="seconds")
            )
            self.lifetime["last_session_summary"] = self._session_summary()

        tmp = self.stats_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.lifetime, indent=2), encoding="utf-8")
        tmp.replace(self.stats_path)

    @staticmethod
    def _iv_sum_from_pk6(pk6):
        ivs = pk6.get("ivs") or {}
        return sum(int(ivs.get(k, 0)) for k in (
            "hp", "attack", "defense", "sp_attack", "sp_defense", "speed"
        ))

    def _record_payload(self, pk6, attempt=None, duration_s=None):
        ivs = pk6.get("ivs") or {}
        return {
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "session_started": self.started_iso,
            "attempt": int(self.current_attempt if attempt is None else attempt),
            "game": (getattr(self, "game_profile", None) or {}).get("name", "ORAS"),
            "hunt_type": "Starter",
            "target": self.meta["name"],
            "species": int(pk6.get("species", 0)),
            "species_name": pk6.get("species_name", self.meta["name"]),
            "pid": pk6.get("pid"),
            "ec": pk6.get("ec"),
            "tid": pk6.get("tid"),
            "sid": pk6.get("sid"),
            "sv": int(pk6.get("shiny_xor", 0)),
            "shiny": bool(pk6.get("is_shiny")),
            "nature": pk6.get("nature", "—"),
            "ability_id": pk6.get("ability_id"),
            "ability": ability_name(pk6.get("ability_id")),
            "gender": pk6.get("gender", "—"),
            "ivs": {
                "hp": int(ivs.get("hp", 0)),
                "attack": int(ivs.get("attack", 0)),
                "defense": int(ivs.get("defense", 0)),
                "sp_attack": int(ivs.get("sp_attack", 0)),
                "sp_defense": int(ivs.get("sp_defense", 0)),
                "speed": int(ivs.get("speed", 0)),
            },
            "iv_sum": self._iv_sum_from_pk6(pk6),
            "raw_sha256": pk6.get("raw_sha256"),
            "duration_s": (round(float(duration_s), 3) if duration_s is not None else None),
            "phase_length": (
                int(self.lifetime.get("phase_seen", 0) or 0)
                if bool(pk6.get("is_shiny")) else None
            ),
            "encounter_shiny_probability": probability_for_rolls(1),
            "shiny_odds_display": "1/4,096",
            "phase_cumulative_probability": float(
                self.lifetime.get("phase_cumulative_probability", 0.0) or 0.0
            ),
        }

    def _session_summary(self):
        return {
            "starter": self._random_display_name(),
            "active_starter": self.meta["name"],
            "random_selection_counts": dict(self.random_selection_counts) if self.random_mode else None,
            "random_selection_policy": (
                "SHUFFLED_BAG_3_EACH_STARTER_ONCE_PER_BLOCK" if self.random_mode else None
            ),
            "session_started": self.started_iso,
            "session_elapsed_s": round(self._elapsed(), 3),
            "session_seen": self._session_seen(),
            "highest_iv_sum": self.session_high_iv_sum,
            "lowest_iv_sum": self.session_low_iv_sum,
            "highest_sv": self.phase_high_sv,
            "lowest_sv": self.phase_low_sv,
            "highest_iv_record": self.session_high_iv_record,
            "lowest_iv_record": self.session_low_iv_record,
            "highest_sv_record": self.session_high_sv_record,
            "lowest_sv_record": self.session_low_sv_record,
            "iv_stat_min": dict(self.session_iv_stat_min),
            "iv_stat_max": dict(self.session_iv_stat_max),
        }

    def _save_session_stats_snapshot(self):
        payload = self._session_summary()
        target = self.random_session_stats_path if self.random_mode else self.session_stats_path
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(target)

    def _append_encounter_ledger(self, pk6, duration_s=None):
        record = self._record_payload(pk6, duration_s=duration_s)
        with self.encounter_jsonl_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")

        csv_fields = [
            "time", "session_started", "attempt", "game", "hunt_type",
            "target", "species", "species_name", "pid", "ec", "tid", "sid",
            "sv", "shiny", "hp", "atk", "def", "spa", "spd", "spe",
            "iv_sum", "duration_s", "raw_sha256",
        ]
        row = {
            "time": record["time"],
            "session_started": record["session_started"],
            "attempt": record["attempt"],
            "game": record["game"],
            "hunt_type": record["hunt_type"],
            "target": record["target"],
            "species": record["species"],
            "species_name": record["species_name"],
            "pid": record["pid"],
            "ec": record["ec"],
            "tid": record["tid"],
            "sid": record["sid"],
            "sv": record["sv"],
            "shiny": record["shiny"],
            "hp": record["ivs"]["hp"],
            "atk": record["ivs"]["attack"],
            "def": record["ivs"]["defense"],
            "spa": record["ivs"]["sp_attack"],
            "spd": record["ivs"]["sp_defense"],
            "spe": record["ivs"]["speed"],
            "iv_sum": record["iv_sum"],
            "duration_s": record.get("duration_s"),
            "raw_sha256": record["raw_sha256"],
        }
        write_header = not self.encounter_csv_path.exists() or self.encounter_csv_path.stat().st_size == 0
        with self.encounter_csv_path.open("a", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=csv_fields)
            if write_header:
                writer.writeheader()
            writer.writerow(row)

    def _load_last_seen(self):
        if not self.last_seen_path.exists():
            return []
        try:
            data = json.loads(
                self.last_seen_path.read_text(encoding="utf-8")
            )
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _save_last_seen_entry(self, item):
        history = self._load_last_seen()
        history.insert(0, dict(item))
        history = history[:7]

        tmp = self.last_seen_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(history, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.last_seen_path)

    def _load_recent_shinies(self):
        if not self.shiny_path.exists():
            return []
        try:
            data = json.loads(self.shiny_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _capture_starter_shiny_framebuffer(self, pk6):
        """Best-effort exact starter shiny top-screen capture.

        RAM shiny authority has already completed before this runs. Capture
        failure is presentation-only and can never authorize another reset.
        Discord retains its own live-capture fallback if this snapshot fails.
        """
        try:
            # Party PK6 becomes authoritative slightly before the battle model
            # is guaranteed to be visually settled. No input is sent here.
            # Party PK6 authority can arrive while the starter battle is still
            # presenting.  Wait until the model is expected to be visible
            # before freezing the Discord/evidence frame.  This does not delay
            # shiny authority or send any controller input.
            time.sleep(3.25)
            species = int(pk6.get("species", 0) or 0)
            pid = str(pk6.get("pid") or "unknown")
            safe_pid = "".join(ch for ch in pid if ch.isalnum())[-16:] or "unknown"
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            out = self.screenshots_dir / (
                f"shiny_starter_{species}_{safe_pid}_{stamp}.png"
            )
            meta = capture_top_screen(
                self.host,
                out,
                port=self.bridge_port,
                timeout=min(max(float(self.timeout), 0.5), 1.25),
            )
            self._log(
                "FRAMEBUFFER_STARTER_SHINY_CAPTURE",
                species=species,
                pokemon_pid=pid,
                geometry=f"{meta['width']}x{meta['height']}",
                path=meta["path"],
            )
            return meta
        except Exception as exc:
            self._log(
                "FRAMEBUFFER_STARTER_SHINY_CAPTURE_UNAVAILABLE",
                error=f"{type(exc).__name__}: {exc}",
                shiny_authority="UNCHANGED",
            )
            return None

    def _save_recent_shiny(self, pk6, attempt, framebuffer_path=None):
        data = self._load_recent_shinies()
        item = {
            "starter": self.meta["name"],
            "species": self.meta["name"],
            "species_id": self.meta["species"],
            "pid": pk6["pid"],
            "ec": pk6["ec"],
            "shiny_xor": int(pk6["shiny_xor"]),
            "nature": pk6.get("nature", "—"),
            "ability_id": pk6.get("ability_id"),
            "ability": ability_name(pk6.get("ability_id")),
            "gender": pk6.get("gender", "—"),
            "ivs": dict(pk6.get("ivs") or {}),
            "attempt": int(attempt),
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "phase_length": int(self.lifetime.get("last_phase_seen", 0) or 0),
            "hunt_type": "Starter",
            "game": (getattr(self, "game_profile", None) or {}).get("name", "ORAS"),
            "framebuffer_path": str(framebuffer_path) if framebuffer_path else None,
        }
        data.insert(0, item)
        data = data[:20]
        self.shiny_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self.recent_shinies.emit(data)

    def _elapsed(self):
        if self.started_mono is None:
            return 0.0
        return max(0.0, time.monotonic() - self.started_mono)

    def _session_seen(self):
        return sum(
            1 for x in self.attempts
            if x.get("status") in ("PASS", "SHINY_HOLD_PASS")
        )

    def _rate(self):
        elapsed = self._elapsed()
        return 0.0 if elapsed <= 0 else self._session_seen() * 3600.0 / elapsed

    def _rolling_cleanup(self):
        files = sorted(
            self.raw_dir.glob(f"Pokebot3DS-CFW_{self.starter_key}_attempt_*.bin"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in files[self.raw_pk6_keep:]:
            try:
                old.unlink()
            except OSError:
                pass

    def _emit_stats(self):
        self.stats.emit({
            "starter": self.meta["name"],
            "location_name": "Aquacorde Town" if self.starter_family == "xy" else "Route 101",
            "hunt_mode": "Random" if self.random_mode else "Single",
            "requested_starter": self.requested_starter_key,
            "random_selection_counts": dict(self.random_selection_counts) if self.random_mode else None,
            "random_selection_policy": (
                "SHUFFLED_BAG_3_EACH_STARTER_ONCE_PER_BLOCK" if self.random_mode else None
            ),
            # The first soft reset is setup: it creates the first encounter
            # and must not count as a completed hunting reset. Internal attempt
            # numbering remains 1-based and unchanged.
            "resets": max(0, int(self.current_attempt) - 1),
            "session_seen": self._session_seen(),
            "lifetime_seen": int(self.lifetime.get("lifetime_seen", 0)),
            "lifetime_shinies": int(self.lifetime.get("lifetime_shinies", 0)),
            "phase_index": int(self.lifetime.get("lifetime_shinies", 0)) + 1,
            "phase_seen": int(self.lifetime.get("phase_seen", 0)),
            "rate": round(self._rate(), 2),
            "elapsed": format_elapsed(self._elapsed()),
            "average_time": (
                round(sum(self.durations) / len(self.durations), 2)
                if self.durations else 0.0
            ),
            "longest_time": round(max(self.durations), 2) if self.durations else 0.0,
            "highest_sv": self.phase_high_sv,
            "lowest_sv": self.phase_low_sv,
            "highest_iv_sum": self.session_high_iv_sum,
            "lowest_iv_sum": self.session_low_iv_sum,
            "lifetime_highest_sv": self.lifetime.get("lifetime_highest_sv"),
            "lifetime_lowest_sv": self.lifetime.get("lifetime_lowest_sv"),
            "lifetime_highest_iv_sum": self.lifetime.get("lifetime_highest_iv_sum"),
            "lifetime_lowest_iv_sum": self.lifetime.get("lifetime_lowest_iv_sum"),
            "phase_log_miss": float(self.lifetime.get("phase_log_miss", 0.0) or 0.0),
            "phase_cumulative_probability": float(
                self.lifetime.get("phase_cumulative_probability", 0.0) or 0.0
            ),
            "shiny_odds_display": "1/4,096",
            "encounter_shiny_probability": probability_for_rolls(1),
            "shiny_charm_applies": False,
            "last_shiny": self.lifetime.get("last_shiny"),
        })

    def _reset_phase_session_extrema(self):
        """Start a fresh extrema window after a completed shiny phase."""
        self.phase_high_sv = None
        self.phase_low_sv = None
        self.session_high_iv_sum = None
        self.session_low_iv_sum = None
        self.session_high_iv_record = None
        self.session_low_iv_record = None
        self.session_high_sv_record = None
        self.session_low_sv_record = None
        self.session_iv_stat_min = {
            "hp": None, "attack": None, "defense": None,
            "sp_attack": None, "sp_defense": None, "speed": None,
        }
        self.session_iv_stat_max = dict(self.session_iv_stat_min)

    def _note_valid(self, pk6, duration_s=None):
        self.lifetime["lifetime_seen"] = int(
            self.lifetime.get("lifetime_seen", 0)
        ) + 1
        self.lifetime["phase_seen"] = int(
            self.lifetime.get("phase_seen", 0)
        ) + 1
        encounter_probability = probability_for_rolls(1)
        self.lifetime["phase_log_miss"] = advance_phase_log_miss(
            self.lifetime.get("phase_log_miss", 0.0), encounter_probability
        )
        self.lifetime["phase_cumulative_probability"] = cumulative_probability_from_log_miss(
            self.lifetime["phase_log_miss"]
        )

        xor = int(pk6["shiny_xor"])
        iv_sum = self._iv_sum_from_pk6(pk6)
        record = self._record_payload(pk6, duration_s=duration_s)

        best = self.lifetime.get("best_shiny_xor")
        high = self.lifetime.get("highest_shiny_xor")
        if best is None or xor < int(best):
            self.lifetime["best_shiny_xor"] = xor
        if high is None or xor > int(high):
            self.lifetime["highest_shiny_xor"] = xor

        if self.phase_high_sv is None or xor > self.phase_high_sv:
            self.phase_high_sv = xor
            self.session_high_sv_record = record
        if self.phase_low_sv is None or xor < self.phase_low_sv:
            self.phase_low_sv = xor
            self.session_low_sv_record = record

        if self.session_high_iv_sum is None or iv_sum > self.session_high_iv_sum:
            self.session_high_iv_sum = iv_sum
            self.session_high_iv_record = record
        if self.session_low_iv_sum is None or iv_sum < self.session_low_iv_sum:
            self.session_low_iv_sum = iv_sum
            self.session_low_iv_record = record

        ivs = record["ivs"]
        for stat, value in ivs.items():
            low = self.session_iv_stat_min.get(stat)
            high_stat = self.session_iv_stat_max.get(stat)
            if low is None or value < low:
                self.session_iv_stat_min[stat] = value
            if high_stat is None or value > high_stat:
                self.session_iv_stat_max[stat] = value

        life_high_iv = self.lifetime.get("lifetime_highest_iv_sum")
        life_low_iv = self.lifetime.get("lifetime_lowest_iv_sum")
        life_high_sv = self.lifetime.get("lifetime_highest_sv")
        life_low_sv = self.lifetime.get("lifetime_lowest_sv")
        if life_high_iv is None or iv_sum > int(life_high_iv):
            self.lifetime["lifetime_highest_iv_sum"] = iv_sum
            self.lifetime["highest_iv_record"] = record
        if life_low_iv is None or iv_sum < int(life_low_iv):
            self.lifetime["lifetime_lowest_iv_sum"] = iv_sum
            self.lifetime["lowest_iv_record"] = record
        if life_high_sv is None or xor > int(life_high_sv):
            self.lifetime["lifetime_highest_sv"] = xor
            self.lifetime["highest_sv_record"] = record
        if life_low_sv is None or xor < int(life_low_sv):
            self.lifetime["lifetime_lowest_sv"] = xor
            self.lifetime["lowest_sv_record"] = record

        if pk6["is_shiny"]:
            increment_species_shiny_total(
                int(pk6["species"]),
                pk6.get("species_name") or self.meta["name"],
                found_time=datetime.now().astimezone().isoformat(timespec="seconds"),
            )
            phase_length = int(self.lifetime.get("phase_seen", 0))
            self.lifetime["lifetime_shinies"] = int(
                self.lifetime.get("lifetime_shinies", 0)
            ) + 1
            self.lifetime["last_phase_seen"] = phase_length
            self.lifetime["last_phase_cumulative_probability"] = float(
                self.lifetime.get("phase_cumulative_probability", 0.0) or 0.0
            )
            self.lifetime["last_shiny"] = {
                "starter": self.meta["name"],
                "pid": pk6["pid"],
                "shiny_xor": xor,
                "phase_seen": phase_length,
                "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            self.lifetime["phase_seen"] = 0
            self.lifetime["phase_log_miss"] = 0.0
            self.lifetime["phase_cumulative_probability"] = 0.0
            self._reset_phase_session_extrema()

        # Preserve per-encounter duration for STATS analytics without changing hunt authority.
        self._append_encounter_ledger(pk6, duration_s=duration_s)
        self._save_session_stats_snapshot()
        self._save_stats("SHINY" if pk6["is_shiny"] else "NON_SHINY")

    def _support(self, status, reason):
        report = {
            "status": status,
            "reason": reason,
            "starter": self._random_display_name(),
            "active_starter": self.meta["name"],
            "species": self.meta["species"],
            "validation": ("RANDOM ORCHESTRATOR + LOCKED 10/10 PER-STARTER MODULES" if self.random_mode else VALIDATION[self.starter_key]),
            "random_selection_counts": dict(self.random_selection_counts) if self.random_mode else None,
            "random_selection_policy": (
                "SHUFFLED_BAG_3_EACH_STARTER_ONCE_PER_BLOCK" if self.random_mode else None
            ),
            "controller": f"Pokebot-Luma acknowledged input UDP {self.input_port}",
            "use_code_ips": self.use_code_ips,
            "reset_policy": self.reset_policy,
            "communication_error_handling_enabled": (self.starter_family == "oras" and not self.use_code_ips),
            "hunt_limit": (
                "Unlimited (all Kalos starters hardware-proven)"
                if self.starter_family == "xy"
                else "Unlimited"
            ),
            "look_for_target": self.target_criteria,
            "session_started": self.started_iso,
            "session_elapsed_s": round(self._elapsed(), 3),
            "session_seen": self._session_seen(),
            "session_rate_per_hour": round(self._rate(), 2),
            "session_stats": self._session_summary(),
            "encounter_ledger_jsonl": str(self.encounter_jsonl_path),
            "encounter_ledger_csv": str(self.encounter_csv_path),
            "attempts": self.attempts,
            "lifetime": self.lifetime,
            "events": self.events,
        }
        self.session_path.write_text(
            json.dumps(report, indent=2, default=str),
            encoding="utf-8",
        )
        out = self.base_dir / (
            f"Pokebot3DS-CFW_{self.requested_starter_key}_support_{self.stamp}.zip"
        )
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            snapshot_path = self.random_session_stats_path if self.random_mode else self.session_stats_path
            for p in (self.log_path, self.session_path, self.stats_path, snapshot_path):
                if p.exists():
                    zf.write(p, arcname=p.name)
            if self.random_mode:
                for key in ("treecko", "torchic", "mudkip"):
                    p = self.stats_dir / f"{key}.json"
                    if p.exists():
                        zf.write(p, arcname=f"starter_stats/{p.name}")
            recent_raw = sorted(
                self.raw_dir.glob(f"Pokebot3DS-CFW_{self.starter_key}_attempt_*.bin"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:self.raw_pk6_keep]
            for p in recent_raw:
                zf.write(p, arcname=f"recent_raw/{p.name}")
        self.support_ready.emit(str(out))
        return out

    def _finish(self, final_status, reason, support=True):
        if self.inputs is not None:
            try:
                self.inputs.release_all()
            except Exception as exc:
                self._log(
                    "FINAL_CONTROLLER_RELEASE_FAIL",
                    controller="Pokebot-Luma acknowledged input UDP 4952",
                    error=f"{type(exc).__name__}: {exc}",
                )
            finally:
                self.inputs.close()
                self.inputs = None
        if self.random_mode:
            # The individual encounter already saved its actual starter stats.
            # Do not charge a mixed session completion to only the last starter.
            if sum(self.random_selection_counts.values()) > 0:
                self._save_stats(reason, end_session=False)
            self._save_random_session_summary(final_status, reason)
        else:
            self.lifetime["lifetime_hunt_seconds"] = round(
                float(self.lifetime.get("lifetime_hunt_seconds", 0.0)) + self._elapsed(), 3
            )
            current_rate = round(self._rate(), 3)
            prior_rate = self.lifetime.get("lifetime_fastest_rate")
            if prior_rate is None or current_rate > float(prior_rate):
                self.lifetime["lifetime_fastest_rate"] = current_rate
            self._save_stats(reason, end_session=True)
        if support and self.auto_support_zip:
            self._support(final_status, reason)
        self.status.emit(final_status, reason)
        self.session_finished.emit(final_status)

    @Slot()
    def run(self):
        self.started_mono = time.monotonic()
        self.started_iso = datetime.now().astimezone().isoformat(timespec="seconds")

        self.status.emit("STARTING", f"{self._random_display_name()} preflight")
        self._log(
            "QT_SESSION_START",
            starter=self._random_display_name(),
            validation=("RANDOM ORCHESTRATOR + LOCKED 10/10 PER-STARTER MODULES" if self.random_mode else VALIDATION[self.starter_key]),
            controller=f"Pokebot-Luma acknowledged input UDP {self.input_port}",
            use_code_ips=self.use_code_ips,
            reset_policy=self.reset_policy,
            communication_error_handling_enabled=(self.starter_family == "oras" and not self.use_code_ips),
            hunt_limit=(
                "Unlimited (all Kalos starters hardware-proven)"
                if self.starter_family == "xy"
                else "Unlimited"
            ),
        )

        bridge = CountingBridge(
            self.host,
            port=self.bridge_port,
            timeout=self.timeout,
            read_callback=self._emit_read_count,
            transport_retry_callback=self._log_transport_retry,
        )
        inputs = AcknowledgedInput(
            self.host,
            port=self.input_port,
            timeout=min(self.timeout, 1.5),
        )
        self.inputs = inputs
        ctx = SimpleNamespace(bridge=bridge, inputs=inputs, log=self._log, stop_event=self.stop_event)

        try:
            controller_info = inputs.input_ping()
            neutral = inputs.release_all()
            self._log(
                "CONTROLLER_PREFLIGHT_PASS",
                controller="Pokebot-Luma acknowledged input UDP 4952",
                protocol=controller_info.get("protocol"),
                capabilities=f"0x{controller_info.get('capabilities', 0):08X}",
                runtime_flags=f"0x{controller_info.get('runtime_flags', 0):08X}",
                raw_hid=f"0x{neutral.get('raw_hid', 0):03X}",
                hunt_limit="Unlimited",
            )
        except Exception as exc:
            self.connection.emit({
                "ram_ready": False,
                "controller_ready": False,
                "host": self.host,
                "input_status": f"Pokebot-Luma acknowledged input UDP {self.input_port}: Not Ready",
                "error": f"{type(exc).__name__}: {exc}",
            })
            self._log(
                "CONTROLLER_PREFLIGHT_FAIL",
                error=f"{type(exc).__name__}: {exc}",
            )
            self._finish("SAFETY HOLD", "CONTROLLER_PREFLIGHT_FAIL")
            inputs.close()
            self.inputs = None
            return

        try:
            gi = bridge.game_info()
        except Exception as exc:
            self.connection.emit({
                "ram_ready": False,
                "host": self.host,
                "error": f"{type(exc).__name__}: {exc}",
            })
            self._log("PREFLIGHT_FAIL", error=f"{type(exc).__name__}: {exc}")
            self._finish("SAFETY HOLD", "PREFLIGHT_GAME_INFO_FAIL")
            return

        game_profile = profile_from_game_info(gi)
        ready = game_profile is not None
        self.game_profile = game_profile
        self.connection.emit({
            "ram_ready": ready,
            "controller_ready": True,
            "host": self.host,
            "game_info": gi,
            "game_profile": game_profile,
            "input_status": (
                f"Pokebot-Luma acknowledged input UDP {self.input_port}: Ready | "
                + ("code.ips NOT REQUIRED (XY)" if game_profile and game_profile.get("family") == "xy"
                   else f"code.ips {'ON' if self.use_code_ips else 'OFF'}")
            ),
        })
        if not ready:
            self._log("PREFLIGHT_FAIL", game_info=gi)
            self._finish("SAFETY HOLD", "PREFLIGHT_GAME_INFO_FAIL")
            return

        if game_profile.get("family") != self.starter_family:
            self._log(
                "STARTER_GAME_FAMILY_MISMATCH",
                selected_starter=self.starter_key,
                selected_family=self.starter_family,
                detected_game=game_profile.get("name"),
                detected_family=game_profile.get("family"),
            )
            self._finish("SAFETY HOLD", "STARTER_GAME_FAMILY_MISMATCH")
            return

        self._log(
            "GEN6_GAME_PROFILE_LOCKED",
            game=game_profile["name"],
            game_key=game_profile["key"],
            title_id=game_profile["title_id"],
            process=game_profile["process"],
        )
        self.recent_shinies.emit(self._load_recent_shinies())
        self._emit_stats()
        self.status.emit("RUNNING", f"{game_profile['name']} • Hunting {self._random_display_name()}")

        while True:
            if self.stop_event.is_set():
                self._log("MANUAL_STOP", boundary="before_reset")
                self._finish("IDLE", "MANUAL_STOP")
                return

            self.current_attempt += 1
            if self.random_mode:
                picked = self._pick_random_starter()
                self._activate_starter_context(picked)
                self.random_selection_counts[picked] += 1
                self._log(
                    "RANDOM_STARTER_SELECTED",
                    attempt=self.current_attempt,
                    starter=self.meta["name"],
                    module=f"pokebot.starters.{picked}",
                    selection_counts=dict(self.random_selection_counts),
                )
                self.status.emit(
                    "RUNNING",
                    f"{game_profile['name']} • Random → {self.meta['name']}",
                )
            attempt_started = time.monotonic()
            self._log("ATTEMPT_BEGIN", attempt=self.current_attempt, starter=self.meta["name"])
            # Session statistics are live telemetry. The reset/attempt counter
            # advances immediately at the start of every reset cycle.
            self._emit_stats()

            try:
                if game_profile.get("family") == "xy":
                    reset_ok, reset_result = run_xy_reset_to_field(
                        bridge,
                        inputs,
                        self._log,
                        game_profile,
                    )
                else:
                    reset_ok, reset_result = run_reset_to_bag_for_profile(
                        bridge,
                        inputs,
                        self._log,
                        game_profile,
                        use_code_ips=self.use_code_ips,
                    )
            except Exception as exc:
                reset_ok = False
                reset_result = {
                    "status": "RESET_ROUTE_EXCEPTION",
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                }

            if not reset_ok:
                if self.stop_event.is_set():
                    self._log("MANUAL_STOP", boundary="during_reset_route")
                    self._finish("IDLE", "MANUAL_STOP_DURING_RESET")
                    inputs.close()
                    self.inputs = None
                    return
                duration = round(time.monotonic() - attempt_started, 3)
                self.attempts.append({
                    "attempt": self.current_attempt,
                    "status": reset_result.get("status", "RESET_ROUTE_FAIL"),
                    "duration_s": duration,
                    "reset_route": reset_result,
                    "next_reset_authorized": False,
                })
                self._log(
                    "SAFETY_HOLD",
                    attempt=self.current_attempt,
                    status=self.attempts[-1]["status"],
                )
                self._finish("SAFETY HOLD", self.attempts[-1]["status"])
                return

            # Stop requested during the reset route: stop at the proven
            # post-load boundary and do not start starter-selection input.
            if self.stop_event.is_set():
                boundary = "xy_field_gate" if game_profile.get("family") == "xy" else "bag_gate"
                self._log("MANUAL_STOP", boundary=boundary)
                self._finish("IDLE", "MANUAL_STOP_AT_POST_LOAD_GATE")
                return

            self._emit_stats()

            self._log(
                "HANDOFF_TO_STARTER_MODULE",
                attempt=self.current_attempt,
                starter_module=f"pokebot.starters.{self.starter_key}",
                old_pid=reset_result.get("old_pid"),
                new_pid=reset_result.get("new_pid"),
            )

            raw_path = self.raw_dir / (
                f"Pokebot3DS-CFW_{self.starter_key}_attempt_"
                f"{self.current_attempt:08d}_{self.stamp}.bin"
            )

            try:
                if game_profile.get("family") == "xy":
                    starter_ok, starter_result = self.starter_module.run_from_field(
                        ctx, raw_path
                    )
                else:
                    starter_ok, starter_result = self.starter_module.run_from_bag(
                        ctx, raw_path
                    )
            except Exception as exc:
                starter_ok = False
                starter_result = {
                    "status": "STARTER_MODULE_EXCEPTION",
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                }

            self._rolling_cleanup()

            if not starter_ok:
                if self.stop_event.is_set():
                    self._log("MANUAL_STOP", boundary="during_starter_sequence")
                    self._finish("IDLE", "MANUAL_STOP_DURING_STARTER")
                    inputs.close()
                    self.inputs = None
                    return
                duration = round(time.monotonic() - attempt_started, 3)
                self.attempts.append({
                    "attempt": self.current_attempt,
                    "status": starter_result.get(
                        "status", "STARTER_ROUTE_FAIL"
                    ),
                    "duration_s": duration,
                    "starter_result": starter_result,
                    "next_reset_authorized": False,
                })
                self._log(
                    "SAFETY_HOLD",
                    attempt=self.current_attempt,
                    status=self.attempts[-1]["status"],
                )
                self._finish("SAFETY HOLD", self.attempts[-1]["status"])
                return

            pk6 = starter_result["pk6"]
            duration = round(time.monotonic() - attempt_started, 3)
            self.durations.append(duration)
            target_result = evaluate_target(pk6, self.target_criteria)
            if target_result.get("enabled"):
                self._log(
                    "TARGET_CHECK",
                    attempt=self.current_attempt,
                    match=bool(target_result.get("match")),
                    hidden_power=(
                        f"{target_result.get('hidden_power_type')} "
                        f"{target_result.get('hidden_power_power')}"
                    ),
                    checks=target_result.get("checks"),
                )

            item = {
                "attempt": self.current_attempt,
                "status": starter_result["status"],
                "duration_s": duration,
                "species": pk6["species"],
                "species_name": pk6["species_name"],
                "checksum_valid": pk6["checksum_valid"],
                "tid_sid_match": starter_result.get("tid_sid_match", False),
                "pokemon_pid": pk6["pid"],
                "ec": pk6["ec"],
                "shiny_xor": int(pk6["shiny_xor"]),
                "is_shiny": bool(pk6["is_shiny"]),
                "nature": pk6.get("nature", "—"),
                "ability_id": pk6.get("ability_id"),
                "ability": ability_name(pk6.get("ability_id")),
                "gender": pk6.get("gender", "—"),
                "form": pk6.get("form", 0),
                "ivs": pk6.get("ivs", {}),
                "raw_sha256": pk6["raw_sha256"],
                "target_mode": bool(target_result.get("enabled")),
                "target_match": bool(target_result.get("match")),
                "target_checks": target_result.get("checks", {}),
                "hidden_power": target_result.get("hidden_power_type"),
                "hidden_power_power": target_result.get("hidden_power_power", 60),
                "old_game_pid": reset_result.get("old_pid"),
                "new_game_pid": reset_result.get("new_pid"),
                "title_inputs": reset_result.get("title_inputs"),
                "next_reset_authorized": (
                    starter_result["status"] == "PASS"
                    and not pk6["is_shiny"]
                    and not bool(target_result.get("match"))
                    and (
                        game_profile.get("family") != "xy"
                        or self.starter_key in {"chespin", "fennekin", "froakie"}
                    )
                ),
            }
            self.attempts.append(item)
            try:
                if self.random_mode:
                    self.lifetime["lifetime_hunt_seconds"] = round(
                        float(self.lifetime.get("lifetime_hunt_seconds", 0.0)) + duration, 3
                    )
                self._note_valid(pk6, duration_s=duration)
            except Exception as exc:
                item["status"] = "PERSISTENCE_FAIL"
                item["next_reset_authorized"] = False
                item["persistence_error"] = f"{type(exc).__name__}: {exc}"
                item["persistence_traceback"] = traceback.format_exc()
                self._log(
                    "SAFETY_HOLD",
                    attempt=self.current_attempt,
                    status="PERSISTENCE_FAIL",
                    error=item["persistence_error"],
                )
                self._finish("SAFETY HOLD", "PERSISTENCE_FAIL")
                return

            starter_frame_meta = None
            if bool(pk6.get("is_shiny")):
                starter_frame_meta = self._capture_starter_shiny_framebuffer(pk6)
                if starter_frame_meta:
                    item["framebuffer_path"] = starter_frame_meta["path"]
                    item["framebuffer_capture"] = dict(starter_frame_meta)

            ivs = pk6.get("ivs", {})

            last_seen_payload = {
                "attempt": self.current_attempt,
                "species": int(pk6.get("species", 0)),
                "species_name": pk6.get("species_name", "—"),
                "nature": pk6.get("nature", "—"),
                "ability_id": pk6.get("ability_id"),
                "ability": ability_name(pk6.get("ability_id")),
                "gender": pk6.get("gender", "—"),
                "pokemon_pid": pk6.get("pid", "—"),
                "ec": pk6.get("ec", "—"),
                "tid": pk6.get("tid"),
                "sid": pk6.get("sid"),
                "shiny_xor": int(pk6.get("shiny_xor", 0)),
                "is_shiny": bool(pk6.get("is_shiny")),
                "ivs": ivs,
                "hidden_power": target_result.get("hidden_power_type"),
                "hidden_power_power": target_result.get("hidden_power_power", 60),
                "target_mode": bool(target_result.get("enabled")),
                "target_match": bool(target_result.get("match")),
                "target_checks": target_result.get("checks", {}),
                "framebuffer_path": (
                    starter_frame_meta["path"] if starter_frame_meta else None
                ),
                "time": datetime.now().astimezone().isoformat(
                    timespec="seconds"
                ),
            }
            self._save_last_seen_entry(last_seen_payload)
            self.last_seen_entry.emit(last_seen_payload)

            self.encounter.emit({
                **item,
                "level": 5,
                "form": pk6.get("form", 0),
                "gender": pk6.get("gender", "—"),
                "ability_id": pk6.get("ability_id"),
                "ability": ability_name(pk6.get("ability_id")),
                "hidden_power": target_result.get("hidden_power_type") or hidden_power_from_ivs(ivs),
                "hidden_power_power": target_result.get("hidden_power_power", 60),
                "tid": pk6.get("tid"),
                "sid": pk6.get("sid"),
                "validation": VALIDATION[self.starter_key],
            })

            # One bounded party snapshot for the dashboard. Authority has
            # already completed; this is display telemetry only.
            try:
                if game_profile.get("family") == "xy":
                    from pokebot.common.xy_ram import read_party_decoded
                    party_payload = payload_from_parsed(read_party_decoded(bridge))
                else:
                    party_payload = build_party_payload(bridge)
            except Exception as exc:
                self._log(
                    "PARTY_SNAPSHOT_FAILED",
                    error=f"{type(exc).__name__}: {exc}",
                )
                party_payload = []
            self.party.emit(party_payload)

            self._emit_stats()

            self._log(
                "ATTEMPT_PASS",
                attempt=self.current_attempt,
                duration_s=duration,
                species=pk6["species"],
                pokemon_pid=pk6["pid"],
                shiny_xor=pk6["shiny_xor"],
                is_shiny=pk6["is_shiny"],
                target_match=bool(target_result.get("match")),
                next_reset_authorized=item["next_reset_authorized"],
            )

            # A real shiny remains an absolute HOLD even if a different target
            # filter was requested. Target mode never weakens shiny safety.
            if pk6["is_shiny"] or starter_result["status"] == "SHINY_HOLD_PASS":
                self._save_recent_shiny(
                    pk6,
                    self.current_attempt,
                    framebuffer_path=(
                        starter_frame_meta["path"] if starter_frame_meta else None
                    ),
                )
                self._finish("SHINY HOLD", "SHINY_DETECTED")
                return

            if bool(target_result.get("match")):
                self._log(
                    "TARGET_MATCH",
                    attempt=self.current_attempt,
                    species=pk6.get("species_name"),
                    pokemon_pid=pk6.get("pid"),
                    nature=pk6.get("nature"),
                    gender=pk6.get("gender"),
                    hidden_power=(
                        f"{target_result.get('hidden_power_type')} "
                        f"{target_result.get('hidden_power_power')}"
                    ),
                    ivs=pk6.get("ivs"),
                )
                self._finish("TARGET HOLD", "TARGET_MATCH")
                return

            # Stop requested while the starter sequence was in progress:
            # encounter authority has completed; do not send the next reset.
            if self.stop_event.is_set():
                self._log("MANUAL_STOP", boundary="after_valid_pk6")
                self._finish("IDLE", "MANUAL_STOP_AFTER_ENCOUNTER")
                return

            # Chespin, Fennekin, and Froakie are now hardware-proven end-to-end
            # with the acknowledged 4952 triple-touch chooser path. A clean,
            # non-shiny, non-target PASS may therefore authorize the next reset.
            if game_profile.get("family") == "xy":
                self._log(
                    "XY_KALOS_CONTINUOUS_HUNT_AUTHORIZED",
                    attempt=self.current_attempt,
                    starter=self.meta.get("name"),
                    species=pk6.get("species"),
                    next_reset_authorized=item["next_reset_authorized"],
                )

            if not item["next_reset_authorized"]:
                self._finish("SAFETY HOLD", "NEXT_RESET_NOT_AUTHORIZED")
                return

            # Match the exact controller boundary that passed the dedicated
            # Torchic 10/10 test: verify the Pokebot-Luma controller remains
            # responsive and force a known-neutral HID state before the next
            # unlimited reset cycle.
            try:
                controller_info = inputs.input_ping()
                neutral = inputs.release_all()
                self._log(
                    "INTER_CYCLE_CONTROLLER_OK",
                    attempt=self.current_attempt,
                    controller="Pokebot-Luma acknowledged input UDP 4952",
                    runtime_flags=f"0x{controller_info.get('runtime_flags', 0):08X}",
                    raw_hid=f"0x{neutral.get('raw_hid', 0):03X}",
                )
            except Exception as exc:
                self._log(
                    "SAFETY_HOLD",
                    attempt=self.current_attempt,
                    status="INTER_CYCLE_CONTROLLER_FAIL",
                    error=f"{type(exc).__name__}: {exc}",
                )
                self._finish("SAFETY HOLD", "INTER_CYCLE_CONTROLLER_FAIL")
                return

            time.sleep(0.25)
