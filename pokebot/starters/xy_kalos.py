from __future__ import annotations

"""Kalos starter handoff for Pokémon X/Y.

Expected save setup: save immediately before speaking to the Aquacorde group.
The shared XY reset backend returns once DllField is loaded.  From the proven
Aquacorde save position we send exactly one LEFT press to cross the group-event
trigger, then advance only with A until the dedicated DllPoke3Select chooser is
QUERY-proven loaded.  Starter selection uses the hardware-confirmed X/Y chooser choreography:
LEFT activates/highlights Fennekin, then an optional LEFT/RIGHT chooses a side
starter, followed by A to open Yes/No and A again to confirm Yes.  X/Y Party Slot 1 PK6 remains the final
authority and input stops as soon as a valid starter exists.

This first integration is intentionally fail-closed: a wrong species, non-empty
preflight party, invalid checksum, or unexpected chooser identity causes a
Safety Hold instead of another reset.
"""

import math
import statistics
import time

from pokebot.common.gen6_cro import locate_loaded_module
from pokebot.common.framebuffer import (
    _FramebufferBridge,
    SCREEN_BOTTOM,
    FramebufferBridgeError,
)
from pokebot.common.pk6 import parse_pk6
from pokebot.common.species_names import SPECIES_NAMES
from pokebot.common.xy_ram import PARTY0, PK6_SIZE, read_trainer_ids

CHOOSER_MODULE = "DllPoke3Select"
CHOOSER_FILE_SIZE = 0xA000
CHOOSER_BSS_SIZE = 0x3C8
CHOOSER_MODULE_NAME_SIZE = 15

# DllPoke3Select is loaded before the chooser is actually visible on X/Y.
# Never use CRO presence alone as a UI-ready signal.  The actual chooser
# bottom screen has three red Poké Balls at stable left/centre/right zones.
# We read sparse live framebuffer rows (not a full screenshot) and require all
# three red zones before allowing starter-selection input.
CHOOSER_SAMPLE_X0 = 50
CHOOSER_SAMPLE_X1 = 270
CHOOSER_SAMPLE_ROWS = (62, 70, 78, 86, 94, 102, 110, 118)
CHOOSER_RED_ZONES = {
    "left": (70, 122),
    "center": (136, 187),
    "right": (202, 255),
}
CHOOSER_MIN_RED_PER_ZONE = 14
CHOOSER_MIN_RED_TOTAL = 60

# The framebuffer remains only a chooser-ready gate.  It is deliberately not used
# for starter selection in HF40; selection reuses the ORAS A/middle/direction/A flow.
STARTER_ZONE = {
    "chespin": "left",
    "fennekin": "center",
    "froakie": "right",
}

# HF38 calibration mode: Chespin's actual touchscreen hitbox is not aligned
# with the visible red-ball centroid, and D-pad selection is ignored by this
# chooser.  Probe one bounded coordinate per reset until authoritative PK6
# proves species 650.  This is deliberately Chespin-only; Fennekin remains on
# the hardware-proven path and Froakie remains fail-closed.
CHESPIN_PROBE_CANDIDATES = [
    (40, 80), (60, 80), (80, 80), (100, 80), (120, 80),
    (40, 110), (60, 110), (80, 110), (100, 110), (120, 110),
    (40, 140), (60, 140), (80, 140), (100, 140), (120, 140),
    (40, 170), (60, 170), (80, 170), (100, 170), (120, 170),
    (40, 200), (60, 200), (80, 200), (100, 200), (120, 200),
]
_chsp_probe_index = 0


def _next_chsp_probe_candidate():
    global _chsp_probe_index
    idx = _chsp_probe_index
    if idx >= len(CHESPIN_PROBE_CANDIDATES):
        return None, idx
    xy = CHESPIN_PROBE_CANDIDATES[idx]
    _chsp_probe_index += 1
    return xy, idx

STARTERS = {
    "chespin": {"name": "Chespin", "species": 650},
    "fennekin": {"name": "Fennekin", "species": 653},
    "froakie": {"name": "Froakie", "species": 656},
}

