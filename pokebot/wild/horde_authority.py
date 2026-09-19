from __future__ import annotations

"""Read-only ORAS opponent-set authority.

This module is intentionally separate from the frozen validated movement/run
backends.  It performs one bounded five-slot PK6 snapshot after the battle
presentation boundary and classifies the opponent set as either a normal
single wild Pokémon or a five-Pokémon Horde.

Hardware proof (Alpha Sapphire 1.4, 2026-08-21):
  slot0 0x081FFA6C
  stride 0x1E4 (484 bytes)
  slots 0..4 all valid/unique in a Wingull Horde
  slots 1..4 clear to the empty template in a subsequent normal battle
"""

HORDE_SLOT_COUNT = 5
HORDE_SLOT_STRIDE = 0x1E4


def _empty_slot_candidate(pk6: dict) -> bool:
    """Return True only for the hardware-observed cleared opponent template."""
    return (
        bool(pk6.get("checksum_valid"))
        and str(pk6.get("sanity")) == "0x0000"
        and int(pk6.get("species", -1)) == 0
        and str(pk6.get("ec")) == "0x00000000"
        and str(pk6.get("pid")) == "0x00000000"
        and int(pk6.get("tid", -1)) == 0
        and int(pk6.get("sid", -1)) == 0
    )


def read_opponent_set(br, core, save_tid: int, save_sid: int) -> dict:
    """Take one bounded five-slot snapshot and validate the opponent set.

    No polling and no controller input occur here.  Every slot is read exactly
    once.  Valid classifications are:
      - SINGLE: slot 0 valid, slots 1..4 are the cleared template
      - HORDE:  all five slots valid and all five identities unique

    Any partial/ambiguous/corrupt layout is fail-closed.
    """
    slots = []
    for slot in range(HORDE_SLOT_COUNT):
        address = int(core.WILD_PK6_ADDR) + slot * HORDE_SLOT_STRIDE
        raw = br.read(address, core.PK6_STORED_SIZE)
        pk6 = core.decode_stored_pk6(raw, save_tid, save_sid)
        slots.append({
            "slot": slot,
            "address": f"0x{address:08X}",
            "pk6": pk6,
            "occupied": bool(pk6.get("valid")),
            "empty": _empty_slot_candidate(pk6),
        })

    occupied = [s for s in slots if s["occupied"]]
    occupied_indexes = [int(s["slot"]) for s in occupied]
    identities = [str(s["pk6"].get("identity")) for s in occupied]
    unique_identities = len(set(identities)) == len(identities)

    # Single normal battle: hardware-proven empty template in slots 1..4.
    if occupied_indexes == [0] and all(s["empty"] for s in slots[1:]):
        return {
            "valid": True,
            "classification": "SINGLE",
            "horde": False,
            "size": 1,
            "slots": slots,
            "occupied": occupied,
            "occupied_indexes": occupied_indexes,
            "identities": identities,
        }

    # Horde: ORAS Horde battles contain five simultaneous opponents.
    if occupied_indexes == [0, 1, 2, 3, 4] and unique_identities:
        return {
            "valid": True,
            "classification": "HORDE",
            "horde": True,
            "size": 5,
            "slots": slots,
            "occupied": occupied,
            "occupied_indexes": occupied_indexes,
            "identities": identities,
        }

    reasons = []
    if not occupied:
        reasons.append("no valid opponent PK6 slots")
    if occupied and not unique_identities:
        reasons.append("duplicate opponent identities")
    for s in slots:
        if not s["occupied"] and not s["empty"]:
            reasons.append(
                f"slot{s['slot']} neither valid nor proven-empty: "
                f"{s['pk6'].get('reason')}"
            )
    if len(occupied) not in (1, 5):
        reasons.append(f"ambiguous occupied slot count {len(occupied)}")
    if len(occupied) == 1 and occupied_indexes != [0]:
        reasons.append(f"single occupied slot is {occupied_indexes}, expected [0]")

    return {
        "valid": False,
        "classification": "AMBIGUOUS",
        "horde": None,
        "size": len(occupied),
        "slots": slots,
        "occupied": occupied,
        "occupied_indexes": occupied_indexes,
        "identities": identities,
        "reason": "; ".join(reasons) or "unclassified opponent layout",
    }
