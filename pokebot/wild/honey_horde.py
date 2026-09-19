from __future__ import annotations

"""Read-only ORAS Honey inventory + field-Bag authority helpers.

Honey is item 94 (0x005E) in the normal Items pocket.  This module never
writes game RAM.  It reads the save-backed Items pocket to locate Honey by
item ID, then can locate the live DllBag controller by matching that exact
inventory sequence in heap RAM.

The field-Bag cursor authority is intentionally stricter than a blind D-pad
route: an item-use A is authorized only when the live controller says the
selected entry is Honey.
"""

import struct

HONEY_ITEM_ID = 94

# ORAS 1.4 live save backing.  The already-proven Key Items live base is
# 0x08C6F2B0 and the ORAS save layout places Items at section+0x5800 and Key
# Items at section+0x5E40, giving 0x08C6EC70 for Items.
ORAS_ITEMS_ADDR = 0x08C6EC70
ORAS_ITEMS_SIZE = 0x434  # 269 x <HH> entries used by the normal Items pocket
ENTRY_SIZE = 4
MAX_ITEMS = ORAS_ITEMS_SIZE // ENTRY_SIZE

# Hardware-proven DllBag controller offsets from the battle Bag.  Field Bag
# must dynamically prove the same shape before these values are trusted.
CONTROLLER_CURSOR_PTR_OFF = 0xF4
CONTROLLER_ENTRY_COUNT_OFF = 0x200
CONTROLLER_ENTRY_ARRAY_OFF = 0x204
CONTROLLER_PAGE_OFF = 0x30C
CURSOR_SELECTOR_OFF = 0x14

# First search band contains the ORAS transient field/UI heap seen by the bot.
# If absent, the caller may request the bounded fallback band.
FAST_SCAN_START = 0x08D30000
FAST_SCAN_END = 0x08D90000
FALLBACK_SCAN_START = 0x08C68000
FALLBACK_SCAN_END = 0x08DF0000


def _read_chunked(br, address: int, length: int) -> bytes:
    out = bytearray()
    at = int(address)
    remaining = int(length)
    while remaining > 0:
        n = min(0x200, remaining)
        out.extend(br.read(at, n))
        at += n
        remaining -= n
    return bytes(out)


def _valid_heap_ptr(value: int) -> bool:
    return 0x08000000 <= int(value) < 0x09000000 and (int(value) & 3) == 0


def read_items_pocket(br) -> dict:
    raw = _read_chunked(br, ORAS_ITEMS_ADDR, ORAS_ITEMS_SIZE)
    entries = []
    active = []
    honey = []
    for index in range(MAX_ITEMS):
        item_id, quantity = struct.unpack_from("<HH", raw, index * ENTRY_SIZE)
        row = {
            "index": int(index),
            "item_id": int(item_id),
            "quantity": int(quantity),
        }
        entries.append(row)
        if item_id and quantity:
            active.append(row)
        if int(item_id) == HONEY_ITEM_ID and int(quantity) > 0:
            honey.append(row)
    return {
        "address": ORAS_ITEMS_ADDR,
        "size": ORAS_ITEMS_SIZE,
        "entries": entries,
        "active": active,
        "honey": honey,
        "raw": raw,
    }


def require_honey(br, *, first_active_slot: bool = False) -> dict:
    snap = read_items_pocket(br)
    honey = list(snap["honey"])
    if len(honey) != 1:
        raise RuntimeError(
            "HONEY PREFLIGHT: expected exactly one live Items-pocket Honey entry "
            f"(item 94); found {len(honey)}"
        )
    row = dict(honey[0])
    active = list(snap["active"])
    active_index = next(
        (i for i, item in enumerate(active) if int(item.get("item_id") or 0) == HONEY_ITEM_ID),
        None,
    )
    if active_index is None:
        raise RuntimeError("HONEY PREFLIGHT: Honey vanished from the active Items list")
    row.update({
        "address": ORAS_ITEMS_ADDR + int(row["index"]) * ENTRY_SIZE,
        "items_active_count": len(active),
        "active_index": int(active_index),
    })
    if first_active_slot and int(active_index) != 0:
        raise RuntimeError(
            "HONEY PREFLIGHT: Honey must be slot 1 (the top/first visible item) "
            f"of the Items pocket for fast Honey Horde mode; current active slot={int(active_index) + 1}"
        )
    return row


def _compact_active_raw(snapshot: dict) -> bytes:
    # DllBag controllers compact active entries. Preserve current Bag order and
    # exact quantities; this provides a far stronger signature than item IDs.
    return b"".join(
        struct.pack("<HH", int(r["item_id"]), int(r["quantity"]))
        for r in snapshot["active"]
    )


def _scan_pattern(br, start: int, end: int, needle: bytes):
    if len(needle) < 12:
        return []
    hits = []
    overlap = len(needle) - 1
    at = int(start)
    carry = b""
    while at < int(end):
        n = min(0x200, int(end) - at)
        try:
            chunk = br.read(at, n)
        except Exception:
            carry = b""
            at += n
            continue
        data = carry + chunk
        base = at - len(carry)
        pos = 0
        while True:
            found = data.find(needle, pos)
            if found < 0:
                break
            hits.append(base + found)
            pos = found + 1
        carry = data[-overlap:] if overlap else b""
        at += n
    return sorted(set(hits))