# Hardware-proven via the XY global touch sanity probe on the native 320x240
# bottom screen using Pokebot's acknowledged UDP/4952 touch path.
STARTER_TOUCH_XY = {
    "chespin": (77, 127),
    "fennekin": (159, 132),
    "froakie": (242, 130),
}
STARTER_TOUCH_PULSE = dict(
    # HF48: three distinct short taps, matching the interaction pattern that
    # registered during the successful global-touch probe. Each pulse is fully
    # released before the next one; the inter-tap interval is deliberately
    # long enough for the chooser to consume each touch as a separate event.
    hold_ms=180,
    resume_settle_ms=120,
    packet_interval_ms=20,
    release_ms=180,
)
STARTER_TOUCH_BURST_COUNT = 3
STARTER_TOUCH_INTER_TAP_S = 0.30
STARTER_TOUCH_POST_SETTLE_S = 0.80
YES_NO_POST_SELECT_SETTLE_S = 0.75

A_PULSE = dict(
    hold_ms=220,
    resume_settle_ms=80,
    packet_interval_ms=30,
    release_ms=100,
)

# From the saved Aquacorde position, one LEFT press crosses the event boundary
# that calls the player over to the friends' table.  Use the same conservative
# directional pulse shape as the proven ORAS starter movement path.
POST_FIELD_CONTROL_SETTLE_S = 2.0

GROUP_TRIGGER_LEFT_PULSE = dict(
    hold_ms=300,
    resume_settle_ms=220,
    packet_interval_ms=30,
    release_ms=120,
)

# Reuse the proven ORAS controller pulse shape, but with the manually verified
# X/Y input ordering. The first LEFT activates/highlights Fennekin; the second
# directional input (when needed) moves to Chespin/Froakie before A/A confirm.
# Previous builds incorrectly sent an activation A before navigation, so that was
# valid test of the ORAS choreography.
ORAS_SELECTOR_PULSE = dict(
    hold_ms=300,
    resume_settle_ms=220,
    packet_interval_ms=30,
    release_ms=120,
)
ORAS_MIDDLE_SETTLE_S = 0.35
ORAS_TARGET_SETTLE_S = 0.35
ORAS_POST_SELECT_SETTLE_S = 0.25


def _encode_touch_xy(x: int, y: int) -> int:
    x = max(0, min(319, int(x)))
    y = max(0, min(239, int(y)))
    xr = int(math.floor(x * 4095.0 / 320.0)) & 0xFFF
    yr = int(math.floor(y * 4095.0 / 240.0)) & 0xFFF
    return 0x01000000 | (yr << 12) | xr


def _read_slot1(bridge):
    raw = bridge.read(PARTY0, PK6_SIZE)
    return raw, parse_pk6(raw, SPECIES_NAMES)


def _stop_requested(ctx):
    ev = getattr(ctx, "stop_event", None)
    return bool(ev is not None and ev.is_set())


def _chooser_identity(rec):
    return bool(
        rec
        and rec.get("module") == CHOOSER_MODULE
        and int(rec.get("file_size", -1)) == CHOOSER_FILE_SIZE
        and int(rec.get("bss_size", -1)) == CHOOSER_BSS_SIZE
        and int(rec.get("module_name_size", -1)) == CHOOSER_MODULE_NAME_SIZE
    )




def _is_red_pixel(b: int, g: int, r: int) -> bool:
    return bool(r >= 135 and r >= g + 30 and r >= b + 20)


