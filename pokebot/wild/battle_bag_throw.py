from __future__ import annotations

"""Alpha Sapphire 1.4 RAM-gated one-Poke-Ball shiny action.

Hardware authority is frozen from AC0 BattleBagMapper v0p2-v0p13 and the
successful one-throw controller run v0p20 (2026-08-25). No RAM writes are used.
Every controller edge is single-shot and followed by a RAM transition gate.
"""

import struct
import time

from pokebot.wild.best_ball import (
    choose_ball,
    plan_path as plan_ball_path,
    read_balls_state,
    wait_for_position as wait_for_ball_position,
)



class BagThrowError(RuntimeError):
    pass


# Alpha Sapphire 1.4 DllBattle object fast path, verified by two relocated
# vtables before use. A changed session/build fails closed; the dashboard does
# not perform the mapper's multi-minute full-memory fallback scan.
OWNER = 0x0852FC74
OWNER_LOCATOR = "v0p20 fixed-address dual-vptr verification"
ACTSELECT_VPTR_SUBOBJECT_OFF = 0x5C
EXPECTED_ACTSELECT_VPTR = 0x007D87C0
# D18b/D25 hardware mapping: owner+0x93 is the live BtlvUiActSelect phase byte.
# COMMAND=1 is stronger touch-readiness authority than the outer view gate alone;
# the view state/mask can become ready before FIGHT/BAG/POKEMON/RUN is actually
# rendered and able to consume a touch. owner+0x54 is the direct-target mask and
# must be zero while the four-command menu is active.
ACTSELECT_TARGET_MASK_OFF = 0x54
ACTSELECT_PHASE_STATE_OFF = 0x93
ACTSELECT_COMMAND_PHASE = 1
BAG_OFF = 0x1D8
EXPECTED_BAG_VPTR = 0x007D90B4
BAG_STATE_OFF = 0x14
BAG_CONTROLLER_OFF = 0x20
CURSOR_PTR_OFF = 0xF4
CURSOR_SELECTOR_OFF = 0x14

HEAP_MIN = 0x08000000
HEAP_MAX = 0x10000000

BAG_TOUCH_STATE = 0x01EA91FF       # bottom screen (40, 220)
BALLS_TOUCH_STATE = 0x014AABFF     # bottom screen (240, 70)

HID_NEUTRAL = 0x00000FFF
HID_A = HID_NEUTRAL & ~(1 << 0)
HID_B = HID_NEUTRAL & ~(1 << 1)
HID_RIGHT = HID_NEUTRAL & ~(1 << 4)
HID_LEFT = HID_NEUTRAL & ~(1 << 5)
HID_DOWN = HID_NEUTRAL & ~(1 << 7)
HID_UP = HID_NEUTRAL & ~(1 << 6)

HOLD_MS = 120
SETTLE_MS = 180
WAIT_SECONDS = 10.0
SELECT_WAIT_SECONDS = 5.0
COMMAND_READY_WAIT_SECONDS = 20.0
COMMAND_READY_POLL_SECONDS = 0.20
COMMAND_READY_STABLE_SAMPLES = 2
COMMAND_TOUCH_READY_WAIT_SECONDS = 20.0
COMMAND_TOUCH_READY_POLL_SECONDS = 0.12
COMMAND_TOUCH_READY_STABLE_SAMPLES = 3
COMMAND_TOUCH_READY_MIN_DWELL_SECONDS = 0.60
THROW_OUTCOME_WAIT_SECONDS = 120.0
THROW_OUTCOME_POLL_SECONDS = 0.10
THROW_OUTCOME_STABLE_SAMPLES = 3
# v0p43AT: a returned command gate / stale PK6 / epoch token is never breakout
# authority. Hardware v0p43AP-v0p43AS proved that a rethrow is safe only after
# the game consumes a BAG touch and the embedded Bag object reaches state 1.
BREAKOUT_BAG_PROBE_ARM_SECONDS = 7.5
BREAKOUT_BAG_PROBE_RETRY_GAP_SECONDS = 1.5
BREAKOUT_BAG_PROBE_ACCEPT_SECONDS = 1.25
BREAKOUT_BAG_PROBE_MAX_ATTEMPTS = 30
MAX_AUTO_CATCH_THROWS = 50
POST_CAPTURE_B_PULSES = 18
POST_CAPTURE_B_HOLD_MS = 90
POST_CAPTURE_B_SETTLE_MS = 240
POST_CAPTURE_B_GAP_SECONDS = 0.20
POST_CAPTURE_INITIAL_SETTLE_SECONDS = 0.70
POKEDEX_RESCAN_EVERY_PULSES = 2
POST_CAPTURE_NICKNAME_DOWN_HOLD_MS = 160
POST_CAPTURE_NICKNAME_DOWN_SETTLE_MS = 350
POST_CAPTURE_NICKNAME_CONFIRM_HOLD_MS = 140
POST_CAPTURE_NICKNAME_CONFIRM_SETTLE_MS = 1000
POST_CAPTURE_BOX_MESSAGE_SETTLE_SECONDS = 0.85
POST_CAPTURE_BOX_ACK_HOLD_MS = 90
POST_CAPTURE_BOX_ACK_SETTLE_MS = 350
POST_CAPTURE_POKEDEX_APPEAR_WAIT_SECONDS = 5.0
POST_CAPTURE_POKEDEX_POLL_SECONDS = 0.45
POST_CAPTURE_POKEDEX_EXIT_HOLD_MS = 100
POST_CAPTURE_POKEDEX_EXIT_SETTLE_MS = 900

# Hardware-mapped Alpha Sapphire 1.4 post-capture state authority.
# These values were captured by the dedicated v0p42ZV registered/unregistered
# mapper probes. Heap pointer *values* are intentionally not hardcoded; only
# the transition relationship is used because allocations can move.
POST_CAPTURE_SENTINEL = 0x004FCDC0
POST_CAPTURE_FLOW_POKEDEX = 0x00001735
# v0p43AU live shiny hardware (2026-08-26): while the *same* Pokédex outer
# object 0x0840FF04 remained visible, FLOW advanced naturally from 0x1735 to
# 0x1736 before any Pokédex input was sent. Therefore 0x1735 is not an
# invariant screen value. Treat 0x1735/0x1736 as phases of the same Pokédex
# object; the outer pointer remains the object authority.
POST_CAPTURE_FLOW_POKEDEX_PHASE_2 = 0x00001736
POST_CAPTURE_FLOW_POKEDEX_PHASES = frozenset({
    POST_CAPTURE_FLOW_POKEDEX,
    POST_CAPTURE_FLOW_POKEDEX_PHASE_2,
})
POST_CAPTURE_FLOW_NICKNAME_REGISTERED = 0x00001742
POST_CAPTURE_FLOW_NICKNAME_UNREGISTERED = 0x0000176B
# v0p43CQ/CR Omega Ruby hardware (2026-08-29): free-party captures have
# branch-specific no-Box return flows after nickname decline B.  The
# unregistered/Pokedex path uses nickname 0x176B -> 0x1767; the already
# registered path uses nickname 0x1742 -> 0x173E.  Both return naturally to
# the overworld with no Box-message object/A required.  Treat these as
# no-input direct-return branches; the caller still requires method-specific
# battle-inactive field/grid authority before hunting resumes.
POST_CAPTURE_FLOW_DIRECT_PARTY_RETURN_REGISTERED = 0x0000173E
POST_CAPTURE_FLOW_DIRECT_PARTY_RETURN_UNREGISTERED = 0x00001767
# v0p43CT Omega Ruby hardware (2026-08-29): an unregistered Zigzagoon with
# pre-catch party count 4 (two free slots) used nickname 0x176B -> 0x1734
# immediately after decline B.  The game was already on the no-Box return
# path.  Treat 0x1734 only as a capacity-corroborated, no-input transition:
# it is accepted only when live pre-catch party authority proves count 1..5
# and the battle/outer tuple still matches the captured hardware signature.
# The caller still must prove field/grid authority before movement resumes.
POST_CAPTURE_FLOW_DIRECT_PARTY_TRANSITION_FREE = 0x00001734
# v0p43CU Omega Ruby hardware (2026-08-29): a registered Wurmple caught with
# pre-catch party count 5 (exactly one free slot) used nickname 0x1742 -> 0x16E6
# after decline B.  This is the special case where the successful catch fills
# the sixth/final party slot without using the Box branch.  Accept it only with
# proven pre-catch count 5, the registered nickname branch, the post-capture
# sentinel, and the same nickname outer object.  No further input is sent;
# caller still requires method-specific field/grid authority before resume.
POST_CAPTURE_FLOW_DIRECT_PARTY_TRANSITION_FINAL_SLOT_REGISTERED = 0x000016E6
# Backward-compatible alias retained for tools that mapped the original CQ
# unregistered branch explicitly.
POST_CAPTURE_FLOW_DIRECT_PARTY_RETURN = POST_CAPTURE_FLOW_DIRECT_PARTY_RETURN_UNREGISTERED
POST_CAPTURE_DIRECT_PARTY_RETURN_BY_NICKNAME = {
    POST_CAPTURE_FLOW_NICKNAME_REGISTERED: POST_CAPTURE_FLOW_DIRECT_PARTY_RETURN_REGISTERED,
    POST_CAPTURE_FLOW_NICKNAME_UNREGISTERED: POST_CAPTURE_FLOW_DIRECT_PARTY_RETURN_UNREGISTERED,
}
# v0p43CS makes party-capacity semantics explicit.  Any pre-catch party count
# 1..5 has at least one free party slot, so a successful catch remains in the
# party and must take a no-Box direct-return branch.  Count 6 is the only
# full-party case and must take the Box-message branch.  Runtime flow remains
# final authority when the live party count cannot be read.
POST_CAPTURE_DIRECT_PARTY_COUNTS = frozenset(range(1, 6))
POST_CAPTURE_FULL_PARTY_COUNT = 6
POST_CAPTURE_FLOW_AFTER_BOX = 0x00000669
POST_CAPTURE_CAPTURE_FLOWS = {
    *POST_CAPTURE_FLOW_POKEDEX_PHASES,
    POST_CAPTURE_FLOW_NICKNAME_REGISTERED,
    POST_CAPTURE_FLOW_NICKNAME_UNREGISTERED,
    POST_CAPTURE_FLOW_DIRECT_PARTY_RETURN_REGISTERED,
    POST_CAPTURE_FLOW_DIRECT_PARTY_RETURN_UNREGISTERED,
    POST_CAPTURE_FLOW_DIRECT_PARTY_TRANSITION_FREE,
    POST_CAPTURE_FLOW_DIRECT_PARTY_TRANSITION_FINAL_SLOT_REGISTERED,
    POST_CAPTURE_FLOW_AFTER_BOX,
}
POST_CAPTURE_STATE_WAIT_SECONDS = 12.0
POST_CAPTURE_TRANSITION_WAIT_SECONDS = 2.0
POST_CAPTURE_POLL_SECONDS = 0.05
POST_CAPTURE_STABLE_SAMPLES = 2
# v0p43AJ: the nickname FLOW word can become visible before the Yes/No prompt
# is ready to consume HID.  Require the exact nickname flow+outer object to
# remain unchanged for a full readiness window before sending B.  If an
# acknowledged B is ignored, retry B only after re-proving the same stable
# prompt.  A is never sent while the nickname prompt remains active.
POST_CAPTURE_NICKNAME_READY_SECONDS = 1.0
POST_CAPTURE_NICKNAME_RETRY_READY_SECONDS = 0.5
POST_CAPTURE_NICKNAME_B_MAX_ATTEMPTS = 5
POST_CAPTURE_NICKNAME_B_TRANSITION_WAIT_SECONDS = 1.5
# v0p43AS hardware: Pokédex FLOW/object can exist before it consumes HID.
# v0p43AU hardware then proved FLOW can naturally advance 0x1735 -> 0x1736
# while the same Pokédex object remains on screen. Readiness is therefore
# same outer-object stability across the hardware-proven Pokédex phase set,
# not exact 0x1735 stability. If A is ACKed but ignored, retry A only after
# re-proving the same Pokédex object.
POST_CAPTURE_POKEDEX_READY_SECONDS = 1.0
POST_CAPTURE_POKEDEX_RETRY_READY_SECONDS = 0.5
POST_CAPTURE_POKEDEX_A_MAX_ATTEMPTS = 5

# D3 hardware map (2026-08-27): a captured-Pokemon EXP level-up can enter
# the four-move replacement UI while the normal battle anchors remain stale:
# battle=0x00040001, flow=5, outer unchanged.  The live DllBattle message buffer
# is authoritative for this interruption.  The validated flow uses a conservative
# policy: decline the new move (preserve all four existing moves) using B -> A -> B.
# No game RAM is written.
MOVE_LEARNING_MSG_LEN_ADDR = OWNER + 0xA5A
MOVE_LEARNING_MSG_TEXT_ADDR = OWNER + 0xA90
MOVE_LEARNING_MSG_MAX_CHARS = 0x200
MOVE_LEARNING_STABLE_SECONDS = 0.80
# D4 capture-first hardware trace showed the replacement text can exist for
# ~20 seconds before the actual decision controller is installed.  Never use
# message age alone for the decision prompts.
MOVE_LEARNING_TRANSITION_SECONDS = 30.0
MOVE_LEARNING_POLL_SECONDS = 0.08
MOVE_LEARNING_HOLD_MS = 180
MOVE_LEARNING_SETTLE_MS = 500

# D6 correction after the live D5 shiny test:
# D5 proved the D4 owner tuple below is NOT an independent/passive readiness
# authority. It remained state=4, ptrs=0/0, aux=0 for 30 s until D5 held.
# Re-reading D4 shows the tuple appeared only after earlier B attempts had
# already interacted with the replacement UI. Keep it as telemetry only.
MOVE_DECISION_STATE_ADDR = OWNER + 0x3C
MOVE_DECISION_CONTROLLER_A_ADDR = OWNER + 0x240
MOVE_DECISION_CONTROLLER_B_ADDR = OWNER + 0x244
MOVE_DECISION_AUX_STATE_ADDR = OWNER + 0x4BC
MOVE_DECISION_READY_STATE = 1
MOVE_DECISION_READY_AUX_STATE = 2
MOVE_DECISION_READY_STABLE_SECONDS = 0.40  # deprecated helper only; never gates D6 input

# Move prompt consumption policy. Once Gotcha has irreversibly committed the
# capture, the exact live DllBattle prompt plus same battle/flow/outer authority
# permits only the semantically safe button for that prompt. Firmware ACK is
# not treated as game consumption: if the exact prompt remains unchanged, retry
# the SAME button only, bounded, after a long gap. A transition to the expected
# next DllBattle stage proves consumption.
MOVE_PROMPT_STABLE_SECONDS = 0.50
MOVE_PROMPT_RETRY_GAP_SECONDS = 1.50
MOVE_PROMPT_TELEMETRY_ACCEL_GAP_SECONDS = 0.35
MOVE_PROMPT_MAX_ATTEMPTS = 10
MOVE_PROMPT_TOTAL_TIMEOUT_SECONDS = 45.0
MOVE_PROMPT_CHANGED_STABLE_SECONDS = 0.25

# After RAM proves Gotcha, failed-capture/BAG authority is permanently disabled
# for that throw.  A capture can then spend a long time in EXP/level-up/move
# learning before the normal post-capture sentinel appears.
POST_CAPTURE_INTERRUPTION_WAIT_SECONDS = 300.0
LEVEL_UP_MESSAGE_STABLE_SECONDS = 0.65
LEVEL_UP_RETRY_GAP_SECONDS = 0.85
LEVEL_UP_B_MAX_ATTEMPTS = 12
LEVEL_UP_HOLD_MS = 180
LEVEL_UP_SETTLE_MS = 500

