from __future__ import annotations

"""Alpha Sapphire 1.4 best-ball policy and Balls-pocket RAM authority.

This module is intentionally read-only.  It consumes the Balls-pocket structure
proved on hardware by the v0p42ZZ/v0p43AB probes and ranks only catch modifiers
whose prerequisites are already authoritative in the live worker.

Master Ball policy is deliberately strict:
  * forbidden for ordinary targets;
  * eligible only for Legendary/Mythical species or a target whose current
    four moves include a known recoil/crash/self-KO/perish risk;
  * if eligible and present, it outranks every other Ball.
"""

from collections import deque
import struct
import time


BALL_IDS = {
    "master ball": 1,
    "ultra ball": 2,
    "great ball": 3,
    "poke ball": 4,
    "safari ball": 5,
    "net ball": 6,
    "dive ball": 7,
    "nest ball": 8,
    "repeat ball": 9,
    "timer ball": 10,
    "luxury ball": 11,
    "premier ball": 12,
    "dusk ball": 13,
    "heal ball": 14,
    "quick ball": 15,
    "cherish ball": 16,
}
ID_TO_BALL = {v: k for k, v in BALL_IDS.items()}

# User-selectable override choices. "best" preserves the existing automatic
# ranking policy byte-for-byte. Safari/Cherish are intentionally not exposed:
# they are not normal ORAS wild-capture inventory choices.
BALL_OVERRIDE_KEYS = (
    "best",
    "poke ball",
    "great ball",
    "ultra ball",
    "net ball",
    "dive ball",
    "nest ball",
    "repeat ball",
    "timer ball",
    "luxury ball",
    "premier ball",
    "dusk ball",
    "heal ball",
    "quick ball",
    "master ball",
)


def normalize_ball_override(value) -> str:
    key = str(value or "best").strip().lower()
    return key if key in BALL_OVERRIDE_KEYS else "best"


# Hardware-proven v0p43AB controller/cursor layout.
CONTROLLER_ENTRY_COUNT_OFF = 0x200
CONTROLLER_ENTRY_ARRAY_OFF = 0x204
CONTROLLER_PAGE_OFF = 0x30C
CURSOR_LOCAL_SLOT_OFF = 0x14
ENTRY_SIZE = 4
MAX_ENTRIES = 18
SLOTS_PER_PAGE = 6
ROWS = 3
COLS = 2

# National Dex IDs through Gen 6.  Mythicals are deliberately included in the
# protected Legendary class because the user's policy is preservation-first.
LEGENDARY_OR_MYTHICAL_SPECIES = {
    144, 145, 146, 150, 151,
    243, 244, 245, 249, 250, 251,
    377, 378, 379, 380, 381, 382, 383, 384, 385, 386,
    480, 481, 482, 483, 484, 485, 486, 487, 488, 489, 490, 491, 492, 493,
    494, 638, 639, 640, 641, 642, 643, 644, 645, 646, 647, 648, 649,
    716, 717, 718, 719, 720, 721,
}

# Current-move IDs that create a credible self-loss risk in Gen 6.
# Recoil/crash moves include any move that can directly damage its user.
# Self-KO moves immediately faint the user; Perish Song is an eventual
# self-faint countdown and is treated as equally preservation-critical.
SELF_LOSS_RISK_MOVES = {
    26: ("Jump Kick", "crash damage"),
    36: ("Take Down", "recoil"),
    38: ("Double-Edge", "recoil"),
    66: ("Submission", "recoil"),
    120: ("Self-Destruct", "self-KO"),
    136: ("High Jump Kick", "crash damage"),
    153: ("Explosion", "self-KO"),
    165: ("Struggle", "recoil"),
    195: ("Perish Song", "eventual self-KO"),
    262: ("Memento", "self-KO"),
    344: ("Volt Tackle", "recoil"),
    361: ("Healing Wish", "self-KO"),
    394: ("Flare Blitz", "recoil"),
    413: ("Brave Bird", "recoil"),
    452: ("Wood Hammer", "recoil"),
    457: ("Head Smash", "recoil"),
    461: ("Lunar Dance", "self-KO"),
    515: ("Final Gambit", "self-KO"),
    528: ("Wild Charge", "recoil"),
    543: ("Head Charge", "recoil"),
    617: ("Light of Ruin", "recoil"),
}

# Tie-breaking when two proven modifiers are equal.  Ordinary Poke Balls are
# preferred over cosmetic 1x Balls so special stock is not burned needlessly.
CONSERVATION_ORDER = {
    4: 0,   # Poke Ball
    12: 1,  # Premier Ball
    14: 2,  # Heal Ball
    11: 3,  # Luxury Ball
    15: 4,
    9: 5,
    6: 6,
    8: 7,
    7: 8,
    13: 9,
    10: 10,
    3: 11,
    2: 12,
    1: 99,
}


