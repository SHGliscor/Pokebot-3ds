from __future__ import annotations

import threading
import time

from . import reset_route
from .oras_profiles import (
    ALPHA_SAPPHIRE_TITLE_ID,
    profile_from_game_info,
    validate_shared_birch_bag,
)

_RESET_ADAPTER_LOCK = threading.RLock()


class _OmegaRubyResetIdentityBridge:
    """Narrow identity adapter for the frozen AS reset-route state machine.

    Omega Ruby S2 hardware proved the same reset anchors/timing/state flow.
    The frozen reset source is kept byte-identical; this adapter changes only
    GAME_INFO identity strings while all RAM reads remain the real OR reads.
    """
    def __init__(self, bridge):
        self._bridge = bridge

    def __getattr__(self, name):
        return getattr(self._bridge, name)

    def read(self, address, length):
        return self._bridge.read(address, length)

    def game_info(self):
        gi = dict(self._bridge.game_info())
        profile = profile_from_game_info(gi)
        if profile and profile["key"] == "omega_ruby":
            gi["title_id"] = ALPHA_SAPPHIRE_TITLE_ID
            gi["process_name"] = "sango-2"
        return gi


def run_reset_to_bag_for_profile(
    bridge,
    inputs,
    log,
    profile,
    *,
    use_code_ips=False,
):
    """Run the proven reset route for a detected ORAS game profile."""
    if not profile:
        return False, {"status": "RESET_UNSUPPORTED_GAME_PROFILE"}

    if profile["key"] == "alpha_sapphire":
        return reset_route.run_reset_to_bag(
            bridge, inputs, log, use_code_ips=use_code_ips
        )

    if profile["key"] != "omega_ruby":
        return False, {
            "status": "RESET_UNSUPPORTED_GAME_PROFILE",
            "profile": profile,
        }

    # The only OR reset path promoted to production is the hardware-proven
    # 1.4 code.ips direct route.
    if not use_code_ips:
        return False, {
            "status": "OMEGA_RUBY_REQUIRES_PROVEN_CODE_IPS_ROUTE",
            "profile": profile,
        }

    adapted = _OmegaRubyResetIdentityBridge(bridge)

    # reset_route.py remains byte-identical. During this one reset call only,
    # swap its final bag callback to the shared cross-version RAM authority
    # proven by OR S2 6/6. One hunt worker can run at a time in the UI; the
    # lock also makes this explicit for any future callers.
    with _RESET_ADAPTER_LOCK:
        previous_validate_bag = reset_route.validate_bag
        reset_route.validate_bag = validate_shared_birch_bag
        try:
            ok, result = reset_route.run_reset_to_bag(
                adapted, inputs, log, use_code_ips=True
            )
        finally:
            reset_route.validate_bag = previous_validate_bag

    result = dict(result or {})
    result["game_profile"] = profile["key"]
    result["identity_adapter"] = "omega_ruby_to_frozen_as_reset_gate"
    result["bag_authority"] = "shared_oras_zone_xz"

    if not ok:
        return False, result

    # Re-assert the real OR identity and real shared Birch-bag authority after
    # the adapted frozen reset route returns.
    real_gi = bridge.game_info()
    real_profile = profile_from_game_info(real_gi)
    bag = validate_shared_birch_bag(bridge)
    result["post_reset_real_game_info"] = real_gi
    result["post_reset_shared_birch_bag"] = bag

    if not real_profile or real_profile["key"] != "omega_ruby":
        result["status"] = "OMEGA_RUBY_POST_RESET_REAL_IDENTITY_FAIL"
        return False, result
    if not bag.get("authority"):
        result["status"] = "OMEGA_RUBY_POST_RESET_BAG_AUTHORITY_FAIL"
        return False, result

    return True, result


