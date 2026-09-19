from __future__ import annotations

from pokebot.common.oras_ram import (
    PARTY0, PK6_SIZE, bounded_battle_gate, bounded_gate, read_trainer_ids
)
from pokebot.common.pk6 import parse_pk6

NAME = "Torchic"
SPECIES = 255
STATUS = "LOCKED_10_OF_10"

# Torchic owns all of its own starter-specific states/timings.
CHOOSER_SLOT = 3
TARGET_SLOT = 1
PROC_READY = 6
LOWER_SELECT = 2
LOWER_CONFIRM = 6

CHOOSER_DELAYS = (2.8, 0.45, 0.45, 0.55)
TARGET_DELAYS = (0.10, 0.20, 0.35)
CONFIRM_DELAYS = (0.20, 0.20, 0.25, 0.35)
BATTLE_DELAYS = (1.50, 1.25, 1.50, 2.00)

PULSE = dict(
    hold_ms=300,
    resume_settle_ms=220,
    packet_interval_ms=30,
    release_ms=120,
)

def _chooser(s):
    return (
        s["proc"]["vptr_matches"]
        and s["proc"]["view_matches"]
        and s["proc"]["completion_flag"] == 0
        and s["proc"]["main_state"] == PROC_READY
        and s["lower"]["vptr_matches"]
        and s["lower"]["state"] == LOWER_SELECT
        and s["lower"]["selected_slot"] == CHOOSER_SLOT
    )

def _target(s):
    return (
        s["proc"]["vptr_matches"]
        and s["proc"]["view_matches"]
        and s["proc"]["completion_flag"] == 0
        and s["proc"]["main_state"] == PROC_READY
        and s["lower"]["vptr_matches"]
        and s["lower"]["state"] == LOWER_SELECT
        and s["lower"]["selected_slot"] == TARGET_SLOT
    )

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

    i.pulse(("A",), **PULSE)
    ok, chooser = bounded_gate(b, CHOOSER_DELAYS, _chooser, log, "TORCHIC_CHOOSER_GATE")
    if not ok:
        return False, {"status": "TORCHIC_CHOOSER_GATE_FAIL", "observations": chooser}

    i.pulse(("A",), **PULSE)
    ok, target = bounded_gate(b, TARGET_DELAYS, _target, log, "TORCHIC_SLOT1_GATE")
    if not ok:
        return False, {"status": "TORCHIC_SLOT1_GATE_FAIL", "observations": target}

    tid, sid = read_trainer_ids(b)

    i.pulse(("A",), **PULSE)
    ok, confirm = bounded_gate(b, CONFIRM_DELAYS, _confirm, log, "TORCHIC_CONFIRM_GATE")
    if not ok:
        return False, {"status": "TORCHIC_CONFIRM_GATE_FAIL", "observations": confirm}

    confirm_ptr = int(confirm[-1]["lower"]["confirm_pointer"], 16)
    b.read(confirm_ptr, 4)

    i.pulse(("A",), **PULSE)

    ok, battle = bounded_battle_gate(b, BATTLE_DELAYS, log)
    if not ok:
        return False, {"status": "TORCHIC_BATTLE_GATE_FAIL", "observations": battle}

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
        return False, {"status": "TORCHIC_PK6_AUTHORITY_FAIL", "pk6": pk6}

    return True, {
        "status": "SHINY_HOLD_PASS" if pk6["is_shiny"] else "PASS",
        "pk6": pk6,
        "tid_sid_match": True,
    }
