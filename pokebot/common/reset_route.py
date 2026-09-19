from __future__ import annotations

import time

from .oras_ram import ALPHA_SAPPHIRE_TITLE_ID, validate_bag

DIRECT_ROUTE_ANCHORS = {
    "DllTitle": (0x00748000, b"DllTitle\x00"),
    "DllStartMenu": (0x006F6000, b"DllStartMenu\x00"),
    "DllField": (0x00802000, b"DllField\x00"),
    "DllPssMessageWindow": (0x008144F5, b"DllPssMessageWindow\x00"),
}

# Fast-but-bounded reset timing profile.
#
# The old route waited 11.5 s before even checking whether Alpha Sapphire had
# restarted. The new profile begins process-restart checks at 8.5 s and only
# advances when a NEW sango-2 PID is observed. This is process-state gating,
# not continuous game-RAM polling.
POST_RESET_INITIAL_SETTLE_S = 8.5
REACQUIRE_DELAYS_S = (0.0, 0.75, 0.75, 0.75, 1.0, 1.0, 1.0)

# Title inputs remain RAM-gated. These reductions remove conservative dead
# time without touching the proven bag/starter choreography.
TITLE_FIRST_ARM_DELAY_S = 1.50
TITLE_SETTLE_S = 1.80
MAX_TITLE_INPUTS = 6

# First Continue transition is checked earlier. If RAM still says Continue,
# one extra settle-only observation is required before a second A is allowed.
CONTINUE_SETTLE_S = 2.50
CONTINUE_RETRY_ARM_S = 0.75
COMM_SETTLE_S = 1.5
UNKNOWN_SETTLE_S = 1.0
FIELD_SETTLE_S = 1.5

MAX_CONTINUE_INPUTS = 2
MAX_COMM_DISMISSALS = 2

# Reset-only controller profile.  v0p42M hardware validation showed that a
# command-6 timed pulse could be acknowledged and completed while ORAS still
# missed the soft-reset chord.  v0p42N uses the firmware's retained-HID latch
# (command 10) only for this chord, then explicitly RELEASE_ALL (command 8).
# Starter/menu inputs remain on their existing timed-pulse choreography.
RESET_CHORD_BUTTONS = ("L", "R", "START", "SELECT")
RESET_CHORD_LATCH_HOLD_MS = 600
RESET_CHORD_PRE_NEUTRAL_MS = 80
RESET_CHORD_POST_RELEASE_MS = 150


def _read_anchor(bridge, module_name):
    address, expected = DIRECT_ROUTE_ANCHORS[module_name]
    try:
        actual = bridge.read(address, len(expected))
        return actual == expected
    except Exception:
        return False

def classify_route(bridge, comm_dismissals):
    pss = _read_anchor(bridge, "DllPssMessageWindow")
    start = _read_anchor(bridge, "DllStartMenu")
    field = _read_anchor(bridge, "DllField")
    title = _read_anchor(bridge, "DllTitle")

    if comm_dismissals >= 1 and field:
        return "field", {"pss": pss, "start": start, "field": field, "title": title}
    if pss:
        return "communication_error", {"pss": pss, "start": start, "field": field, "title": title}
    if start:
        return "continue_menu", {"pss": pss, "start": start, "field": field, "title": title}
    if field:
        return "field", {"pss": pss, "start": start, "field": field, "title": title}
    if title:
        return "title", {"pss": pss, "start": start, "field": field, "title": title}
    return "unknown", {"pss": pss, "start": start, "field": field, "title": title}