# A completed firmware touch pulse proves delivery to HID, not that the game
# consumed the touch on that frame.  Re-send the *same* menu touch only when
# RAM proves the UI is still in the exact pre-touch state.  This is bounded and
# fail-closed: no alternate coordinates and no blind multi-tap.
BAG_OPEN_ATTEMPTS = 3
BALLS_OPEN_ATTEMPTS = 3
# A command-menu touch has two distinct outcomes:
#   1) the command gate remains stably ready -> the game ignored the touch and
#      the exact same touch may be retried;
#   2) the command gate drops -> the game has begun consuming the touch.
#      Omega Ruby hardware (2026-08-29) proved Bag construction can take longer
#      than the old 2.5 s AS acceptance window after the gate drops.  Once that
#      transition is observed we never replay the touch; instead we allow the
#      embedded Bag object a bounded grace period to reach state 1.
MENU_TOUCH_ACCEPT_SECONDS = 2.50
MENU_TOUCH_TRANSITION_WAIT_SECONDS = 6.00
MENU_TOUCH_RETRY_PROOF_AFTER = 0.75
MENU_TOUCH_STABLE_SAMPLES = 2
MENU_TOUCH_TRANSITION_STABLE_SAMPLES = 2


def _hx(value: int) -> str:
    return f"0x{int(value) & 0xFFFFFFFF:08X}"


def post_capture_destination_for_party_count(pre_capture_party_count: int | None) -> str:
    """Return the expected successful-catch destination for a proven party count.

    1..5 Pokemon before the catch means at least one free slot and therefore a
    direct party return.  Six Pokemon means the caught Pokemon must be sent to
    a Box.  Unknown/invalid counts deliberately remain UNKNOWN so flow authority
    can still fail closed without inventing party state.
    """
    if pre_capture_party_count is None:
        return "UNKNOWN"
    try:
        count = int(pre_capture_party_count)
    except Exception:
        return "UNKNOWN"
    if count in POST_CAPTURE_DIRECT_PARTY_COUNTS:
        return "DIRECT_PARTY_RETURN"
    if count == POST_CAPTURE_FULL_PARTY_COUNT:
        return "BOX_MESSAGE"
    return "UNKNOWN"


def classify_post_nickname_b_transition(
    *,
    battle: int,
    flow: int,
    outer_ptr: int,
    nickname_flow: int,
    nickname_outer: int,
    battle_inactive: int,
    pre_capture_party_count: int | None = None,
) -> str:
    """Classify the first state observed after nickname-decline B.

    Hardware/capacity branches:
    - PROMPT_STILL_ACTIVE: B was ignored; no other input is allowed yet.
    - BOX_MESSAGE: full-party/Box branch; same nickname flow, new outer object.
    - DIRECT_PARTY_RETURN: when proven pre-catch party count is 1..5, any
      observed transition away from the exact nickname prompt while the
      post-capture sentinel remains active is treated as a no-input
      direct-return-in-progress.  Intermediate flow/outer values are not
      enumerated because Omega Ruby hardware has produced several legitimate
      variants (0x173E, 0x1767, 0x1734, 0x16E6, 0x0BB8).  The caller sends no
      more input and still requires real field/grid authority before movement.
      With unknown capacity, only previously hardware-mapped direct-return
      flows or a direct field return are accepted.
    - UNKNOWN: fail closed.
    """
    battle = int(battle)
    flow = int(flow)
    outer_ptr = int(outer_ptr)
    nickname_flow = int(nickname_flow)
    nickname_outer = int(nickname_outer)
    expected_destination = post_capture_destination_for_party_count(pre_capture_party_count)
    if flow == nickname_flow and outer_ptr == nickname_outer:
        return "PROMPT_STILL_ACTIVE"

    # v0p43CV: stop overfitting free-party recovery to individual transient
    # flow values.  Once the exact nickname prompt was proven and one decline
    # B was sent, a proven pre-catch count 1..5 is sufficient authority to
    # switch into a NO-FURTHER-INPUT return wait as soon as the prompt changes.
    # Omega Ruby hardware has emitted multiple legitimate intermediate flows
    # for this same semantic outcome, including 0x173E/0x1767/0x1734/0x16E6
    # and now 0x0BB8.  We deliberately do not press A/B/touch in this state;
    # the caller must still prove battle teardown plus method-specific field
    # and grid authority before hunting movement resumes.  Requiring the
    # post-capture sentinel here keeps unrelated battle states fail-closed.
    if expected_destination == "DIRECT_PARTY_RETURN":
        if battle == int(battle_inactive) and flow == 0x00000005:
            return "DIRECT_PARTY_RETURN"
        if battle == POST_CAPTURE_SENTINEL:
            return "DIRECT_PARTY_RETURN"
        return "UNKNOWN"

    if flow == nickname_flow and outer_ptr not in {0, nickname_outer}:
        return "BOX_MESSAGE"
    expected_direct = POST_CAPTURE_DIRECT_PARTY_RETURN_BY_NICKNAME.get(nickname_flow)
    if expected_direct is not None and flow == expected_direct:
        # With a proven full party, a direct return contradicts capacity.
        if expected_destination == "BOX_MESSAGE":
            return "UNKNOWN"
        return "DIRECT_PARTY_RETURN"
    # v0p43CT hardware: flow 0x1734 can be the immediate post-B no-Box
    # transition even after the unregistered nickname branch.  It is not
    # accepted as a generic flow value: require proven free-party capacity,
    # the post-capture sentinel, and the same mapped outer object.  No input is
    # sent after this classification; the caller waits for field/grid RAM.
    if flow == POST_CAPTURE_FLOW_DIRECT_PARTY_TRANSITION_FREE:
        if (
            expected_destination == "DIRECT_PARTY_RETURN"
            and battle == POST_CAPTURE_SENTINEL
            and outer_ptr == nickname_outer
        ):
            return "DIRECT_PARTY_RETURN"
        return "UNKNOWN"
    # v0p43CU hardware: when a registered capture starts with exactly five
    # party members, the catch fills slot six and nickname B can transition to
    # 0x16E6 instead of the previously mapped 0x173E.  This is deliberately
    # narrower than the generic free-party rule: require the registered branch,
    # exact pre-catch count 5, post-capture sentinel, and unchanged outer.
    if flow == POST_CAPTURE_FLOW_DIRECT_PARTY_TRANSITION_FINAL_SLOT_REGISTERED:
        if (
            nickname_flow == POST_CAPTURE_FLOW_NICKNAME_REGISTERED
            and int(pre_capture_party_count or 0) == 5
            and expected_destination == "DIRECT_PARTY_RETURN"
            and battle == POST_CAPTURE_SENTINEL
            and outer_ptr == nickname_outer
        ):
            return "DIRECT_PARTY_RETURN"
        return "UNKNOWN"
    if battle == int(battle_inactive) and flow == 0x00000005:
        if expected_destination == "BOX_MESSAGE":
            return "UNKNOWN"
        return "DIRECT_PARTY_RETURN"
    return "UNKNOWN"


def _valid_heap_ptr(value: int) -> bool:
    return HEAP_MIN <= int(value) < HEAP_MAX and (int(value) & 3) == 0


def bind_runtime_owner(owner: int, *, locator: str = "runtime dual-vptr discovery") -> dict:
    """Bind the Battle Bag/message helpers to a hardware-proven relocated owner.

    D11 hardware validation proved Horde battles instantiate the same ActSelect + embedded Bag
    layout at a heap-relocated owner.  This helper changes only Python-side
    read addresses; it performs no game RAM writes.  Every subsequent Bag call
    still re-verifies both vtables before input.
    """
    global OWNER, OWNER_LOCATOR
    global MOVE_LEARNING_MSG_LEN_ADDR, MOVE_LEARNING_MSG_TEXT_ADDR
    global MOVE_DECISION_STATE_ADDR, MOVE_DECISION_CONTROLLER_A_ADDR
    global MOVE_DECISION_CONTROLLER_B_ADDR, MOVE_DECISION_AUX_STATE_ADDR

    owner = int(owner)
    if not _valid_heap_ptr(owner):
        raise BagThrowError(f"invalid runtime Battle Bag owner {_hx(owner)}")
    OWNER = owner
    OWNER_LOCATOR = str(locator or "runtime dual-vptr discovery")
    MOVE_LEARNING_MSG_LEN_ADDR = OWNER + 0xA5A
    MOVE_LEARNING_MSG_TEXT_ADDR = OWNER + 0xA90
    MOVE_DECISION_STATE_ADDR = OWNER + 0x3C
    MOVE_DECISION_CONTROLLER_A_ADDR = OWNER + 0x240
    MOVE_DECISION_CONTROLLER_B_ADDR = OWNER + 0x244
    MOVE_DECISION_AUX_STATE_ADDR = OWNER + 0x4BC
    return {
        "owner": _hx(OWNER),
        "bag": _hx(OWNER + BAG_OFF),
        "locator": OWNER_LOCATOR,
        "ram_writes": False,
    }


def _verify_fixed_owner(br) -> dict:
    act_vptr = br.u32(OWNER + ACTSELECT_VPTR_SUBOBJECT_OFF)
    bag = OWNER + BAG_OFF
    bag_vptr = br.u32(bag)
    if act_vptr != EXPECTED_ACTSELECT_VPTR or bag_vptr != EXPECTED_BAG_VPTR:
        raise BagThrowError(
            "Alpha Sapphire DllBattle Bag authority changed: "
            f"ActSelect={_hx(act_vptr)} Bag={_hx(bag_vptr)}; no input sent"
        )
    return {
        "owner": _hx(OWNER),
        "bag": _hx(bag),
        "actselect_vptr": _hx(act_vptr),
        "bag_vptr": _hx(bag_vptr),
        "locator": OWNER_LOCATOR,
    }