def _probe_chooser_visual(ctx):
    """Return (ready, diagnostics) from sparse bottom-screen pixels.

    Besides the fail-closed three-ball visual gate, retain the actual red pixel
    coordinates for each ball.  The median point is used as the touch target,
    eliminating guessed left/centre/right coordinates.
    """
    host = getattr(ctx.bridge, "host", None)
    port = int(getattr(ctx.bridge, "port", 4952))
    timeout = min(0.8, float(getattr(ctx.bridge, "timeout", 1.0)))
    if not host:
        return None, {"error": "bridge host unavailable"}

    counts = {name: 0 for name in CHOOSER_RED_ZONES}
    points = {name: [] for name in CHOOSER_RED_ZONES}
    total = 0
    fb = _FramebufferBridge(host, port=port, timeout=timeout)
    try:
        info = fb.framebuffer_info(SCREEN_BOTTOM)
        if int(info.get("width", 0)) != 320 or int(info.get("height", 0)) != 240:
            return None, {"error": "unexpected bottom framebuffer geometry", "info": info}
        count = CHOOSER_SAMPLE_X1 - CHOOSER_SAMPLE_X0
        for raw_y in CHOOSER_SAMPLE_ROWS:
            row = fb.framebuffer_span(SCREEN_BOTTOM, raw_y, CHOOSER_SAMPLE_X0, count)
            for px in range(count):
                off = px * 3
                b, g, r = row[off], row[off + 1], row[off + 2]
                if not _is_red_pixel(b, g, r):
                    continue
                x = CHOOSER_SAMPLE_X0 + px
                total += 1
                for name, (x0, x1) in CHOOSER_RED_ZONES.items():
                    if x0 <= x < x1:
                        counts[name] += 1
                        # The framebuffer bridge already exposes logical 320x240
                        # rows. Touch encoding uses the same native logical Y.
                        points[name].append((x, raw_y))
                        break
    except (FramebufferBridgeError, OSError, RuntimeError, ValueError) as exc:
        return None, {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        fb.close()

    centers = {}
    bounds = {}
    for name, pts in points.items():
        if pts:
            xs = [pt[0] for pt in pts]
            ys = [pt[1] for pt in pts]
            centers[name] = [int(round(statistics.median(xs))), int(round(statistics.median(ys)))]
            bounds[name] = [min(xs), min(ys), max(xs), max(ys)]
        else:
            centers[name] = None
            bounds[name] = None

    ready = bool(
        total >= CHOOSER_MIN_RED_TOTAL
        and all(v >= CHOOSER_MIN_RED_PER_ZONE for v in counts.values())
        and all(centers[name] is not None for name in CHOOSER_RED_ZONES)
    )
    return ready, {
        "red_counts": counts,
        "red_total": total,
        "rows": list(CHOOSER_SAMPLE_ROWS),
        "x_span": [CHOOSER_SAMPLE_X0, CHOOSER_SAMPLE_X1],
        "touch_centers": centers,
        "touch_bounds": bounds,
        "y_transform": "touch_y=framebuffer_raw_y",
    }


def _poll_expected_pk6(ctx, starter_key: str, *, seconds: float, label: str):
    expected = STARTERS[starter_key]
    started = time.monotonic()
    samples = []
    while time.monotonic() - started < float(seconds):
        if _stop_requested(ctx):
            return "STOP", None, None, samples
        try:
            raw, pk6 = _read_slot1(ctx.bridge)
        except Exception as exc:
            samples.append({"error": f"{type(exc).__name__}: {exc}"})
            time.sleep(0.10)
            continue
        rec = {
            "elapsed": round(time.monotonic() - started, 3),
            "valid": bool(pk6.get("valid")),
            "checksum_valid": bool(pk6.get("checksum_valid")),
            "species": int(pk6.get("species", 0)),
            "species_name": pk6.get("species_name"),
        }
        samples.append(rec)
        if len(samples) > 24:
            samples.pop(0)
        if pk6.get("valid") and pk6.get("checksum_valid"):
            if int(pk6.get("species", 0)) != int(expected["species"]):
                return "WRONG_SPECIES", raw, pk6, samples
            return "EXPECTED", raw, pk6, samples
        time.sleep(0.10)
    ctx.log("XY_STARTER_PK6_WAIT_TIMEOUT", label=label, samples=samples[-6:])
    return "TIMEOUT", None, None, samples


def run(ctx, raw_path, starter_key: str):
    starter_key = str(starter_key).lower()
    if starter_key not in STARTERS:
        return False, {"status": "XY_STARTER_UNKNOWN", "starter_key": starter_key}
    meta = STARTERS[starter_key]
    b, i, log = ctx.bridge, ctx.inputs, ctx.log

    # A fresh Kalos-starter save has no party Pokémon yet. This is a strong,
    # simple setup guard that prevents A-mashing on an unrelated save.
    pre_raw, pre_pk6 = _read_slot1(b)
    if pre_pk6.get("valid"):
        return False, {
            "status": "XY_STARTER_PREFLIGHT_PARTY_NOT_EMPTY",
            "party_slot1": pre_pk6,
            "expected_setup": "Save immediately before talking to the Aquacorde starter group with no starter received yet.",
        }

    tid, sid = read_trainer_ids(b)
    log(
        "XY_STARTER_POST_LOAD_BEGIN",
        starter=meta["name"],
        expected_species=meta["species"],
        party_address=f"0x{PARTY0:08X}",
        trainer_tid=int(tid),
        trainer_sid=int(sid),
    )

    # DllField can become visible before X/Y has returned control to the player.
    # The HF31 hardware trace proved a LEFT pulse was bridge-acknowledged but
    # visually ignored when sent immediately after field handoff.  Keep the
    # proven single-LEFT choreography, but wait for the field transition to
    # settle before sending it.
    log("XY_POST_FIELD_CONTROL_SETTLE", seconds=POST_FIELD_CONTROL_SETTLE_S)
    deadline = time.monotonic() + POST_FIELD_CONTROL_SETTLE_S
    while time.monotonic() < deadline:
        if _stop_requested(ctx):
            return False, {"status": "USER_STOP"}
        time.sleep(0.05)

    if _stop_requested(ctx):
        return False, {"status": "USER_STOP"}
    left_ack = i.pulse(("LEFT",), **GROUP_TRIGGER_LEFT_PULSE)
    log(
        "XY_AQUACORDE_GROUP_TRIGGER_LEFT",
        hold_ms=GROUP_TRIGGER_LEFT_PULSE["hold_ms"],
        sequence=left_ack.get("sequence") if isinstance(left_ack, dict) else None,
    )
    # Give the field event time to seize control before the first dialogue A.
    time.sleep(0.45)

    chooser = None
    chooser_visual = None
    chooser_diag = None
    cro_first_seen_press = None
    max_dialogue_presses = 48

    # Important X/Y distinction: DllPoke3Select can be loaded while the group
    # conversation is still running.  CRO presence proves the code exists, not
    # that the three-ball chooser is on screen.  Continue advancing dialogue
    # until BOTH the CRO identity and bottom-screen visual are present.
    for press_no in range(0, max_dialogue_presses + 1):
        if _stop_requested(ctx):
            return False, {"status": "USER_STOP"}

        chooser = locate_loaded_module(b, CHOOSER_MODULE)
        if chooser is not None and cro_first_seen_press is None:
            cro_first_seen_press = press_no
            log(
                "XY_STARTER_CHOOSER_CRO_EARLY_SEEN",
                after_a_presses=press_no,
                base=chooser.get("base_hex"),
            )

        if chooser is not None:
            chooser_visual, chooser_diag = _probe_chooser_visual(ctx)
            log(
                "XY_STARTER_CHOOSER_VISUAL_PROBE",
                after_a_presses=press_no,
                ready=chooser_visual,
                diagnostics=chooser_diag,
            )
            if chooser_visual is True:
                break
            if chooser_visual is None:
                return False, {
                    "status": "XY_STARTER_CHOOSER_VISUAL_UNAVAILABLE",
                    "a_presses": press_no,
                    "module": chooser,
                    "visual": chooser_diag,
                }

        if press_no >= max_dialogue_presses:
            break

        i.pulse(("A",), **A_PULSE)
        log("XY_AQUACORDE_ADVANCE_A", press=press_no + 1)
        time.sleep(0.28)

        # Fail closed if any Pokémon appears before the chooser-ready gate.
        # This catches an unexpected A selecting/confirming the default ball.
        try:
            _, guard_pk6 = _read_slot1(b)
        except Exception:
            guard_pk6 = None
        if guard_pk6 and guard_pk6.get("valid") and guard_pk6.get("checksum_valid"):
            return False, {
                "status": "XY_STARTER_UNEXPECTED_PK6_DURING_DIALOGUE_HOLD",
                "after_a_presses": press_no + 1,
                "pk6": guard_pk6,
                "cro_first_seen_press": cro_first_seen_press,
                "last_visual": chooser_diag,
            }

    if chooser is None:
        return False, {
            "status": "XY_STARTER_CHOOSER_CRO_NOT_REACHED",
            "a_presses": max_dialogue_presses,
            "module": CHOOSER_MODULE,
        }
    if chooser_visual is not True:
        return False, {
            "status": "XY_STARTER_CHOOSER_VISUAL_NOT_REACHED",
            "a_presses": max_dialogue_presses,
            "cro_first_seen_press": cro_first_seen_press,
            "module": chooser,
            "last_visual": chooser_diag,
        }
    if not _chooser_identity(chooser):
        return False, {
            "status": "XY_STARTER_CHOOSER_IDENTITY_MISMATCH",
            "observed": chooser,
            "expected": {
                "module": CHOOSER_MODULE,
                "file_size": CHOOSER_FILE_SIZE,
                "bss_size": CHOOSER_BSS_SIZE,
                "module_name_size": CHOOSER_MODULE_NAME_SIZE,
            },
        }

    log(
        "XY_STARTER_CHOOSER_GATE_PASS",
        module=chooser.get("module"),
        base=chooser.get("base_hex"),
        file_size=chooser.get("file_size_hex"),
        bss_size=chooser.get("bss_size_hex"),
        visual=chooser_diag,
        cro_first_seen_press=cro_first_seen_press,
    )

    # XY starter selection now uses the same acknowledged native touch path
    # proven by the global touch sanity probe on both PSS and this chooser.
    # The framebuffer remains a readiness gate only; the coordinates below are
    # native 320x240 touchscreen positions validated interactively on hardware.
    selection_method = "xy_acknowledged_triple_touch_4952"
    touch_x, touch_y = STARTER_TOUCH_XY[starter_key]
    touch_state = _encode_touch_xy(touch_x, touch_y)

    for tap_index in range(1, STARTER_TOUCH_BURST_COUNT + 1):
        touch_ack = i.touch_pulse(
            touch_state,
            **STARTER_TOUCH_PULSE,
        )
        log(
            "XY_STARTER_CHOOSER_TOUCH_ACK",
            starter=meta["name"],
            tap=tap_index,
            tap_count=STARTER_TOUCH_BURST_COUNT,
            touch_xy=[touch_x, touch_y],
            touch_state=f"0x{touch_state:08X}",
            sequence=touch_ack.get("sequence") if isinstance(touch_ack, dict) else None,
            completed=touch_ack.get("completed") if isinstance(touch_ack, dict) else None,
            pulse=STARTER_TOUCH_PULSE,
        )
        if tap_index < STARTER_TOUCH_BURST_COUNT:
            time.sleep(STARTER_TOUCH_INTER_TAP_S)

    time.sleep(STARTER_TOUCH_POST_SETTLE_S)

    # A opens the Yes/No confirmation for the touched starter.
    select_ack = i.pulse(("A",), **ORAS_SELECTOR_PULSE)
    log(
        "XY_STARTER_CHOOSER_SELECT_A_ACK",
        starter=meta["name"],
        sequence=select_ack.get("sequence") if isinstance(select_ack, dict) else None,
        selection_method=selection_method,
        touch_xy=[touch_x, touch_y],
    )
    time.sleep(YES_NO_POST_SELECT_SETTLE_S)

    # Default confirmation is Yes.
    yes_ack = i.pulse(("A",), **ORAS_SELECTOR_PULSE)
    log(
        "XY_STARTER_CHOOSER_YES_A_ACK",
        starter=meta["name"],
        sequence=yes_ack.get("sequence") if isinstance(yes_ack, dict) else None,
        selection_method=selection_method,
        touch_xy=[touch_x, touch_y],
    )
    time.sleep(ORAS_POST_SELECT_SETTLE_S)

    direction = None

    # After the acknowledged X/Y touchscreen selection input, wait for the generated starter. Only
    # if no PK6 exists do we authorize additional A presses. Up to five guarded A presses
    # are allowed; PK6 is polled between every press and stops the sequence
    # immediately once the starter exists.
    for stage in range(0, 6):
        state, raw, pk6, samples = _poll_expected_pk6(
            ctx, starter_key, seconds=1.8, label=f"post_xy_touch_select_stage_{stage}"
        )
        if state == "STOP":
            return False, {"status": "USER_STOP"}
        if state == "WRONG_SPECIES":
            return False, {
                "status": "XY_STARTER_WRONG_SPECIES_HOLD",
                "expected_species": meta["species"],
                "expected_name": meta["name"],
                "selection_method": selection_method,
                "selection_direction": direction,
                "pk6": pk6,
                "samples": samples,
            }
        if state == "EXPECTED":
            raw_path.write_bytes(raw)
            authority = bool(
                pk6.get("valid")
                and pk6.get("checksum_valid")
                and int(pk6.get("species", 0)) == int(meta["species"])
                and int(pk6.get("tid", -1)) == int(tid)
                and int(pk6.get("sid", -1)) == int(sid)
            )
            if not authority:
                return False, {
                    "status": "XY_STARTER_PK6_AUTHORITY_FAIL",
                    "pk6": pk6,
                    "expected_tid": int(tid),
                    "expected_sid": int(sid),
                }
            return True, {
                "status": "SHINY_HOLD_PASS" if pk6.get("is_shiny") else "PASS",
                "pk6": pk6,
                "tid_sid_match": True,
                "chooser": chooser,
                "selection_method": selection_method,
                "selection_direction": direction,
                "confirmation_a_presses": stage,
            }

        if stage < 5:
            i.pulse(("A",), **A_PULSE)
            log("XY_STARTER_CONFIRM_A", press=stage + 1)
            time.sleep(0.15)

    return False, {
        "status": "XY_STARTER_PK6_TIMEOUT",
        "starter": meta["name"],
        "selection_method": selection_method,
        "selection_direction": direction,
        "confirmation_a_presses": 5,
    }
