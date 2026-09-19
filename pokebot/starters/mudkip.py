from __future__ import annotations

from pokebot.common.oras_ram import (
    PARTY0,
    PK6_SIZE,
    bounded_battle_gate,
    bounded_gate,
    read_trainer_ids,
)
from pokebot.common.pk6 import parse_pk6

NAME = "Mudkip"
SPECIES = 258
STATUS = "PORT_IN_VALIDATION"

# Mudkip owns all of these values.
# Torchic.py and Treecko.py are not imported here.
CHOOSER_SLOT = 3
MIDDLE_SLOT = 1
TARGET_SLOT = 2

PROC_READY = 6
LOWER_SELECT = 2
LOWER_CONFIRM = 6

# Initial conservative delays. If Mudkip needs adjustment, only this file changes.
CHOOSER_DELAYS = (2.8, 0.45, 0.45, 0.55)
MIDDLE_DELAYS = (0.10, 0.20, 0.35)
MUDKIP_DELAYS = (0.10, 0.20, 0.35)
CONFIRM_DELAYS = (0.20, 0.20, 0.25, 0.35)
BATTLE_DELAYS = (1.50, 1.25, 1.50, 2.00)

A_PULSE = dict(
    hold_ms=300,
    resume_settle_ms=220,
    packet_interval_ms=30,
    release_ms=120,
)

RIGHT_PULSE = dict(
    hold_ms=300,
    resume_settle_ms=220,
    packet_interval_ms=30,
    release_ms=120,
)


def _base_ready(s):
    return (
        s["proc"]["vptr_matches"]
        and s["proc"]["view_matches"]
        and s["proc"]["completion_flag"] == 0
        and s["proc"]["main_state"] == PROC_READY
        and s["lower"]["vptr_matches"]
        and s["lower"]["state"] == LOWER_SELECT
    )


def _chooser(s):
    return _base_ready(s) and s["lower"]["selected_slot"] == CHOOSER_SLOT


def _middle(s):
    return _base_ready(s) and s["lower"]["selected_slot"] == MIDDLE_SLOT


def _mudkip(s):
    return _base_ready(s) and s["lower"]["selected_slot"] == TARGET_SLOT


def _confirm(s):
    ptr = int(s["lower"]["confirm_pointer"], 16)
    return (
        s["proc"]["vptr_matches"]
        and s["proc"]["view_matches"]
        and s["proc"]["completion_flag"] == 0
        and s["proc"]["main_state"] == PROC_READY
        and s["lower"]["vptr_matches"]
        and s["lower"]["state"] == LOWER_CONFIRM
        and s["lower"]["selected_slot"] == TARGET_SLOT
        and ptr != 0
    )


def run_from_bag(ctx, raw_path):
    b, i, log = ctx.bridge, ctx.inputs, ctx.log

    # Bag -> chooser.
    i.pulse(("A",), **A_PULSE)
    ok, chooser = bounded_gate(
        b,
        CHOOSER_DELAYS,
        _chooser,
        log,
        "MUDKIP_CHOOSER_GATE",
    )
    if not ok:
        return False, {
            "status": "MUDKIP_CHOOSER_GATE_FAIL",
            "observations": chooser,
        }

    # Establish default middle slot1 first.
    i.pulse(("A",), **A_PULSE)
    ok, middle = bounded_gate(
        b,
        MIDDLE_DELAYS,
        _middle,
        log,
        "MUDKIP_MIDDLE_SLOT1_GATE",
    )
    if not ok:
        return False, {
            "status": "MUDKIP_MIDDLE_SLOT1_GATE_FAIL",
            "observations": middle,
        }

    # Mudkip-specific movement: exactly one RIGHT from middle slot1.
    i.pulse(("RIGHT",), **RIGHT_PULSE)
    ok, target = bounded_gate(
        b,
        MUDKIP_DELAYS,
        _mudkip,
        log,
        "MUDKIP_SLOT2_GATE",
    )
    if not ok:
        return False, {
            "status": "MUDKIP_SLOT2_GATE_FAIL",
            "observations": target,
        }

    # Capture trainer identity before selecting Mudkip.
    tid, sid = read_trainer_ids(b)

    # RAM slot2 authority is required before selection A.
    i.pulse(("A",), **A_PULSE)
    ok, confirm = bounded_gate(
        b,
        CONFIRM_DELAYS,
        _confirm,
        log,
        "MUDKIP_CONFIRM_GATE",
    )
    if not ok:
        return False, {
            "status": "MUDKIP_CONFIRM_GATE_FAIL",
            "observations": confirm,
        }

    # Require the confirmation object itself to be readable.
    confirm_ptr = int(confirm[-1]["lower"]["confirm_pointer"], 16)
    b.read(confirm_ptr, 4)

    # YES is default-selected in the validated confirmation state.
    i.pulse(("A",), **A_PULSE)

    ok, battle = bounded_battle_gate(
        b,
        BATTLE_DELAYS,
        log,
    )
    if not ok:
        return False, {
            "status": "MUDKIP_BATTLE_GATE_FAIL",
            "observations": battle,
        }

    # Exactly one authoritative party0 read.
    raw = b.read(PARTY0, PK6_SIZE)
    raw_path.write_bytes(raw)

    pk6 = parse_pk6(raw, {SPECIES: NAME})

    authority = (
        pk6["valid"]
        and pk6["checksum_valid"]
        and pk6["species"] == SPECIES
        and pk6["tid"] == tid
        and pk6["sid"] == sid
    )

    if not authority:
        return False, {
            "status": "MUDKIP_PK6_AUTHORITY_FAIL",
            "pk6": pk6,
            "expected_species": SPECIES,
            "expected_name": NAME,
            "expected_tid": tid,
            "expected_sid": sid,
        }

    return True, {
        "status": "SHINY_HOLD_PASS" if pk6["is_shiny"] else "PASS",
        "pk6": pk6,
        "tid_sid_match": True,
    }