def run_reset_to_field_for_profile(
    bridge,
    inputs,
    log,
    profile,
    field_validator,
    *,
    use_code_ips=False,
):
    """Reuse the proven reset route but end on a caller-supplied field gate.

    ``reset_route.py`` itself is intentionally not edited.  During this single
    reset call its final ``validate_bag`` callback is replaced under the same
    process-wide lock used by the Omega Ruby adapter.  This lets Static hunts
    reuse the proven soft-reset/title/Continue/communication-error flow while
    requiring RAM proof of the exact saved field tile instead of Birch's bag.

    The caller's validator must return a mapping containing ``authority``.
    """
    if not profile:
        return False, {"status": "RESET_UNSUPPORTED_GAME_PROFILE"}
    if profile.get("key") not in {"alpha_sapphire", "omega_ruby"}:
        return False, {
            "status": "RESET_UNSUPPORTED_GAME_PROFILE",
            "profile": profile,
        }
    if not callable(field_validator):
        return False, {"status": "RESET_FIELD_VALIDATOR_NOT_CALLABLE"}

    # OR has only one reset route promoted by hardware evidence: code.ips.
    if profile["key"] == "omega_ruby" and not use_code_ips:
        return False, {
            "status": "OMEGA_RUBY_REQUIRES_PROVEN_CODE_IPS_ROUTE",
            "profile": profile,
        }

    adapted = (
        bridge
        if profile["key"] == "alpha_sapphire"
        else _OmegaRubyResetIdentityBridge(bridge)
    )

    with _RESET_ADAPTER_LOCK:
        previous_validate_bag = reset_route.validate_bag
        reset_route.validate_bag = field_validator
        try:
            ok, result = reset_route.run_reset_to_bag(
                adapted,
                inputs,
                log,
                use_code_ips=bool(use_code_ips),
                # Static-only recovery: if code.ips still presents ORAS's
                # RAM-proven Communication Error screen, dismiss it once and
                # require this caller's exact saved-field validator afterward.
                allow_code_ips_comm_recovery=True,
            )
        finally:
            reset_route.validate_bag = previous_validate_bag

    result = dict(result or {})
    result["game_profile"] = profile["key"]
    result["final_authority"] = "caller_saved_field_validator"
    if profile["key"] == "omega_ruby":
        result["identity_adapter"] = "omega_ruby_to_frozen_as_reset_gate"

    if not ok:
        return False, result

    # Never trust the adapted identity outside the frozen route. Re-assert the
    # real title/process and the caller's exact field gate before authorising
    # any Static encounter input.
    real_gi = bridge.game_info()
    real_profile = profile_from_game_info(real_gi)
    # Final saved-field validation is read-only/idempotent. Hardware Zekrom
    # soak testing showed a rare single lost UDP reply after the reset route
    # had already re-established exact field/bag authority. Retry only a
    # TimeoutError, exactly once, against the same caller validator. No
    # controller input or reset action is replayed here.
    field = None
    validator_timeout_retry = False
    for validator_attempt in (1, 2):
        try:
            field = field_validator(bridge)
            if validator_attempt == 2:
                log(
                    "STATIC_POST_RESET_FIELD_VALIDATOR_RECOVERED",
                    attempt=validator_attempt,
                    attempts=2,
                )
            break
        except Exception as exc:
            # Preserve an explicit worker Stop across the common reset adapter;
            # callers already handle UserStop as a normal STOPPED outcome.
            if type(exc).__name__ == "UserStop":
                raise
            if isinstance(exc, TimeoutError) and validator_attempt == 1:
                validator_timeout_retry = True
                log(
                    "STATIC_POST_RESET_FIELD_VALIDATOR_RETRY",
                    attempt=validator_attempt,
                    attempts=2,
                    error=f"{type(exc).__name__}: {exc}",
                )
                time.sleep(0.08)
                continue
            result["status"] = "STATIC_POST_RESET_FIELD_VALIDATOR_ERROR"
            result["post_reset_real_game_info"] = real_gi
            result["field_validator_error"] = f"{type(exc).__name__}: {exc}"
            result["field_validator_attempts"] = validator_attempt
            result["field_validator_timeout_retry"] = validator_timeout_retry
            return False, result

    result["field_validator_attempts"] = 2 if validator_timeout_retry else 1
    result["field_validator_timeout_retry"] = validator_timeout_retry

    result["post_reset_real_game_info"] = real_gi
    result["post_reset_saved_field"] = field
    if not real_profile or real_profile["key"] != profile["key"]:
        result["status"] = "STATIC_POST_RESET_REAL_IDENTITY_FAIL"
        return False, result
    if not bool(field.get("authority")):
        result["status"] = "STATIC_POST_RESET_SAVED_FIELD_FAIL"
        return False, result
    return True, result
