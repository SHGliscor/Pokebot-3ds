from __future__ import annotations

"""Pokémon X/Y reset/title/Continue route.

This deliberately reuses the same controller timings and reset chord as the
proven ORAS reset backend, but replaces ORAS fixed-address module anchors with
read-only CRO module-presence gates.  X/Y ship the same DllTitle,
DllStartMenu and DllField modules, while PSS is a normal resident subsystem in
XY and is therefore not treated as a communication-error gate.
"""

import time

from .gen6_profiles import profile_from_game_info
from .gen6_cro import locate_loaded_modules
from .reset_route import (
    POST_RESET_INITIAL_SETTLE_S,
    REACQUIRE_DELAYS_S,
    TITLE_FIRST_ARM_DELAY_S,
    TITLE_SETTLE_S,
    CONTINUE_SETTLE_S,
    CONTINUE_RETRY_ARM_S,
    MAX_TITLE_INPUTS,
    MAX_CONTINUE_INPUTS,
    RESET_CHORD_BUTTONS,
    RESET_CHORD_LATCH_HOLD_MS,
    RESET_CHORD_PRE_NEUTRAL_MS,
    RESET_CHORD_POST_RELEASE_MS,
)

XY_ROUTE_MODULES = ("DllTitle", "DllStartMenu", "DllField")


def _pulse_a(inputs):
    return inputs.pulse(
        ("A",),
        hold_ms=300,
        resume_settle_ms=220,
        packet_interval_ms=30,
        release_ms=120,
    )


def _same_xy_profile(info, expected_profile):
    p = profile_from_game_info(info)
    return bool(
        p
        and p.get("family") == "xy"
        and p.get("key") == expected_profile.get("key")
        and p.get("process") == expected_profile.get("process")
    )


def _wait_for_restart(bridge, log, old_pid, profile):
    observations = []
    last_same = None
    for index, delay in enumerate(REACQUIRE_DELAYS_S, 1):
        if delay:
            time.sleep(delay)
        try:
            gi = bridge.game_info()
            ready = _same_xy_profile(gi, profile)
            pid = int(gi.get("pid", 0)) if ready else 0
            changed = bool(ready and pid > 0 and pid != int(old_pid or 0))
            if ready and pid == int(old_pid or 0):
                last_same = gi
            obs = {
                "check": index,
                "delay_s": delay,
                "process_ready": ready,
                "pid_changed": changed,
                "response": gi,
            }
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
            "XY_POST_RESET_REACQUIRE",
            check=index,
            delay_s=delay,
            ready=obs.get("process_ready"),
            pid_changed=obs.get("pid_changed"),
            pid=(obs.get("response") or {}).get("pid"),
            old_pid=old_pid,
            error=obs.get("error"),
        )
        if obs.get("pid_changed"):
            return "changed", obs.get("response"), observations
    if last_same is not None:
        return "same", last_same, observations
    return "missing", None, observations


def _sample_modules(bridge):
    found = locate_loaded_modules(bridge, XY_ROUTE_MODULES)
    return {
        "title": found.get("DllTitle"),
        "start": found.get("DllStartMenu"),
        "field": found.get("DllField"),
    }


def _module_presence(sample):
    return {k: bool(v) for k, v in sample.items()}