def wait_for_game_restart(bridge, log, old_pid):
    """Wait for authoritative process restart without blindly sleeping to 11.5 s.

    Returns:
        ("changed", game_info, observations) when a new Alpha Sapphire PID is live.
        ("same", last_same_pid_info, observations) when the old process stayed live.
        ("missing", None, observations) when no authoritative game process was
        present by the end of the bounded window.
    """
    observations = []
    last_same = None

    for index, delay in enumerate(REACQUIRE_DELAYS_S, 1):
        if delay:
            time.sleep(delay)

        try:
            gi = bridge.game_info()
            process_ready = (
                gi.get("status") == 0
                and gi.get("title_id") == ALPHA_SAPPHIRE_TITLE_ID
                and gi.get("process_name") == "sango-2"
                and int(gi.get("pid", 0)) > 0
            )
            pid = gi.get("pid") if process_ready else None
            pid_changed = bool(process_ready and pid != old_pid)

            obs = {
                "check": index,
                "delay_s": delay,
                "process_ready": process_ready,
                "pid_changed": pid_changed,
                "response": gi,
            }

            if process_ready and pid == old_pid:
                last_same = gi

        except Exception as exc:
            obs = {
                "check": index,
                "delay_s": delay,
                "process_ready": False,
                "pid_changed": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

        observations.append(obs)
        log(
            "POST_RESET_REACQUIRE",
            check=index,
            delay_s=delay,
            ready=obs["process_ready"],
            pid_changed=obs["pid_changed"],
            pid=(obs.get("response") or {}).get("pid"),
            old_pid=old_pid,
            error=obs.get("error"),
        )

        if obs["pid_changed"]:
            return "changed", obs["response"], observations

    if last_same is not None:
        return "same", last_same, observations
    return "missing", None, observations

def run_reset_to_bag(
    bridge,
    inputs,
    log,
    use_code_ips=False,
    *,
    allow_code_ips_comm_recovery=False,
):
    gi = bridge.game_info()
    if gi.get("status") != 0 or gi.get("title_id") != ALPHA_SAPPHIRE_TITLE_ID:
        return False, {"status": "RESET_PREFLIGHT_GAME_INFO_FAIL", "game_info": gi}

    reset_policy = "code_ips_direct" if use_code_ips else "legacy_comm_error"
    log(
        "RESET_POLICY",
        use_code_ips=bool(use_code_ips),
        reset_policy=reset_policy,
        input_transport="Pokebot-Luma acknowledged controller UDP 4952",
    )
    log(
        "RESET_TIMING_PROFILE",
        pre_title_initial_s=POST_RESET_INITIAL_SETTLE_S,
        reacquire_delays_s=REACQUIRE_DELAYS_S,
        title_first_arm_s=TITLE_FIRST_ARM_DELAY_S,
        title_settle_s=TITLE_SETTLE_S,
        continue_settle_s=CONTINUE_SETTLE_S,
        continue_retry_arm_s=CONTINUE_RETRY_ARM_S,
        field_settle_s=FIELD_SETTLE_S,
        starter_timing_unchanged=True,
        reset_chord_mode="retained_hid_latch",
        reset_chord_hold_ms=RESET_CHORD_LATCH_HOLD_MS,
        code_ips_comm_recovery_enabled=bool(
            use_code_ips and allow_code_ips_comm_recovery
        ),
    )

    old_pid = gi.get("pid")

    # Hardware evidence from the unlimited Qt loop showed one Pokebot-Luma
    # reset chord that completed at the controller layer but did not actually
    # restart the Alpha Sapphire process: post-reset GAME_INFO returned the
    # exact same PID and route anchors stayed unknown.
    #
    # Safety policy:
    # - Require process restart authority (PID must change).
    # - Retry the reset chord exactly once ONLY when the old process is still
    #   positively alive with the same PID.
    # - Do not retry when the game cannot be reacquired; that remains a HOLD.
    reset_chord_attempts = 0
    reset_reacquire_runs = []
    post_gi = None

    for reset_chord_attempts in range(1, 3):
        chord_ack = inputs.hold_chord_latched(
            RESET_CHORD_BUTTONS,
            hold_ms=RESET_CHORD_LATCH_HOLD_MS,
            resume_settle_ms=RESET_CHORD_PRE_NEUTRAL_MS,
            release_ms=RESET_CHORD_POST_RELEASE_MS,
        )
        log(
            "RESET_CHORD_LATCH_ACK",
            reset_chord_attempt=reset_chord_attempts,
            mode=chord_ack.get("mode"),
            sequence=chord_ack.get("sequence"),
            requested_raw_hid=f"0x{int(chord_ack.get('requested_raw_hid', 0)):03X}",
            active_state=chord_ack.get("active_state"),
            active_raw_hid=f"0x{int(chord_ack.get('active_raw_hid', 0)):03X}",
            release_state=chord_ack.get("release_state"),
            release_raw_hid=f"0x{int(chord_ack.get('release_raw_hid', 0)):03X}",
            hold_ms=RESET_CHORD_LATCH_HOLD_MS,
        )

        time.sleep(POST_RESET_INITIAL_SETTLE_S)

        restart_state, candidate_gi, reacq = wait_for_game_restart(bridge, log, old_pid)
        reset_reacquire_runs.append({
            "reset_chord_attempt": reset_chord_attempts,
            "restart_state": restart_state,
            "observations": reacq,
        })

        if restart_state == "changed":
            post_gi = candidate_gi
            log(
                "RESET_PROCESS_RESTART_CONFIRMED",
                reset_chord_attempt=reset_chord_attempts,
                old_pid=old_pid,
                new_pid=post_gi.get("pid"),
            )
            break

        if restart_state == "missing":
            return False, {
                "status": "RESET_GAME_REACQUIRE_FAIL",
                "old_pid": old_pid,
                "reset_chord_attempts": reset_chord_attempts,
                "reacquire_observations": reset_reacquire_runs,
            }

        candidate_pid = candidate_gi.get("pid")
        log(
            "RESET_PID_UNCHANGED_AFTER_CHORD",
            reset_chord_attempt=reset_chord_attempts,
            old_pid=old_pid,
            observed_pid=candidate_pid,
            retry_authorized=reset_chord_attempts < 2,
        )

        if reset_chord_attempts >= 2:
            return False, {
                "status": "RESET_PID_UNCHANGED_AFTER_RETRY",
                "old_pid": old_pid,
                "observed_pid": candidate_pid,
                "reset_chord_attempts": reset_chord_attempts,
                "reacquire_observations": reset_reacquire_runs,
            }

        # Mirror the controller-neutral boundary that passed the dedicated
        # 10-cycle hardware test before issuing the one allowed retry.
        inputs.input_ping()
        inputs.release_all()
        log(
            "RESET_RETRY_CONTROLLER_NEUTRAL",
            reset_chord_attempt=reset_chord_attempts,
            old_pid=old_pid,
        )

    title_inputs = 0
    title_armed = False
    continue_inputs = 0
    continue_retry_armed = False
    comm_dismissals = 0
    comm_detected = False
    unknown_budget = 2
    # Pre-title startup blanks and the short post-title -> Continue transition
    # are separate states. Hardware support evidence shows one post-title
    # unknown frame can be legitimate even after both pre-title unknown probes
    # were consumed, so keep a dedicated one-probe transition budget.
    post_title_unknown_budget = 1
    route_trace = []
    field_reached = False

    for step in range(1, 14):
        kind, presence = classify_route(bridge, comm_dismissals)
        route_trace.append({
            "step": step,
            "kind": kind,
            "presence": presence,
        })
        log(
            "RESET_ROUTE_STATE",
            step=step,
            kind=kind,
            title_inputs=title_inputs,
            presence=presence,
        )

        if kind == "title":
            if not title_armed:
                title_armed = True
                time.sleep(TITLE_FIRST_ARM_DELAY_S)
                continue
            if title_inputs >= MAX_TITLE_INPUTS:
                return False, {
                    "status": "RESET_TITLE_INPUT_BUDGET_EXHAUSTED",
                    "route_trace": route_trace,
                }
            title_inputs += 1
            inputs.pulse(
                ("A",),
                hold_ms=300,
                resume_settle_ms=220,
                packet_interval_ms=30,
                release_ms=120,
            )
            time.sleep(TITLE_SETTLE_S)
            continue

        if kind == "continue_menu":
            # The first Continue A is unchanged. After it, inspect RAM earlier.
            # If the menu is still resident, do NOT immediately send another A:
            # first spend one additional bounded settle-only window. Only if RAM
            # still says Continue afterward is the second A authorized.
            if continue_inputs == 1 and not continue_retry_armed:
                continue_retry_armed = True
                log(
                    "RESET_CONTINUE_RETRY_ARM",
                    continue_inputs=continue_inputs,
                    settle_only_s=CONTINUE_RETRY_ARM_S,
                )
                time.sleep(CONTINUE_RETRY_ARM_S)
                continue

            if continue_inputs >= MAX_CONTINUE_INPUTS:
                return False, {"status": "RESET_CONTINUE_MAX", "route_trace": route_trace}

            continue_inputs += 1
            continue_retry_armed = False
            inputs.pulse(
                ("A",),
                hold_ms=300,
                resume_settle_ms=220,
                packet_interval_ms=30,
                release_ms=120,
            )
            time.sleep(CONTINUE_SETTLE_S)
            continue

        if kind == "communication_error":
            # With code.ips enabled, hardware evidence showed that
            # DllPssMessageWindow can remain resident after Continue even
            # though the player has already returned to the Birch bag and
            # DllField is authoritative. Presence of PSS alone is therefore
            # not enough to prove an on-screen communication error when Field
            # is also resident.
            #
            # Safety rule: never send communication-error dismissal input in
            # code.ips mode. For the ambiguous PSS+Field combination, perform
            # one bounded Birch-bag authority validation. Only a fully valid
            # bag state is allowed to win; otherwise HOLD exactly as before.
            if use_code_ips and presence.get("field"):
                try:
                    field_pss_bag = validate_bag(bridge)
                except Exception as exc:
                    # Worker cancellation must remain a normal manual-stop
                    # signal.  Gift/static validators may call their worker's
                    # _check_stop() while this shared reset route is probing
                    # field authority.  Do not reclassify UserStop as a reset
                    # communication/probe failure.
                    if type(exc).__name__ == "UserStop":
                        raise
                    log(
                        "RESET_CODE_IPS_FIELD_PSS_BAG_PROBE_ERROR",
                        presence=presence,
                        reset_policy=reset_policy,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    return False, {
                        "status": "RESET_CODE_IPS_FIELD_PSS_BAG_PROBE_ERROR",
                        "use_code_ips": True,
                        "reset_policy": reset_policy,
                        "communication_error_dismissals": 0,
                        "presence": presence,
                        "error": f"{type(exc).__name__}: {exc}",
                        "route_trace": route_trace,
                    }

                log(
                    "RESET_CODE_IPS_FIELD_PSS_BAG_PROBE",
                    presence=presence,
                    reset_policy=reset_policy,
                    bag_authority=bool(field_pss_bag.get("authority")),
                )

                if field_pss_bag.get("authority"):
                    route_trace[-1]["kind"] = "field_pss_resident"
                    route_trace[-1]["bag_authority"] = True
                    field_reached = True
                    break

                comm_detected = True
                route_trace[-1]["bag_authority"] = False
                log(
                    "RESET_UNEXPECTED_COMM_ERROR_CODE_IPS",
                    presence=presence,
                    reset_policy=reset_policy,
                    reason="pss_and_field_present_but_bag_authority_failed",
                )
                return False, {
                    "status": "RESET_UNEXPECTED_COMM_ERROR_CODE_IPS",
                    "use_code_ips": True,
                    "reset_policy": reset_policy,
                    "communication_error_dismissals": 0,
                    "bag": field_pss_bag,
                    "route_trace": route_trace,
                }

            comm_detected = True

            # Static hunts can legitimately reach ORAS's Communication Error
            # screen even while code.ips is enabled. Hardware evidence from
            # the Zekrom soak test shows the unambiguous route signature:
            # PSS resident, with Title/Start/Field all absent.
            #
            # Recovery remains deliberately narrower than the legacy path:
            # authorize exactly ONE A only for that exact RAM signature, then
            # require the route to leave communication_error and re-prove the
            # saved field. Mixed PSS+Field remains governed by the bag/field
            # validator above and never receives dismissal input.
            if use_code_ips:
                unambiguous_comm_error = bool(
                    presence.get("pss")
                    and not presence.get("field")
                    and not presence.get("start")
                    and not presence.get("title")
                )
                if allow_code_ips_comm_recovery and unambiguous_comm_error:
                    if comm_dismissals >= 1:
                        log(
                            "RESET_CODE_IPS_COMM_REMAINED",
                            dismissals=comm_dismissals,
                            presence=presence,
                            reset_policy=reset_policy,
                        )
                        return False, {
                            "status": "RESET_CODE_IPS_COMM_REMAINED",
                            "use_code_ips": True,
                            "reset_policy": reset_policy,
                            "communication_error_dismissals": comm_dismissals,
                            "route_trace": route_trace,
                        }

                    comm_dismissals += 1
                    log(
                        "RESET_CODE_IPS_COMM_DISMISS",
                        dismissal=comm_dismissals,
                        max_dismissals=1,
                        presence=presence,
                        reset_policy=reset_policy,
                    )
                    inputs.pulse(
                        ("A",),
                        hold_ms=300,
                        resume_settle_ms=220,
                        packet_interval_ms=30,
                        release_ms=120,
                    )
                    time.sleep(COMM_SETTLE_S)
                    continue

                log(
                    "RESET_UNEXPECTED_COMM_ERROR_CODE_IPS",
                    presence=presence,
                    reset_policy=reset_policy,
                    recovery_enabled=bool(allow_code_ips_comm_recovery),
                    unambiguous_comm_error=unambiguous_comm_error,
                )
                return False, {
                    "status": "RESET_UNEXPECTED_COMM_ERROR_CODE_IPS",
                    "use_code_ips": True,
                    "reset_policy": reset_policy,
                    "communication_error_dismissals": comm_dismissals,
                    "route_trace": route_trace,
                }

            # No code.ips: restore/preserve the proven legacy RAM-gated
            # communication-error handling. A second A is allowed ONLY when
            # RAM still classifies the state as communication_error.
            if comm_dismissals >= MAX_COMM_DISMISSALS:
                return False, {
                    "status": "RESET_COMM_REMAINED",
                    "communication_error_dismissals": comm_dismissals,
                    "route_trace": route_trace,
                }

            comm_dismissals += 1
            log(
                "RESET_COMM_DISMISS",
                dismissal=comm_dismissals,
                max_dismissals=MAX_COMM_DISMISSALS,
                presence=presence,
            )
            if comm_dismissals > 1:
                log(
                    "RESET_COMM_SECOND_DISMISS",
                    dismissal=comm_dismissals,
                    max_dismissals=MAX_COMM_DISMISSALS,
                    presence=presence,
                )

            inputs.pulse(
                ("A",),
                hold_ms=300,
                resume_settle_ms=220,
                packet_interval_ms=30,
                release_ms=120,
            )
            time.sleep(COMM_SETTLE_S)
            continue

        if kind == "field":
            field_reached = True
            break

        if kind == "unknown":
            # Support exports 20260824_031738 / 105056 / 132004 show the same
            # legitimate route shape:
            #   unknown, unknown, title, title, title, unknown, continue_menu
            # The shared unknown_budget used to HOLD at that post-title blank
            # because the two startup unknowns had already consumed it.
            #
            # This allowance is intentionally narrow:
            # - title input must already have been sent;
            # - Continue must not have been pressed yet;
            # - only one settle-only post-title unknown is allowed;
            # - no gameplay input is sent from this path.
            if title_inputs > 0 and continue_inputs == 0:
                if post_title_unknown_budget <= 0:
                    return False, {
                        "status": "RESET_POST_TITLE_UNKNOWN_BUDGET_EXHAUSTED",
                        "route_trace": route_trace,
                    }
                post_title_unknown_budget -= 1
                log(
                    "RESET_POST_TITLE_UNKNOWN_SETTLE",
                    title_inputs=title_inputs,
                    remaining_budget=post_title_unknown_budget,
                    presence=presence,
                )
                time.sleep(UNKNOWN_SETTLE_S)
                continue

            if unknown_budget <= 0:
                return False, {
                    "status": "RESET_UNKNOWN_BUDGET_EXHAUSTED",
                    "route_trace": route_trace,
                }
            unknown_budget -= 1
            time.sleep(UNKNOWN_SETTLE_S)
            continue

    if not field_reached:
        return False, {"status": "RESET_FIELD_NOT_REACHED", "route_trace": route_trace}

    time.sleep(FIELD_SETTLE_S)

    # Final bag authority validation.
    #
    # Hardware support evidence showed rare single-packet UDP bridge timeouts
    # here after the route had already reached the authoritative field state.
    # Retry ONLY TimeoutError, and only once. A completed invalid bag read
    # remains an immediate safety failure.
    bag = None
    bag_transport_attempts = 0
    for bag_transport_attempts in range(1, 3):
        try:
            bag = validate_bag(bridge)
            break
        except TimeoutError as exc:
            log(
                "RESET_ROUTE_BAG_TRANSPORT_RETRY",
                attempt=bag_transport_attempts,
                max_attempts=2,
                error=f"{type(exc).__name__}: {exc}",
            )
            if bag_transport_attempts >= 2:
                raise
            time.sleep(0.30)

    if not bag["authority"]:
        return False, {
            "status": "RESET_BAG_GATE_FAIL",
            "bag": bag,
            "route_trace": route_trace,
            "bag_transport_attempts": bag_transport_attempts,
        }

    return True, {
        "status": "PASS",
        "old_pid": old_pid,
        "new_pid": post_gi.get("pid"),
        "pid_changed": True,
        "reset_chord_attempts": reset_chord_attempts,
        "reset_reacquire_runs": reset_reacquire_runs,
        "title_inputs": title_inputs,
        "continue_inputs": continue_inputs,
        "use_code_ips": bool(use_code_ips),
        "reset_policy": reset_policy,
        "communication_error_handling_enabled": bool(
            (not use_code_ips) or allow_code_ips_comm_recovery
        ),
        "code_ips_communication_error_recovery": bool(
            use_code_ips and allow_code_ips_comm_recovery
        ),
        "communication_error_detected": comm_detected,
        "communication_error_dismissals": comm_dismissals,
        "bag": bag,
        "bag_transport_attempts": bag_transport_attempts,
        "route_trace": route_trace,
    }