def _validate_controller(br, controller: int, expected_active: list[dict]) -> dict | None:
    try:
        count = struct.unpack("<H", br.read(controller + CONTROLLER_ENTRY_COUNT_OFF, 2))[0]
        if count != len(expected_active) or not (1 <= count <= MAX_ITEMS):
            return None
        raw = _read_chunked(br, controller + CONTROLLER_ENTRY_ARRAY_OFF, count * ENTRY_SIZE)
        expected = b"".join(
            struct.pack("<HH", int(r["item_id"]), int(r["quantity"]))
            for r in expected_active
        )
        if raw != expected:
            return None
        cursor = struct.unpack("<I", br.read(controller + CONTROLLER_CURSOR_PTR_OFF, 4))[0]
        if not _valid_heap_ptr(cursor):
            return None
        page = int(br.read(controller + CONTROLLER_PAGE_OFF, 1)[0])
        selector = int(br.read(cursor + CURSOR_SELECTOR_OFF, 1)[0])
        return {
            "controller": int(controller),
            "cursor": int(cursor),
            "entry_count": int(count),
            "page": int(page),
            "selector": int(selector),
            "entries": [dict(r) for r in expected_active],
        }
    except Exception:
        return None


def locate_items_controller(br, *, fallback: bool = False) -> dict:
    snapshot = read_items_pocket(br)
    active = list(snapshot["active"])
    if not active:
        raise RuntimeError("FIELD BAG: live Items pocket is empty")

    # A 6-entry exact id+quantity prefix is normally 24 bytes and highly
    # distinctive. If fewer than 6 entries exist, use all active entries.
    prefix_count = min(6, len(active))
    needle = b"".join(
        struct.pack("<HH", int(r["item_id"]), int(r["quantity"]))
        for r in active[:prefix_count]
    )
    bands = [(FAST_SCAN_START, FAST_SCAN_END)]
    if fallback:
        bands.append((FALLBACK_SCAN_START, FALLBACK_SCAN_END))

    raw_hits = []
    valid = []
    for start, end in bands:
        for hit in _scan_pattern(br, start, end, needle):
            # Ignore the live save copy itself and nearby false derivations.
            if ORAS_ITEMS_ADDR - 0x1000 <= hit <= ORAS_ITEMS_ADDR + ORAS_ITEMS_SIZE + 0x1000:
                continue
            raw_hits.append(hit)
            controller = int(hit) - CONTROLLER_ENTRY_ARRAY_OFF
            candidate = _validate_controller(br, controller, active)
            if candidate is not None:
                valid.append(candidate)
        if valid:
            break

    uniq = {int(v["controller"]): v for v in valid}
    valid = list(uniq.values())
    if len(valid) != 1:
        raise RuntimeError(
            "FIELD BAG ITEMS CONTROLLER: expected one exact live controller; "
            f"valid={len(valid)} raw_pattern_hits={len(set(raw_hits))} "
            f"scan={'fallback' if fallback else 'fast'}"
        )
    return valid[0]


def read_items_controller_state(br, authority: dict) -> dict:
    controller = int(authority["controller"])
    expected = list(authority["entries"])
    current = _validate_controller(br, controller, expected)
    if current is None:
        raise RuntimeError("FIELD BAG: cached Items controller lost exact inventory authority")
    selector = int(current["selector"])
    count = int(current["entry_count"])
    selected = expected[selector] if 0 <= selector < count else None
    return {
        **current,
        # Global is one known DllBag encoding, but field navigation must keep
        # alternative page/local models alive until movement proves them.
        "selected_index": selector if selected is not None else None,
        "selected": dict(selected) if selected is not None else None,
        "cursor_encoding": "UNRESOLVED_FIELD_DLLBAG",
    }


def honey_active_index(authority: dict) -> int:
    rows = [r for r in authority["entries"] if int(r["item_id"]) == HONEY_ITEM_ID and int(r["quantity"]) > 0]
    if len(rows) != 1:
        raise RuntimeError(f"FIELD BAG: expected one Honey in controller, got {len(rows)}")
    # authority entries are compact active entries, so find position by identity.
    target = rows[0]
    for index, row in enumerate(authority["entries"]):
        if int(row["item_id"]) == HONEY_ITEM_ID and int(row["quantity"]) == int(target["quantity"]):
            return int(index)
    raise RuntimeError("FIELD BAG: Honey active index disappeared")


def cursor_index_models(state: dict) -> dict[str, int]:
    """Return conservative candidate index interpretations for field DllBag.

    Battle DllBag has exposed both page-local and global selector encodings on
    hardware.  Field Bag is not yet hardware-mapped, so Honey navigation keeps
    all plausible models alive and authorizes Use only when the surviving
    models agree on the exact Honey entry.
    """
    count = int(state.get("entry_count") or 0)
    page = int(state.get("page") or 0)
    selector = int(state.get("selector") or 0)
    models = {}

    def add(name, value):
        value = int(value)
        if 0 <= value < count:
            models[name] = value

    add("selector_global", selector)
    add("page_plus_selector", page + selector)       # scroll-offset model
    add("page6_local", page * 6 + selector)
    add("page7_local", page * 7 + selector)
    add("page8_local", page * 8 + selector)
    add("page_global", page)
    return models