def _valid_heap_ptr(value: int) -> bool:
    return 0x08000000 <= int(value) < 0x10000000 and (int(value) & 3) == 0


def read_balls_state(br, cursor_reader) -> dict:
    """Read live Balls-pocket entries plus selected page/slot/item."""
    cur = cursor_reader(br)
    if not (cur.get("valid") and cur.get("bag_state") == 2):
        raise RuntimeError(f"Balls-pocket authority is not active: {cur}")

    controller = int(cur["controller"], 16)
    cursor = int(cur["cursor"], 16)
    if not _valid_heap_ptr(controller) or not _valid_heap_ptr(cursor):
        raise RuntimeError(f"invalid Balls-pocket controller/cursor pointers: {cur}")

    entry_count = struct.unpack("<H", br.read(controller + CONTROLLER_ENTRY_COUNT_OFF, 2))[0]
    if not (1 <= entry_count <= MAX_ENTRIES):
        raise RuntimeError(f"unexpected Balls-pocket entry_count={entry_count}")

    raw = br.read(controller + CONTROLLER_ENTRY_ARRAY_OFF, entry_count * ENTRY_SIZE)
    entries = []
    for index in range(entry_count):
        item_id, quantity = struct.unpack_from("<HH", raw, index * ENTRY_SIZE)
        entries.append({
            "index": index,
            "page": index // SLOTS_PER_PAGE,
            "slot": index % SLOTS_PER_PAGE,
            "item_id": int(item_id),
            "ball_name": ID_TO_BALL.get(int(item_id), f"item {item_id}"),
            "quantity": int(quantity),
        })

    page = int(br.read(controller + CONTROLLER_PAGE_OFF, 1)[0])
    raw_cursor_slot = int(br.read(cursor + CURSOR_LOCAL_SLOT_OFF, 1)[0])

    # Hardware 2026-08-27: after a failed Horde capture, the game-consumed
    # Bag-state-1 handoff can reopen the Balls pocket with cursor+0x14 encoded
    # as the global entry index rather than the normal page-local 0..5 slot.
    # Example observed on Ball 2: page=1, raw=8, entry_count=14.  Treat that
    # form as global only when it is self-consistent with the independently
    # read controller page.  Otherwise fail closed; never guess a cursor cell.
    max_page = (int(entry_count) - 1) // SLOTS_PER_PAGE
    if not (0 <= page <= max_page):
        raise RuntimeError(
            f"unexpected Balls-pocket page={page} for entry_count={entry_count}"
        )
    if 0 <= raw_cursor_slot < SLOTS_PER_PAGE:
        local_slot = raw_cursor_slot
        cursor_slot_encoding = "PAGE_LOCAL"
    elif (
        0 <= raw_cursor_slot < int(entry_count)
        and raw_cursor_slot // SLOTS_PER_PAGE == page
    ):
        local_slot = raw_cursor_slot % SLOTS_PER_PAGE
        cursor_slot_encoding = "GLOBAL_INDEX_NORMALIZED"
    else:
        raise RuntimeError(
            "unexpected Balls-pocket cursor+0x14 encoding; no navigation authorized: "
            f"page={page} raw={raw_cursor_slot} entry_count={entry_count}"
        )

    selected_index = page * SLOTS_PER_PAGE + local_slot
    selected = entries[selected_index] if 0 <= selected_index < len(entries) else None
    return {
        "entry_count": entry_count,
        "controller": controller,
        "cursor": cursor,
        "page": page,
        "local_slot": local_slot,
        "raw_cursor_slot": raw_cursor_slot,
        "cursor_slot_encoding": cursor_slot_encoding,
        "selected_index": selected_index,
        "selected": selected,
        "entries": entries,
        "selector_u16": cur.get("selector_u16"),
        "selector_bytes": cur.get("selector_bytes"),
    }


def master_ball_policy(target: dict | None) -> dict:
    target = dict(target or {})
    species = int(target.get("species") or 0)
    moves = [int(x or 0) for x in (target.get("moves") or [])]
    legendary = species in LEGENDARY_OR_MYTHICAL_SPECIES
    risky = []
    for move_id in moves:
        rec = SELF_LOSS_RISK_MOVES.get(move_id)
        if rec:
            risky.append({"move_id": move_id, "move_name": rec[0], "risk": rec[1]})

    allowed = bool(legendary or risky)
    if legendary and risky:
        reason = "legendary_or_mythical_and_self_loss_move"
    elif legendary:
        reason = "legendary_or_mythical_species"
    elif risky:
        reason = "self_loss_move_detected"
    else:
        reason = "ordinary_target_master_ball_forbidden"
    return {
        "allowed": allowed,
        "reason": reason,
        "species": species,
        "legendary_or_mythical": legendary,
        "moves": moves,
        "risk_moves": risky,
    }