def _parse_u32(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return int(value) & 0xFFFFFFFF
    try:
        return int(str(value), 0) & 0xFFFFFFFF
    except Exception:
        return None


def _verify_or_relocate_owner(core, br, log) -> dict:
    """Verify the current DllBattle Bag owner, relocating it if this process moved it.

    Static reset loops restart the game process repeatedly. Hardware on 2026-08-28
    proved that the normal single-battle DllBattle owner can move after those resets:
    the historic 0x0852FC74 owner then reads as zero even though the command menu is
    live.  The ActSelect + embedded Bag dual-vptr layout itself remains the authority.

    Fast path: verify the currently bound owner.
    Recovery path: discover exactly one live dual-vptr owner from the current battle
    outer/view pointer graph (bounded heap scan fallback), bind it, and re-verify it.
    No controller input or RAM write is performed by this resolver.
    """
    try:
        proof = _verify_fixed_owner(br)
        proof["owner_resolution"] = "CURRENT_BINDING_VALID"
        return proof
    except BagThrowError as original_exc:
        pass

    gate = core.read_gate(br)
    battle = _parse_u32(gate.get("battle"))
    outer = _parse_u32(gate.get("outer"))
    view = _parse_u32(gate.get("view"))
    if battle != getattr(core, "BATTLE_ACTIVE", 0x00040001):
        raise BagThrowError(
            "DllBattle owner relocation requested without active-battle authority; "
            f"battle={gate.get('battle')}; no input sent"
        )
    if not bool(gate.get("valid_view")) or not _valid_heap_ptr(outer or 0) or not _valid_heap_ptr(view or 0):
        raise BagThrowError(
            "DllBattle owner relocation lacks a valid live battle outer/view; "
            f"outer={gate.get('outer')} view={gate.get('view')}; no input sent"
        )

    # Lazy import avoids a module-load cycle: the mapper imports this module's
    # frozen vptr/layout constants.  At runtime this module is already loaded.
    from tools.horde_dynamic_owner_capture_validator import discover_unique_owner

    found = discover_unique_owner(core, br, int(view), int(outer))
    chosen = dict(found.get("chosen") or {})
    owner = int(chosen.get("owner_u32") or 0)
    if not _valid_heap_ptr(owner):
        raise BagThrowError(
            "DllBattle owner relocation did not produce one valid dual-vptr owner; no input sent"
        )
    previous = OWNER
    bind_runtime_owner(owner, locator="live single-battle dual-vptr relocation")
    proof = _verify_fixed_owner(br)
    proof["owner_resolution"] = "RUNTIME_RELOCATED"
    proof["previous_owner"] = _hx(previous)
    proof["graph_candidates"] = len((found.get("graph") or {}).get("candidates") or [])
    proof["linear_scan_used"] = not bool((found.get("linear") or {}).get("skipped"))
    if log is not None:
        log(
            "SHINY AUTO-CATCH: rebound live DllBattle Bag owner "
            f"{_hx(previous)} -> {_hx(owner)} after process relocation"
        )
    return proof


def _cursor(br) -> dict:
    bag = OWNER + BAG_OFF
    state = br.read(bag + BAG_STATE_OFF, 1)[0]
    controller = br.u32(bag + BAG_CONTROLLER_OFF)
    if not _valid_heap_ptr(controller):
        return {
            "bag": _hx(bag), "bag_state": state,
            "controller": _hx(controller), "valid": False,
        }
    cursor = br.u32(controller + CURSOR_PTR_OFF)
    if not _valid_heap_ptr(cursor):
        return {
            "bag": _hx(bag), "bag_state": state,
            "controller": _hx(controller), "cursor": _hx(cursor),
            "valid": False,
        }
    raw = br.read(cursor + CURSOR_SELECTOR_OFF, 4)
    selector = struct.unpack_from("<H", raw)[0]
    return {
        "bag": _hx(bag), "bag_state": state,
        "controller": _hx(controller), "cursor": _hx(cursor),
        "selector_u16": f"0x{selector:04X}",
        "selector_bytes": raw.hex(" "), "valid": True,
    }


def _wait(br, predicate, timeout: float, label: str, check_stop) -> tuple[dict, list[dict]]:
    started = time.monotonic()
    deadline = started + float(timeout)
    samples = []
    while time.monotonic() < deadline:
        check_stop()
        try:
            sample = _cursor(br)
        except Exception as exc:
            sample = {"error": f"{type(exc).__name__}: {exc}"}
        sample["elapsed"] = round(time.monotonic() - started, 3)
        samples.append(sample)
        if predicate(sample):
            return sample, samples
        time.sleep(0.10)
    raise BagThrowError(
        f"timeout waiting for {label}; last={samples[-1] if samples else None}"
    )


def _wait_command_gate(core, br, check_stop, log) -> tuple[dict, list[dict]]:
    """Wait through the shiny presentation until the command menu is real.

    RAM shiny authority arrives before FIGHT / BAG / POKEMON / RUN is ready.
    Require two consecutive ready samples so no touch is sent on a transient.
    """
    started = time.monotonic()
    deadline = started + COMMAND_READY_WAIT_SECONDS
    samples = []
    stable = 0
    next_progress = 5.0
    log(
        "SHINY AUTO-THROW: waiting for RAM-confirmed "
        "FIGHT / BAG / POKEMON / RUN readiness"
    )
    while time.monotonic() < deadline:
        check_stop()
        try:
            sample = dict(core.read_gate(br))
        except Exception as exc:
            sample = {"gate": False, "error": f"{type(exc).__name__}: {exc}"}
        elapsed = time.monotonic() - started
        sample["elapsed"] = round(elapsed, 3)
        samples.append(sample)
        if sample.get("gate"):
            stable += 1
            if stable >= COMMAND_READY_STABLE_SAMPLES:
                log(
                    "SHINY AUTO-THROW: command menu ready and stable "
                    f"after {elapsed:.1f}s"
                )
                return sample, samples
        else:
            stable = 0
        if elapsed >= next_progress:
            log(
                "SHINY AUTO-THROW: still waiting for command menu "
                f"({elapsed:.1f}/{COMMAND_READY_WAIT_SECONDS:.0f}s)"
            )
            next_progress += 5.0
        time.sleep(COMMAND_READY_POLL_SECONDS)
    raise BagThrowError(
        "timeout waiting for stable FIGHT / BAG / POKEMON / RUN gate; "
        f"last={samples[-1] if samples else None}"
    )


def _wait_command_touch_ready(core, br, owner: int, check_stop, log) -> tuple[dict, list[dict]]:
    """Wait for the actual BtlvUiActSelect COMMAND phase before touching BAG.

    The outer battle view gate (single: state=2/mask=0x100) is necessary but not
    sufficient. Omega Ruby hardware on 2026-08-29 showed it can become true
    before the command UI is visibly/materially ready; touching BAG in that
    pre-render window can make the gate flicker without ever constructing the
    embedded Bag object.

    D18b/D25 hardware mapping independently proved owner+0x93=1 for COMMAND and
    owner+0x93=2 for MOVE. Require the live ActSelect vptr, zero target mask,
    COMMAND phase byte 1, and the normal command gate together, stably, with a
    small dwell. This is read-only and fails closed.
    """
    started = time.monotonic()
    deadline = started + COMMAND_TOUCH_READY_WAIT_SECONDS
    samples = []
    stable = 0
    first_good_at = None
    next_progress = 5.0

    log(
        "SHINY AUTO-THROW: command gate appeared; waiting for exact "
        "touch-ready ActSelect COMMAND phase (owner+0x93=1)"
    )
    while time.monotonic() < deadline:
        check_stop()
        elapsed = time.monotonic() - started
        try:
            gate = dict(core.read_gate(br))
            vptr = br.u32(int(owner) + ACTSELECT_VPTR_SUBOBJECT_OFF)
            target_mask = br.u32(int(owner) + ACTSELECT_TARGET_MASK_OFF) & 0xFFFFFFFF
            phase = br.read(int(owner) + ACTSELECT_PHASE_STATE_OFF, 1)[0]
            good = bool(
                gate.get("gate")
                and vptr == EXPECTED_ACTSELECT_VPTR
                and target_mask == 0
                and phase == ACTSELECT_COMMAND_PHASE
            )
            rec = {
                "elapsed": round(elapsed, 3),
                "gate": bool(gate.get("gate")),
                "gate_state": gate.get("state"),
                "gate_mask": gate.get("mask"),
                "owner": _hx(owner),
                "actselect_vptr": _hx(vptr),
                "target_mask": _hx(target_mask),
                "phase_state_offset": _hx(ACTSELECT_PHASE_STATE_OFF),
                "phase_state_byte": int(phase),
                "good": good,
            }
        except Exception as exc:
            good = False
            rec = {
                "elapsed": round(elapsed, 3),
                "owner": _hx(owner),
                "good": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        samples.append(rec)
        if len(samples) > 80:
            samples.pop(0)

        if good:
            if stable == 0:
                first_good_at = time.monotonic()
            stable += 1
            dwell = time.monotonic() - float(first_good_at or time.monotonic())
            if (
                stable >= COMMAND_TOUCH_READY_STABLE_SAMPLES
                and dwell >= COMMAND_TOUCH_READY_MIN_DWELL_SECONDS
            ):
                rec = dict(rec)
                rec["stable_samples"] = stable
                rec["ready_dwell_seconds"] = round(dwell, 3)
                log(
                    "SHINY AUTO-THROW: exact touch-ready COMMAND phase proven "
                    f"after {elapsed:.1f}s (owner+0x93=1, dwell={dwell:.2f}s)"
                )
                return rec, samples
        else:
            stable = 0
            first_good_at = None

        if elapsed >= next_progress:
            last_phase = rec.get("phase_state_byte")
            log(
                "SHINY AUTO-THROW: still waiting for touch-ready COMMAND phase "
                f"({elapsed:.1f}/{COMMAND_TOUCH_READY_WAIT_SECONDS:.0f}s; "
                f"owner+0x93={last_phase})"
            )
            next_progress += 5.0
        time.sleep(COMMAND_TOUCH_READY_POLL_SECONDS)

    raise BagThrowError(
        "timeout waiting for exact touch-ready BtlvUiActSelect COMMAND phase; "
        f"last={samples[-1] if samples else None}"
    )


def _touch(br, touch_state: int, label: str) -> dict:
    result = br.touch_pulse_no_retransmit(touch_state, HOLD_MS, SETTLE_MS)
    if not result.get("completed"):
        raise BagThrowError(f"{label} did not reach acknowledged COMPLETED")
    return {"label": label, **result}


def _hid(br, raw_hid: int, label: str) -> dict:
    result = br.hid_pulse_no_retransmit(raw_hid, HOLD_MS, SETTLE_MS)
    if not result.get("completed"):
        raise BagThrowError(f"{label} did not reach acknowledged COMPLETED")
    return {"label": label, **result}


def _wait_after_open_bag_touch(core, br, check_stop) -> dict:
    """Classify an acknowledged Bag touch without replaying during transition.

    A firmware ACK proves only HID delivery.  Replaying the touch is permitted
    *only* while RAM continues to prove the exact command gate is stably ready.
    Once the command gate drops for two clean samples, the game has begun a UI
    transition; from that point the same touch is never replayed and we wait a
    longer bounded grace period for the embedded Bag object to reach state 1.

    Omega Ruby hardware on 2026-08-29 produced exactly this sequence: the second
    Bag touch dropped the command gate, but Bag state was still 0/controller 0
    when the old 2.5 s window expired.  Treating that as "ambiguous" stopped
    Auto-Capture before the Bag had time to finish constructing.
    """
    started = time.monotonic()
    accept_deadline = started + MENU_TOUCH_ACCEPT_SECONDS
    transition_deadline = started + MENU_TOUCH_TRANSITION_WAIT_SECONDS
    gate_stable = 0
    gate_dropped_stable = 0
    transition_seen = False
    transition_at = None
    samples = []

    while time.monotonic() < transition_deadline:
        check_stop()
        try:
            cursor = _cursor(br)
        except Exception as exc:
            cursor = {"error": f"{type(exc).__name__}: {exc}"}
        try:
            gate = dict(core.read_gate(br))
        except Exception as exc:
            gate = {"gate": False, "error": f"{type(exc).__name__}: {exc}"}

        elapsed = time.monotonic() - started
        sample = {
            "elapsed": round(elapsed, 3),
            "cursor": cursor,
            "command_gate": bool(gate.get("gate")),
            "gate_state": gate.get("state"),
            "gate_mask": gate.get("mask"),
            "transition_seen": transition_seen,
        }
        if gate.get("error"):
            sample["gate_error"] = gate["error"]
        samples.append(sample)

        if cursor.get("valid") and cursor.get("bag_state") == 1:
            return {
                "status": "BAG_OPEN",
                "sample": cursor,
                "samples": samples,
                "transition_seen": transition_seen,
                "transition_at": transition_at,
            }

        # Gate read failures are not evidence for either replay or transition.
        if gate.get("error"):
            gate_stable = 0
            gate_dropped_stable = 0
        elif gate.get("gate"):
            gate_stable += 1
            gate_dropped_stable = 0
        else:
            gate_stable = 0
            gate_dropped_stable += 1
            if (
                not transition_seen
                and gate_dropped_stable >= MENU_TOUCH_TRANSITION_STABLE_SAMPLES
            ):
                transition_seen = True
                transition_at = round(elapsed, 3)

        # The *only* replay authority is an unchanged, stably-ready command gate.
        # Once a transition has been seen, never replay this touch even if the
        # gate later flickers back; fail closed instead if Bag state 1 never arrives.
        if (
            not transition_seen
            and elapsed >= MENU_TOUCH_RETRY_PROOF_AFTER
            and gate_stable >= MENU_TOUCH_STABLE_SAMPLES
        ):
            return {
                "status": "TOUCH_IGNORED_COMMAND_GATE_STILL_READY",
                "sample": cursor,
                "samples": samples,
                "transition_seen": False,
                "transition_at": None,
            }

        # Before any transition evidence, retain the historic short acceptance
        # window.  Reaching it without stable replay authority is ambiguous.
        if not transition_seen and time.monotonic() >= accept_deadline:
            raise BagThrowError(
                "ambiguous state after acknowledged OPEN_BAG touch before any "
                "RAM-confirmed command-gate transition; "
                f"last={samples[-1] if samples else None}"
            )

        time.sleep(0.10)

    if transition_seen:
        raise BagThrowError(
            "OPEN_BAG touch was RAM-confirmed consumed (command gate dropped) "
            f"at {transition_at}s, but embedded Bag state 1 did not materialize "
            f"within {MENU_TOUCH_TRANSITION_WAIT_SECONDS:.1f}s; no touch replayed; "
            f"last={samples[-1] if samples else None}"
        )
    raise BagThrowError(
        "ambiguous state after acknowledged OPEN_BAG touch; "
        f"last={samples[-1] if samples else None}"
    )


def _open_bag_ram_confirmed(core, br, *, check_stop, log) -> tuple[dict, list[dict]]:
    attempts = []
    for attempt in range(1, BAG_OPEN_ATTEMPTS + 1):
        check_stop()
        # Attempt 1 follows the already-proven command gate in the caller.
        # Every retry reacquires that exact gate before replaying the same touch.
        if attempt > 1:
            _wait_command_gate(core, br, check_stop, log)
            _wait_command_touch_ready(core, br, OWNER, check_stop, log)
        event = _touch(br, BAG_TOUCH_STATE, f"OPEN_BAG_ATTEMPT_{attempt}")
        outcome = _wait_after_open_bag_touch(core, br, check_stop)
        attempts.append({"attempt": attempt, "event": event, "outcome": outcome})
        if outcome["status"] == "BAG_OPEN":
            log(f"SHINY AUTO-THROW: Bag opened on RAM-confirmed attempt {attempt}")
            return outcome["sample"], attempts
        if attempt < BAG_OPEN_ATTEMPTS:
            log(
                "SHINY AUTO-THROW: Bag touch was acknowledged but RAM proves "
                f"the command menu is still ready; retrying same Bag touch "
                f"({attempt + 1}/{BAG_OPEN_ATTEMPTS})"
            )
            continue
        raise BagThrowError(
            f"OPEN_BAG touch acknowledged {BAG_OPEN_ATTEMPTS} times but "
            "RAM still proves the battle command menu is active; no alternate "
            "coordinate or unrelated input sent"
        )
    raise BagThrowError("unreachable Bag-open retry state")


def _wait_after_balls_touch(br, check_stop) -> dict:
    """Confirm Poke Balls category, or prove the category touch was ignored."""
    started = time.monotonic()
    deadline = started + MENU_TOUCH_ACCEPT_SECONDS
    state1_stable = 0
    samples = []
    while time.monotonic() < deadline:
        check_stop()
        try:
            sample = _cursor(br)
        except Exception as exc:
            sample = {"error": f"{type(exc).__name__}: {exc}"}
        elapsed = time.monotonic() - started
        sample = dict(sample)
        sample["elapsed"] = round(elapsed, 3)
        samples.append(sample)

        if sample.get("valid") and sample.get("bag_state") == 2:
            return {"status": "BALLS_LIST_OPEN", "sample": sample, "samples": samples}

        if sample.get("valid") and sample.get("bag_state") == 1:
            state1_stable += 1
        else:
            state1_stable = 0

        if (
            elapsed >= MENU_TOUCH_RETRY_PROOF_AFTER
            and state1_stable >= MENU_TOUCH_STABLE_SAMPLES
        ):
            return {
                "status": "TOUCH_IGNORED_BAG_CATEGORY_STILL_READY",
                "sample": sample,
                "samples": samples,
            }
        time.sleep(0.10)

    raise BagThrowError(
        "ambiguous state after acknowledged OPEN_POKE_BALLS_CATEGORY touch; "
        f"last={samples[-1] if samples else None}"
    )


def _open_balls_ram_confirmed(br, *, check_stop, log) -> tuple[dict, list[dict]]:
    attempts = []
    for attempt in range(1, BALLS_OPEN_ATTEMPTS + 1):
        check_stop()
        # A retry is legal only while the embedded Bag object still proves
        # category state 1.  Never replay after an unknown or changed state.
        if attempt > 1:
            current = _cursor(br)
            if not (current.get("valid") and current.get("bag_state") == 1):
                raise BagThrowError(
                    "Poke Balls retry lost Bag category state 1 authority; "
                    f"current={current}"
                )
        event = _touch(
            br, BALLS_TOUCH_STATE, f"OPEN_POKE_BALLS_CATEGORY_ATTEMPT_{attempt}"
        )
        outcome = _wait_after_balls_touch(br, check_stop)
        attempts.append({"attempt": attempt, "event": event, "outcome": outcome})
        if outcome["status"] == "BALLS_LIST_OPEN":
            log(
                "SHINY AUTO-THROW: Poke Balls list opened on RAM-confirmed "
                f"attempt {attempt}"
            )
            return outcome["sample"], attempts
        if attempt < BALLS_OPEN_ATTEMPTS:
            log(
                "SHINY AUTO-THROW: Poke Balls touch was acknowledged but RAM "
                f"still proves Bag category state 1; retrying same touch "
                f"({attempt + 1}/{BALLS_OPEN_ATTEMPTS})"
            )
            continue
        raise BagThrowError(
            f"Poke Balls category touch acknowledged {BALLS_OPEN_ATTEMPTS} "
            "times but RAM still proves Bag category state 1; no alternate "
            "coordinate or unrelated input sent"
        )
    raise BagThrowError("unreachable Poke Balls-open retry state")


def throw_one_prepared_poke_ball(core, br, *, check_stop, log) -> dict:
    """Throw exactly one prepared normal Poke Ball for one gated attempt.

    Precondition: a single Alpha Sapphire wild shiny is RAM-confirmed and the
    command menu is ready. Before starting the hunt, the user must leave the
    normal Poke Ball as the visible starting tile in the in-battle Ball list.
    """
    report = {
        "authority": (
            "AC0 BattleBagThrowTester v0p20 hardware proof + "
            "v0p42ZM command gate + D18b/D25 owner+0x93 COMMAND touch-ready phase + v0p42ZP RAM-proven touch retry"
        ),
        "ram_writes": False,
        "maximum_throws": 1,
        "events": [],
    }
    check_stop()
    gate, gate_samples = _wait_command_gate(core, br, check_stop, log)
    report["command_gate"] = gate
    report["command_gate_samples"] = gate_samples
    flow_addr = getattr(core, "FLOW_ADDR", 0x081FB390)
    try:
        report["pre_throw_flow"] = br.u32(flow_addr)
        report["pre_throw_flow_hex"] = _hx(report["pre_throw_flow"])
    except Exception as exc:
        raise BagThrowError(f"could not read pre-throw battle flow; no Ball authorized: {exc}") from exc

    report["location"] = _verify_or_relocate_owner(core, br, log)
    log("SHINY AUTO-THROW: verified command gate and DllBattle Bag authority")

    category, bag_attempts = _open_bag_ram_confirmed(
        core, br, check_stop=check_stop, log=log
    )
    report["category"] = category
    report["bag_open_attempts"] = bag_attempts
    report["events"].extend(a["event"] for a in bag_attempts)

    list_ready, balls_attempts = _open_balls_ram_confirmed(
        br, check_stop=check_stop, log=log
    )
    report["list_ready"] = list_ready
    report["balls_open_attempts"] = balls_attempts
    report["events"].extend(a["event"] for a in balls_attempts)
    start_selector = list_ready.get("selector_u16")
    if start_selector is None:
        raise BagThrowError("Poke Balls list has no readable starting selector")
    report["start_selector"] = start_selector

    report["events"].append(
        _hid(br, HID_RIGHT, "CURSOR_RIGHT_FROM_PREPARED_POKE_BALL")
    )
    right, samples = _wait(
        br,
        lambda s: (
            s.get("valid") and s.get("bag_state") == 2
            and s.get("selector_u16") not in (None, start_selector)
        ),
        WAIT_SECONDS, "changed selector after Right", check_stop,
    )
    report["right"] = right
    report["right_samples"] = samples
    right_selector = right.get("selector_u16")

    report["events"].append(
        _hid(br, HID_LEFT, "CURSOR_LEFT_TO_PREPARED_POKE_BALL")
    )
    returned, samples = _wait(
        br,
        lambda s: (
            s.get("valid") and s.get("bag_state") == 2
            and s.get("selector_u16") not in (
                None, start_selector, right_selector
            )
        ),
        WAIT_SECONDS, "second distinct selector transition after Left", check_stop,
    )
    report["returned"] = returned
    report["left_samples"] = samples
    log(
        "SHINY AUTO-THROW: cursor round trip verified "
        f"{start_selector} -> {right_selector} -> {returned.get('selector_u16')}"
    )

    report["events"].append(_hid(br, HID_A, "SELECT_POKE_BALL"))
    selected, samples = _wait(
        br, lambda s: s.get("valid") and s.get("bag_state") == 3,
        SELECT_WAIT_SECONDS, "embedded Bag selected/Use state 3", check_stop,
    )
    report["selected"] = selected
    report["selected_samples"] = samples

    report["events"].append(_hid(br, HID_A, "CONFIRM_USE_ONCE"))
    report["result"] = "ONE_POKE_BALL_THROWN"
    report["finished_monotonic"] = time.monotonic()
    log("SHINY AUTO-THROW: one Poke Ball thrown; outcome must be RAM-classified before any retry")
    return report





def _adaptive_navigate_to_ball(br, state: dict, chosen: dict, *, check_stop, log,
                               label_prefix: str) -> tuple[dict, list, list, list]:
    """Navigate the Balls pocket using the actual RAM cursor after every pulse.

    v0p43AQ hardware showed that one acknowledged D-pad pulse can occasionally
    land more than one logical grid edge away.  Never wait forever for a
    predicted cell: read the actual stable cursor and re-plan from there.
    """
    target_page = int(chosen["page"])
    target_slot = int(chosen["slot"])
    initial_path = plan_ball_path(
        int(state["page"]), int(state["local_slot"]),
        target_page, target_slot, int(state["entry_count"]),
    )
    navigation = []
    replans = []
    current = state
    hid_for_direction = {
        "RIGHT": HID_RIGHT,
        "LEFT": HID_LEFT,
        "UP": HID_UP,
        "DOWN": HID_DOWN,
    }
    max_nav_pulses = 12
    nav_step = 0

    while (
        int(current["page"]) != target_page
        or int(current["local_slot"]) != target_slot
    ):
        check_stop()
        if nav_step >= max_nav_pulses:
            raise BagThrowError(
                f"adaptive Balls-pocket navigation exceeded {max_nav_pulses} pulses; "
                f"target=({target_page},{target_slot}) current="
                f"({current.get('page')},{current.get('local_slot')})"
            )
        path_now = plan_ball_path(
            int(current["page"]), int(current["local_slot"]),
            target_page, target_slot, int(current["entry_count"]),
        )
        if not path_now:
            break
        step = path_now[0]
        nav_step += 1
        direction = step["direction"]
        before_page = int(current["page"])
        before_slot = int(current["local_slot"])
        event = _hid(
            br, hid_for_direction[direction],
            f"{label_prefix}_ADAPTIVE_NAV_{nav_step}_{direction}",
        )

        # Wait for the actual RAM cursor to become stable, not for the predicted
        # cell to appear.  Two identical samples are sufficient to re-plan.
        settle_deadline = time.monotonic() + 2.5
        stable = 0
        last_key = None
        observed = None
        observed_samples = []
        while time.monotonic() < settle_deadline:
            check_stop()
            observed = read_balls_state(br, _cursor)
            selected = observed.get("selected") or {}
            key = (
                int(observed["page"]), int(observed["local_slot"]),
                int(observed["selected_index"]), selected.get("item_id"),
            )
            observed_samples.append({
                "page": int(observed["page"]),
                "slot": int(observed["local_slot"]),
                "selected_index": int(observed["selected_index"]),
                "item_id": selected.get("item_id"),
            })
            stable = stable + 1 if key == last_key else 1
            last_key = key
            if stable >= 2:
                break
            time.sleep(0.08)
        if observed is None or stable < 2:
            raise BagThrowError(
                f"Balls-pocket cursor did not stabilize after {direction}; last={observed}"
            )

        current = observed
        actual_page = int(current["page"])
        actual_slot = int(current["local_slot"])
        navigation.append({
            "step": nav_step,
            "direction": direction,
            "before_page": before_page,
            "before_slot": before_slot,
            "predicted_page": int(step["expected_page"]),
            "predicted_slot": int(step["expected_slot"]),
            "actual_page": actual_page,
            "actual_slot": actual_slot,
            "actual_selected": current.get("selected"),
            "samples": observed_samples,
            "event": event,
        })
        if (
            actual_page != int(step["expected_page"])
            or actual_slot != int(step["expected_slot"])
        ):
            replans.append({
                "after_step": nav_step,
                "requested_direction": direction,
                "predicted_page": int(step["expected_page"]),
                "predicted_slot": int(step["expected_slot"]),
                "actual_page": actual_page,
                "actual_slot": actual_slot,
                "reason": "RAM_PROVEN_CURSOR_DID_NOT_LAND_ON_PREDICTED_CELL",
            })
            log(
                f"SHINY AUTO-CATCH: Ball cursor recovery — {direction} expected "
                f"p{int(step['expected_page'])}/s{int(step['expected_slot'])} but RAM proved "
                f"p{actual_page}/s{actual_slot}; replanning from actual cursor"
            )
        if actual_page == before_page and actual_slot == before_slot:
            replans.append({
                "after_step": nav_step,
                "requested_direction": direction,
                "actual_page": actual_page,
                "actual_slot": actual_slot,
                "reason": "CURSOR_UNCHANGED_AFTER_ACKNOWLEDGED_PULSE",
            })

    final = read_balls_state(br, _cursor)
    selected = final.get("selected")
    if not selected:
        raise BagThrowError(
            "adaptive Ball navigator ended on an empty/nonexistent cell; no A authorized"
        )
    if (
        int(final["selected_index"]) != int(chosen["index"])
        or int(selected.get("item_id") or 0) != int(chosen["item_id"])
        or int(selected.get("quantity") or 0) <= 0
    ):
        raise BagThrowError(
            "adaptive final selected-Ball RAM proof mismatch; no A authorized: "
            f"wanted={chosen} got={selected} index={final.get('selected_index')}"
        )
    return final, initial_path, navigation, replans


def _throw_best_ball_from_open_bag(core, br, *, target, throw_index,
                                   method_key="", environment="", ball_override="best",
                                   check_stop, log) -> dict:
    """Throw one Best Ball from a hardware-proven already-open Bag state 1."""
    report = {
        "authority": (
            "GAME_CONSUMED_BAG_TOUCH_STATE_1 + v0p43AR adaptive RAM Balls selector"
        ),
        "ram_writes": False,
        "throw_index": int(throw_index),
        "method_key": str(method_key or ""),
        "environment": str(environment or ""),
        "ball_override": str(ball_override or "best"),
        "events": [],
        "navigation": [],
        "adaptive_replans": [],
    }
    check_stop()
    cur = _cursor(br)
    if not (cur.get("valid") and int(cur.get("bag_state") or 0) == 1):
        raise BagThrowError(
            f"open-Bag rethrow handoff lost state 1 before Ball selection: {cur}"
        )
    report["bag_open_state"] = cur

    list_ready, attempts = _open_balls_ram_confirmed(
        br, check_stop=check_stop, log=log
    )
    report["list_ready"] = list_ready
    report["balls_open_attempts"] = attempts
    report["events"].extend(a["event"] for a in attempts)

    try:
        state = read_balls_state(br, _cursor)
        if state.get("selected") is None:
            raise RuntimeError("Balls pocket opened on an empty/nonexistent cell")
        decision = choose_ball(
            state["entries"], throw_index=int(throw_index), target=target,
            method_key=method_key, environment=environment,
            ball_override=ball_override,
        )
    except Exception as exc:
        raise BagThrowError(f"rethrow best-ball RAM selection failed: {exc}") from exc

    chosen = dict(decision["chosen"])
    master = decision.get("master_policy") or {}
    report["start_balls_state"] = state
    report["decision"] = decision
    if state.get("cursor_slot_encoding") == "GLOBAL_INDEX_NORMALIZED":
        log(
            "SHINY AUTO-CATCH: rethrow cursor normalized from global entry index "
            f"{state.get('raw_cursor_slot')} to page/local "
            f"({state.get('page')},{state.get('local_slot')}); RAM-selected entry "
            f"{state.get('selected_index')} remains exact"
        )
    explicit_master_override = bool(
        decision.get("selection_mode") == "override"
        and int(chosen.get("item_id") or 0) == 1
    )
    if (
        int(chosen.get("item_id") or 0) == 1
        and not master.get("allowed")
        and not explicit_master_override
    ):
        raise BagThrowError("Master Ball reached rethrow selection while automatic policy forbids it")

    final, initial_path, navigation, replans = _adaptive_navigate_to_ball(
        br, state, chosen, check_stop=check_stop, log=log,
        label_prefix="RETHROW",
    )
    report["planned_path"] = initial_path
    report["navigation"] = navigation
    report["adaptive_replans"] = replans
    report["events"].extend(x["event"] for x in navigation)
    selected = final["selected"]
    if (
        int(selected.get("item_id") or 0) == 1
        and not master.get("allowed")
        and not explicit_master_override
    ):
        raise BagThrowError("Master Ball forbidden by automatic target policy at rethrow final A gate")

    report["final_selection"] = {
        "page": final["page"],
        "slot": final["local_slot"],
        "index": final["selected_index"],
        "item_id": selected["item_id"],
        "ball_name": selected["ball_name"],
        "quantity_before_throw": selected["quantity"],
    }
    log(
        "SHINY AUTO-CATCH: RAM verified rethrow Ball — "
        f"{selected['ball_name'].title()} x{selected['quantity']}"
    )
    report["events"].append(_hid(br, HID_A, "RETHROW_SELECT_VERIFIED_BALL"))
    selected_state, samples = _wait(
        br, lambda x: x.get("valid") and x.get("bag_state") == 3,
        SELECT_WAIT_SECONDS, "rethrow Bag selected/Use state 3", check_stop,
    )
    report["selected_use_state"] = selected_state
    report["selected_use_samples"] = samples
    report["events"].append(_hid(br, HID_A, "RETHROW_CONFIRM_USE"))
    flow_addr = getattr(core, "FLOW_ADDR", 0x081FB390)
    report["pre_throw_flow"] = br.u32(flow_addr)
    report["pre_throw_flow_hex"] = _hx(report["pre_throw_flow"])
    report["result"] = (
        "OVERRIDE_BALL_THROWN_FROM_CONSUMED_BAG_HANDSHAKE"
        if decision.get("selection_mode") == "override"
        else "BEST_BALL_THROWN_FROM_CONSUMED_BAG_HANDSHAKE"
    )
    report["finished_monotonic"] = time.monotonic()
    log(
        f"SHINY AUTO-CATCH: {selected['ball_name'].title()} sent only after "
        "game-consumed BAG readiness"
    )
    return report


def throw_one_best_ball(core, br, *, target, throw_index, method_key="", environment="",
                        ball_override="best", check_stop, log) -> dict:
    """Choose, RAM-verify, and throw the best currently eligible Ball once.

    Selection authority comes from the v0p43AB Balls-pocket mapping:
      controller+0x200 entry count
      controller+0x204 item/count array
      controller+0x30C current page
      cursor+0x14 page-local slot

    Master Ball is handled by best_ball.master_ball_policy and is forbidden for
    ordinary targets.  No RAM writes are performed.
    """
    report = {
        "authority": (
            "v0p20 hardware-proven Bag throw + v0p43AB Balls-pocket RAM selection + "
            "v0p42ZM command gate + D18b/D25 owner+0x93 COMMAND touch-ready phase + v0p42ZP RAM-proven touch retry"
        ),
        "ram_writes": False,
        "throw_index": int(throw_index),
        "method_key": str(method_key or ""),
        "environment": str(environment or ""),
        "ball_override": str(ball_override or "best"),
        "events": [],
        "navigation": [],
    }

    check_stop()
    gate, gate_samples = _wait_command_gate(core, br, check_stop, log)
    report["command_gate"] = gate
    report["command_gate_samples"] = gate_samples
    flow_addr = getattr(core, "FLOW_ADDR", 0x081FB390)
    try:
        report["pre_throw_flow"] = br.u32(flow_addr)
        report["pre_throw_flow_hex"] = _hx(report["pre_throw_flow"])
    except Exception as exc:
        raise BagThrowError(f"could not read pre-throw battle flow; no Ball authorized: {exc}") from exc
    report["location"] = _verify_or_relocate_owner(core, br, log)
    touch_ready, touch_ready_samples = _wait_command_touch_ready(
        core, br, OWNER, check_stop, log
    )
    report["command_touch_ready"] = touch_ready
    report["command_touch_ready_samples"] = touch_ready_samples

    category, bag_attempts = _open_bag_ram_confirmed(
        core, br, check_stop=check_stop, log=log
    )
    report["category"] = category
    report["bag_open_attempts"] = bag_attempts
    report["events"].extend(a["event"] for a in bag_attempts)

    list_ready, balls_attempts = _open_balls_ram_confirmed(
        br, check_stop=check_stop, log=log
    )
    report["list_ready"] = list_ready
    report["balls_open_attempts"] = balls_attempts
    report["events"].extend(a["event"] for a in balls_attempts)

    try:
        state = read_balls_state(br, _cursor)
        if state.get("selected") is None:
            raise RuntimeError(
                "Balls pocket opened on an empty/nonexistent cell; no navigation authorized"
            )
        decision = choose_ball(
            state["entries"],
            throw_index=int(throw_index),
            target=target,
            method_key=method_key,
            environment=environment,
            ball_override=ball_override,
        )
    except Exception as exc:
        raise BagThrowError(f"best-ball RAM selection failed before navigation: {exc}") from exc

    chosen = dict(decision["chosen"])
    report["start_balls_state"] = {
        "entry_count": state["entry_count"],
        "page": state["page"],
        "local_slot": state["local_slot"],
        "selected_index": state["selected_index"],
        "selected": state.get("selected"),
        "entries": state["entries"],
    }
    report["decision"] = decision

    selection_mode = str(decision.get("selection_mode") or "best")
    if selection_mode == "override":
        log(
            "SHINY AUTO-CATCH: user Ball override = "
            f"{chosen['ball_name'].title()} x{chosen['quantity']} "
            f"({chosen['reason']})"
        )
    else:
        log(
            "SHINY AUTO-CATCH: best Ball = "
            f"{chosen['ball_name'].title()} x{chosen['quantity']} "
            f"(proven multiplier {chosen['multiplier']:.2f}x; {chosen['reason']})"
        )
    master = decision.get("master_policy") or {}
    explicit_master_override = bool(
        selection_mode == "override" and int(chosen.get("item_id") or 0) == 1
    )
    if int(chosen.get("item_id") or 0) == 1:
        if not master.get("allowed") and not explicit_master_override:
            raise BagThrowError(
                "Master Ball reached selection while automatic policy forbids it; no A authorized"
            )
        if explicit_master_override:
            log("SHINY AUTO-CATCH: explicit Master Ball override authorized by user selection")
        else:
            log(
                "SHINY AUTO-CATCH: Master Ball preservation override authorized — "
                + str(master.get("reason"))
            )
    else:
        log(
            "SHINY AUTO-CATCH: Master Ball policy — "
            + str(master.get("reason"))
        )

    try:
        final, initial_path, navigation, replans = _adaptive_navigate_to_ball(
            br, state, chosen, check_stop=check_stop, log=log,
            label_prefix="BEST_BALL",
        )
    except Exception as exc:
        if isinstance(exc, BagThrowError):
            raise
        raise BagThrowError(f"adaptive best-ball navigation failed: {exc}") from exc
    report["planned_path"] = initial_path
    report["navigation"] = navigation
    report["adaptive_replans"] = replans
    report["events"].extend(x["event"] for x in navigation)
    selected_ball = final.get("selected")

    # Re-assert the Master restriction immediately before the irreversible A.
    if (
        int(selected_ball["item_id"]) == 1
        and not master.get("allowed")
        and not explicit_master_override
    ):
        raise BagThrowError(
            "Master Ball forbidden by automatic target policy at final A gate; no input sent"
        )

    report["final_selection"] = {
        "page": final["page"],
        "slot": final["local_slot"],
        "index": final["selected_index"],
        "item_id": selected_ball["item_id"],
        "ball_name": selected_ball["ball_name"],
        "quantity_before_throw": selected_ball["quantity"],
    }
    log(
        "SHINY AUTO-CATCH: RAM verified exact selected Ball before A — "
        f"{selected_ball['ball_name'].title()} x{selected_ball['quantity']}"
    )

    report["events"].append(_hid(br, HID_A, "SELECT_RAM_VERIFIED_BEST_BALL"))
    selected_state, samples = _wait(
        br,
        lambda s: s.get("valid") and s.get("bag_state") == 3,
        SELECT_WAIT_SECONDS,
        "embedded Bag selected/Use state 3",
        check_stop,
    )
    report["selected_use_state"] = selected_state
    report["selected_use_samples"] = samples

    report["events"].append(_hid(br, HID_A, "CONFIRM_USE_RAM_VERIFIED_BEST_BALL"))
    report["result"] = (
        "OVERRIDE_BALL_THROWN"
        if decision.get("selection_mode") == "override"
        else "BEST_BALL_THROWN"
    )
    report["finished_monotonic"] = time.monotonic()
    log(
        "SHINY AUTO-CATCH: threw RAM-verified "
        f"{selected_ball['ball_name'].title()}; classifying outcome before any retry"
    )
    return report

def _read_bounded(br, address: int, length: int) -> bytes:
    """Read through the bridge's proven 0x200-byte maximum chunk size."""
    out = bytearray()
    remaining = max(0, int(length))
    cursor = int(address)
    while remaining:
        size = min(0x200, remaining)
        out += br.read(cursor, size)
        cursor += size
        remaining -= size
    return bytes(out)


def _read_active_battle_message(br) -> dict:
    """Read the active DllBattle UTF-16 message buffer used by move learning.

    D2 hardware proved that stale older strings can remain after the active
    message changes, so only the live length-prefixed message is decoded.
    """
    raw_len = br.read(MOVE_LEARNING_MSG_LEN_ADDR, 2)
    if len(raw_len) != 2:
        raise BagThrowError("short read from move-learning active-message length")
    char_count = int.from_bytes(raw_len, "little")
    if char_count <= 0 or char_count > MOVE_LEARNING_MSG_MAX_CHARS:
        return {"char_count": char_count, "text": "", "stage": None}
    raw = _read_bounded(br, MOVE_LEARNING_MSG_TEXT_ADDR, char_count * 2)
    text = raw.decode("utf-16le", errors="replace").replace("\x00", "")
    stage = None
    # All of these are live RAM strings from DllBattle, never OCR.  Gotcha is
    # treated as irreversible capture commitment, but we still wait for the
    # normal sentinel/flow after handling any EXP interruptions.
    stripped = text.lstrip()
    if stripped.startswith("Gotcha!") and " was caught!" in text:
        stage = "CAPTURE_CONFIRMED_TEXT"
    elif " grew to Lv. " in text and "!" in text:
        stage = "LEVEL_UP_RESULT"
    # Stage 1 covers both the Yes/No question and the old-move chooser reached
    # from it. B is safe for the capture policy in either case: decline replacement.
    elif (
        "Should a move be deleted and replaced with" in text
        and "already knows four" in text
    ):
        stage = "REPLACE_OR_OLD_MOVE_SELECTION"
    elif stripped.startswith("Give up on learning the move") and "?" in text:
        stage = "GIVE_UP_CONFIRM"
    elif " did not learn" in text:
        stage = "DID_NOT_LEARN_RESULT"
    return {"char_count": char_count, "text": text, "stage": stage}


def _read_move_decision_readiness(br) -> dict:
    state = br.u32(MOVE_DECISION_STATE_ADDR)
    ptr_a = br.u32(MOVE_DECISION_CONTROLLER_A_ADDR)
    ptr_b = br.u32(MOVE_DECISION_CONTROLLER_B_ADDR)
    aux = br.u32(MOVE_DECISION_AUX_STATE_ADDR)
    ready = bool(
        state == MOVE_DECISION_READY_STATE
        and ptr_a == ptr_b
        and _valid_heap_ptr(ptr_a)
        and aux == MOVE_DECISION_READY_AUX_STATE
    )
    return {
        "ready": ready,
        "state": state,
        "controller_a": ptr_a,
        "controller_b": ptr_b,
        "aux_state": aux,
    }


def _wait_move_decision_ready(core, br, *, expected_stage: str,
                              latched_outer: int, timeout: float, check_stop) -> dict:
    """Wait for the D4-proven move decision controller, not just visible text."""
    deadline = time.monotonic() + float(timeout)
    stable_since = None
    stable_key = None
    last = None
    while time.monotonic() < deadline:
        check_stop()
        anchor = _move_learning_anchor(core, br)
        msg = _read_active_battle_message(br)
        readiness = _read_move_decision_readiness(br)
        last = {**anchor, **msg, "decision": readiness}
        if (
            anchor["battle"] != core.BATTLE_ACTIVE
            or anchor["flow"] != 0x00000005
            or anchor["outer_ptr"] != latched_outer
        ):
            raise BagThrowError(
                "move-learning battle authority changed before decision readiness "
                f"{expected_stage}: battle={_hx(anchor['battle'])} "
                f"flow={_hx(anchor['flow'])} outer={_hx(anchor['outer_ptr'])}"
            )
        if msg.get("stage") != expected_stage:
            stable_since = None
            stable_key = None
        elif readiness.get("ready"):
            key = (
                msg.get("stage"), msg.get("char_count"), msg.get("text"),
                readiness.get("state"), readiness.get("controller_a"),
                readiness.get("controller_b"), readiness.get("aux_state"),
            )
            if key != stable_key:
                stable_key = key
                stable_since = time.monotonic()
            elif (
                stable_since is not None
                and time.monotonic() - stable_since >= MOVE_DECISION_READY_STABLE_SECONDS
            ):
                return {
                    "battle": _hx(anchor["battle"]),
                    "flow": _hx(anchor["flow"]),
                    "outer_ptr": _hx(anchor["outer_ptr"]),
                    "stage": msg["stage"],
                    "char_count": msg["char_count"],
                    "text": msg["text"],
                    "decision_state": readiness["state"],
                    "decision_controller": _hx(readiness["controller_a"]),
                    "decision_aux_state": readiness["aux_state"],
                    "stable_seconds": round(time.monotonic() - stable_since, 3),
                }
        else:
            stable_since = None
            stable_key = None
        time.sleep(MOVE_LEARNING_POLL_SECONDS)
    raise BagThrowError(
        f"timeout waiting for D4-proven move decision readiness {expected_stage}; last={last}"
    )


def _move_learning_anchor(core, br) -> dict:
    flow_addr = getattr(core, "FLOW_ADDR", 0x081FB390)
    outer_ptr_addr = getattr(core, "OUTER_PTR_ADDR", 0x081FB384)
    return {
        "battle": br.u32(core.BATTLE_ADDR),
        "flow": br.u32(flow_addr),
        "outer_ptr": br.u32(outer_ptr_addr),
    }


def _wait_move_learning_stage(core, br, *, expected_stage: str,
                              latched_outer: int, stable_seconds: float,
                              timeout: float, check_stop) -> dict:
    deadline = time.monotonic() + float(timeout)
    stable_since = None
    stable_key = None
    last = None
    while time.monotonic() < deadline:
        check_stop()
        anchor = _move_learning_anchor(core, br)
        msg = _read_active_battle_message(br)
        last = {**anchor, **msg}
        if (
            anchor["battle"] != core.BATTLE_ACTIVE
            or anchor["flow"] != 0x00000005
            or anchor["outer_ptr"] != latched_outer
        ):
            raise BagThrowError(
                "move-learning battle authority changed before expected stage "
                f"{expected_stage}: battle={_hx(anchor['battle'])} "
                f"flow={_hx(anchor['flow'])} outer={_hx(anchor['outer_ptr'])}"
            )
        if msg.get("stage") == expected_stage:
            key = (msg.get("stage"), msg.get("char_count"), msg.get("text"))
            if key != stable_key:
                stable_key = key
                stable_since = time.monotonic()
            elif stable_since is not None and time.monotonic() - stable_since >= float(stable_seconds):
                return {
                    "battle": _hx(anchor["battle"]),
                    "flow": _hx(anchor["flow"]),
                    "outer_ptr": _hx(anchor["outer_ptr"]),
                    "stage": msg["stage"],
                    "char_count": msg["char_count"],
                    "text": msg["text"],
                    "stable_seconds": round(time.monotonic() - stable_since, 3),
                }
        else:
            stable_since = None
            stable_key = None
        time.sleep(MOVE_LEARNING_POLL_SECONDS)
    raise BagThrowError(
        f"timeout waiting for move-learning stage {expected_stage}; last={last}"
    )



def _drive_move_prompt_consumed_retry(
    core, br, *, expected_stage: str, button_name: str, hid_value: int,
    latched_outer: int, next_stage: str | None, final_stage: bool,
    check_stop, log
) -> dict:
    """D8 hardware-proven: bounded same-button retries until live RAM proves consumption.

    D5 hardware disproved the D4 owner tuple as a passive pre-input readiness
    gate. This helper instead requires exact prompt/stale-battle authority and
    proves consumption by a live DllBattle stage transition. ACK alone never
    counts as game consumption.
    """
    deadline = time.monotonic() + MOVE_PROMPT_TOTAL_TIMEOUT_SECONDS
    stable_since = None
    stable_key = None
    last_press = None
    attempts = 0
    changed_since = None
    changed_key = None
    last = None
    events = []

    while time.monotonic() < deadline:
        check_stop()
        anchor = _move_learning_anchor(core, br)
        msg = _read_active_battle_message(br)
        telemetry = _read_move_decision_readiness(br)
        last = {**anchor, **msg, "decision_telemetry": telemetry}

        if (
            anchor["battle"] != core.BATTLE_ACTIVE
            or anchor["flow"] != 0x00000005
            or anchor["outer_ptr"] != latched_outer
        ):
            raise BagThrowError(
                "move-learning stale-battle authority changed while waiting for "
                f"{expected_stage} consumption: battle={_hx(anchor['battle'])} "
                f"flow={_hx(anchor['flow'])} outer={_hx(anchor['outer_ptr'])}"
            )

        stage = msg.get("stage")
        if next_stage is not None and stage == next_stage:
            return {
                "expected_stage": expected_stage,
                "result": "CONSUMED_TO_EXPECTED_STAGE",
                "next_stage": next_stage,
                "attempts": attempts,
                "events": events,
                "final_message": msg,
                "decision_telemetry": telemetry,
            }

        if final_stage and stage != expected_stage:
            key = (stage, msg.get("char_count"), msg.get("text"))
            if key != changed_key:
                changed_key = key
                changed_since = time.monotonic()
            elif (
                changed_since is not None
                and time.monotonic() - changed_since >= MOVE_PROMPT_CHANGED_STABLE_SECONDS
            ):
                return {
                    "expected_stage": expected_stage,
                    "result": "CONSUMED_STAGE_CHANGED",
                    "next_stage": stage,
                    "attempts": attempts,
                    "events": events,
                    "final_message": msg,
                    "decision_telemetry": telemetry,
                }
            time.sleep(MOVE_LEARNING_POLL_SECONDS)
            continue

        if stage != expected_stage:
            raise BagThrowError(
                f"move-learning prompt changed unexpectedly while waiting for {expected_stage}; "
                f"stage={stage!r} text={msg.get('text')!r}"
            )

        key = (stage, msg.get("char_count"), msg.get("text"))
        if key != stable_key:
            stable_key = key
            stable_since = time.monotonic()
        now = time.monotonic()
        stable_for = now - (stable_since or now)
        retry_gap_required = MOVE_PROMPT_RETRY_GAP_SECONDS
        if telemetry.get("ready"):
            retry_gap_required = min(
                retry_gap_required, MOVE_PROMPT_TELEMETRY_ACCEL_GAP_SECONDS
            )
        retry_gap_ok = last_press is None or now - last_press >= retry_gap_required

        if stable_for >= MOVE_PROMPT_STABLE_SECONDS and retry_gap_ok:
            if attempts >= MOVE_PROMPT_MAX_ATTEMPTS:
                raise BagThrowError(
                    f"move-learning {expected_stage} remained unchanged after "
                    f"{MOVE_PROMPT_MAX_ATTEMPTS} bounded {button_name} attempts; last={last}"
                )
            attempts += 1
            log(
                f"SHINY AUTO-CATCH: exact {expected_stage} RAM prompt stable; "
                f"{button_name} attempt {attempts}/{MOVE_PROMPT_MAX_ATTEMPTS} "
                f"(decision tuple telemetry ready={telemetry.get('ready')})"
            )
            ev = br.hid_pulse_no_retransmit(
                hid_value, MOVE_LEARNING_HOLD_MS, MOVE_LEARNING_SETTLE_MS
            )
            if not ev.get("completed"):
                raise BagThrowError(
                    f"move-learning {button_name} did not reach acknowledged COMPLETED"
                )
            events.append({
                "button": button_name,
                "attempt": attempts,
                "stage": expected_stage,
                "text": msg.get("text"),
                "decision_telemetry": telemetry,
                **ev,
            })
            last_press = time.monotonic()
        time.sleep(MOVE_LEARNING_POLL_SECONDS)

    raise BagThrowError(
        f"timeout waiting for RAM-proven consumption of move-learning stage "
        f"{expected_stage}; attempts={attempts} last={last}"
    )


def _clear_move_learning_decline(core, br, *, check_stop, log) -> dict:
    """D8 hardware-proven handler: capture-committed prompt-consumed B -> A -> B."""
    owner_proof = _verify_fixed_owner(br)
    first_anchor = _move_learning_anchor(core, br)
    first_msg = _read_active_battle_message(br)
    if first_msg.get("stage") != "REPLACE_OR_OLD_MOVE_SELECTION":
        raise BagThrowError(
            "move-learning handler entered without replacement authority; no input sent"
        )
    if (
        first_anchor["battle"] != core.BATTLE_ACTIVE
        or first_anchor["flow"] != 0x00000005
        or not _valid_heap_ptr(first_anchor["outer_ptr"])
    ):
        raise BagThrowError(
            "move-learning message present without exact stale-battle authority; no input sent"
        )
    latched_outer = int(first_anchor["outer_ptr"])
    report = {
        "policy": "DECLINE_NEW_MOVE_PRESERVE_EXISTING_FOUR",
        "authority": (
            "Gotcha-committed caller + exact live DllBattle prompt + same "
            "battle/flow/outer + bounded same-button retry + RAM stage-transition proof"
        ),
        "ram_writes": False,
        "battle_owner_proof": owner_proof,
        "message_length_addr": _hx(MOVE_LEARNING_MSG_LEN_ADDR),
        "message_text_addr": _hx(MOVE_LEARNING_MSG_TEXT_ADDR),
        "latched_outer_ptr": _hx(latched_outer),
        "sequence": [],
        "states": [],
    }

    step1 = _drive_move_prompt_consumed_retry(
        core, br,
        expected_stage="REPLACE_OR_OLD_MOVE_SELECTION",
        button_name="B", hid_value=HID_B,
        latched_outer=latched_outer,
        next_stage="GIVE_UP_CONFIRM", final_stage=False,
        check_stop=check_stop, log=log,
    )
    report["states"].append(step1)
    report["sequence"].extend(step1["events"])

    step2 = _drive_move_prompt_consumed_retry(
        core, br,
        expected_stage="GIVE_UP_CONFIRM",
        button_name="A", hid_value=HID_A,
        latched_outer=latched_outer,
        next_stage="DID_NOT_LEARN_RESULT", final_stage=False,
        check_stop=check_stop, log=log,
    )
    report["states"].append(step2)
    report["sequence"].extend(step2["events"])

    step3 = _drive_move_prompt_consumed_retry(
        core, br,
        expected_stage="DID_NOT_LEARN_RESULT",
        button_name="B", hid_value=HID_B,
        latched_outer=latched_outer,
        next_stage=None, final_stage=True,
        check_stop=check_stop, log=log,
    )
    report["states"].append(step3)
    report["sequence"].extend(step3["events"])
    report["result"] = "MOVE_LEARNING_DECLINED_RAM_CONSUMPTION_PROVEN"
    return report

def _target_identity_sample(core, br, target) -> dict:
    """Read the live wild PK6 and compare it with the shiny we are preserving.

    This is read-only corroboration for a *real* breakout.  A stale battle-menu
    object must never authorize another Ball by itself after a successful catch.
    """
    out = {
        "available": False,
        "valid": False,
        "same_target": False,
        "species": None,
        "pid": None,
        "reason": None,
    }
    if not target:
        out["reason"] = "target_not_supplied"
        return out
    try:
        # Normal single-battle callers continue to use core.WILD_PK6_ADDR.
        # Horde callers may supply the original stored PK6 slot address so the
        # breakout-readiness corroboration follows the protected survivor
        # instead of incorrectly rereading Horde slot 0.
        supplied_addr = target.get("stored_pk6_address")
        if supplied_addr is None:
            addr = int(getattr(core, "WILD_PK6_ADDR"))
            out["address_source"] = "core.WILD_PK6_ADDR"
        elif isinstance(supplied_addr, str):
            addr = int(supplied_addr, 0)
            out["address_source"] = "target.stored_pk6_address"
        else:
            addr = int(supplied_addr)
            out["address_source"] = "target.stored_pk6_address"
        if addr <= 0:
            raise ValueError(f"invalid stored PK6 address {addr!r}")
        out["address"] = _hx(addr)
        size = int(getattr(core, "PK6_STORED_SIZE"))
        decoder = getattr(core, "decode_stored_pk6")
        tid = int(target.get("tid"))
        sid = int(target.get("sid"))
        raw = br.read(addr, size)
        pk6 = decoder(raw, tid, sid)
        out["available"] = True
        out["valid"] = bool(pk6.get("valid"))
        out["species"] = pk6.get("species")
        out["pid"] = pk6.get("pid")
        wanted_species = int(target.get("species") or 0)
        wanted_pid = str(target.get("pokemon_pid") or target.get("pid") or "").upper()
        got_pid = str(pk6.get("pid") or "").upper()
        out["same_target"] = bool(
            pk6.get("valid")
            and int(pk6.get("species") or 0) == wanted_species
            and wanted_pid
            and got_pid == wanted_pid
        )
        out["reason"] = pk6.get("reason")
    except Exception as exc:
        out["reason"] = f"{type(exc).__name__}: {exc}"
    return out


def _wait_throw_outcome(core, br, *, target=None, pre_throw_flow=None, check_stop, log) -> dict:
    """Classify capture or prove a safe rethrow by game-consumed BAG input.

    Hardware v0p43AO rejected epoch/gate/PK6 as breakout authority: all can
    look battle-ready while the capture animation is still running.  v0p43AP
    through v0p43AS then proved the safe handshake repeatedly: a rethrow is
    authorized only when a BAG touch is actually consumed and the embedded Bag
    object reaches state 1.  ACKed-but-ignored touches are not readiness.
    """
    started = time.monotonic()
    deadline = started + THROW_OUTCOME_WAIT_SECONDS
    flow_addr = getattr(core, "FLOW_ADDR", 0x081FB390)
    inactive_stable = 0
    next_probe = started + BREAKOUT_BAG_PROBE_ARM_SECONDS
    probe_attempts = []
    samples = []
    next_sample = started
    next_progress = 10.0
    move_learning_reports = []
    move_learning_capture_interruption = False
    capture_committed = False
    capture_commit = None
    interruption_inputs = []
    level_up_text = None
    level_up_stable_since = None
    level_up_last_press = None
    level_up_attempts = 0

    while time.monotonic() < deadline:
        check_stop()
        battle = br.u32(core.BATTLE_ADDR)
        flow = br.u32(flow_addr)
        elapsed = time.monotonic() - started

        # D8 hardware-proven: read the active DllBattle message every active/stale-battle
        # iteration.  D4 proved the whole successful capture/EXP/move path can
        # retain battle=active, flow=5 and the old outer object for >100 seconds.
        move_msg = {"stage": None, "text": ""}
        if battle == core.BATTLE_ACTIVE and flow == 0x00000005:
            try:
                move_msg = _read_active_battle_message(br)
            except Exception as exc:
                move_msg = {"stage": None, "text": "", "error": f"{type(exc).__name__}: {exc}"}

        if move_msg.get("stage") == "CAPTURE_CONFIRMED_TEXT" and not capture_committed:
            capture_committed = True
            capture_commit = {
                "authority": "LIVE_DLLBATTLE_GOTCHA_MESSAGE",
                "elapsed": round(elapsed, 3),
                "text": move_msg.get("text"),
            }
            # Once Gotcha is proven, another Ball is categorically illegal.
            # Extend only the passive/post-capture deadline; do not loosen any
            # input authority.
            deadline = max(deadline, time.monotonic() + POST_CAPTURE_INTERRUPTION_WAIT_SECONDS)
            log(
                "SHINY AUTO-CATCH: Gotcha confirmed in live DllBattle RAM — "
                "capture committed; all breakout/BAG probes permanently disabled for this throw"
            )

        # A level-up result can be the final blocker after move learning.  D4
        # showed an early B may be ACKed but ignored, while a later B on the
        # exact same RAM message is consumed.  Retry B only while capture is
        # already committed and the exact level-up text remains unchanged.
        if capture_committed and move_msg.get("stage") == "LEVEL_UP_RESULT":
            current_text = move_msg.get("text") or ""
            now_level = time.monotonic()
            if current_text != level_up_text:
                level_up_text = current_text
                level_up_stable_since = now_level
                level_up_last_press = None
                level_up_attempts = 0
                log(f"SHINY AUTO-CATCH: level-up RAM message observed — {current_text!r}")
            stable_for = now_level - (level_up_stable_since or now_level)
            retry_ready = (
                level_up_last_press is None
                or now_level - level_up_last_press >= LEVEL_UP_RETRY_GAP_SECONDS
            )
            if stable_for >= LEVEL_UP_MESSAGE_STABLE_SECONDS and retry_ready:
                if level_up_attempts >= LEVEL_UP_B_MAX_ATTEMPTS:
                    raise BagThrowError(
                        "post-capture level-up message remained unchanged after bounded B retries; "
                        f"text={current_text!r}"
                    )
                anchor = _move_learning_anchor(core, br)
                if (
                    anchor["battle"] != core.BATTLE_ACTIVE
                    or anchor["flow"] != 0x00000005
                    or not _valid_heap_ptr(anchor["outer_ptr"])
                ):
                    raise BagThrowError(
                        "level-up RAM message lost stale-battle authority before B; no input sent"
                    )
                level_up_attempts += 1
                log(
                    f"SHINY AUTO-CATCH: level-up message stable; B attempt "
                    f"{level_up_attempts}/{LEVEL_UP_B_MAX_ATTEMPTS}"
                )
                ev = br.hid_pulse_no_retransmit(HID_B, LEVEL_UP_HOLD_MS, LEVEL_UP_SETTLE_MS)
                if not ev.get("completed"):
                    raise BagThrowError("level-up B did not reach acknowledged COMPLETED")
                interruption_inputs.append({
                    "kind": "LEVEL_UP_RESULT",
                    "text": current_text,
                    "attempt": level_up_attempts,
                    "button": "B",
                    **ev,
                })
                level_up_last_press = time.monotonic()
                # The message may change during settle; next loop re-proves it.
                battle = br.u32(core.BATTLE_ADDR)
                flow = br.u32(flow_addr)
                elapsed = time.monotonic() - started
        elif move_msg.get("stage") != "LEVEL_UP_RESULT":
            level_up_text = None
            level_up_stable_since = None
            level_up_last_press = None
            level_up_attempts = 0

        # D6 enters the move handler only after Gotcha/capture commitment.  This
        # prevents an unrelated/stale string from ever suppressing a legitimate
        # failed-capture rethrow.
        if (
            capture_committed
            and battle == core.BATTLE_ACTIVE
            and flow == 0x00000005
            and move_msg.get("stage") == "REPLACE_OR_OLD_MOVE_SELECTION"
        ):
            log(
                "SHINY AUTO-CATCH: four-move interruption detected after proven capture; "
                "using exact-prompt bounded same-button retries until RAM proves consumption"
            )
            move_report = _clear_move_learning_decline(
                core, br, check_stop=check_stop, log=log
            )
            move_learning_reports.append(move_report)
            move_learning_capture_interruption = True
            battle = br.u32(core.BATTLE_ADDR)
            flow = br.u32(flow_addr)
            elapsed = time.monotonic() - started

        # Capture always outranks any readiness probe.
        if flow in POST_CAPTURE_CAPTURE_FLOWS:
            log(
                "SHINY AUTO-CATCH: mapped post-capture flow observed — "
                f"{_hx(flow)}; capture confirmed"
            )
            return {
                "status": "CAPTURED",
                "authority": "POST_CAPTURE_FLOW",
                "post_capture_flow": _hx(flow),
                "elapsed": round(elapsed, 3),
                "bag_probe_attempts": probe_attempts,
                "samples": samples,
                "capture_commit": capture_commit,
                "move_learning_recoveries": move_learning_reports,
                "post_capture_interruption_inputs": interruption_inputs,
            }
        if battle == POST_CAPTURE_SENTINEL:
            log("SHINY AUTO-CATCH: post-capture sentinel observed — capture confirmed")
            return {
                "status": "CAPTURED",
                "authority": "POST_CAPTURE_SENTINEL",
                "elapsed": round(elapsed, 3),
                "bag_probe_attempts": probe_attempts,
                "samples": samples,
                "capture_commit": capture_commit,
                "move_learning_recoveries": move_learning_reports,
                "post_capture_interruption_inputs": interruption_inputs,
            }

        if battle == core.BATTLE_INACTIVE:
            inactive_stable += 1
            if inactive_stable >= THROW_OUTCOME_STABLE_SAMPLES:
                log("SHINY AUTO-CATCH: battle stably inactive — capture accepted")
                return {
                    "status": "CAPTURED",
                    "authority": "BATTLE_INACTIVE_STABLE",
                    "elapsed": round(elapsed, 3),
                    "bag_probe_attempts": probe_attempts,
                    "samples": samples,
                    "capture_commit": capture_commit,
                "move_learning_recoveries": move_learning_reports,
                "post_capture_interruption_inputs": interruption_inputs,
                }
        else:
            inactive_stable = 0

        gate = {"gate": False}
        if battle == core.BATTLE_ACTIVE:
            try:
                gate = dict(core.read_gate(br))
            except Exception as exc:
                gate = {"gate": False, "error": f"{type(exc).__name__}: {exc}"}

        # Keep live reports useful without recording hundreds of redundant
        # samples.  The probe entries below carry full evidence around inputs.
        now = time.monotonic()
        if now >= next_sample:
            samples.append({
                "elapsed": round(elapsed, 3),
                "battle": _hx(battle),
                "flow": _hx(flow),
                "gate": bool(gate.get("gate")),
                "gate_state": gate.get("state"),
                "gate_mask": gate.get("mask"),
                "flow_matches_pre_throw": (
                    pre_throw_flow is not None and int(flow) == int(pre_throw_flow)
                ),
            })
            next_sample = now + 1.0

        if (
            not capture_committed
            and not move_learning_capture_interruption
            and len(probe_attempts) < BREAKOUT_BAG_PROBE_MAX_ATTEMPTS
            and now >= next_probe
        ):
            flow_matches = (
                pre_throw_flow is not None and int(flow) == int(pre_throw_flow)
            )
            identity = None
            if battle == core.BATTLE_ACTIVE and flow_matches and gate.get("gate"):
                identity = _target_identity_sample(core, br, target)
            # For Best-Ball shiny Auto-Catch target is supplied and must still
            # match before a readiness touch can be sent.  Legacy target=None
            # callers never get a rethrow from this authority.
            target_ok = bool(identity and identity.get("same_target"))
            if battle == core.BATTLE_ACTIVE and flow_matches and gate.get("gate") and target_ok:
                attempt_no = len(probe_attempts) + 1
                log(
                    f"SHINY AUTO-CATCH: readiness probe {attempt_no} — one BAG touch; "
                    "only embedded Bag state 1 can authorize another Ball"
                )
                before = {
                    "elapsed": round(elapsed, 3),
                    "battle": _hx(battle),
                    "flow": _hx(flow),
                    "gate": True,
                    "target_identity": identity,
                }
                event = _touch(
                    br, BAG_TOUCH_STATE,
                    f"BREAKOUT_READINESS_BAG_PROBE_{attempt_no}",
                )
                accept_started = time.monotonic()
                accept_samples = []
                consumed = None
                capture_during_probe = None
                while time.monotonic() - accept_started < BREAKOUT_BAG_PROBE_ACCEPT_SECONDS:
                    check_stop()
                    battle2 = br.u32(core.BATTLE_ADDR)
                    flow2 = br.u32(flow_addr)
                    if flow2 in POST_CAPTURE_CAPTURE_FLOWS:
                        capture_during_probe = "POST_CAPTURE_FLOW"
                        break
                    if battle2 == POST_CAPTURE_SENTINEL:
                        capture_during_probe = "POST_CAPTURE_SENTINEL"
                        break
                    try:
                        cur = dict(_cursor(br))
                    except Exception as exc:
                        cur = {"error": f"{type(exc).__name__}: {exc}"}
                    cur["elapsed"] = round(time.monotonic() - accept_started, 3)
                    accept_samples.append(cur)
                    if cur.get("valid") and int(cur.get("bag_state") or 0) == 1:
                        consumed = cur
                        break
                    time.sleep(0.08)

                probe = {
                    "attempt": attempt_no,
                    "at_elapsed": round(elapsed, 3),
                    "before": before,
                    "event": event,
                    "accept_samples": accept_samples,
                    "consumed": bool(consumed),
                    "consumed_state": consumed,
                    "capture_during_probe": capture_during_probe,
                }
                probe_attempts.append(probe)

                if capture_during_probe:
                    log(
                        "SHINY AUTO-CATCH: capture authority appeared during BAG readiness probe — "
                        "no retry Ball authorized"
                    )
                    return {
                        "status": "CAPTURED",
                        "authority": capture_during_probe,
                        "elapsed": round(time.monotonic() - started, 3),
                        "bag_probe_attempts": probe_attempts,
                        "samples": samples,
                    }
                if consumed:
                    log(
                        "SHINY AUTO-CATCH: breakout input-readiness proven — game consumed "
                        "BAG touch and embedded Bag reached state 1"
                    )
                    return {
                        "status": "BREAKOUT_BAG_CONSUMED_CONFIRMED",
                        "authority": "GAME_CONSUMED_BAG_TOUCH_STATE_1",
                        "elapsed": round(time.monotonic() - started, 3),
                        "bag_open_state": consumed,
                        "bag_probe_attempts": probe_attempts,
                        "samples": samples,
                    }
                log(
                    "SHINY AUTO-CATCH: BAG touch ACKed but ignored by game; still not "
                    "input-ready — no Ball authorized"
                )
                next_probe = time.monotonic() + BREAKOUT_BAG_PROBE_RETRY_GAP_SECONDS
            else:
                next_probe = time.monotonic() + 0.25

        if elapsed >= next_progress:
            if capture_committed:
                log(
                    "SHINY AUTO-CATCH: capture committed; processing/waiting through "
                    f"EXP interruptions for normal post-capture authority ({elapsed:.1f}s; "
                    f"move_events={len(move_learning_reports)} level_inputs={len(interruption_inputs)})"
                )
            else:
                log(
                    "SHINY AUTO-CATCH: waiting for capture or game-consumed BAG readiness "
                    f"({elapsed:.1f}/{THROW_OUTCOME_WAIT_SECONDS:.0f}s; probes={len(probe_attempts)})"
                )
            next_progress += 10.0
        time.sleep(THROW_OUTCOME_POLL_SECONDS)

    raise BagThrowError(
        "timeout classifying Ball outcome; no retry authorized; "
        f"capture_committed={capture_committed} move_events={len(move_learning_reports)} "
        f"level_inputs={len(interruption_inputs)} probes={len(probe_attempts)} "
        f"last={samples[-1] if samples else None}"
    )


def auto_catch_prepared_poke_ball(core, br, *, check_stop, log,
                                  maximum_throws=MAX_AUTO_CATCH_THROWS) -> dict:
    """Retry the prepared visible Poke Ball until RAM confirms a catch.

    Each additional throw is authorized only by a fresh, stable return of the
    battle command gate after the previous throw departed that gate. The loop
    is bounded and performs no RAM writes.
    """
    maximum_throws = max(1, int(maximum_throws))
    report = {
        "authority": (
            "v0p20 hardware-proven Bag throw + v0p42ZM command-ready gate + "
            "v0p42ZN RAM outcome classifier"
        ),
        "ram_writes": False,
        "maximum_throws": maximum_throws,
        "throws": [],
        "throw_count": 0,
    }

    for throw_index in range(1, maximum_throws + 1):
        check_stop()
        log(
            f"SHINY AUTO-CATCH: Ball {throw_index}/{maximum_throws} — "
            "waiting for gated throw path"
        )
        throw_report = throw_one_prepared_poke_ball(
            core, br, check_stop=check_stop, log=log
        )
        outcome = _wait_throw_outcome(
            core, br, target=None,
            pre_throw_flow=throw_report.get("pre_throw_flow"),
            check_stop=check_stop, log=log
        )
        item = {
            "throw_index": throw_index,
            "throw": throw_report,
            "outcome": outcome,
        }
        report["throws"].append(item)
        report["throw_count"] = throw_index

        if outcome.get("status") == "CAPTURED":
            report["result"] = "CAPTURED"
            report["finished_monotonic"] = time.monotonic()
            return report

        if outcome.get("status") != "BREAKOUT_COMMAND_READY":
            raise BagThrowError(
                "unrecognized Ball outcome; no retry authorized: "
                + str(outcome.get("status"))
            )

    raise BagThrowError(
        f"automatic catch reached bounded limit of {maximum_throws} Balls; "
        "no further input authorized"
    )



def auto_catch_best_ball(core, br, *, target, method_key="", environment="", ball_override="best",
                         check_stop, log, maximum_throws=MAX_AUTO_CATCH_THROWS) -> dict:
    """Use Best Ball repeatedly with hardware-proven consumed-BAG rethrow authority.

    Ball 1 uses the normal gated Bag path.  After a failed capture, no fresh
    Ball is possible until the game consumes a readiness BAG touch and RAM
    proves embedded Bag state 1.  Because that proof leaves Bag already open,
    subsequent throws continue directly from that state and re-rank inventory.
    """
    maximum_throws = max(1, int(maximum_throws))
    report = {
        "authority": (
            "v0p43AS consumed-BAG rethrow + v0p43AR adaptive Ball cursor + "
            "Accelerated Gotcha/EXP/move-learning prompt-consumption validator"
        ),
        "ram_writes": False,
        "maximum_throws": maximum_throws,
        "target": {
            "species": int((target or {}).get("species") or 0),
            "species_name": (target or {}).get("species_name"),
            "moves": list((target or {}).get("moves") or []),
        },
        "method_key": str(method_key or ""),
        "environment": str(environment or ""),
        "ball_override": str(ball_override or "best"),
        "throws": [],
        "throw_count": 0,
    }

    bag_already_open = False
    for throw_index in range(1, maximum_throws + 1):
        check_stop()
        log(
            f"SHINY AUTO-CATCH: Ball {throw_index}/{maximum_throws} — "
            + ("ranking live inventory" if str(ball_override or "best").lower() == "best"
               else f"using override {str(ball_override).title()}")
        )
        if throw_index == 1:
            throw_report = throw_one_best_ball(
                core, br, target=target, throw_index=throw_index,
                method_key=method_key, environment=environment,
                ball_override=ball_override,
                check_stop=check_stop, log=log,
            )
        else:
            if not bag_already_open:
                raise BagThrowError(
                    f"internal safety: Ball {throw_index} requested without a "
                    "game-consumed Bag-state-1 handoff"
                )
            throw_report = _throw_best_ball_from_open_bag(
                core, br, target=target, throw_index=throw_index,
                method_key=method_key, environment=environment,
                ball_override=ball_override,
                check_stop=check_stop, log=log,
            )
            bag_already_open = False

        outcome = _wait_throw_outcome(
            core, br, target=target,
            pre_throw_flow=throw_report.get("pre_throw_flow"),
            check_stop=check_stop, log=log,
        )
        report["throws"].append({
            "throw_index": throw_index,
            "ball": (throw_report.get("final_selection") or {}).get("ball_name"),
            "throw": throw_report,
            "outcome": outcome,
        })
        report["throw_count"] = throw_index

        if outcome.get("status") == "CAPTURED":
            report["result"] = "CAPTURED"
            report["finished_monotonic"] = time.monotonic()
            return report
        if outcome.get("status") == "BREAKOUT_BAG_CONSUMED_CONFIRMED":
            bag_already_open = True
            continue
        raise BagThrowError(
            "unrecognized Ball outcome; no retry authorized: "
            + str(outcome.get("status"))
        )

    raise BagThrowError(
        f"automatic catch reached bounded limit of {maximum_throws} Balls; "
        "no further input authorized"
    )


def clear_post_capture_screens(br, core, *, check_stop, log, pre_capture_party_count: int | None = None) -> dict:
    """Resolve Alpha Sapphire 1.4 post-capture using hardware-mapped RAM states.

    Hardware mapping now covers both nickname branches and both party-capacity
    outcomes:

      registered nickname:   flow 0x1742
      unregistered nickname: Pokédex 0x1735/0x1736 -> nickname 0x176B

      pre-catch party count 1..5:
        stable B decline -> mapped direct-return flow -> FIELD (no Box A)

      pre-catch party count 6:
        stable B decline -> BOX(pointer transition) -> A -> FIELD

    The original mapper runs observed battle word 0x004FCDC0 on post-capture
    screens, but later live Auto-Catch hardware proved the battle word can lag
    while the mapped flow already identifies Pokédex/nickname.  Flow is therefore
    the screen authority; the battle word is retained as corroboration only.
    DllSangoZukan residency is not
    used for screen visibility because the mapper proved it can remain loaded
    across nickname and Box screens.

    No game RAM writes are performed.  The caller still performs the existing
    method-specific field-authority proof before hunting movement resumes.
    """
    flow_addr = getattr(core, "FLOW_ADDR", 0x081FB390)
    outer_ptr_addr = getattr(core, "OUTER_PTR_ADDR", 0x081FB384)

    report = {
        "authority": (
            "ORAS 1.4 hardware-mapped post-capture RAM: battle 0x004FCDC0 + "
            "flow-state branch + relative outer-pointer transition + optional pre-catch party capacity"
        ),
        "ram_writes": False,
        "result": "POST_CAPTURE_MAPPED_RECOVERY_RUNNING",
        "branch": None,
        "pokedex_observed": False,
        "pokedex_cleared": False,
        "pokedex_base": None,
        "pokedex_visibility_authority": "same Pokédex outer object across flow phases 0x1735/0x1736; CRO residency ignored",
        "post_capture_order": [],
        "states": [],
        "events": [],
        "pre_capture_party_count": (
            int(pre_capture_party_count)
            if pre_capture_party_count is not None and str(pre_capture_party_count).lstrip("-").isdigit()
            else None
        ),
        "pre_capture_free_slots": (
            6 - int(pre_capture_party_count)
            if pre_capture_party_count is not None and str(pre_capture_party_count).lstrip("-").isdigit() and 1 <= int(pre_capture_party_count) <= 6
            else None
        ),
        "expected_post_capture_destination": post_capture_destination_for_party_count(pre_capture_party_count),
    }

    def sample(label: str) -> dict:
        check_stop()
        s = {
            "label": label,
            "battle": br.u32(core.BATTLE_ADDR),
            "flow": br.u32(flow_addr),
            "outer_ptr": br.u32(outer_ptr_addr),
        }
        report["states"].append({
            "label": label,
            "battle": _hx(s["battle"]),
            "flow": _hx(s["flow"]),
            "outer_ptr": _hx(s["outer_ptr"]),
        })
        return s

    def wait_state(predicate, timeout: float, label: str) -> dict:
        deadline = time.monotonic() + float(timeout)
        stable = 0
        last = None
        last_key = None
        while time.monotonic() < deadline:
            check_stop()
            cur = {
                "battle": br.u32(core.BATTLE_ADDR),
                "flow": br.u32(flow_addr),
                "outer_ptr": br.u32(outer_ptr_addr),
            }
            last = cur
            if predicate(cur):
                key = (cur["battle"], cur["flow"], cur["outer_ptr"])
                if key == last_key:
                    stable += 1
                else:
                    stable = 1
                    last_key = key
                if stable >= POST_CAPTURE_STABLE_SAMPLES:
                    report["states"].append({
                        "label": label,
                        "battle": _hx(cur["battle"]),
                        "flow": _hx(cur["flow"]),
                        "outer_ptr": _hx(cur["outer_ptr"]),
                    })
                    return cur
            else:
                stable = 0
                last_key = None
            time.sleep(POST_CAPTURE_POLL_SECONDS)
        last_fmt = None if last is None else {
            "battle": _hx(last["battle"]),
            "flow": _hx(last["flow"]),
            "outer_ptr": _hx(last["outer_ptr"]),
        }
        raise BagThrowError(
            f"timeout waiting for mapped post-capture state {label}; "
            f"last={last_fmt}"
        )

    def pulse_expected(raw_hid: int, hold_ms: int, settle_ms: int, label: str,
                       expected_battle: int | None,
                       expected_flow: int | set[int] | frozenset[int],
                       expected_outer: int | None = None) -> dict:
        before = sample(label + "_pre")
        expected_flows = (
            {int(expected_flow)}
            if isinstance(expected_flow, int)
            else {int(v) for v in expected_flow}
        )
        if before["flow"] not in expected_flows:
            raise BagThrowError(
                f"{label} flow authority mismatch before input: "
                f"battle={_hx(before['battle'])} flow={_hx(before['flow'])}; "
                f"expected one of {[_hx(v) for v in sorted(expected_flows)]}"
            )
        if expected_battle is not None and before["battle"] != expected_battle:
            raise BagThrowError(
                f"{label} battle corroboration mismatch before input: "
                f"expected={_hx(expected_battle)} got={_hx(before['battle'])}"
            )
        if expected_outer is not None and before["outer_ptr"] != expected_outer:
            raise BagThrowError(
                f"{label} outer-pointer authority changed before input: "
                f"expected={_hx(expected_outer)} got={_hx(before['outer_ptr'])}"
            )
        result = br.hid_pulse_no_retransmit(raw_hid, hold_ms, settle_ms)
        if not result.get("completed"):
            raise BagThrowError(label + " did not reach acknowledged COMPLETED")
        report["events"].append({"step": label, "input": result})
        return result

    def wait_exact_pokedex_ready(outer_ptr: int | None, stable_seconds: float,
                                 timeout: float, label: str) -> dict:
        deadline = time.monotonic() + float(timeout)
        stable_since = None
        last = None
        # v0p43BA hardware evidence (2026-08-27): on one real shiny capture the
        # first mapped Pokédex state was FLOW 0x1735 / outer 0x0840FF04, but
        # ~20 ms later FLOW was still 0x1735 while OUTER had naturally changed
        # to 0x086F8114 before any input. Therefore the first outer observed is
        # not authoritative. During initial readiness only, reacquire the live
        # nonzero outer and restart the stability timer when it changes. Once
        # the object has itself remained stable for the full readiness window,
        # that outer becomes authoritative for A and any A-only retry.
        locked_outer = outer_ptr
        candidate_outer = outer_ptr
        while time.monotonic() < deadline:
            check_stop()
            cur = {
                "battle": br.u32(core.BATTLE_ADDR),
                "flow": br.u32(flow_addr),
                "outer_ptr": br.u32(outer_ptr_addr),
            }
            last = cur
            if cur["flow"] in POST_CAPTURE_FLOW_POKEDEX_PHASES:
                if cur["outer_ptr"] == 0:
                    stable_since = None
                elif locked_outer is not None:
                    if cur["outer_ptr"] != locked_outer:
                        raise BagThrowError(
                            f"{label} latched Pokédex object authority changed before input: "
                            f"battle={_hx(cur['battle'])} flow={_hx(cur['flow'])} "
                            f"outer={_hx(cur['outer_ptr'])}; expected Pokédex phases "
                            f"{[_hx(v) for v in sorted(POST_CAPTURE_FLOW_POKEDEX_PHASES)]} "
                            f"on latched outer={_hx(locked_outer)}"
                        )
                    if stable_since is None:
                        stable_since = time.monotonic()
                else:
                    if candidate_outer != cur["outer_ptr"]:
                        if candidate_outer not in {None, 0}:
                            log(
                                "SHINY AUTO-CATCH: Pokédex outer changed during initial "
                                f"readiness {_hx(candidate_outer)} -> {_hx(cur['outer_ptr'])}; "
                                "restarting stability proof without input"
                            )
                        candidate_outer = cur["outer_ptr"]
                        stable_since = time.monotonic()
                    elif stable_since is None:
                        stable_since = time.monotonic()

                if stable_since is not None:
                    elapsed_ready = time.monotonic() - stable_since
                    if elapsed_ready >= float(stable_seconds):
                        report["states"].append({
                            "label": label,
                            "battle": _hx(cur["battle"]),
                            "flow": _hx(cur["flow"]),
                            "outer_ptr": _hx(cur["outer_ptr"]),
                            "stable_seconds": round(elapsed_ready, 3),
                            "pokedex_phase_set": [
                                _hx(v) for v in sorted(POST_CAPTURE_FLOW_POKEDEX_PHASES)
                            ],
                            "outer_authority": (
                                "prelatched" if locked_outer is not None
                                else "reacquired_then_stable"
                            ),
                        })
                        return cur
            else:
                # Once Pokédex readiness has started, leaving the proven phase
                # set before input is unexpected. Pointer=0 is tolerated only
                # while the phase itself remains 0x1735/0x1736.
                if candidate_outer not in {None, 0} or locked_outer is not None:
                    expected = locked_outer if locked_outer is not None else candidate_outer
                    raise BagThrowError(
                        f"{label} Pokédex flow authority changed before input: "
                        f"battle={_hx(cur['battle'])} flow={_hx(cur['flow'])} "
                        f"outer={_hx(cur['outer_ptr'])}; expected Pokédex phases "
                        f"{[_hx(v) for v in sorted(POST_CAPTURE_FLOW_POKEDEX_PHASES)]} "
                        f"while proving outer={_hx(expected)}"
                    )
                stable_since = None
            time.sleep(POST_CAPTURE_POLL_SECONDS)
        last_fmt = None if last is None else {
            "battle": _hx(last["battle"]),
            "flow": _hx(last["flow"]),
            "outer_ptr": _hx(last["outer_ptr"]),
        }
        raise BagThrowError(
            f"timeout waiting for input-ready Pokédex {label}; last={last_fmt}"
        )

    log(
        "SHINY AUTO-CATCH: capture confirmed; waiting for hardware-mapped "
        "post-capture state (Pokédex or nickname)"
    )

    # The catch classifier can briefly observe normal battle-inactive before the
    # post-capture UI object is installed.  Wait for the first mapped screen
    # rather than requiring field BATTLE_INACTIVE once the screen is visible.
    first = wait_state(
        lambda s: s["flow"] in {
            *POST_CAPTURE_FLOW_POKEDEX_PHASES,
            POST_CAPTURE_FLOW_NICKNAME_REGISTERED,
            POST_CAPTURE_FLOW_NICKNAME_UNREGISTERED,
        },
        POST_CAPTURE_STATE_WAIT_SECONDS,
        "POST_CAPTURE_FIRST_SCREEN",
    )

    if first["flow"] in POST_CAPTURE_FLOW_POKEDEX_PHASES:
        report["branch"] = "UNREGISTERED"
        report["pokedex_observed"] = True
        report["post_capture_order"].append("POKEDEX")
        # The first observed outer is only a transient lead. v0p43BA hardware
        # proved it can be replaced while FLOW remains 0x1735 before any input.
        # Latch authority only after the first full readiness proof.
        first_pokedex_outer = first["outer_ptr"]
        pokedex_outer = None
        pokedex_attempts = []
        nick = None
        for attempt in range(1, POST_CAPTURE_POKEDEX_A_MAX_ATTEMPTS + 1):
            stable_seconds = (
                POST_CAPTURE_POKEDEX_READY_SECONDS
                if attempt == 1 else POST_CAPTURE_POKEDEX_RETRY_READY_SECONDS
            )
            log(
                "SHINY AUTO-CATCH: mapped unregistered Pokédex object "
                "(flow phase 0x1735/0x1736); "
                f"waiting {stable_seconds:.1f}s stable before A attempt "
                f"{attempt}/{POST_CAPTURE_POKEDEX_A_MAX_ATTEMPTS}"
            )
            ready = wait_exact_pokedex_ready(
                pokedex_outer, stable_seconds, POST_CAPTURE_STATE_WAIT_SECONDS,
                f"POST_CAPTURE_POKEDEX_READY_{attempt}",
            )
            if pokedex_outer is None:
                pokedex_outer = ready["outer_ptr"]
                log(
                    "SHINY AUTO-CATCH: Pokédex outer authority latched after "
                    f"stable readiness proof: initial={_hx(first_pokedex_outer)} "
                    f"latched={_hx(pokedex_outer)}"
                )
            event = pulse_expected(
                HID_A, 180, 500, f"POST_CAPTURE_POKEDEX_A_{attempt}",
                None, POST_CAPTURE_FLOW_POKEDEX_PHASES, pokedex_outer,
            )
            attempt_report = {
                "attempt": attempt,
                "ready": {
                    "battle": _hx(ready["battle"]),
                    "flow": _hx(ready["flow"]),
                    "outer_ptr": _hx(ready["outer_ptr"]),
                    "stable_seconds_required": stable_seconds,
                },
                "input": event,
            }
            try:
                nick = wait_state(
                    lambda s: (
                        s["flow"] == POST_CAPTURE_FLOW_NICKNAME_UNREGISTERED
                        and s["outer_ptr"] != pokedex_outer
                    ),
                    POST_CAPTURE_TRANSITION_WAIT_SECONDS,
                    "POST_CAPTURE_NICKNAME_UNREGISTERED",
                )
                attempt_report["result"] = "NICKNAME_TRANSITION_CONFIRMED"
                pokedex_attempts.append(attempt_report)
                break
            except BagThrowError as exc:
                cur = {
                    "battle": br.u32(core.BATTLE_ADDR),
                    "flow": br.u32(flow_addr),
                    "outer_ptr": br.u32(outer_ptr_addr),
                }
                attempt_report["delegate_error"] = str(exc)
                attempt_report["after_error"] = {
                    "battle": _hx(cur["battle"]),
                    "flow": _hx(cur["flow"]),
                    "outer_ptr": _hx(cur["outer_ptr"]),
                }
                pokedex_attempts.append(attempt_report)
                exact_same = (
                    cur["flow"] in POST_CAPTURE_FLOW_POKEDEX_PHASES
                    and cur["outer_ptr"] == pokedex_outer
                )
                if not exact_same or attempt >= POST_CAPTURE_POKEDEX_A_MAX_ATTEMPTS:
                    raise
                log(
                    f"SHINY AUTO-CATCH: Pokédex A attempt {attempt} was ACKed but "
                    "exact same Pokédex object remained active; re-proving readiness "
                    "before A-only retry"
                )
        if nick is None:
            raise BagThrowError(
                "Pokédex A retry guard exhausted without nickname transition; no further input sent"
            )
        report["pokedex_ready_retry_guard"] = {
            "authority": "same Pokédex outer stability across flow phases 0x1735/0x1736 + A-only retry",
            "initial_ready_seconds": POST_CAPTURE_POKEDEX_READY_SECONDS,
            "retry_ready_seconds": POST_CAPTURE_POKEDEX_RETRY_READY_SECONDS,
            "max_attempts": POST_CAPTURE_POKEDEX_A_MAX_ATTEMPTS,
            "attempts": pokedex_attempts,
            "successful_attempt": len(pokedex_attempts),
            "pokedex_outer_ptr": _hx(pokedex_outer),
        }
        report["pokedex_cleared"] = True
    elif first["flow"] == POST_CAPTURE_FLOW_NICKNAME_REGISTERED:
        report["branch"] = "REGISTERED"
        nick = first
        log(
            "SHINY AUTO-CATCH: mapped registered branch — nickname flow "
            "0x1742 confirmed"
        )
    else:
        # Defensive resume if the worker begins after the Pokédex A transition.
        report["branch"] = "UNREGISTERED_NICKNAME_ALREADY_VISIBLE"
        report["pokedex_observed"] = True
        report["pokedex_cleared"] = True
        nick = first
        log(
            "SHINY AUTO-CATCH: unregistered nickname flow 0x176B already visible; "
            "continuing from mapped nickname state"
        )

    report["post_capture_order"].append("NICKNAME")
    nickname_flow = nick["flow"]
    nickname_outer = nick["outer_ptr"]

    # v0p43AH hardware showed that an acknowledged DOWN pulse can be ignored by
    # the game while the mapped nickname flow/outer object remains unchanged.
    # v0p43AI then proved that the nickname FLOW can itself appear before the
    # visible Yes/No prompt is ready to consume a B pulse.  Therefore FLOW
    # presence alone is not input-readiness authority.
    #
    # v0p43AJ requires the exact nickname flow + outer object to remain unchanged
    # for a full readiness interval before any B is sent.  If an acknowledged B
    # is ignored, the same exact prompt must be re-proven stable before another B
    # attempt.  Only B is retried; A is impossible while the nickname prompt is
    # still the active object.  Any unexpected flow/pointer transition fails
    # closed instead of guessing.
    def wait_exact_nickname_ready(stable_seconds: float, timeout: float, label: str) -> dict:
        deadline = time.monotonic() + float(timeout)
        stable_since = None
        last = None
        while time.monotonic() < deadline:
            check_stop()
            cur = {
                "battle": br.u32(core.BATTLE_ADDR),
                "flow": br.u32(flow_addr),
                "outer_ptr": br.u32(outer_ptr_addr),
            }
            last = cur
            if cur["flow"] == nickname_flow and cur["outer_ptr"] == nickname_outer:
                if stable_since is None:
                    stable_since = time.monotonic()
                if time.monotonic() - stable_since >= float(stable_seconds):
                    report["states"].append({
                        "label": label,
                        "battle": _hx(cur["battle"]),
                        "flow": _hx(cur["flow"]),
                        "outer_ptr": _hx(cur["outer_ptr"]),
                        "stable_seconds": round(time.monotonic() - stable_since, 3),
                    })
                    return cur
            else:
                # An unexpected transition is not something to wait through.
                # If it is already the mapped Box object, caller will observe it
                # after the B attempt; otherwise stop rather than sending input.
                if cur["flow"] != nickname_flow or cur["outer_ptr"] not in {0, nickname_outer}:
                    raise BagThrowError(
                        f"{label} nickname authority changed before input: "
                        f"battle={_hx(cur['battle'])} flow={_hx(cur['flow'])} "
                        f"outer={_hx(cur['outer_ptr'])}; expected flow={_hx(nickname_flow)} "
                        f"outer={_hx(nickname_outer)}"
                    )
                stable_since = None
            time.sleep(POST_CAPTURE_POLL_SECONDS)
        last_fmt = None if last is None else {
            "battle": _hx(last["battle"]),
            "flow": _hx(last["flow"]),
            "outer_ptr": _hx(last["outer_ptr"]),
        }
        raise BagThrowError(
            f"timeout waiting for input-ready nickname prompt {label}; last={last_fmt}"
        )

    log(
        "SHINY AUTO-CATCH: nickname flow confirmed; waiting for 1.0 s of exact "
        "flow+outer stability before B"
    )
    wait_exact_nickname_ready(
        POST_CAPTURE_NICKNAME_READY_SECONDS,
        POST_CAPTURE_STATE_WAIT_SECONDS,
        "POST_CAPTURE_NICKNAME_INPUT_READY",
    )

    nickname_b_attempts = []
    box = None
    direct_return = None
    for attempt in range(1, POST_CAPTURE_NICKNAME_B_MAX_ATTEMPTS + 1):
        if attempt > 1:
            log(
                f"SHINY AUTO-CATCH: nickname B attempt {attempt-1} was acknowledged "
                "but prompt remained; re-proving exact prompt before B retry"
            )
            wait_exact_nickname_ready(
                POST_CAPTURE_NICKNAME_RETRY_READY_SECONDS,
                POST_CAPTURE_STATE_WAIT_SECONDS,
                f"POST_CAPTURE_NICKNAME_RETRY_{attempt}_READY",
            )

        log(
            f"SHINY AUTO-CATCH: sending nickname decline B attempt {attempt}/"
            f"{POST_CAPTURE_NICKNAME_B_MAX_ATTEMPTS}; A remains forbidden"
        )
        b_event = pulse_expected(
            HID_B, 180, 500, f"POST_CAPTURE_NICKNAME_DECLINE_B_{attempt}",
            None, nickname_flow, nickname_outer,
        )
        nickname_b_attempts.append(b_event)

        transition_deadline = time.monotonic() + POST_CAPTURE_NICKNAME_B_TRANSITION_WAIT_SECONDS
        while time.monotonic() < transition_deadline:
            check_stop()
            cur = {
                "battle": br.u32(core.BATTLE_ADDR),
                "flow": br.u32(flow_addr),
                "outer_ptr": br.u32(outer_ptr_addr),
            }
            transition = classify_post_nickname_b_transition(
                battle=cur["battle"],
                flow=cur["flow"],
                outer_ptr=cur["outer_ptr"],
                nickname_flow=nickname_flow,
                nickname_outer=nickname_outer,
                battle_inactive=core.BATTLE_INACTIVE,
                pre_capture_party_count=pre_capture_party_count,
            )
            if transition == "BOX_MESSAGE":
                box = cur
                report["states"].append({
                    "label": f"POST_CAPTURE_BOX_MESSAGE_AFTER_NICKNAME_B_{attempt}",
                    "battle": _hx(cur["battle"]),
                    "flow": _hx(cur["flow"]),
                    "outer_ptr": _hx(cur["outer_ptr"]),
                })
                break
            if transition == "PROMPT_STILL_ACTIVE":
                # B may have been ignored; keep observing until this attempt's
                # transition window expires, then safely retry B.
                time.sleep(POST_CAPTURE_POLL_SECONDS)
                continue
            if transition == "DIRECT_PARTY_RETURN":
                direct_return = cur
                report["states"].append({
                    "label": f"POST_CAPTURE_DIRECT_PARTY_RETURN_AFTER_NICKNAME_B_{attempt}",
                    "battle": _hx(cur["battle"]),
                    "flow": _hx(cur["flow"]),
                    "outer_ptr": _hx(cur["outer_ptr"]),
                })
                break

            # Any other transition is ambiguous and must not be followed by A.
            raise BagThrowError(
                "nickname B produced unexpected post-capture transition; "
                f"battle={_hx(cur['battle'])} flow={_hx(cur['flow'])} "
                f"outer={_hx(cur['outer_ptr'])}"
            )
        if box is not None or direct_return is not None:
            break

    if direct_return is not None:
        report["post_capture_order"].append("DIRECT_PARTY_RETURN")
        report["nickname_recovery"] = {
            "selection": "NO",
            "sequence": ["B"] * len(nickname_b_attempts),
            "decline_b_attempts": nickname_b_attempts,
            "decline_b_attempt_count": len(nickname_b_attempts),
            "initial_ready_stable_seconds": POST_CAPTURE_NICKNAME_READY_SECONDS,
            "retry_ready_stable_seconds": POST_CAPTURE_NICKNAME_RETRY_READY_SECONDS,
            "nickname_flow": _hx(nickname_flow),
            "nickname_outer_ptr": _hx(nickname_outer),
            "direct_return_flow": _hx(direct_return["flow"]),
            "direct_return_battle": _hx(direct_return["battle"]),
            "branch": "FREE_PARTY_SLOT_NO_BOX_MESSAGE",
            "pre_capture_party_count": report.get("pre_capture_party_count"),
            "pre_capture_free_slots": report.get("pre_capture_free_slots"),
            "expected_destination": report.get("expected_post_capture_destination"),
            "box_message_ack_count": 0,
            "safety": (
                "No further input after capacity-authorized free-party post-nickname transition/direct field; "
                "caller must prove method-specific field/grid authority before movement"
            ),
        }
        report["direct_return_state"] = {
            "battle": _hx(direct_return["battle"]),
            "flow": _hx(direct_return["flow"]),
            "outer_ptr": _hx(direct_return["outer_ptr"]),
        }
        report["result"] = (
            "POST_CAPTURE_RAM_RECOVERY_COMPLETE"
            if report["pokedex_observed"]
            else "POST_CAPTURE_RECOVERY_COMPLETE_POKEDEX_NOT_OBSERVED"
        )
        log(
            "SHINY AUTO-CATCH: free-party/no-Box return-in-progress confirmed "
            f"(flow={_hx(direct_return['flow'])}); sending no further input and "
            "requiring final field/grid RAM authority before hunting resumes"
        )
        return report

    if box is None:
        raise BagThrowError(
            f"nickname decline B was acknowledged {len(nickname_b_attempts)} times but "
            "the exact nickname flow+outer object remained active; no A sent"
        )

    report["post_capture_order"].append("BOX_MESSAGE")
    report["nickname_recovery"] = {
        "selection": "NO",
        "sequence": ["B"] * len(nickname_b_attempts),
        "decline_b_attempts": nickname_b_attempts,
        "decline_b_attempt_count": len(nickname_b_attempts),
        "initial_ready_stable_seconds": POST_CAPTURE_NICKNAME_READY_SECONDS,
        "retry_ready_stable_seconds": POST_CAPTURE_NICKNAME_RETRY_READY_SECONDS,
        "nickname_flow": _hx(nickname_flow),
        "nickname_outer_ptr": _hx(nickname_outer),
        "box_outer_ptr": _hx(box["outer_ptr"]),
        "box_authority": "same branch flow + outer_ptr changed from nickname object after B",
        "pre_capture_party_count": report.get("pre_capture_party_count"),
        "pre_capture_free_slots": report.get("pre_capture_free_slots"),
        "expected_destination": report.get("expected_post_capture_destination"),
        "safety": "A is impossible while exact nickname flow+outer object remains active",
    }

    log(
        "SHINY AUTO-CATCH: Box-message pointer transition confirmed — "
        "sending exactly one A"
    )
    box_a = pulse_expected(
        HID_A, 180, 500, "POST_CAPTURE_BOX_ACK_A",
        None, nickname_flow, box["outer_ptr"],
    )
    report["nickname_recovery"]["box_message_ack"] = box_a
    report["nickname_recovery"]["box_message_ack_count"] = 1

    # The probes saw flow 0x669 about 200 ms after the Box A, then normal field
    # battle=0x00040000/flow=5 by ~850 ms.  Accept either the mapped transition
    # or direct field return here; the caller still performs the stronger
    # method-specific field/grid authority gate before movement resumes.
    after_box = wait_state(
        lambda s: (
            s["flow"] == POST_CAPTURE_FLOW_AFTER_BOX
            or (s["battle"] == core.BATTLE_INACTIVE and s["flow"] == 0x00000005)
        ),
        POST_CAPTURE_TRANSITION_WAIT_SECONDS,
        "POST_CAPTURE_AFTER_BOX_A",
    )
    report["after_box_state"] = {
        "battle": _hx(after_box["battle"]),
        "flow": _hx(after_box["flow"]),
        "outer_ptr": _hx(after_box["outer_ptr"]),
    }

    report["result"] = (
        "POST_CAPTURE_RAM_RECOVERY_COMPLETE"
        if report["pokedex_observed"]
        else "POST_CAPTURE_RECOVERY_COMPLETE_POKEDEX_NOT_OBSERVED"
    )
    log(
        "SHINY AUTO-CATCH: mapped post-capture sequence complete; final "
        "field/grid RAM authority required before hunting resumes"
    )
    return report

