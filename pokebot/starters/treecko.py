from __future__ import annotations

from pokebot.common.oras_ram import (
    PARTY0, PK6_SIZE, bounded_battle_gate, bounded_gate, read_trainer_ids
)
from pokebot.common.pk6 import parse_pk6

NAME = "Treecko"
SPECIES = 252
STATUS = "PORT_IN_VALIDATION"

# Treecko owns these values. Torchic.py is never imported.
CHOOSER_SLOT = 3
MIDDLE_SLOT = 1
TARGET_SLOT = 0
PROC_READY = 6
LOWER_SELECT = 2
LOWER_CONFIRM = 6

CHOOSER_DELAYS = (2.8, 0.45, 0.45, 0.55)
MIDDLE_DELAYS = (0.10, 0.20, 0.35)
TREECKO_DELAYS = (0.10, 0.20, 0.35)
CONFIRM_DELAYS = (0.20, 0.20, 0.25, 0.35)
# Preserve Treecko's early probes, but allow the same total 6.25 s battle
# authority window used by Torchic/Mudkip.  HF71 recorded a real N3DS XL
# false hold after 60 successful random-starter cycles: confirmation authority
# was valid, but BATTLE_STATE was still 0x00000000 at the old 4.25 s cutoff.
# The extra probes send no input and cannot authorize a PK6 read unless the
# hardware-backed BATTLE_ACTIVE value is actually observed.
BATTLE_DELAYS = (1.50, 1.25, 0.25, 1.25, 0.75, 1.25)

A_PULSE = dict(
    hold_ms=300,
    resume_settle_ms=220,
    packet_interval_ms=30,
    release_ms=120,
)

LEFT_PULSE = dict(
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

def _treecko(s):
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
    ok, chooser = bounded_gate(b, CHOOSER_DELAYS, _chooser, log, "TREECKO_CHOOSER_GATE")
    if not ok:
        return False, {"status": "TREECKO_CHOOSER_GATE_FAIL", "observations": chooser}

    # Establish default middle slot.
    i.pulse(("A",), **A_PULSE)
    ok, middle = bounded_gate(b, MIDDLE_DELAYS, _middle, log, "TREECKO_MIDDLE_SLOT1_GATE")
    if not ok:
        return False, {"status": "TREECKO_MIDDLE_SLOT1_GATE_FAIL", "observations": middle}

    # Treecko-specific movement lives here and nowhere else.
    i.pulse(("LEFT",), **LEFT_PULSE)
    ok, target = bounded_gate(b, TREECKO_DELAYS, _treecko, log, "TREECKO_SLOT0_GATE")
    if not ok:
        return False, {"status": "TREECKO_SLOT0_GATE_FAIL", "observations": target}

    tid, sid = read_trainer_ids(b)

    # Select Treecko.
    i.pulse(("A",), **A_PULSE)
    ok, confirm = bounded_gate(b, CONFIRM_DELAYS, _confirm, log, "TREECKO_CONFIRM_GATE")
    if not ok:
        return False, {"status": "TREECKO_CONFIRM_GATE_FAIL", "observations": confirm}

    confirm_ptr = int(confirm[-1]["lower"]["confirm_pointer"], 16)
    b.read(confirm_ptr, 4)

    # Confirm YES.
    i.pulse(("A",), **A_PULSE)

    ok, battle = bounded_battle_gate(b, BATTLE_DELAYS, log)
    if not ok:
        return False, {"status": "TREECKO_BATTLE_GATE_FAIL", "observations": battle}

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
        return False, {"status": "TREECKO_PK6_AUTHORITY_FAIL", "pk6": pk6}

    return True, {
        "status": "SHINY_HOLD_PASS" if pk6["is_shiny"] else "PASS",
        "pk6": pk6,
        "tid_sid_match": True,
    }