def proven_multiplier(item_id: int, *, throw_index: int, method_key: str = "", environment: str = "") -> tuple[float, str]:
    """Return a conservative Gen-6 catch modifier and its authority reason."""
    item_id = int(item_id)
    throw_index = max(1, int(throw_index))
    method = str(method_key or "").strip().lower()
    env = str(environment or "").strip().lower()

    if item_id == 15:  # Quick Ball
        if throw_index == 1:
            return 5.0, "quick_ball_first_turn_gen6"
        return 1.0, "quick_ball_after_first_turn"
    if item_id == 10:  # Timer Ball, Gen V+
        turns_passed = max(0, throw_index - 1)
        mult = min(4.0, 1.0 + turns_passed * (1229.0 / 4096.0))
        return mult, f"timer_ball_turns_passed_{turns_passed}"
    if item_id == 13:  # Dusk Ball
        if method in {"cave", "cave_run", "cave_bunny"} or "cave" in env:
            return 3.5, "dusk_ball_cave_gen6"
        return 1.0, "dusk_bonus_not_ram_proven"
    if item_id == 7:  # Dive Ball
        if method in {"surf", "fishing"} or any(x in env for x in ("water", "ocean", "surf", "fishing")):
            return 3.5, "dive_ball_water_method_gen6"
        return 1.0, "dive_bonus_not_proven"
    if item_id == 2:
        return 2.0, "ultra_ball"
    if item_id == 3:
        return 1.5, "great_ball"
    if item_id == 9:
        return 1.0, "repeat_caught_status_not_mapped"
    if item_id == 6:
        return 1.0, "net_target_type_not_mapped"
    if item_id == 8:
        return 1.0, "nest_target_level_not_mapped"
    if item_id in {4, 11, 12, 14}:
        return 1.0, "standard_1x_ball"
    return 0.0, "unsupported_or_unusable_ball"


def choose_best_ball(entries: list[dict], *, throw_index: int, target: dict | None, method_key: str = "", environment: str = "") -> dict:
    """Choose the highest proven effective held Ball under Master safety policy."""
    master = master_ball_policy(target)
    candidates = []
    for entry in entries:
        item_id = int(entry.get("item_id") or 0)
        quantity = int(entry.get("quantity") or 0)
        if quantity <= 0:
            continue
        if item_id == 1:
            if not master["allowed"]:
                candidates.append({**entry, "eligible": False, "multiplier": None,
                                   "reason": "master_ball_forbidden_for_target"})
                continue
            candidates.append({**entry, "eligible": True, "multiplier": 999.0,
                               "reason": "master_ball_eligible_preservation_override"})
            continue
        mult, reason = proven_multiplier(
            item_id,
            throw_index=throw_index,
            method_key=method_key,
            environment=environment,
        )
        candidates.append({**entry, "eligible": mult > 0.0, "multiplier": mult, "reason": reason})

    eligible = [c for c in candidates if c.get("eligible")]
    if not eligible:
        raise RuntimeError(
            "no eligible non-Master Ball is available and Master Ball is forbidden/unavailable"
        )

    # Highest modifier wins; for equal multipliers preserve special 1x stock,
    # then prefer larger stock count to reduce depletion risk.
    chosen = max(
        eligible,
        key=lambda c: (
            float(c.get("multiplier") or 0.0),
            -int(CONSERVATION_ORDER.get(int(c.get("item_id") or 0), 50)),
            int(c.get("quantity") or 0),
        ),
    )
    return {
        "chosen": chosen,
        "candidates": candidates,
        "master_policy": master,
        "throw_index": int(throw_index),
        "method_key": method_key,
        "environment": environment,
    }