def run_reset_to_field(bridge, inputs, log, profile):
    if not profile or profile.get("family") != "xy":
        return False, {"status": "XY_RESET_UNSUPPORTED_PROFILE", "profile": profile}

    gi = bridge.game_info()
    if not _same_xy_profile(gi, profile):
        return False, {"status": "XY_RESET_PREFLIGHT_GAME_INFO_FAIL", "game_info": gi, "profile": profile}

    old_pid = int(gi.get("pid", 0))
    log(
        "XY_RESET_POLICY",
        game=profile.get("name"),
        process=profile.get("process"),
        use_code_ips=False,
        pss_policy="NORMAL_XY_SUBSYSTEM_NOT_A_RESET_ERROR_GATE",
        state_authority="GAME_INFO_PID + QUERY_ENUMERATED_CRO_MODULES",
        timing_source="shared_ORAS_reset_backend",
    )

    reset_runs = []
    post_gi = None
    reset_chord_attempts = 0
    for reset_chord_attempts in (1, 2):
        ack = inputs.hold_chord_latched(
            RESET_CHORD_BUTTONS,
            hold_ms=RESET_CHORD_LATCH_HOLD_MS,
            resume_settle_ms=RESET_CHORD_PRE_NEUTRAL_MS,
            release_ms=RESET_CHORD_POST_RELEASE_MS,
        )
        log(
            "XY_RESET_CHORD_LATCH_ACK",
            attempt=reset_chord_attempts,
            sequence=ack.get("sequence"),
            hold_ms=RESET_CHORD_LATCH_HOLD_MS,
            active_state=ack.get("active_state"),
            release_state=ack.get("release_state"),
        )
        time.sleep(POST_RESET_INITIAL_SETTLE_S)
        state, candidate, observations = _wait_for_restart(bridge, log, old_pid, profile)
        reset_runs.append({
            "attempt": reset_chord_attempts,
            "state": state,
            "observations": observations,
        })
        if state == "changed":
            post_gi = candidate
            break
        if state != "same":
            return False, {
                "status": "XY_RESET_PROCESS_REACQUIRE_FAIL",
                "old_pid": old_pid,
                "reset_chord_attempts": reset_chord_attempts,
                "reacquire_runs": reset_runs,
            }
        log("XY_RESET_CHORD_RETRY", reason="SAME_PID_STILL_ALIVE", old_pid=old_pid)

    if post_gi is None:
        return False, {
            "status": "XY_RESET_PROCESS_DID_NOT_RESTART",
            "old_pid": old_pid,
            "reset_chord_attempts": reset_chord_attempts,
            "reacquire_runs": reset_runs,
        }

    new_pid = int(post_gi.get("pid", 0))
    route_trace = []
    title_inputs = 0
    continue_inputs = 0
    continue_retry_armed = False
    title_armed = False
    start_seen = False
    post_start_a_inputs = 0
    deadline = time.monotonic() + 32.0

    while time.monotonic() < deadline:
        modules = _sample_modules(bridge)
        presence = _module_presence(modules)
        route_trace.append({
            "elapsed": round(32.0 - max(0.0, deadline - time.monotonic()), 3),
            "presence": presence,
            "bases": {k: (v or {}).get("base_hex") for k, v in modules.items()},
        })
        if len(route_trace) > 40:
            route_trace.pop(0)
        if presence["start"] and not start_seen:
            start_seen = True
            log(
                "XY_RESET_START_MENU_SEEN",
                title=presence["title"],
                field=presence["field"],
                title_inputs=title_inputs,
                continue_inputs=continue_inputs,
            )

        log(
            "XY_RESET_ROUTE_STATE",
            title=presence["title"],
            start=presence["start"],
            field=presence["field"],
            title_inputs=title_inputs,
            continue_inputs=continue_inputs,
            start_seen=start_seen,
            post_start_a_inputs=post_start_a_inputs,
        )

        # X/Y can keep DllTitle resident while the A press that actually
        # activates Continue is sent.  Counting inputs by whichever CRO happens
        # to be reported therefore misclassified a real Continue press as a
        # title press and blocked the post-load handoff.
        #
        # Hardware-safe authority:
        #   1. DllStartMenu must have been observed in this fresh process.
        #   2. At least one A pulse must have been sent AFTER that observation,
        #      regardless of whether stale DllTitle was still resident.
        #   3. DllField must then be stably present in two samples.
        # This proves we crossed the Continue boundary without relying on CRO
        # unload timing.
        if presence["field"] and start_seen and post_start_a_inputs > 0:
            time.sleep(0.35)
            confirm_modules = _sample_modules(bridge)
            confirm_presence = _module_presence(confirm_modules)
            log(
                "XY_FIELD_READY_CONFIRM",
                field=confirm_presence["field"],
                lingering_title=confirm_presence["title"],
                lingering_start=confirm_presence["start"],
                continue_inputs=continue_inputs,
                start_seen=start_seen,
                post_start_a_inputs=post_start_a_inputs,
            )
            if confirm_presence["field"]:
                return True, {
                    "status": "XY_FIELD_READY",
                    "old_pid": old_pid,
                    "new_pid": new_pid,
                    "reset_chord_attempts": reset_chord_attempts,
                    "title_inputs": title_inputs,
                    "continue_inputs": continue_inputs,
                    "start_seen": start_seen,
                    "post_start_a_inputs": post_start_a_inputs,
                    "use_code_ips": False,
                    "lingering_title_nonblocking": bool(confirm_presence["title"]),
                    "lingering_start_nonblocking": bool(confirm_presence["start"]),
                    "route_trace": route_trace,
                    "final_modules": confirm_modules,
                }

        if presence["title"]:
            if not title_armed:
                title_armed = True
                time.sleep(TITLE_FIRST_ARM_DELAY_S)
                continue
            if title_inputs >= MAX_TITLE_INPUTS:
                return False, {
                    "status": "XY_RESET_TITLE_INPUT_BUDGET_EXHAUSTED",
                    "old_pid": old_pid,
                    "new_pid": new_pid,
                    "route_trace": route_trace,
                }
            title_inputs += 1
            ack = _pulse_a(inputs)
            if start_seen:
                post_start_a_inputs += 1
                log(
                    "XY_RESET_POST_START_A",
                    classified_as="title",
                    post_start_a_inputs=post_start_a_inputs,
                    sequence=ack.get("sequence") if isinstance(ack, dict) else None,
                )
            time.sleep(TITLE_SETTLE_S)
            continue

        if presence["start"]:
            if continue_inputs == 1 and not continue_retry_armed:
                continue_retry_armed = True
                time.sleep(CONTINUE_RETRY_ARM_S)
                continue
            if continue_inputs >= MAX_CONTINUE_INPUTS:
                return False, {
                    "status": "XY_RESET_CONTINUE_INPUT_BUDGET_EXHAUSTED",
                    "old_pid": old_pid,
                    "new_pid": new_pid,
                    "route_trace": route_trace,
                }
            continue_inputs += 1
            continue_retry_armed = False
            ack = _pulse_a(inputs)
            post_start_a_inputs += 1
            log(
                "XY_RESET_POST_START_A",
                classified_as="continue_menu",
                post_start_a_inputs=post_start_a_inputs,
                sequence=ack.get("sequence") if isinstance(ack, dict) else None,
            )
            time.sleep(CONTINUE_SETTLE_S)
            continue

        # Normal transition blank between modules. No input is authorized.
        time.sleep(0.35)

    return False, {
        "status": "XY_RESET_FIELD_TIMEOUT",
        "old_pid": old_pid,
        "new_pid": new_pid,
        "title_inputs": title_inputs,
        "continue_inputs": continue_inputs,
        "start_seen": start_seen,
        "post_start_a_inputs": post_start_a_inputs,
        "route_trace": route_trace,
    }