def choose_ball(entries: list[dict], *, throw_index: int, target: dict | None,
                method_key: str = "", environment: str = "",
                ball_override: str = "best") -> dict:
    """Choose either the existing Best Ball policy or one exact user override.

    An explicit override NEVER falls back to another Ball. If the requested Ball
    is unavailable/depleted, selection fails closed before navigation or A.
    Master Ball remains protected in automatic Best Ball mode; selecting Master
    Ball explicitly is the user's deliberate override authority.
    """
    override = normalize_ball_override(ball_override)
    if override == "best":
        out = choose_best_ball(
            entries, throw_index=throw_index, target=target,
            method_key=method_key, environment=environment,
        )
        out["selection_mode"] = "best"
        out["ball_override"] = "best"
        return out

    item_id = int(BALL_IDS[override])
    held = [
        dict(entry) for entry in entries
        if int(entry.get("item_id") or 0) == item_id
        and int(entry.get("quantity") or 0) > 0
    ]
    if not held:
        raise RuntimeError(
            f"selected Ball override {override.title()} is unavailable or depleted; "
            "no fallback Ball is authorized"
        )
    # The Balls pocket should contain one entry per item ID. If corrupted/odd,
    # require an exact unique live entry rather than guessing between duplicates.
    if len(held) != 1:
        raise RuntimeError(
            f"selected Ball override {override.title()} has {len(held)} live entries; "
            "exact selection is ambiguous"
        )
    chosen = held[0]
    if item_id == 1:
        mult, reason = 999.0, "explicit_user_master_ball_override"
    else:
        mult, base_reason = proven_multiplier(
            item_id, throw_index=throw_index, method_key=method_key,
            environment=environment,
        )
        reason = f"explicit_user_ball_override; {base_reason}"
    chosen.update({
        "eligible": True,
        "multiplier": float(mult),
        "reason": reason,
    })
    return {
        "chosen": chosen,
        "candidates": [chosen],
        "master_policy": master_ball_policy(target),
        "throw_index": int(throw_index),
        "method_key": method_key,
        "environment": environment,
        "selection_mode": "override",
        "ball_override": override,
    }


def _neighbors(page: int, slot: int, entry_count: int):
    """Yield valid live-grid neighbors as (direction, next_page, next_slot)."""
    row, col = divmod(slot, COLS)
    possibilities = []
    if row > 0:
        possibilities.append(("UP", page, slot - COLS))
    if row < ROWS - 1:
        possibilities.append(("DOWN", page, slot + COLS))
    if col == 0:
        possibilities.append(("RIGHT", page, slot + 1))
        if page > 0:
            possibilities.append(("LEFT", page - 1, slot))
    else:
        possibilities.append(("LEFT", page, slot - 1))
        possibilities.append(("RIGHT", page + 1, slot))

    for direction, np, ns in possibilities:
        if np < 0 or ns < 0 or ns >= SLOTS_PER_PAGE:
            continue
        idx = np * SLOTS_PER_PAGE + ns
        if 0 <= idx < int(entry_count):
            yield direction, np, ns


def plan_path(start_page: int, start_slot: int, target_page: int, target_slot: int, entry_count: int) -> list[dict]:
    """Shortest path over only existing Balls; never routes through empty slots."""
    start = (int(start_page), int(start_slot))
    target = (int(target_page), int(target_slot))
    if start == target:
        return []
    q = deque([start])
    parent = {start: None}
    edge = {}
    while q:
        cur = q.popleft()
        for direction, np, ns in _neighbors(cur[0], cur[1], entry_count):
            nxt = (np, ns)
            if nxt in parent:
                continue
            parent[nxt] = cur
            edge[nxt] = direction
            if nxt == target:
                q.clear()
                break
            q.append(nxt)
    if target not in parent:
        raise RuntimeError(
            f"no safe Balls-pocket path from page/slot {start} to {target} with entry_count={entry_count}"
        )
    rev = []
    cur = target
    while parent[cur] is not None:
        rev.append({
            "direction": edge[cur],
            "expected_page": cur[0],
            "expected_slot": cur[1],
            "expected_index": cur[0] * SLOTS_PER_PAGE + cur[1],
        })
        cur = parent[cur]
    return list(reversed(rev))


def wait_for_position(br, cursor_reader, *, expected_page: int, expected_slot: int, check_stop, timeout: float = 3.0) -> dict:
    deadline = time.monotonic() + float(timeout)
    stable = 0
    last_key = None
    last = None
    while time.monotonic() < deadline:
        check_stop()
        last = read_balls_state(br, cursor_reader)
        key = (
            last["page"], last["local_slot"], last["selected_index"],
            (last.get("selected") or {}).get("item_id"),
            (last.get("selected") or {}).get("quantity"),
        )
        if last["page"] == expected_page and last["local_slot"] == expected_slot:
            stable = stable + 1 if key == last_key else 1
            if stable >= 2:
                return last
        else:
            stable = 0
        last_key = key
        time.sleep(0.08)
    raise RuntimeError(
        f"timeout waiting for Balls-pocket page={expected_page} slot={expected_slot}; last={last}"
    )
