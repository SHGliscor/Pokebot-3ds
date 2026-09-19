from __future__ import annotations

import json
import math
import sys
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pokebot.wild.registry import GAMES
from pokebot.wild.fishing_chain import (
    ConsecutiveFishingTracker,
    shiny_odds_for_chain,
    shiny_rolls_for_chain,
)
from pokebot.wild.auto_capture import build_identity_lock, auto_capture_supported_for_game, auto_capture_test_eligible
from pokebot.wild.oras_move_policy import ensure_move_metadata, choose_safe_attack
from pokebot.wild.honey_horde import (
    HONEY_ITEM_ID, ORAS_ITEMS_ADDR, ORAS_ITEMS_SIZE,
    require_honey, read_items_controller_state, honey_active_index,
    CONTROLLER_CURSOR_PTR_OFF, CONTROLLER_ENTRY_COUNT_OFF,
    CONTROLLER_ENTRY_ARRAY_OFF, CONTROLLER_PAGE_OFF, CURSOR_SELECTOR_OFF,
)
from pokebot.wild.battle_bag_throw import (
    classify_post_nickname_b_transition,
    POST_CAPTURE_FLOW_DIRECT_PARTY_RETURN,
    POST_CAPTURE_FLOW_DIRECT_PARTY_RETURN_REGISTERED,
    POST_CAPTURE_FLOW_DIRECT_PARTY_RETURN_UNREGISTERED,
    POST_CAPTURE_FLOW_DIRECT_PARTY_TRANSITION_FREE,
    POST_CAPTURE_FLOW_DIRECT_PARTY_TRANSITION_FINAL_SLOT_REGISTERED,
    post_capture_destination_for_party_count,
)
from pokebot.common.evolution_prediction import predict_split_evolution
from pokebot.common.live_party import payload_from_parsed
from pokebot.common.shiny_odds import (
    detect_oras_shiny_charm, ORAS_14_KEY_ITEMS_ADDR, ORAS_KEY_ITEMS_SIZE,
    resolve_shiny_odds, cumulative_probability, format_cumulative_percent,
    phase_progress_bar_value, advance_phase_log_miss,
    cumulative_probability_from_log_miss,
)
from pokebot.static.soaring_mapper import read_soaring_sample, summarize_trace
from pokebot.static.skytrip_runtime import (
    SKYTRIP_FILE_SIZE, SKYTRIP_BSS_SIZE, SKYTRIP_MODULE_NAME_SIZE,
    SKYTRIP_DATA_OFFSET, SKYTRIP_DATA_SIZE, MAINPROC_VPTR_LITERAL_OFFSET,
    SKYTRIP_VPTR_OFFSETS, locate_skytrip_module, verify_skytrip_relocation,
    discover_skytrip_objects, choose_trace_objects, read_trace_objects,
    read_runtime_module_state, diff_runtime_module_state, _probe_heap_anchor_vptrs,
    discover_skytrip_external_references, read_external_trace_targets, diff_external_trace_state,
    discover_skytrip_fast_references, build_fast_trace_batches,
    read_external_trace_targets_batched, update_fast_motion_scores,
)
from pokebot.static.oras_static import (
    StaticEncounterStateMachine,
    get_static_profile,
    static_profiles_for_game,
    read_saved_field_anchor,
    validate_any_loaded_field,
    validate_saved_field_anchor,
    hardware_validation_label,
)
from pokebot.common.oras_profiles import BATTLE_STATE, BATTLE_INACTIVE, ZONE_ADDR, PRIMARY_BASE, SECONDARY_BASE
from pokebot.gift.oras_gifts import gift_profiles_for_game, get_gift_profile, find_new_gift, find_new_party_member, parsed_identity, FOSSIL_SPECIES
import struct


def expect(cond, message):
    if not cond:
        raise AssertionError(message)


def test_omega_ruby_surf_fishing_exposed():
    omega = GAMES["0x000400000011c400"]
    methods = {m["key"]: m for m in omega["wild_methods"]}
    expect("surf" in methods, "Omega Ruby Surf must be selectable for hardware validation")
    expect("fishing" in methods, "Omega Ruby Fishing must be selectable for hardware validation")
    expect(methods["surf"].get("requires_hid_pulse") is True, "OR Surf HID pulse contract")
    expect(methods["fishing"].get("requires_hid_pulse") is True, "OR Fishing HID pulse contract")


def test_eon_ticket_run_reinteract_static_contract():
    latias = get_static_profile("latias_eon")
    latios = get_static_profile("latios_eon")
    expect(latias.name == "Latias" and latias.location == "Southern Island (Eon Ticket)", "OR Eon Ticket static label")
    expect(latios.name == "Latios" and latios.location == "Southern Island (Eon Ticket)", "AS Eon Ticket static label")
    expect(latias.reset_kind == "RUN_REINTERACT" and latios.reset_kind == "RUN_REINTERACT", "Eon Ticket statics use generic RUN_REINTERACT reset strategy")
    kecleon = get_static_profile("kecleon")
    expect(kecleon.max_trigger_pulses == 12, "Kecleon Devon Scope trigger has twelve-press bounded A ceiling with battle RAM checked after every press")
    dexnav_tutorial = get_static_profile("poochyena_dexnav_tutorial")
    expect(dexnav_tutorial.max_trigger_pulses == 17, "DexNav tutorial Poochyena uses fourteen fast A presses plus three hardware-corrected cleanup presses")
    expect(kecleon.choreography_status.startswith("HARDWARE_PROVEN_SHARED_ORAS"), "Kecleon hardware proof is shared across ORAS")
    expect(latias.trigger_kind == "INTERACT_A" and latios.trigger_kind == "INTERACT_A", "Eon Ticket statics re-interact with A")
    expect("RUN → re-interact" in hardware_validation_label(latias, "omega_ruby"), "Eon Ticket validation label exposes run/re-interact mapper")
    static_text = (ROOT / "qt_ui" / "static_worker.py").read_text(encoding="utf-8")
    for token in (
        'self.static_profile.reset_kind == "RUN_REINTERACT"',
        'core.causal_run_until_field(battle_br)',
        'STATIC RUN_REINTERACT FIELD AUTHORITY',
        '"next_action": "INTERACT_A_ON_NEXT_LOOP"',
        'stable_samples=3',
        'def _trigger_run_reinteract',
        'bounded Eon Ticket re-interact A budget exhausted without battle',
        'for index in range(1, 9)',
        'grace_deadline = time.monotonic() + 2.0',
        'self.run_reinteract_pending = True',
    ):
        expect(token in static_text, f"Eon Ticket RUN_REINTERACT worker token {token}")
    dash = (ROOT / "qt_ui" / "dashboard_page.py").read_text(encoding="utf-8")
    expect('profile.reset_kind == "RUN_REINTERACT"' in dash, "Static UI shows RUN_REINTERACT method")


def test_fishing_odds():
    expect(shiny_rolls_for_chain(0, False) == 1, "base fishing rolls")
    expect(shiny_rolls_for_chain(1, False) == 3, "chain1 fishing rolls")
    expect(shiny_rolls_for_chain(20, False) == 41, "chain20 fishing rolls")
    expect(shiny_rolls_for_chain(999, False) == 41, "fishing rolls cap")
    expect(shiny_rolls_for_chain(20, True) == 43, "chain20 charm rolls")
    odds = shiny_odds_for_chain(20, False)
    expect(99.0 < odds["one_in"] < 101.0, "chain20 approx 1/100")


def test_cumulative_phase_probability_and_charm_contract():
    full = resolve_shiny_odds("ORAS", "Wild", False, True)
    charm = resolve_shiny_odds("ORAS", "Wild", True, True)
    starter = resolve_shiny_odds("ORAS", "Starter", True, False)
    gift = resolve_shiny_odds("ORAS", "Gift", True, False)
    static = resolve_shiny_odds("ORAS", "Static", True, True)

    expect(full.rolls == 1 and abs(full.probability - (1.0 / 4096.0)) < 1e-15,
           "full odds are one 1/4096 roll")
    p4096 = cumulative_probability(4096, full.probability)
    expect(abs(p4096 - 0.6321654705556539) < 1e-12,
           "4096 full-odds checks are ~63.2165% cumulative, not 100%")
    expect(format_cumulative_percent(p4096) == "63.22%",
           "phase text reports cumulative probability")
    expect(phase_progress_bar_value(p4096) < 10000,
           "finite phase never fills the probability bar to 100%")
    extreme = cumulative_probability(1_000_000, full.probability)
    expect(extreme < 1.0, "even extreme finite hunt counts remain below mathematical certainty")
    expect(format_cumulative_percent(extreme) == "99.99+%",
           "extreme finite phases never display 100.00%")
    expect(phase_progress_bar_value(extreme) == 9999,
           "extreme finite phases never paint a fully complete bar")

    expect(charm.rolls == 3 and 1365.0 < charm.one_in < 1366.0,
           "Gen VI Charm uses three total rolls (~1/1365.67)")
    p1366 = cumulative_probability(1366, charm.probability)
    expect(0.632 < p1366 < 0.633,
           "1366 Charm-affected encounters are ~63.2% cumulative, not 100%")
    expect(starter.rolls == 1 and gift.rolls == 1,
           "starters and gifts/fossils ignore Shiny Charm")
    expect(static.rolls == 3, "Charm applies to eligible static encounter telemetry")

    log_miss = 0.0
    expected_miss = 1.0
    for pre_chain in (0, 1, 2):
        row = shiny_odds_for_chain(pre_chain, False)
        log_miss = advance_phase_log_miss(log_miss, row["probability"])
        expected_miss *= 1.0 - row["probability"]
    actual = cumulative_probability_from_log_miss(log_miss)
    expect(abs(actual - (1.0 - expected_miss)) < 1e-15,
           "fishing cumulative phase multiplies each encounter's actual chain miss chance")

    dash = (ROOT / "qt_ui" / "dashboard_page.py").read_text(encoding="utf-8")
    expect("CUMULATIVE SHINY CHANCE" in dash, "Dashboard phase bar is cumulative probability")
    expect("phase_progress_bar_value" in dash, "Dashboard uses finite cumulative bar value")
    stats = (ROOT / "qt_ui" / "stats_page.py").read_text(encoding="utf-8")
    expect("Cumulative Shiny Chance" in stats and "Est. Time to 63.2%" in stats,
           "Stats page uses cumulative chance and probability ETA")
    gift_worker = (ROOT / "qt_ui" / "gift_worker.py").read_text(encoding="utf-8")
    expect("max(0, self.session_seen - 1)" not in gift_worker,
           "Gift reset stats are real reset counts, not encounter-count inference")
    expect("_record_fossil_batch_completion" in gift_worker,
           "Fossil batch completion is persisted separately")
    expect("fossil_batch_size_counts" in gift_worker,
           "Fossil stats preserve adaptive 1-5 batch-size history")


def test_fishing_tracker():
    t = ConsecutiveFishingTracker()
    t.observe_anchor(101, [3, 4])
    t.record_cast(); e1 = t.record_hooked_encounter()
    expect(t.chain == 1 and e1["encounter_odds"]["rolls"] == 1, "first hook")
    t.record_cast(); e2 = t.record_hooked_encounter()
    expect(t.chain == 2 and e2["encounter_odds"]["rolls"] == 3, "second hook")
    no = t.record_no_bite()
    expect(t.chain == 0 and no["previous_chain"] == 2, "no bite resets chain")
    t.record_hooked_encounter(); t.record_hooked_encounter()
    t.observe_anchor(101, [4, 4])
    expect(t.chain == 0 and t.last_break_reason == "MOVED_FROM_FISHING_SPOT", "movement resets chain")
    t.record_hooked_encounter(); t.record_missed_hook("TEST_MISS")
    expect(t.chain == 0 and t.missed_hooks == 1, "missed hook resets chain")


def test_auto_capture_lock():
    lock = build_identity_lock({
        "species": 129,
        "species_name": "Magikarp",
        "pid": "0x12345678",
        "ec": "0x9ABCDEF0",
    })
    expect(lock["token"] == "0x9ABCDEF0:0x12345678:129", "identity lock token")
    try:
        build_identity_lock({"species": 129, "pid": "0x12345678"})
    except RuntimeError:
        pass
    else:
        raise AssertionError("missing EC must fail closed")



def test_party_wurmple_prediction_and_auto_capture_test_policy():
    rows = payload_from_parsed([{
        "valid": True, "checksum_valid": True, "species": 265,
        "species_name": "Wurmple", "ec": "0x00050000", "pid": "0x11112222",
        "nature": "Hardy", "gender": "♀", "shiny_xor": 200, "is_shiny": False,
        "ivs": {"hp":1,"attack":2,"defense":3,"speed":4,"sp_attack":5,"sp_defense":6},
        "evs": {}, "moves": [], "move_pp": [], "pokerus_status": "Never infected",
        "pokerus_days": 0, "pokerus_strain": 0,
    }])
    expect(rows[0]["ec"] == "0x00050000", "Party Viewer preserves PK6 EC")
    expect(rows[0]["predicted_evolution"] == "Cascoon → Dustox", "Party Viewer shows Wurmple deterministic evolution line")
    expect(auto_capture_test_eligible(game_key="omega_ruby", horde_size=1, is_shiny=False, target_match=False), "OR non-shiny single is eligible for one-shot Auto-Capture Test")
    expect(auto_capture_test_eligible(game_key="alpha_sapphire", horde_size=1, is_shiny=False, target_match=False), "AS non-shiny single remains eligible for one-shot Auto-Capture Test")
    expect(not auto_capture_test_eligible(game_key="omega_ruby", horde_size=5, is_shiny=False, target_match=False), "Auto-Capture Test never consumes a Horde")
    expect(not auto_capture_test_eligible(game_key="omega_ruby", horde_size=1, is_shiny=True, target_match=False), "real shiny takes priority over test")
    expect(not auto_capture_test_eligible(game_key="omega_ruby", horde_size=1, is_shiny=False, target_match=True), "Target HOLD takes priority over test")


def test_oras_split_evolution_and_omega_ruby_capture_charm():
    # Gen VI Wurmple uses EC upper16 % 10, never PID.
    silcoon = predict_split_evolution(265, 0x00040000)  # upper16=4 -> Silcoon
    cascoon = predict_split_evolution(265, 0x00050000)  # upper16=5 -> Cascoon
    expect(silcoon and silcoon["line"] == "Silcoon → Beautifly", "Wurmple EC selector 0-4 predicts Silcoon line")
    expect(cascoon and cascoon["line"] == "Cascoon → Dustox", "Wurmple EC selector 5-9 predicts Cascoon line")
    expect(predict_split_evolution(263, 0x00050000) is None, "non-Wurmple has no random split prediction")
    expect(auto_capture_supported_for_game("alpha_sapphire"), "AS Auto-Capture remains enabled")
    expect(auto_capture_supported_for_game("omega_ruby"), "OR Auto-Capture enabled through shared ORAS dispatcher")

    raw = bytearray(ORAS_KEY_ITEMS_SIZE)
    struct.pack_into("<HH", raw, 0x20, 632, 1)
    class CharmBridge:
        def read(self, address, length):
            expect(address == ORAS_14_KEY_ITEMS_ADDR, "OR charm uses shared ORAS Key Items address")
            expect(length == ORAS_KEY_ITEMS_SIZE, "OR charm read remains bounded")
            return bytes(raw)
    charm = detect_oras_shiny_charm(CharmBridge(), game_key="omega_ruby")
    expect(charm["detected"] is True, "Omega Ruby Shiny Charm is detected from shared ORAS pocket")
    expect(charm["game_key"] == "omega_ruby", "Omega Ruby charm evidence keeps real game identity")
    expect(charm["ram_write"] is False, "Shiny Charm probe is read-only")

def test_static_framework():
    as_keys = {p.key for p in static_profiles_for_game("alpha_sapphire")}
    or_keys = {p.key for p in static_profiles_for_game("omega_ruby")}
    for key in ("kecleon", "spiritomb", "regirock", "regice", "registeel", "regigigas", "heatran", "uxie", "mesprit", "azelf", "giratina", "cresselia", "cobalion", "terrakion", "virizion", "raikou", "entei", "suicune", "landorus", "kyurem", "voltorb", "electrode"):
        expect(key in as_keys and key in or_keys, f"shared static profile {key}")
    for key in ("lugia", "latios_eon", "dialga", "thundurus", "zekrom"):
        expect(key in as_keys and key not in or_keys, f"AS exclusive {key}")
    for key in ("ho_oh", "latias_eon", "palkia", "tornadus", "reshiram"):
        expect(key in or_keys and key not in as_keys, f"OR exclusive {key}")

    p = get_static_profile("regirock")
    sm = StaticEncounterStateMachine(p, "alpha_sapphire")
    sm.field_ready(); sm.trigger_sent(); sm.battle_ready()
    r = sm.opponent({
        "valid": True, "checksum_valid": True, "species": 377,
        "pid": "0x11111111", "ec": "0x22222222", "is_shiny": False,
    })
    expect(r["event"] == "NONSHINY_RESET_REQUIRED", "nonshiny static reset")
    sm.field_ready(); sm.trigger_sent(); sm.battle_ready()
    r = sm.opponent({
        "valid": True, "checksum_valid": True, "species": 377,
        "pid": "0x33333333", "ec": "0x44444444", "is_shiny": True,
    })
    expect(r["event"] == "SHINY_HOLD", "shiny static hold")
    sm.captured()
    expect(sm.snapshot()["state"] == "CAPTURED", "static captured state")

    wrong = StaticEncounterStateMachine(p, "alpha_sapphire")
    wrong.field_ready(); wrong.trigger_sent(); wrong.battle_ready()
    r = wrong.opponent({"valid": True, "checksum_valid": True, "species": 378, "pid": "0x1", "ec": "0x2", "is_shiny": False})
    expect(r["event"] == "WRONG_SPECIES" and wrong.snapshot()["state"] == "SAFETY_HOLD", "wrong species fails closed")

    stale = StaticEncounterStateMachine(p, "alpha_sapphire")
    mon = {"valid": True, "checksum_valid": True, "species": 377, "pid": "0xAAAA", "ec": "0xBBBB", "is_shiny": False}
    stale.field_ready(); stale.trigger_sent(); stale.battle_ready(); stale.opponent(mon)
    stale.field_ready(); stale.trigger_sent(); stale.battle_ready(); r = stale.opponent(mon)
    expect(r["event"] == "STALE_IDENTITY", "stale PK6 identity fails closed")

    try:
        StaticEncounterStateMachine(get_static_profile("kyogre"), "alpha_sapphire")
    except ValueError:
        pass
    else:
        raise AssertionError("shiny-locked static must fail")


class _StaticFakeBridge:
    def __init__(self, zone=123, x=99.0, z=117.0, sx=None, sz=None, battle=BATTLE_INACTIVE):
        self.zone, self.x, self.z, self.battle = zone, x, z, battle
        self.sx = x if sx is None else sx
        self.sz = z if sz is None else sz
    def read(self, address, length):
        if address == BATTLE_STATE:
            return struct.pack("<I", self.battle)
        if address == ZONE_ADDR:
            return struct.pack("<I", self.zone)
        if address == PRIMARY_BASE:
            return struct.pack("<fff", self.x, 0.0, self.z)
        if address == SECONDARY_BASE:
            return struct.pack("<fff", self.sx, 0.0, self.sz)
        raise AssertionError(f"unexpected read 0x{address:X} len={length}")


def test_static_reset_first_bootstrap_authority():
    # Bootstrap accepts the freshly loaded save position regardless of where
    # the user happened to be when Start Static was pressed.  Exact position
    # authority is captured only after this generic loaded-field gate passes.
    br = _StaticFakeBridge(zone=777, x=999.0, z=-333.0, sx=0.0, sz=0.0)
    loaded = validate_any_loaded_field(br)
    expect(loaded["authority"], "arbitrary loaded overworld position accepted for first Static bootstrap")
    anchor = read_saved_field_anchor(br)
    expect(anchor.zone == 777 and anchor.world_x == 999.0 and anchor.world_z == -333.0, "anchor captured from loaded save, not pre-start position")
    br.battle = 0x00040001
    busy = validate_any_loaded_field(br)
    expect(not busy["authority"], "bootstrap does not authorise field while battle is active")


def test_static_saved_field_authority():
    br = _StaticFakeBridge()
    anchor = read_saved_field_anchor(br)
    good = validate_saved_field_anchor(br, anchor)
    expect(good["authority"], "same static saved tile validates")
    br.x += 18.0
    moved = validate_saved_field_anchor(br, anchor)
    expect(not moved["authority"] and not moved["checks"]["same_grid"], "moved tile rejects")
    br = _StaticFakeBridge(sx=0.0, sz=0.0)
    anchor = read_saved_field_anchor(br)
    zero_secondary = validate_saved_field_anchor(br, anchor)
    expect(zero_secondary["authority"], "secondary zero accepted on proven reset pattern")
    br.battle = 0x00040001
    in_battle = validate_saved_field_anchor(br, anchor)
    expect(not in_battle["authority"] and not in_battle["checks"]["battle_inactive"], "battle state rejects field authority")



def test_manual_stop_propagates_through_reset_contract():
    route_path = ROOT / "pokebot" / "common" / "reset_route.py"
    adapter_path = ROOT / "pokebot" / "common" / "reset_adapter.py"
    route = route_path.read_text(encoding="utf-8")
    adapter = adapter_path.read_text(encoding="utf-8")

    expect('if type(exc).__name__ == "UserStop":\n                        raise' in route,
           "code.ips field/PSS reset probe re-raises explicit UserStop")
    expect('if type(exc).__name__ == "UserStop":\n                raise' in adapter,
           "post-reset field adapter re-raises explicit UserStop")


def test_static_reset_adapter_callback_isolation():
    from pokebot.common import reset_adapter, reset_route
    from pokebot.common.oras_profiles import PROFILES, ALPHA_SAPPHIRE_TITLE_ID

    class FakeBridge:
        def game_info(self):
            return {
                "title_id": ALPHA_SAPPHIRE_TITLE_ID,
                "process_name": "sango-2",
                "pid": 77,
                "status": 0,
            }

    bridge = FakeBridge()
    inputs = object()
    profile = dict(PROFILES[ALPHA_SAPPHIRE_TITLE_ID.lower()])
    original_callback = reset_route.validate_bag
    original_runner = reset_route.run_reset_to_bag
    seen = {}

    def field_validator(_bridge):
        return {"authority": True, "marker": "saved-field"}

    def fake_runner(br, inp, log, use_code_ips=False, **kwargs):
        seen["callback_is_field"] = reset_route.validate_bag is field_validator
        seen["field"] = reset_route.validate_bag(br)
        return True, {"status": "RESET_RETURNED"}

    reset_route.run_reset_to_bag = fake_runner
    try:
        ok, result = reset_adapter.run_reset_to_field_for_profile(
            bridge, inputs, lambda *a, **k: None, profile, field_validator, use_code_ips=False
        )
        expect(ok, "static reset adapter successful fake reset")
        expect(seen.get("callback_is_field"), "static reset injects field callback only during route")
        expect(result.get("post_reset_saved_field", {}).get("authority"), "static reset revalidates field after route")
        expect(reset_route.validate_bag is original_callback, "reset callback restored after static reset")
    finally:
        reset_route.run_reset_to_bag = original_runner
        reset_route.validate_bag = original_callback


def test_public_profile_fishing_defaults():
    from qt_ui import appdata_store as appdata
    old = os.environ.get("POKEBOT_APPDATA_ROOT")
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "profile"
            os.environ["POKEBOT_APPDATA_ROOT"] = str(root)
            profile = appdata.ensure_profile(ROOT)
            expect(profile.root == root.resolve(), "public profile override root")
            expect("fishing" in appdata.WILD_DEFAULTS, "fishing public stats default")
            expect((profile.stats_dir / "wild_fishing.json").is_file(), "fishing stats file initialized")
    finally:
        if old is None:
            os.environ.pop("POKEBOT_APPDATA_ROOT", None)
        else:
            os.environ["POKEBOT_APPDATA_ROOT"] = old



def test_single_capture_waits_for_exact_touch_ready_command_phase():
    import pokebot.wild.battle_bag_throw as bagmod

    class FakeClock:
        def __init__(self):
            self.now = 0.0
        def monotonic(self):
            return self.now
        def sleep(self, seconds):
            self.now += float(seconds)

    class FakeCore:
        def read_gate(self, br):
            # Reproduce the 2026-08-29 OR failure: outer gate goes true early,
            # while ActSelect has not entered the touch-ready COMMAND phase.
            return {
                "gate": True,
                "state": "0x00000002",
                "mask": "0x00000100",
            }

    class FakeBridge:
        def __init__(self, clock, owner):
            self.clock = clock
            self.owner = owner
        def u32(self, address):
            if address == self.owner + bagmod.ACTSELECT_VPTR_SUBOBJECT_OFF:
                return bagmod.EXPECTED_ACTSELECT_VPTR
            if address == self.owner + bagmod.ACTSELECT_TARGET_MASK_OFF:
                return 0
            raise AssertionError(f"unexpected u32 0x{address:X}")
        def read(self, address, length):
            if address == self.owner + bagmod.ACTSELECT_PHASE_STATE_OFF and length == 1:
                # UI becomes genuinely COMMAND/touch-ready only after 2.4 sec.
                return bytes([0 if self.clock.now < 2.4 else bagmod.ACTSELECT_COMMAND_PHASE])
            raise AssertionError(f"unexpected read 0x{address:X}+{length}")

    clock = FakeClock()
    owner = 0x0852FC74
    original_time = bagmod.time
    try:
        bagmod.time = clock
        ready, samples = bagmod._wait_command_touch_ready(
            FakeCore(), FakeBridge(clock, owner), owner, lambda: None, lambda msg: None
        )
        expect(clock.now >= 3.0, "early command gate cannot authorize BAG before ActSelect COMMAND+dwell")
        expect(ready.get("phase_state_byte") == 1, "owner+0x93 COMMAND byte is required for touch readiness")
        expect(float(ready.get("ready_dwell_seconds") or 0) >= bagmod.COMMAND_TOUCH_READY_MIN_DWELL_SECONDS, "touch-ready phase must remain stable for minimum dwell")
        expect(any(s.get("gate") and s.get("phase_state_byte") == 0 for s in samples), "regression reproduces gate-true/pre-render phase-zero window")
    finally:
        bagmod.time = original_time


def test_or_bag_transition_grace_after_command_gate_drop():
    import pokebot.wild.battle_bag_throw as bagmod

    class FakeClock:
        def __init__(self):
            self.now = 0.0
        def monotonic(self):
            return self.now
        def sleep(self, seconds):
            self.now += float(seconds)

    class FakeCore:
        def __init__(self, clock):
            self.clock = clock
        def read_gate(self, br):
            # Touch is initially delivered while command menu is visible, then
            # the game consumes it and the command gate disappears.
            return {
                "gate": self.clock.now < 0.10,
                "state": "0x00000003" if self.clock.now < 0.10 else "0x00000002",
                "mask": "0x0000000F" if self.clock.now < 0.10 else "0x00000000",
            }

    clock = FakeClock()
    original_time = bagmod.time
    original_cursor = bagmod._cursor
    try:
        bagmod.time = clock
        # Reproduce OR hardware: Bag owner exists but controller/state stay zero
        # for >2.5 s after the command gate drops, then state 1 materialises.
        def fake_cursor(br):
            if clock.now < 3.20:
                return {
                    "bag": "0x0852FE4C", "bag_state": 0,
                    "controller": "0x00000000", "valid": False,
                }
            return {
                "bag": "0x0852FE4C", "bag_state": 1,
                "controller": "0x08600000", "cursor": "0x08601000",
                "selector_u16": "0x0000", "selector_bytes": "00 00 00 00",
                "valid": True,
            }
        bagmod._cursor = fake_cursor
        outcome = bagmod._wait_after_open_bag_touch(FakeCore(clock), object(), lambda: None)
        expect(outcome.get("status") == "BAG_OPEN", "OR delayed Bag construction reaches state 1")
        expect(outcome.get("transition_seen") is True, "command-gate drop is retained as touch-consumption evidence")
        expect(float(outcome.get("transition_at") or 0) < 1.0, "transition is observed before old 2.5s timeout")
        expect(clock.now >= 3.2, "Bag transition grace extends beyond old 2.5s acceptance window")
    finally:
        bagmod.time = original_time
        bagmod._cursor = original_cursor


def test_single_capture_dynamic_bag_owner_relocation():
    import pokebot.wild.battle_bag_throw as bagmod
    import tools.horde_dynamic_owner_capture_validator as owner_discovery

    historic = 0x0852FC74
    relocated = 0x08612340

    class FakeCore:
        BATTLE_ACTIVE = 0x00040001
        def read_gate(self, br):
            return {
                "battle": "0x00040001",
                "outer": "0x08650000",
                "view": "0x08651000",
                "valid_view": True,
            }

    class FakeBridge:
        def u32(self, address):
            if address == relocated + bagmod.ACTSELECT_VPTR_SUBOBJECT_OFF:
                return bagmod.EXPECTED_ACTSELECT_VPTR
            if address == relocated + bagmod.BAG_OFF:
                return bagmod.EXPECTED_BAG_VPTR
            return 0

    original_discover = owner_discovery.discover_unique_owner
    original_owner = bagmod.OWNER
    original_locator = bagmod.OWNER_LOCATOR
    logs = []
    try:
        owner_discovery.discover_unique_owner = lambda core, br, view, outer: {
            "chosen": {"owner_u32": relocated},
            "graph": {"candidates": [{"owner_u32": relocated}]},
            "linear": {"skipped": "unique pointer-graph candidate"},
        }
        bagmod.bind_runtime_owner(historic, locator="offline historic owner")
        proof = bagmod._verify_or_relocate_owner(FakeCore(), FakeBridge(), logs.append)
        expect(proof.get("owner_resolution") == "RUNTIME_RELOCATED", "single capture relocates stale Bag owner")
        expect(proof.get("owner") == f"0x{relocated:08X}", "single capture binds discovered Bag owner")
        expect(logs and "rebound live DllBattle Bag owner" in logs[-1], "single capture logs owner relocation")
    finally:
        owner_discovery.discover_unique_owner = original_discover
        bagmod.bind_runtime_owner(original_owner, locator=original_locator)


def test_static_persistence_contract():
    static_text = (ROOT / "qt_ui" / "static_worker.py").read_text(encoding="utf-8")
    stats_text = (ROOT / "qt_ui" / "stats_page.py").read_text(encoding="utf-8")
    for token in ("self.last_seen_path", "self.encounter_path", "_save_last_seen_entry", "_append_encounter_ledger", "lifetime_hunt_seconds"):
        expect(token in static_text, f"Static persistence token {token}")
    expect('default["hardware_validation"] = self.static_profile.choreography_status' in static_text, "Static loaded stats refresh current hardware validation")
    expect('self.lifetime["hardware_validation"] = self.static_profile.choreography_status' in static_text, "Static saved stats refresh current hardware validation")
    expect('note("Static",shiny,duration)' in stats_text, "Static method analytics row")
    expect('categories=["Starter","Static"' in stats_text, "Static method listed after Starter")



def test_static_regigigas_transport_and_zone_contract():
    p = get_static_profile("regigigas")
    expect(p.trigger_kind == "STEP_NORTH", "Regigigas uses one north-step trigger")
    expect(p.max_trigger_pulses == 1, "Regigigas trigger remains bounded to one step")
    expect(p.expected_zone_id == 158, "Regigigas is guarded to hardware-proven Island Cave zone")
    expect(p.choreography_status.startswith("HARDWARE_PROVEN_"), "Regigigas hardware validation metadata is promoted")
    expect(get_static_profile("regice").expected_zone_id == 158, "Regice Island Cave zone retained")
    expect(get_static_profile("registeel").expected_zone_id == 159, "Registeel Ancient Tomb zone retained")
    static_text = (ROOT / "qt_ui" / "static_worker.py").read_text(encoding="utf-8")
    for token in (
        "STATIC STARTUP TRANSPORT RETRY",
        "STATIC STARTUP TRANSPORT RECOVERED",
        "STATIC STEP TRIGGER ABOUT TO SEND",
        "STATIC STEP TRIGGER SENT",
        "STATIC STEP DIALOGUE A ABOUT TO SEND",
        "STATIC STEP DIALOGUE A SENT",
        "Regigigas post-step dialogue A budget exhausted without battle",
        "dialogue_budget = 6",
        'self._startup_rpc(bridge.game_info, label="GAME_INFO", attempts=4)',
        'self._startup_rpc(inputs.input_ping, label="INPUT_PING", attempts=4)',
        "saved location mismatch",
    ):
        expect(token in static_text, f"Regigigas startup/trigger contract token {token}")



def test_soaring_rift_mapper_contract():
    dialga = get_static_profile("dialga")
    expect(dialga.trigger_kind == "SOARING_RIFT_MAPPER", "Dialga uses dedicated Soaring mapper")
    expect(dialga.expected_zone_id == 8, "Dialga mapper is Dewford-save anchored")
    expect(tuple(dialga.required_party_species) == (480, 481, 482), "Dialga mapper requires lake trio")
    palkia = get_static_profile("palkia")
    expect(palkia.trigger_kind == "SOARING_RIFT_MAPPER", "Palkia shares Dimensional Rift mapper")
    giratina = get_static_profile("giratina")
    expect(tuple(giratina.required_party_species) == (483, 484), "Giratina requires Dialga and Palkia")
    for key in ("tornadus", "thundurus", "landorus"):
        expect(get_static_profile(key).trigger_kind == "SOARING_WEATHER_UNIMPLEMENTED", f"{key} weather trigger stays blocked")

    br = _StaticFakeBridge(zone=8, x=117.0, z=135.0, sx=117.0, sz=135.0, battle=BATTLE_INACTIVE)
    sample = read_soaring_sample(br.read)
    expect(sample["battle_phase"] == "FIELD", "soaring mapper field sample")
    expect(sample["zone"] == 8, "soaring mapper reads Dewford zone")
    expect(sample["primary_xyz"] == [117.0, 0.0, 135.0], "soaring mapper preserves primary XYZ")
    br.battle = 0x00040001
    active = read_soaring_sample(br.read)
    expect(active["battle_phase"] == "ACTIVE" and "primary_xyz" not in active, "active battle stops field-coordinate reads")
    summary = summarize_trace([sample, active], anchor={"zone": 8})
    expect(summary["samples"] == 2 and summary["battle_active_samples"] == 1, "soaring mapper trace summary")

    static_text = (ROOT / "qt_ui" / "static_worker.py").read_text(encoding="utf-8")
    for token in (
        '_locate_skytrip("BEFORE_Y", 0.0)',
        '"AFTER_Y_TRANSITION"',
        '"MANUAL_FLIGHT"',
        '"BATTLE_BOUNDARY" if battle_seen else "MAPPER_TIMEOUT"',
        '"skytrip_lifecycle_events"',
        'last_fast_ref_retry',
        'self._record_encounter(payload)',
        'payload["attempt"] = max(1, int(self.session_seen) + 1)',
    ):
        expect(token in static_text, f"SkyTrip lifecycle worker token {token}")


def test_dialga_state2_profile_policy():
    for rel in (
        "pokebot/wild/validated/walk_v0p23/causal_core_v0p14.py",
        "pokebot/wild/validated/acro_v0p27/causal_core_v0p14.py",
    ):
        text = (ROOT / rel).read_text(encoding="utf-8")
        expect('legacy_exact_profile_match' in text, f"{rel} retains legacy exact diagnostics")
        expect('"profile_match": all(authority_checks.values())' in text, f"{rel} uses authority-only settled profile")
        expect('vA8/vB4/vB8/vD4 diagnostic-only' in text, f"{rel} marks disproven exact fields diagnostic-only")
        expect('if s.get("state_3C") == "0x00000002"' in text, f"{rel} state_3C=2 PK6 boundary retained")


def test_static_ring_family_promotion_and_party_contract():
    # Zekrom hardware-proved the ordinary ground Mirage/portal ring trigger.
    # All ground-ring profiles inherit that choreography; Soaring targets do not.
    as_ring = (
        "heatran", "lugia", "uxie", "mesprit", "azelf", "cresselia",
        "cobalion", "terrakion", "virizion", "raikou", "entei", "suicune",
        "zekrom", "kyurem",
    )
    for key in as_ring:
        p = get_static_profile(key)
        expect(p.family == "ring_static", f"{key} inherits ring trigger family")
        expect(hardware_validation_label(p, "alpha_sapphire") == "Hardware proven — shared ORAS ring trigger", f"{key} shared ORAS ring label")

    for key in ("dialga", "palkia", "giratina"):
        p = get_static_profile(key)
        expect(p.family == "soaring_static", f"{key} remains separate Soaring family")
    expect(hardware_validation_label(get_static_profile("dialga"), "alpha_sapphire") == "Manual rift path proven — automation mapping", "Dialga manual rift mapper hardware proof promoted")
    expect(hardware_validation_label(get_static_profile("palkia"), "omega_ruby") == "Manual rift path proven — automation mapping", "Palkia inherits shared ORAS manual rift proof")
    expect("Soaring automation unfinished" in hardware_validation_label(get_static_profile("giratina"), "alpha_sapphire"), "Giratina remains blocked by unfinished Soaring automation")

    for key in ("tornadus", "thundurus", "landorus"):
        expect(hardware_validation_label(get_static_profile(key), "alpha_sapphire") == "Storm Cloud mapper needed", f"{key} weather remains unpromoted")

    expect(hardware_validation_label(get_static_profile("reshiram"), "omega_ruby") == "Hardware proven — shared ORAS ring trigger", "Reshiram inherits shared ORAS ring proof")
    expect(hardware_validation_label(get_static_profile("regigigas"), "alpha_sapphire") == "Hardware proven", "individual Regigigas proof label")

    static_text = (ROOT / "qt_ui" / "static_worker.py").read_text(encoding="utf-8")
    dash_text = (ROOT / "qt_ui" / "dashboard_page.py").read_text(encoding="utf-8")
    expect('party_payload.get("payload")' in static_text, "Static telemetry consumes live-party payload key")
    expect('mon.get("species_id")' in static_text, "Soaring requirement uses numeric live-party species_id")
    expect('SOARING RIFT PARTY REQUIREMENT' in static_text, "Soaring party requirement diagnostics logged")
    expect('suffix = hardware_validation_label(profile, game_key)' in dash_text, "Static dropdown uses real validation metadata")
    expect('suffix = "Hardware validation needed"' not in dash_text, "Static dropdown hard-coded validation suffix removed")


def test_skytrip_runtime_object_mapper_contract():
    module_base = 0x02000000
    mainproc = 0x08001000
    camera = 0x08002000
    encount = 0x08003000
    runtime_data_addr = 0x08004000
    runtime_bss_addr = 0x08005000

    module = bytearray(SKYTRIP_FILE_SIZE)
    module[0x80:0x84] = b"FIXD"
    struct.pack_into("<I", module, 0x90, SKYTRIP_FILE_SIZE)
    struct.pack_into("<I", module, 0x94, SKYTRIP_BSS_SIZE)
    struct.pack_into("<I", module, 0xB0, 0x138)
    struct.pack_into("<I", module, 0xB4, 0x9000)
    struct.pack_into("<I", module, 0xB8, SKYTRIP_DATA_OFFSET)
    struct.pack_into("<I", module, 0xBC, SKYTRIP_DATA_SIZE)
    struct.pack_into("<I", module, 0xC0, module_base + 0xF000)
    struct.pack_into("<I", module, 0xC4, SKYTRIP_MODULE_NAME_SIZE)
    struct.pack_into("<I", module, 0xC8, 0xF00C)
    struct.pack_into("<I", module, 0xCC, 6)
    module[0xF000:0xF000 + 11] = b"DllSkyTrip\0"
    # Runtime segment entries are rebased by RO.  In particular .data/.bss
    # point to application-supplied buffers, not base + file offsets.
    segments = [
        (module_base + 0x138, 0x8000, 0),
        (module_base + 0x8200, 0x5000, 1),
        (module_base + 0xD200, 0x1000, 1),
        (module_base + 0xE200, 0x0E00, 1),
        (runtime_data_addr, SKYTRIP_DATA_SIZE, 2),
        (runtime_bss_addr, SKYTRIP_BSS_SIZE, 3),
    ]
    for i, row in enumerate(segments):
        struct.pack_into("<III", module, 0xF00C + i * 12, *row)
    struct.pack_into("<I", module, MAINPROC_VPTR_LITERAL_OFFSET, module_base + SKYTRIP_VPTR_OFFSETS["MainProc"])
    # Leave the file-tail .data zeroed: a fixed dynamic CRO may crop it.
    runtime_data = bytearray(SKYTRIP_DATA_SIZE)
    struct.pack_into("<I", runtime_data, 0, mainproc)
    runtime_bss = bytearray(SKYTRIP_BSS_SIZE)
    struct.pack_into("<f", runtime_bss, 0x20, 1234.5)
    struct.pack_into("<f", runtime_bss, 0x24, -678.25)

    main_raw = bytearray(0x200)
    struct.pack_into("<I", main_raw, 0x00, module_base + SKYTRIP_VPTR_OFFSETS["MainProc"])
    struct.pack_into("<I", main_raw, 0x20, camera)
    struct.pack_into("<I", main_raw, 0x24, encount)
    camera_raw = bytearray(0x200)
    struct.pack_into("<I", camera_raw, 0x00, module_base + SKYTRIP_VPTR_OFFSETS["Camera"])
    encount_raw = bytearray(0x200)
    struct.pack_into("<I", encount_raw, 0x00, module_base + SKYTRIP_VPTR_OFFSETS["BMEncount"])

    class FakeBridge:
        def query(self, address):
            address = int(address)
            if address < module_base:
                return {"status": 0, "base": "0x00100000", "size": module_base - 0x00100000, "perm": 0, "state": 0, "page_flags": 0}
            if module_base <= address < module_base + SKYTRIP_FILE_SIZE:
                return {"status": 0, "base": f"0x{module_base:08X}", "size": SKYTRIP_FILE_SIZE, "perm": 5, "state": 1, "page_flags": 0}
            return {"status": 0, "base": f"0x{address:08X}", "size": 0x1000, "perm": 0, "state": 0, "page_flags": 0}

        def read(self, address, length):
            address, length = int(address), int(length)
            if module_base <= address and address + length <= module_base + len(module):
                off = address - module_base
                return bytes(module[off:off + length])
            for base, raw in (
                (mainproc, main_raw), (camera, camera_raw), (encount, encount_raw),
                (runtime_data_addr, runtime_data), (runtime_bss_addr, runtime_bss),
            ):
                if base <= address and address + length <= base + len(raw):
                    off = address - base
                    return bytes(raw[off:off + length])
            raise RuntimeError(f"unmapped fake read 0x{address:X}+{length}")

    br = FakeBridge()
    located = locate_skytrip_module(br)
    expect(located["present"], "SkyTrip exact CRO identity located")
    expect(located["target"]["base_int"] == module_base, "SkyTrip module base retained")
    reloc = verify_skytrip_relocation(br, located["target"])
    expect(reloc["verified"], "SkyTrip MainProc constructor relocation verifies")
    discovery = discover_skytrip_objects(br, located["target"])
    expect(discovery["status"] == "FOUND", "SkyTrip pointer graph finds known vptr objects")
    expect(discovery["module_data"]["address"] == f"0x{runtime_data_addr:08X}", "SkyTrip discovery follows relocated type-2 data segment")
    expect(discovery["module_data"]["source"] == "relocated_type2_segment", "SkyTrip discovery records relocated data authority")
    expect(discovery["candidates"]["MainProc"][0]["address_int"] == mainproc, "MainProc discovered from runtime data seed")
    expect(discovery["candidates"]["Camera"][0]["address_int"] == camera, "Camera discovered from MainProc graph")
    expect(discovery["candidates"]["BMEncount"][0]["address_int"] == encount, "BMEncount discovered from MainProc graph")
    selected = choose_trace_objects(discovery)
    expect([x["class"] for x in selected][:3] == ["MainProc", "Camera", "BMEncount"], "SkyTrip trace prioritises MainProc/Camera/encounter")
    snapshots = read_trace_objects(br, selected)
    expect(all("hex" in x for x in snapshots), "SkyTrip object snapshots are bounded readable blobs")
    module_state = read_runtime_module_state(br, located["target"])
    expect(module_state["address"] == f"0x{runtime_data_addr:08X}", "SkyTrip live-state read uses relocated .data buffer")
    expect(module_state["source"] == "relocated_type2_segment", "SkyTrip live-state source is runtime segment table")
    expect(module_state["heap_pointers"][0]["pointer"] == f"0x{mainproc:08X}", "SkyTrip mutable data snapshot exposes heap seed")
    expect(module_state["bss"]["address"] == f"0x{runtime_bss_addr:08X}", "SkyTrip live-state read includes relocated type-3 BSS")
    expect(any(abs(float(x["f32"]) - 1234.5) < 0.01 for x in module_state["bss"]["float_words"]), "SkyTrip BSS float diagnostics decode live values")
    expect(SKYTRIP_DATA_SIZE == 0x294, "SkyTrip 1.4 data-size contract")



def test_skytrip_runtime_state_delta_contract():
    before_data = bytearray(0x20)
    after_data = bytearray(before_data)
    struct.pack_into("<I", after_data, 0x0C, 1)

    before_bss = bytearray(0x40)
    after_bss = bytearray(before_bss)
    struct.pack_into("<f", before_bss, 0x10, 100.0)
    struct.pack_into("<f", after_bss, 0x10, 125.5)
    struct.pack_into("<f", after_bss, 0x14, -50.0)

    before = {
        "hex": before_data.hex(),
        "bss": {"hex": before_bss.hex()},
    }
    after = {
        "hex": after_data.hex(),
        "bss": {"hex": after_bss.hex()},
    }
    delta = diff_runtime_module_state(before, after)
    expect(delta["changed"], "SkyTrip state delta reports changed runtime words")
    expect(delta["data_change_count"] == 1, "SkyTrip data delta isolates +0x0C transition")
    expect(delta["data_changes"][0]["offset"] == "0xC", "SkyTrip data delta retains exact aligned offset")
    expect(delta["bss_change_count"] == 2, "SkyTrip BSS delta captures two changed float words")
    expect(delta["bss_changes"][0]["offset"] == "0x10", "SkyTrip BSS delta retains exact first changed offset")
    expect(abs(float(delta["bss_changes"][0]["new_f32"]) - 125.5) < 0.01, "SkyTrip BSS delta decodes changed float value")



def test_skytrip_external_reference_state_mapper_contract():
    module_base = 0x02000000
    code_address = module_base + 0x180
    code_size = 0x1000
    root_addr = 0x08012000
    child_addr = 0x08013000

    module = bytearray(SKYTRIP_FILE_SIZE)
    # Runtime layout header: live code address/size are absolute after load.
    struct.pack_into("<I", module, 0xB0, code_address)
    struct.pack_into("<I", module, 0xB4, code_size)
    struct.pack_into("<I", module, 0xB8, 0)
    struct.pack_into("<I", module, 0xBC, SKYTRIP_DATA_SIZE)
    # One aligned code literal points at writable application memory.
    struct.pack_into("<I", module, (code_address - module_base) + 0x100, root_addr)
    struct.pack_into("<I", module, (code_address - module_base) + 0x180, root_addr)

    root_raw = bytearray(0x1000)
    child_raw = bytearray(0x1000)
    struct.pack_into("<I", root_raw, 0x00, child_addr)
    struct.pack_into("<f", root_raw, 0x10, 10.0)
    struct.pack_into("<f", child_raw, 0x20, 20.0)

    class ExternalBridge:
        def query(self, address):
            address = int(address)
            if module_base <= address < module_base + SKYTRIP_FILE_SIZE:
                return {"status": 0, "base": module_base, "size": SKYTRIP_FILE_SIZE, "perm": 5, "state": 1, "page_flags": 0}
            if root_addr <= address < root_addr + len(root_raw):
                return {"status": 0, "base": root_addr, "size": len(root_raw), "perm": 3, "state": 5, "page_flags": 0}
            if child_addr <= address < child_addr + len(child_raw):
                return {"status": 0, "base": child_addr, "size": len(child_raw), "perm": 3, "state": 5, "page_flags": 0}
            return {"status": 0, "base": address, "size": 0x1000, "perm": 0, "state": 0, "page_flags": 0}

        def read(self, address, length):
            address, length = int(address), int(length)
            if module_base <= address and address + length <= module_base + len(module):
                off = address - module_base
                return bytes(module[off:off + length])
            if root_addr <= address and address + length <= root_addr + len(root_raw):
                off = address - root_addr
                return bytes(root_raw[off:off + length])
            if child_addr <= address and address + length <= child_addr + len(child_raw):
                off = address - child_addr
                return bytes(child_raw[off:off + length])
            raise RuntimeError(f"unmapped external mapper read 0x{address:X}+{length}")

    br = ExternalBridge()
    mapped = discover_skytrip_external_references(br, {"base_int": module_base})
    expect(mapped["status"] == "FOUND_ESCAPED_EXTERNAL_REFS", "SkyTrip code-literal mapper finds true external roots")
    expect(mapped["code_address"] == f"0x{code_address:08X}", "SkyTrip external mapper uses relocated live code address")
    expect(mapped["writable_roots"][0]["address_int"] == root_addr, "SkyTrip external root comes from code literal")
    targets = mapped["trace_targets"]
    expect(any(x["address_int"] == root_addr for x in targets), "SkyTrip external mapper traces root")
    expect(any(x["address_int"] == child_addr for x in targets), "SkyTrip external mapper follows one bounded child pointer")
    before = read_external_trace_targets(br, targets)
    struct.pack_into("<f", root_raw, 0x10, 11.5)
    struct.pack_into("<f", child_raw, 0x20, 25.0)
    after = read_external_trace_targets(br, targets)
    delta = diff_external_trace_state(before, after)
    expect(delta["changed"], "SkyTrip external target delta reports live changes")
    expect(delta["target_change_count"] >= 2, "SkyTrip external delta captures root and child changes")
    expect(delta["word_change_count"] >= 2, "SkyTrip external delta captures aligned changed words")
    expect(mapped["raw_candidates_considered"] <= 128, "SkyTrip external mapper raw literal budget is bounded")
    expect(len(targets) <= 16, "SkyTrip external trace target budget is bounded")



def test_skytrip_external_reference_query_safe_segment_contract():
    """Regression for CH hardware: nominal code span crosses an unmapped CRO gap."""
    module_base = 0x02000000
    text_addr = module_base + 0x180
    text_size = 0x800
    rodata_addr = module_base + 0x1000
    rodata_size = 0x400
    nominal_code_size = 0x1800  # intentionally spans the unmapped text->rodata gap
    seg_table_addr = module_base + 0xF00C
    root_addr = 0x08022000
    child_addr = 0x08023000

    header = bytearray(0x200)
    struct.pack_into("<I", header, 0xB0, text_addr)
    struct.pack_into("<I", header, 0xB4, nominal_code_size)
    struct.pack_into("<I", header, 0xB8, 0)
    struct.pack_into("<I", header, 0xBC, SKYTRIP_DATA_SIZE)
    struct.pack_into("<I", header, 0xC0, module_base + 0xF000)
    struct.pack_into("<I", header, 0xC4, 11)
    struct.pack_into("<I", header, 0xC8, seg_table_addr)
    struct.pack_into("<I", header, 0xCC, 4)

    text_raw = bytearray(text_size)
    rodata_raw = bytearray(rodata_size)
    # Put the same writable root in both actual mapped runtime segments.
    struct.pack_into("<I", text_raw, 0x100, root_addr)
    struct.pack_into("<I", rodata_raw, 0x80, root_addr)

    seg_table = bytearray(4 * 12)
    struct.pack_into("<III", seg_table, 0 * 12, text_addr, text_size, 0)
    struct.pack_into("<III", seg_table, 1 * 12, rodata_addr, rodata_size, 1)
    struct.pack_into("<III", seg_table, 2 * 12, 0x08024000, SKYTRIP_DATA_SIZE, 2)
    struct.pack_into("<III", seg_table, 3 * 12, 0x08025000, SKYTRIP_BSS_SIZE, 3)

    root_raw = bytearray(0x1000)
    child_raw = bytearray(0x1000)
    struct.pack_into("<I", root_raw, 0x00, child_addr)
    struct.pack_into("<f", root_raw, 0x10, 42.0)
    struct.pack_into("<f", child_raw, 0x20, -17.5)

    class GapBridge:
        def query(self, address):
            address = int(address)
            if module_base <= address < module_base + 0x1000:
                return {"status": 0, "base": module_base, "size": 0x1000, "perm": 5, "state": 10, "page_flags": 0}
            if rodata_addr <= address < rodata_addr + 0x1000:
                return {"status": 0, "base": rodata_addr, "size": 0x1000, "perm": 1, "state": 10, "page_flags": 0}
            if seg_table_addr <= address < seg_table_addr + 0x1000:
                return {"status": 0, "base": seg_table_addr & ~0xFFF, "size": 0x1000, "perm": 1, "state": 10, "page_flags": 0}
            if root_addr <= address < root_addr + len(root_raw):
                return {"status": 0, "base": root_addr, "size": len(root_raw), "perm": 3, "state": 5, "page_flags": 0}
            if child_addr <= address < child_addr + len(child_raw):
                return {"status": 0, "base": child_addr, "size": len(child_raw), "perm": 3, "state": 5, "page_flags": 0}
            if 0x08024000 <= address < 0x08026000:
                return {"status": 0, "base": 0x08024000, "size": 0x2000, "perm": 3, "state": 5, "page_flags": 0}
            return {"status": 0, "base": address & ~0xFFF, "size": 0x1000, "perm": 0, "state": 0, "page_flags": 0}

        def read(self, address, length):
            address, length = int(address), int(length)
            # Header is readable, but the nominal code span is NOT contiguous.
            if module_base <= address and address + length <= module_base + len(header):
                off = address - module_base
                return bytes(header[off:off + length])
            if text_addr <= address and address + length <= text_addr + len(text_raw):
                off = address - text_addr
                return bytes(text_raw[off:off + length])
            if rodata_addr <= address and address + length <= rodata_addr + len(rodata_raw):
                off = address - rodata_addr
                return bytes(rodata_raw[off:off + length])
            if seg_table_addr <= address and address + length <= seg_table_addr + len(seg_table):
                off = address - seg_table_addr
                return bytes(seg_table[off:off + length])
            if root_addr <= address and address + length <= root_addr + len(root_raw):
                off = address - root_addr
                return bytes(root_raw[off:off + length])
            if child_addr <= address and address + length <= child_addr + len(child_raw):
                off = address - child_addr
                return bytes(child_raw[off:off + length])
            raise RuntimeError(f"RANGE_INVALID simulated gap read 0x{address:X}+{length}")

    mapped = discover_skytrip_external_references(GapBridge(), {"base_int": module_base})
    expect(mapped["status"] == "FOUND_ESCAPED_EXTERNAL_REFS", "SkyTrip query-safe mapper survives nominal CRO code gap")
    expect(mapped["code_size"] == text_size + rodata_size, "SkyTrip query-safe mapper scans actual type-0/type-1 segment bytes only")
    expect(len(mapped["scanned_segments"]) == 2, "SkyTrip query-safe mapper reports two actual runtime scan segments")
    expect({row["kind"] for row in mapped["scanned_segments"]} == {0, 1}, "SkyTrip query-safe mapper scans text and rodata segment kinds")
    expect(mapped["nominal_code_size"] == nominal_code_size, "SkyTrip query-safe mapper retains nominal header size as diagnostic evidence")
    expect(mapped["writable_roots"][0]["address_int"] == root_addr, "SkyTrip query-safe mapper still resolves external writable root")
    expect(len(mapped["writable_roots"][0]["code_literal_addresses"]) == 2, "SkyTrip query-safe mapper keeps absolute literal locations across split segments")
    expect(not mapped["scan_errors"], "SkyTrip query-safe mapper avoids the unmapped nominal gap entirely")



def test_skytrip_escaped_pointer_ownership_contract():
    """CI hardware regression: local .data/.bss aliases are not external roots."""
    module_base = 0x02000000
    text_addr = module_base + 0x180
    text_size = 0x800
    rodata_addr = module_base + 0x1000
    rodata_size = 0x400
    seg_table_addr = module_base + 0xF00C
    local_data = 0x08024000
    local_bss = local_data + SKYTRIP_DATA_SIZE
    direct_external = 0x06012000  # deliberately below CI's old 0x08000000 floor
    escaped_external = 0x06013000

    header = bytearray(0x200)
    struct.pack_into("<I", header, 0xB0, text_addr)
    struct.pack_into("<I", header, 0xB4, text_size + rodata_size)
    struct.pack_into("<I", header, 0xB8, local_data)
    struct.pack_into("<I", header, 0xBC, SKYTRIP_DATA_SIZE)
    struct.pack_into("<I", header, 0xC8, seg_table_addr)
    struct.pack_into("<I", header, 0xCC, 4)

    text_raw = bytearray(text_size)
    rodata_raw = bytearray(rodata_size)
    # CI would rank these repeated local aliases as writable "external" roots.
    for off in (0x100, 0x120, 0x140, 0x160):
        struct.pack_into("<I", text_raw, off, local_data)
    struct.pack_into("<I", text_raw, 0x180, local_bss)
    struct.pack_into("<I", text_raw, 0x1A0, direct_external)
    struct.pack_into("<I", rodata_raw, 0x80, direct_external)

    seg_table = bytearray(4 * 12)
    struct.pack_into("<III", seg_table, 0 * 12, text_addr, text_size, 0)
    struct.pack_into("<III", seg_table, 1 * 12, rodata_addr, rodata_size, 1)
    struct.pack_into("<III", seg_table, 2 * 12, local_data, SKYTRIP_DATA_SIZE, 2)
    # Reproduce hardware's shorter runtime BSS entry; mapper must still exclude
    # the full known/header BSS tail through +0x1A4.
    struct.pack_into("<III", seg_table, 3 * 12, local_bss, 0x184, 3)

    local_data_raw = bytearray(SKYTRIP_DATA_SIZE)
    local_bss_raw = bytearray(SKYTRIP_BSS_SIZE)
    struct.pack_into("<I", local_data_raw, 0x20, escaped_external)
    # A module-local pointer into the extended BSS tail must remain filtered.
    struct.pack_into("<I", local_data_raw, 0x24, local_bss + 0x190)

    direct_raw = bytearray(0x1000)
    escaped_raw = bytearray(0x1000)
    struct.pack_into("<f", direct_raw, 0x10, 12.0)
    struct.pack_into("<f", escaped_raw, 0x20, -32.0)

    class OwnershipBridge:
        def query(self, address):
            address = int(address)
            if module_base <= address < module_base + 0x1000:
                return {"status": 0, "base": module_base, "size": 0x1000, "perm": 5, "state": 10, "page_flags": 0}
            if rodata_addr <= address < rodata_addr + 0x1000:
                return {"status": 0, "base": rodata_addr, "size": 0x1000, "perm": 1, "state": 10, "page_flags": 0}
            if seg_table_addr <= address < seg_table_addr + 0x1000:
                return {"status": 0, "base": seg_table_addr & ~0xFFF, "size": 0x1000, "perm": 1, "state": 10, "page_flags": 0}
            if local_data <= address < local_bss + SKYTRIP_BSS_SIZE:
                return {"status": 0, "base": local_data & ~0xFFF, "size": 0x2000, "perm": 3, "state": 5, "page_flags": 0}
            if direct_external <= address < direct_external + len(direct_raw):
                return {"status": 0, "base": direct_external, "size": len(direct_raw), "perm": 3, "state": 5, "page_flags": 0}
            if escaped_external <= address < escaped_external + len(escaped_raw):
                return {"status": 0, "base": escaped_external, "size": len(escaped_raw), "perm": 3, "state": 5, "page_flags": 0}
            return {"status": 0, "base": address & ~0xFFF, "size": 0x1000, "perm": 0, "state": 0, "page_flags": 0}

        def read(self, address, length):
            address, length = int(address), int(length)
            if module_base <= address and address + length <= module_base + len(header):
                off = address - module_base
                return bytes(header[off:off + length])
            if text_addr <= address and address + length <= text_addr + len(text_raw):
                off = address - text_addr
                return bytes(text_raw[off:off + length])
            if rodata_addr <= address and address + length <= rodata_addr + len(rodata_raw):
                off = address - rodata_addr
                return bytes(rodata_raw[off:off + length])
            if seg_table_addr <= address and address + length <= seg_table_addr + len(seg_table):
                off = address - seg_table_addr
                return bytes(seg_table[off:off + length])
            if local_data <= address and address + length <= local_data + len(local_data_raw):
                off = address - local_data
                return bytes(local_data_raw[off:off + length])
            if local_bss <= address and address + length <= local_bss + len(local_bss_raw):
                off = address - local_bss
                return bytes(local_bss_raw[off:off + length])
            if direct_external <= address and address + length <= direct_external + len(direct_raw):
                off = address - direct_external
                return bytes(direct_raw[off:off + length])
            if escaped_external <= address and address + length <= escaped_external + len(escaped_raw):
                off = address - escaped_external
                return bytes(escaped_raw[off:off + length])
            raise RuntimeError(f"unmapped ownership read 0x{address:X}+{length}")

    mapped = discover_skytrip_external_references(OwnershipBridge(), {"base_int": module_base, "bss_size": SKYTRIP_BSS_SIZE})
    expect(mapped["status"] == "FOUND_ESCAPED_EXTERNAL_REFS", "CJ mapper finds genuine escaped external roots")
    roots = {row["address_int"]: row for row in mapped["writable_roots"]}
    expect(local_data not in roots and local_bss not in roots, "CJ mapper rejects SkyTrip local data/BSS aliases")
    expect(direct_external in roots, "CJ mapper accepts lower-userland direct writable root")
    expect(escaped_external in roots, "CJ mapper follows pointer escaping module-local data")
    expect("direct_code_writable" in roots[direct_external]["categories"], "CJ direct-root provenance retained")
    expect("module_local_escape" in roots[escaped_external]["categories"], "CJ local-escape provenance retained")
    expect(mapped["internal_runtime_literal_values_filtered"] >= 2, "CJ reports filtered module-local literal aliases")
    expect(mapped["module_local_escaped_roots"] >= 1, "CJ reports module-local escaped-root count")
    expect(all(not (local_data <= row["address_int"] < local_bss + SKYTRIP_BSS_SIZE) for row in mapped["trace_targets"]), "CJ trace set excludes all module-local mutable addresses")



def test_skytrip_fast_cached_reference_and_motion_contract():
    """CK: instruction-derived plan caches, batches reads, and locks repeated motion."""
    module_base = 0x02000000
    text_addr = module_base + 0x180
    text_size = 0x400
    rodata_addr = module_base + 0x1000
    rodata_size = 0x100
    seg_table_addr = module_base + 0xF00C
    local_data = 0x08024000
    local_bss = local_data + SKYTRIP_DATA_SIZE
    root_addr = 0x06012000
    child_addr = root_addr + 0x100

    header = bytearray(0x200)
    struct.pack_into("<I", header, 0xB0, text_addr)
    struct.pack_into("<I", header, 0xB4, text_size + rodata_size)
    struct.pack_into("<I", header, 0xB8, local_data)
    struct.pack_into("<I", header, 0xBC, SKYTRIP_DATA_SIZE)
    struct.pack_into("<I", header, 0xC8, seg_table_addr)
    struct.pack_into("<I", header, 0xCC, 4)

    text_raw = bytearray(text_size)
    # ARM LDR r0,[pc,#0xF8] at +0 reads literal pool slot at +0x100.
    struct.pack_into("<I", text_raw, 0x00, 0xE59F00F8)
    struct.pack_into("<I", text_raw, 0x100, root_addr)
    rodata_raw = bytearray(rodata_size)
    seg_table = bytearray(4 * 12)
    struct.pack_into("<III", seg_table, 0 * 12, text_addr, text_size, 0)
    struct.pack_into("<III", seg_table, 1 * 12, rodata_addr, rodata_size, 1)
    struct.pack_into("<III", seg_table, 2 * 12, local_data, SKYTRIP_DATA_SIZE, 2)
    struct.pack_into("<III", seg_table, 3 * 12, local_bss, SKYTRIP_BSS_SIZE, 3)
    local_data_raw = bytearray(SKYTRIP_DATA_SIZE)
    local_bss_raw = bytearray(SKYTRIP_BSS_SIZE)
    root_raw = bytearray(0x1000)
    struct.pack_into("<I", root_raw, 0x00, child_addr)
    struct.pack_into("<f", root_raw, 0x10, 10.0)
    child_raw = root_raw  # same QUERY region/backing store

    class FastBridge:
        def __init__(self):
            self.read_log = []
        def query(self, address):
            address = int(address)
            if module_base <= address < module_base + 0x1000:
                return {"status": 0, "base": module_base, "size": 0x1000, "perm": 5, "state": 10, "page_flags": 0}
            if rodata_addr <= address < rodata_addr + 0x1000:
                return {"status": 0, "base": rodata_addr, "size": 0x1000, "perm": 1, "state": 10, "page_flags": 0}
            if seg_table_addr <= address < seg_table_addr + 0x1000:
                return {"status": 0, "base": seg_table_addr & ~0xFFF, "size": 0x1000, "perm": 1, "state": 10, "page_flags": 0}
            if local_data <= address < local_bss + SKYTRIP_BSS_SIZE:
                return {"status": 0, "base": local_data & ~0xFFF, "size": 0x2000, "perm": 3, "state": 5, "page_flags": 0}
            if root_addr <= address < root_addr + len(root_raw):
                return {"status": 0, "base": root_addr, "size": len(root_raw), "perm": 3, "state": 5, "page_flags": 0}
            return {"status": 0, "base": address & ~0xFFF, "size": 0x1000, "perm": 0, "state": 0, "page_flags": 0}
        def read(self, address, length):
            address, length = int(address), int(length)
            self.read_log.append((address, length))
            if module_base <= address and address + length <= module_base + len(header):
                off = address - module_base; return bytes(header[off:off+length])
            if text_addr <= address and address + length <= text_addr + len(text_raw):
                off = address - text_addr; return bytes(text_raw[off:off+length])
            if rodata_addr <= address and address + length <= rodata_addr + len(rodata_raw):
                off = address - rodata_addr; return bytes(rodata_raw[off:off+length])
            if seg_table_addr <= address and address + length <= seg_table_addr + len(seg_table):
                off = address - seg_table_addr; return bytes(seg_table[off:off+length])
            if local_data <= address and address + length <= local_data + len(local_data_raw):
                off = address - local_data; return bytes(local_data_raw[off:off+length])
            if local_bss <= address and address + length <= local_bss + len(local_bss_raw):
                off = address - local_bss; return bytes(local_bss_raw[off:off+length])
            if root_addr <= address and address + length <= root_addr + len(root_raw):
                off = address - root_addr; return bytes(root_raw[off:off+length])
            raise RuntimeError(f"unmapped fast read 0x{address:X}+{length}")

    first_br = FastBridge()
    first = discover_skytrip_fast_references(first_br, {"base_int": module_base, "bss_size": SKYTRIP_BSS_SIZE})
    expect(first["status"] == "FOUND_FAST_EXTERNAL_REFS", "CK fast mapper resolves ARM literal external root")
    expect(first["cache_status"] == "MISS_BUILT", "CK first pass builds static reference plan")
    expect(first["literal_decoder"] == "arm_pc_relative_ldr", "CK uses ARM PC-relative literal decoder")
    expect(first["writable_roots"][0]["address_int"] == root_addr, "CK ARM literal resolves expected writable root")
    plan = first["static_reference_plan"]

    second_br = FastBridge()
    second = discover_skytrip_fast_references(second_br, {"base_int": module_base, "bss_size": SKYTRIP_BSS_SIZE}, static_plan=plan)
    expect(second["cache_status"] == "HIT", "CK second pass reuses cached static plan")
    expect(not second["scanned_segments"], "CK cache hit skips full text/rodata scan")
    text_reads = [(a, n) for a, n in second_br.read_log if text_addr <= a < text_addr + text_size]
    expect(sum(n for _a, n in text_reads) <= 0x200, "CK cache hit reads only one literal-pool bucket from text")

    batches = build_fast_trace_batches(second["trace_targets"])
    expect(len(second["trace_targets"]) <= 8, "CK trace target cap is eight")
    expect(len(batches) <= len(second["trace_targets"]), "CK nearby targets are batchable")
    state = read_external_trace_targets_batched(second_br, second["trace_targets"], batches)
    expect(len(state) == len(second["trace_targets"]), "CK batched reader returns every trace target")

    motion = {}
    locked = None
    for i in range(1, 7):
        delta = {
            "targets": [{
                "id": "fast00", "address": "0x06012000", "via": "test",
                "word_changes": [
                    {"offset": "0x10", "old_f32": float(i), "new_f32": float(i) + 1.0},
                    {"offset": "0x14", "old_f32": float(i) * 2.0, "new_f32": float(i) * 2.0 + 0.5},
                ],
            }]
        }
        scored = update_fast_motion_scores(motion, delta, i * 0.25)
        motion = scored["state"]
        if scored["locked"]:
            locked = scored
            break
    expect(locked is not None, "CK repeated multi-offset float motion reaches early-lock threshold")
    expect(locked["winner"]["id"] == "fast00", "CK motion scorer retains winning target identity")

def test_free_party_post_capture_direct_return_contract():
    # v0p43CV: every legal free-party capacity is one semantic class.  The
    # intermediate Omega Ruby flow/outer values are not stable and therefore
    # must not be enumerated as prerequisites for a no-input return wait.
    for count in range(1, 6):
        expect(
            post_capture_destination_for_party_count(count) == "DIRECT_PARTY_RETURN",
            f"CV party count {count} ({6-count} free slots) maps to direct-party return",
        )
    expect(
        post_capture_destination_for_party_count(6) == "BOX_MESSAGE",
        "CV party count 6 maps to Box-message return",
    )
    expect(
        post_capture_destination_for_party_count(0) == "UNKNOWN"
        and post_capture_destination_for_party_count(7) == "UNKNOWN",
        "CV invalid party counts fail closed",
    )

    inactive = 0x00040000
    sentinel = 0x004FCDC0
    nickname_unregistered = 0x0000176B
    nickname_registered = 0x00001742
    outer = 0x0840FF04

    # The exact nickname prompt must remain retry-only if B was ignored.
    expect(
        classify_post_nickname_b_transition(
            battle=sentinel, flow=nickname_registered, outer_ptr=outer,
            nickname_flow=nickname_registered, nickname_outer=outer,
            battle_inactive=inactive, pre_capture_party_count=1,
        ) == "PROMPT_STILL_ACTIVE",
        "CV exact nickname prompt remains retry-only",
    )

    # Hardware 2026-08-29 produced 0x0BB8 + a changed outer for a perfectly
    # valid free-party capture.  All five free-party capacities must accept the
    # semantic transition, then rely on the caller's final field/grid proof.
    for count in range(1, 6):
        expect(
            classify_post_nickname_b_transition(
                battle=sentinel, flow=0x00000BB8, outer_ptr=0x0839EF68,
                nickname_flow=nickname_registered, nickname_outer=outer,
                battle_inactive=inactive, pre_capture_party_count=count,
            ) == "DIRECT_PARTY_RETURN",
            f"CV generic post-nickname transition works for pre-catch party count {count}",
        )
        expect(
            classify_post_nickname_b_transition(
                battle=sentinel, flow=nickname_registered, outer_ptr=outer + 0x100,
                nickname_flow=nickname_registered, nickname_outer=outer,
                battle_inactive=inactive, pre_capture_party_count=count,
            ) == "DIRECT_PARTY_RETURN",
            f"CV changed outer with same flow is no-input return-in-progress for free-party count {count}",
        )

    # Previously mapped intermediate flows remain valid, but are no longer
    # special prerequisites when capacity is known.
    for flow in (
        POST_CAPTURE_FLOW_DIRECT_PARTY_RETURN_REGISTERED,
        POST_CAPTURE_FLOW_DIRECT_PARTY_RETURN_UNREGISTERED,
        POST_CAPTURE_FLOW_DIRECT_PARTY_TRANSITION_FREE,
        POST_CAPTURE_FLOW_DIRECT_PARTY_TRANSITION_FINAL_SLOT_REGISTERED,
    ):
        expect(
            classify_post_nickname_b_transition(
                battle=sentinel, flow=flow, outer_ptr=outer,
                nickname_flow=nickname_registered, nickname_outer=outer,
                battle_inactive=inactive, pre_capture_party_count=3,
            ) == "DIRECT_PARTY_RETURN",
            f"CV known legacy flow 0x{flow:04X} is accepted under generic free-party policy",
        )

    # A direct field return is also valid for free-party captures.
    expect(
        classify_post_nickname_b_transition(
            battle=inactive, flow=0x00000005, outer_ptr=0,
            nickname_flow=nickname_unregistered, nickname_outer=outer,
            battle_inactive=inactive, pre_capture_party_count=2,
        ) == "DIRECT_PARTY_RETURN",
        "CV accepts direct field return after free-party nickname decline",
    )

    # Full party remains Box-only and must not inherit the generic free-party
    # behavior.
    expect(
        classify_post_nickname_b_transition(
            battle=sentinel, flow=nickname_registered, outer_ptr=outer + 0x100,
            nickname_flow=nickname_registered, nickname_outer=outer,
            battle_inactive=inactive, pre_capture_party_count=6,
        ) == "BOX_MESSAGE",
        "CV full-party branch remains Box-message-only",
    )
    expect(
        classify_post_nickname_b_transition(
            battle=sentinel, flow=0x00000BB8, outer_ptr=0x0839EF68,
            nickname_flow=nickname_registered, nickname_outer=outer,
            battle_inactive=inactive, pre_capture_party_count=6,
        ) == "UNKNOWN",
        "CV full party rejects generic free-party transition",
    )

    # Unknown capacity retains the old mapped-flow fallback and does not accept
    # arbitrary transient values.
    expect(
        classify_post_nickname_b_transition(
            battle=sentinel, flow=POST_CAPTURE_FLOW_DIRECT_PARTY_RETURN_UNREGISTERED,
            outer_ptr=outer, nickname_flow=nickname_unregistered,
            nickname_outer=outer, battle_inactive=inactive,
            pre_capture_party_count=None,
        ) == "DIRECT_PARTY_RETURN",
        "CV unknown capacity retains hardware-mapped direct-return fallback",
    )
    expect(
        classify_post_nickname_b_transition(
            battle=sentinel, flow=0x00000BB8, outer_ptr=0x0839EF68,
            nickname_flow=nickname_registered, nickname_outer=outer,
            battle_inactive=inactive, pre_capture_party_count=None,
        ) == "UNKNOWN",
        "CV unknown capacity does not accept arbitrary transient flow",
    )

    # Capacity alone never authorizes unrelated battle states.
    expect(
        classify_post_nickname_b_transition(
            battle=0x12345678, flow=0x00000BB8, outer_ptr=0x0839EF68,
            nickname_flow=nickname_registered, nickname_outer=outer,
            battle_inactive=inactive, pre_capture_party_count=4,
        ) == "UNKNOWN",
        "CV free-party generic transition still requires post-capture sentinel or direct field",
    )


def test_shiny_phase_extrema_reset_contract():
    dashboard = (ROOT / "qt_ui" / "dashboard_page.py").read_text(encoding="utf-8")
    starter = (ROOT / "qt_ui" / "backend_worker.py").read_text(encoding="utf-8")
    wild = (ROOT / "qt_ui" / "wild_worker.py").read_text(encoding="utf-8")
    static = (ROOT / "qt_ui" / "static_worker.py").read_text(encoding="utf-8")
    stats = (ROOT / "qt_ui" / "stats_page.py").read_text(encoding="utf-8")

    expect("def _reset_phase_session_extrema" in dashboard, "dashboard has shiny phase extrema reset helper")
    expect('if data.get("is_shiny"):\n            self._reset_phase_session_extrema()' in dashboard, "dashboard clears extrema on authoritative shiny")
    expect("def _reset_phase_session_extrema" in starter, "starter worker has extrema reset helper")
    starter_phase_reset = starter.index('self.lifetime["phase_seen"] = 0', starter.index('if pk6["is_shiny"]:'))
    starter_extrema_reset = starter.index('self._reset_phase_session_extrema()', starter_phase_reset)
    expect(starter_phase_reset < starter_extrema_reset, "starter shiny resets extrema after phase/probability commit")
    expect("def _reset_phase_extrema" in wild, "wild worker has phase extrema reset helper")
    expect(wild.count("self._reset_phase_extrema()") >= 3, "wild single/blocklist/horde shiny paths reset extrema")
    expect("def _reset_phase_extrema" in static, "static worker has phase extrema reset helper")
    static_shiny = static.index('if payload.get("is_shiny")')
    static_phase_reset = static.index('self.lifetime["phase_seen"] = 0', static_shiny)
    static_extrema_reset = static.index('self._reset_phase_extrema()', static_phase_reset)
    expect(static_phase_reset < static_extrema_reset, "static shiny resets extrema after phase/probability commit")
    expect('fmt_extreme = lambda value: "—" if value is None else str(value)' in stats, "stats page renders cleared extrema as em dash")

def test_skytrip_heap_anchor_probe_contract():
    module_base = 0x02000000
    region_base = 0x08080000
    region_size = 0x10000
    seed = region_base + 0x2000
    mainproc = region_base + 0x2800
    raw = bytearray(region_size)
    struct.pack_into("<I", raw, mainproc - region_base, module_base + SKYTRIP_VPTR_OFFSETS["MainProc"])

    class ProbeBridge:
        def query(self, address):
            address = int(address)
            if region_base <= address < region_base + region_size:
                return {"status": 0, "base": region_base, "size": region_size, "perm": 3, "state": 5, "page_flags": 0}
            return {"status": 0, "base": address, "size": 0x1000, "perm": 0, "state": 0, "page_flags": 0}

        def read(self, address, length):
            address, length = int(address), int(length)
            if region_base <= address and address + length <= region_base + region_size:
                off = address - region_base
                return bytes(raw[off:off + length])
            raise RuntimeError(f"unmapped probe read 0x{address:X}+{length}")

    candidates = {name: [] for name in SKYTRIP_VPTR_OFFSETS}
    reverse = {module_base + off: name for name, off in SKYTRIP_VPTR_OFFSETS.items()}
    probe = _probe_heap_anchor_vptrs(ProbeBridge(), [(0x290, seed)], reverse, candidates)
    expect(probe["status"] == "FOUND", "SkyTrip heap-anchor probe finds local exact vptr")
    expect(candidates["MainProc"][0]["address_int"] == mainproc, "SkyTrip local probe classifies MainProc address")
    expect(probe["reads"] <= 128, "SkyTrip heap-anchor probe stays within 64KiB/128 reads")
    expect(probe["bytes_read"] <= 0x10000, "SkyTrip heap-anchor probe byte budget is bounded")


def test_cu_final_party_slot_16e6_transition():
    # v0p43CV supersedes CU's count-5-only interpretation.  Once capacity is
    # proven free (1..5), 0x16E6 is simply one of several harmless no-input
    # intermediate return states.  Count 6 remains invalid for direct return.
    sentinel = 0x004FCDC0
    inactive = 0x00040001
    nick = 0x00001742
    outer = 0x08403E44
    flow = POST_CAPTURE_FLOW_DIRECT_PARTY_TRANSITION_FINAL_SLOT_REGISTERED
    for count in range(1, 6):
        expect(
            classify_post_nickname_b_transition(
                battle=sentinel, flow=flow, outer_ptr=outer,
                nickname_flow=nick, nickname_outer=outer, battle_inactive=inactive,
                pre_capture_party_count=count,
            ) == "DIRECT_PARTY_RETURN",
            f"CV treats 0x16E6 as generic no-input free-party transition for count {count}",
        )
    expect(
        classify_post_nickname_b_transition(
            battle=sentinel, flow=flow, outer_ptr=outer,
            nickname_flow=nick, nickname_outer=outer, battle_inactive=inactive,
            pre_capture_party_count=6,
        ) == "UNKNOWN",
        "CV still rejects 0x16E6 as direct return when the pre-catch party was full",
    )

def test_cw_cave_edge_start_contract():
    worker = (ROOT / "qt_ui" / "wild_worker.py").read_text(encoding="utf-8")
    cave = (ROOT / "qt_ui" / "cave_movement.py").read_text(encoding="utf-8")

    expect("def _cave_zone_ids_for_location" in worker, "CW resolves Cave authority from packaged encounter-zone data")
    expect("def _promote_runtime_to_cave" in worker, "CW can promote land movement to Cave runtime mode")
    expect("CAVE AUTO-AUTHORITY" in worker, "CW logs live Cave auto-authority")
    expect("edge tiles are legal starts" in worker, "CW explicitly authorizes Cave edge starts")
    expect("WALL_BOUNDED_NO_CORRIDOR_PREFLIGHT" in cave, "CW Cave Run uses wall-bounded no-corridor policy")
    expect("BLOCKED_BY_CAVE_COLLISION" in cave, "CW treats a blocked Cave edge as normal collision")
    expect("edge_start_allowed" in cave, "CW records edge-start Cave Run telemetry")
    expect("needs six clear tiles on BOTH sides" not in cave, "CW removes legacy +/-6 Cave Run startup requirement")



def test_oras_gift_party_slots_2_to_6_and_shared_static_labels():
    # The direct Gift engine may only authorize a newly-added target from
    # party slots 2-6; slot 1 is the existing lead and must never be mistaken
    # for the accepted gift even if it is the same species.
    def mon(species, pid, ec):
        return {
            "valid": True, "checksum_valid": True, "species": species,
            "pid": pid, "ec": ec, "is_shiny": False, "shiny_xor": 100,
        }
    lead_beldum = mon(374, "0x11111111", "0xAAAAAAAA")
    baseline = {parsed_identity(lead_beldum)}
    rows = [lead_beldum, {}, {}, {}, {}, {}]
    expect(find_new_gift(rows, baseline, 374) is None, "Gift engine never reuses slot-1 lead as gift authority")
    for slot in range(2, 7):
        rows = [lead_beldum, {}, {}, {}, {}, {}]
        rows[slot - 1] = mon(374, f"0x{slot:08X}", f"0x{(slot+100):08X}")
        found = find_new_gift(rows, baseline, 374)
        expect(found is not None, f"Gift PK6 readable from party slot {slot}")
        expect(found.get("party_slot") == slot, f"Gift evidence records party slot {slot}")

    as_gifts = {p.key for p in gift_profiles_for_game("alpha_sapphire")}
    or_gifts = {p.key for p in gift_profiles_for_game("omega_ruby")}
    for key in ("wynaut_egg", "togepi_egg", "castform", "beldum", "camerupt", "sharpedo"):
        expect(key in as_gifts and key in or_gifts, f"shared ORAS direct gift profile {key}")
        expect(get_gift_profile(key).automation_ready, f"direct gift {key} is selectable")

    # Shared ORAS rule: version-exclusive ground rings inherit one proof.
    expect(hardware_validation_label(get_static_profile("zekrom"), "alpha_sapphire").startswith("Hardware proven"), "Zekrom ring passed label")
    expect(hardware_validation_label(get_static_profile("reshiram"), "omega_ruby").startswith("Hardware proven"), "Reshiram ring passed label")
    dash = (ROOT / "qt_ui" / "dashboard_page.py").read_text(encoding="utf-8")
    expect('label = f"{profile.name} — {profile.location}"' in dash, "Static passed target label is Pokémon — Location")
    expect('self.gift_btn = QPushButton("Gifts")' in dash, "Gift Pokémon top-level category exists")
    expect('self.gift_start_requested.emit(self.selected_gift_profile)' in dash, "Gift Start routes selected gift profile")



def test_db_fossil_batch_five_policy():
    def mon(species, pid, ec, shiny=False):
        return {
            "valid": True, "checksum_valid": True, "species": species,
            "pid": pid, "ec": ec, "is_shiny": shiny, "shiny_xor": 0 if shiny else 100,
        }
    lead = mon(25, "0x11111111", "0xAAAAAAAA")
    # Mixed fossil batch: five different valid fossil species are legitimate.
    revived = [696, 140, 142, 410, 566]  # Tyrunt, Kabuto, Aerodactyl, Shieldon, Archen
    rows = [lead, {}, {}, {}, {}, {}]
    baseline = {parsed_identity(lead)}
    for slot, species in zip(range(2, 7), revived):
        rows[slot - 1] = mon(species, f"0x{slot:08X}", f"0x{slot+500:08X}")
        found = find_new_party_member(rows, baseline)
        expect(found is not None and found.get("party_slot") == slot, f"DC fossil batch observes next PK6 in slot {slot}")
        expect(int(found.get("species") or 0) in FOSSIL_SPECIES, f"DC slot {slot} accepts valid fossil species")
        baseline.add(parsed_identity(rows[slot - 1]))
    expect(len(baseline) == 6, "DC five mixed fossil revivals fill slots 2-6 before reset")

    for game in ("alpha_sapphire", "omega_ruby"):
        profiles = {p.key: p for p in gift_profiles_for_game(game)}
        batch = profiles.get("fossil_batch")
        expect(batch is not None and batch.automation_ready and batch.trigger_kind == "FOSSIL_BATCH_5",
               f"DC mixed fossil batch exposed for {game}")
        for key in ("fossil_kabuto", "fossil_lileep", "fossil_aerodactyl"):
            expect(key not in profiles,
                   f"DC target-specific fossil profile {key} hidden because Fossil Batch supersedes it")

    worker = (ROOT / "qt_ui" / "gift_worker.py").read_text(encoding="utf-8")
    for token in (
        'if self.gift_profile.trigger_kind == "FOSSIL_BATCH_5":',
        'species not in FOSSIL_SPECIES',
        'for batch_index, expected_slot in enumerate(range(2, 7), start=1):',
        'if payload.get("is_shiny"):',
        'no further fossil input sent',
        'batch_exhausted',
        'FOSSIL ADAPTIVE BATCH SIZE DISCOVERED',
        'batch_target = completed_nonshiny',
        'batch_target = 5',
    ):
        expect(token in worker, f"DC fossil mixed-batch worker contract: {token}")





def test_fossil_batch_only_ui_contract():
    from pokebot.gift.oras_gifts import GIFT_PROFILES, gift_profiles_for_game

    legacy = {
        "fossil_lileep", "fossil_anorith", "fossil_aerodactyl",
        "fossil_kabuto", "fossil_omanyte", "fossil_shieldon",
        "fossil_cranidos", "fossil_archen", "fossil_tirtouga",
        "fossil_tyrunt", "fossil_amaura",
    }
    expect(legacy.issubset(set(GIFT_PROFILES)),
           "legacy individual fossil profiles remain available internally")
    for game in ("alpha_sapphire", "omega_ruby"):
        visible = {p.key for p in gift_profiles_for_game(game)}
        expect("fossil_batch" in visible,
               f"Fossil Batch remains visible for {game}")
        expect(not (legacy & visible),
               f"individual fossil profiles are hidden from Gift UI for {game}")


def test_dp_mixed_fossil_species_agnostic_state_contract():
    worker = (ROOT / "qt_ui" / "gift_worker.py").read_text(encoding="utf-8")
    expect('postgift_owner = owner is not None and owner5c not in (None, 0)' in worker,
           "DP mixed fossils use zero/non-zero post-gift owner invariant")
    expect('if postgift_owner and owner174 not in (None, 0):' in worker,
           "DP received gift authority uses child pointer for any fossil species")
    expect('elif owner4 == 3 and postgift_owner:' in worker,
           "DP nickname Yes/No accepts any non-zero mixed-fossil owner marker")
    expect('or (postgift_owner and menu_flag == 1)' in worker,
           "DP return-ready accepts any non-zero mixed-fossil owner marker")
    expect('owner5c == 0x11 and owner174' not in worker,
           "DP removes Tyrunt-specific received owner value")



def test_dq_adaptive_fossil_batch_1_to_5_contract():
    worker = (ROOT / "qt_ui" / "gift_worker.py").read_text(encoding="utf-8")
    profile = (ROOT / "pokebot" / "gift" / "oras_gifts.py").read_text(encoding="utf-8")
    for token in (
        'allow_batch_exhausted=(completed_nonshiny >= 1)',
        'def _drain_or_fossil_exhausted_dialogue',
        '"batch_exhausted": True',
        'FOSSIL OR BATCH EXHAUSTED',
        'FOSSIL ADAPTIVE BATCH SIZE DISCOVERED',
        'if batch_index == 5:',
        'batch_target = 5',
        'if batch_target < 1 or batch_target > 5:',
        'current_count != 1 + batch_target',
        'completed_nonshiny != batch_target',
    ):
        expect(token in worker, f"DQ adaptive fossil batch contract: {token}")
    expect('Fossil Batch — Any 1–5 Fossils' in profile,
           "DQ exposes adaptive 1-5 fossil batch name")
    expect('allow_batch_exhausted=(completed_nonshiny >= 1)' in worker,
           "DQ cannot accept zero fossils as a reset batch")
    # Exhaustion must not be accepted after a real fossil offer/menu appeared.
    expect('and not saw_offer' in worker,
           "DQ only treats no-offer Devon return as fossil exhaustion")
    trigger_start = worker.index('def _trigger_or_fossil_state_machine')
    trigger_end = worker.index('def _', trigger_start + len('def _trigger_or_fossil_state_machine'))
    trigger = worker[trigger_start:trigger_end]
    expect('if state_name in {"FOSSIL_YES_NO", "FOSSIL_LIST"}:' in trigger,
           "DR counts only real Yes/No/list states as another fossil offer")
    expect('if state_name in {"FOSSIL_YES_NO", "FOSSIL_LIST", "FOSSIL_PROGRESS"}:' not in trigger,
           "DR does not misclassify generic FOSSIL_PROGRESS as another fossil offer")
    # The no-fossil drainer may only advance ordinary Devon dialogue.
    helper_start = worker.index('def _drain_or_fossil_exhausted_dialogue')
    helper_end = worker.index('def _trigger_or_fossil_state_machine', helper_start)
    helper = worker[helper_start:helper_end]
    expect('if state == "DEVON_DIALOGUE":' in helper,
           "DQ exhaustion helper advances only proven Devon dialogue")
    expect('FOSSIL_YES_NO' in helper and 'return None' in helper,
           "DQ refuses exhaustion when a real fossil offer appears")


def test_di_fossil_auto_capture_b_only_nickname_contract():
    worker = (ROOT / "qt_ui" / "gift_worker.py").read_text(encoding="utf-8")
    for token in (
        'inputs.pulse(("B",), hold_ms=180',
        'for attempt in range(1, 5):',
        'FOSSIL POST-REVIVAL B-ONLY CLEAR',
        'FOSSIL POST-REVIVAL B-ONLY CLEAR COMPLETE',
        'AUTO_CAPTURE_STYLE_B_ONLY_CLEAR',
        'Auto-Capture nickname policy: B only; A forbidden until clear completes',
        'allow_unmapped=True',
    ):
        expect(token in worker, f"DI fossil Auto-Capture-style B-only contract: {token}")
    for forbidden in (
        'FOSSIL NICKNAME SELECT NO DOWN',
        'FOSSIL NICKNAME CONFIRM NO A',
        'inputs.pulse(("DOWN",), hold_ms=160',
    ):
        expect(forbidden not in worker, f"DI removes explicit fossil Yes-selection path: {forbidden}")
    shiny_idx = worker.index('if payload.get("is_shiny"):', worker.index('for batch_index, expected_slot'))
    decline_idx = worker.index('self._decline_fossil_nickname(', shiny_idx)
    expect(shiny_idx < decline_idx, "DI shiny HOLD branch occurs before fossil B-only clear")
    helper_start = worker.index('def _decline_fossil_nickname')
    helper_end = worker.index('def _snapshot_party', helper_start)
    helper = worker[helper_start:helper_end]
    expect('inputs.pulse(("A",)' not in helper, "DI forbids A inside fossil post-revival nickname clear")
    expect('inputs.pulse(("DOWN",)' not in helper, "DI forbids DOWN inside fossil post-revival nickname clear")


def test_dd_idle_party_qthread_deferred_shutdown_contract():
    text = (ROOT / "qt_ui" / "main_window.py").read_text(encoding="utf-8")
    for token in (
        'self._closing = True',
        'self._request_auxiliary_shutdown()',
        'def _running_background_threads',
        'def _finish_deferred_close_when_idle',
        'self._shutdown_wait_timer.setInterval(100)',
        'QTimer.singleShot(0, self.close)',
        'MAIN_WINDOW_CLOSE_DEFERRED',
    ):
        expect(token in text, f"DD clean shutdown contract: {token}")
    expect('thread.wait(5_000)' not in text, "DD removes blocking 5-second auxiliary QThread wait")
    expect('.terminate()' not in text, "DD never force-terminates Qt threads")


def test_discord_shutdown_drains_async_tasks_contract():
    text = (ROOT / "qt_ui" / "discord_service.py").read_text(encoding="utf-8")
    expect("asyncio.all_tasks(loop)" in text, "Discord shutdown must inspect pending loop tasks")
    expect("asyncio.gather(*pending, return_exceptions=True)" in text, "Discord shutdown must drain cancelled tasks")
    expect("future.result(timeout=1.5)" in text, "Discord shutdown must await client.close before app exit")
    expect("thread.join(timeout=1.5)" in text, "Discord shutdown must join its worker thread")


def test_wild_bridge_recovery_covers_transient_wifi_drop_contract():
    text = (ROOT / "qt_ui" / "wild_worker.py").read_text(encoding="utf-8")
    expect("WILD_READ_RECOVERY_ROUNDS = 5" in text, "Wild bridge recovery must use five bounded probes")
    expect("1.80" in text, "Wild bridge recovery must cover a short Wi-Fi reconnect window")
    expect("BRIDGE READ RECOVERY: release probe unavailable" in text, "Recovery must record release probe failures")



def test_honey_any_items_slot_authority_contract():
    class FakeBridge:
        def __init__(self):
            self.mem = {}

        def put(self, address, data):
            for i, b in enumerate(bytes(data)):
                self.mem[int(address) + i] = int(b)

        def read(self, address, length, **_kwargs):
            return bytes(self.mem.get(int(address) + i, 0) for i in range(int(length)))

    br = FakeBridge()
    # Honey deliberately sits in save slot 37 and active-entry index 3.  The
    # feature must never depend on a fixed bag/save position.
    items = bytearray(ORAS_ITEMS_SIZE)
    rows = [(17, 4), (2, 12), (79, 1), (HONEY_ITEM_ID, 999), (125, 3)]
    for active_index, (item_id, qty) in enumerate(rows):
        save_index = active_index if active_index < 3 else active_index + 34
        if item_id == HONEY_ITEM_ID:
            save_index = 37
        struct.pack_into('<HH', items, save_index * 4, item_id, qty)
    br.put(ORAS_ITEMS_ADDR, items)
    honey = require_honey(br)
    assert honey['item_id'] == HONEY_ITEM_ID
    assert honey['quantity'] == 999
    assert honey['index'] == 37

    # Field controller compacts the active entries in the Bag's actual order.
    controller = 0x08D51000
    cursor = 0x08D52000
    active = [
        {'index': 0, 'item_id': 17, 'quantity': 4},
        {'index': 1, 'item_id': 2, 'quantity': 12},
        {'index': 2, 'item_id': 79, 'quantity': 1},
        {'index': 37, 'item_id': HONEY_ITEM_ID, 'quantity': 999},
        {'index': 38, 'item_id': 125, 'quantity': 3},
    ]
    br.put(controller + CONTROLLER_ENTRY_COUNT_OFF, struct.pack('<H', len(active)))
    br.put(controller + CONTROLLER_ENTRY_ARRAY_OFF, b''.join(
        struct.pack('<HH', r['item_id'], r['quantity']) for r in active
    ))
    br.put(controller + CONTROLLER_CURSOR_PTR_OFF, struct.pack('<I', cursor))
    br.put(controller + CONTROLLER_PAGE_OFF, b'\x00')
    br.put(cursor + CURSOR_SELECTOR_OFF, b'\x03')
    authority = {
        'controller': controller,
        'cursor': cursor,
        'entry_count': len(active),
        'page': 0,
        'selector': 3,
        'entries': active,
    }
    assert honey_active_index(authority) == 3
    state = read_items_controller_state(br, authority)
    assert state['cursor_encoding'] == 'UNRESOLVED_FIELD_DLLBAG'
    assert state['selected_index'] == 3
    assert state['selected']['item_id'] == HONEY_ITEM_ID
    assert state['selected']['quantity'] == 999


def test_honey_slot1_direct_bag_shortcut_contract():
    from pathlib import Path
    worker = (Path(__file__).resolve().parents[1] / 'qt_ui' / 'wild_worker.py').read_text(encoding='utf-8')
    dash = (Path(__file__).resolve().parents[1] / 'qt_ui' / 'dashboard_page.py').read_text(encoding='utf-8')
    assert 'require_honey(br, first_active_slot=True)' in worker
    assert 'HONEY YELLOW BAG SHORTCUT' in worker
    assert 'br, (133, 230)' in worker
    assert 'HONEY FIELD MENU X' not in worker
    assert 'HONEY FIELD MENU BAG' not in worker
    assert 'HONEY FIELD BAG R-POCKET' not in worker
    assert 'HONEY FIELD BAG HOME-UP' not in worker
    assert 'PRESELECTED_SLOT1_DIRECT_TWO_A' in worker
    assert 'disabled_after_hardware_raw_pattern_hits_0' in worker
    assert 'HONEY A1 SELECT' in worker
    assert 'HONEY A2 USE' in worker
    assert 'HONEY A2 READINESS: waiting 1.25 s after A1 before Use' in worker
    assert 'self._sleep_stop_aware(1.25)' in worker
    assert 'HONEY A2 RETRY AUTHORIZED' in worker
    assert 'HONEY A2 RETRY USE' in worker
    assert 'observe_deadline = time.monotonic() + 1.80' in worker
    assert 'guard_qty == expected' in worker
    assert 'HONEY PROBE' in worker
    assert 'highlight Honey once' in dash
    assert 'It will not scroll or change pockets' in dash



def test_horde_nonshiny_auto_attack_selector_contract():
    db = ensure_move_metadata(ROOT)
    available = {1, 2, 3, 4}

    cases = [
        ({"moves": [33, 230, 0, 0], "move_pp": [35, 20, 0, 0]}, 1),
        ({"moves": [230, 33, 0, 0], "move_pp": [20, 35, 0, 0]}, 2),
        ({"moves": [230, 0, 85, 0], "move_pp": [20, 0, 15, 0]}, 3),
        ({"moves": [230, 0, 0, 98], "move_pp": [20, 0, 0, 30]}, 4),
    ]
    for pk6, expected_slot in cases:
        plan = choose_safe_attack(pk6, db, available)
        selected = plan.get("selected")
        assert selected is not None
        assert int(selected["move_slot"]) == expected_slot
        assert bool(selected["safe"])

    zero_pp = choose_safe_attack(
        {"moves": [33, 85, 0, 0], "move_pp": [0, 15, 0, 0]}, db, available
    )
    assert int(zero_pp["selected"]["move_slot"]) == 2

    unsafe_only = choose_safe_attack(
        {"moves": [89, 57, 230, 45], "move_pp": [10, 15, 20, 40]}, db, available
    )
    assert unsafe_only.get("selected") is None

    worker = (ROOT / "qt_ui" / "wild_worker.py").read_text(encoding="utf-8")
    dash = (ROOT / "qt_ui" / "dashboard_page.py").read_text(encoding="utf-8")
    live = (ROOT / "pokebot" / "wild" / "horde_live_capture.py").read_text(encoding="utf-8")
    assert "Test Auto-Attack on next non-shiny Horde" in dash
    assert "take_horde_auto_attack_test_snapshot" in dash
    assert "def _run_horde_auto_attack_test" in worker
    assert "and not shiny_payloads" in worker
    assert "live_nonshiny_horde_auto_attack_test" in worker
    assert "Horde Auto-Attack TEST refuses every attack because a real shiny is present" in live
    assert "LIVE_AUTO_ATTACK_TEST_MOVE_SLOT_" in live
    assert "LIVE_AUTO_ATTACK_TEST_TARGET_" in live
    assert "D25_PRODUCTION_MOVE_SELECTOR_ONE_ATTACK_NONSHINY_HORDE_TEST" in live
    assert "LIVE_POLICY_WITH_HARDWARE_CALIBRATED_MOVE_BUTTONS" in live
    assert "move XY from v0p43EF hardware framebuffer calibration" in live
    touch = (ROOT / "pokebot" / "wild" / "oras_touch_profile.py").read_text(encoding="utf-8")
    assert "3: (74, 133)" in touch
    assert "4: (246, 133)" in touch
    assert "TARGET SELECTOR MISS" in live



def test_horde_three_discriminator_phase_fingerprint_contract():
    helper = (ROOT / "tools" / "horde_protected_survivor_full_auto.py").read_text(encoding="utf-8")
    expect('if len(offsets) < 3:' in helper, "hardware-proven 3-byte command/move discriminator floor")
    expect('strong_phase' in helper and 'exact_phase' in helper, "owner+0x93 exact phase remains authority")


def test_static_u32_single_timeout_retry_contract():
    worker = (ROOT / "qt_ui" / "static_worker.py").read_text(encoding="utf-8")
    expect('def _read_u32(self, bridge: Bridge, address: int)' in worker, "static u32 helper missing")
    expect('attempts=2, label="STATIC_U32"' in worker, "static u32 read must retry one lost UDP reply")
    expect('raw = self._transport_read(' in worker, "static u32 must use idempotent transport retry helper")


def test_postgame_birch_starter_contract():
    from pokebot.gift.oras_gifts import get_gift_profile, gift_profiles_for_game
    keys = (
        "chikorita", "cyndaquil", "totodile",
        "snivy", "tepig", "oshawott",
        "turtwig", "chimchar", "piplup",
    )
    for key in keys:
        p = get_gift_profile(key)
        expect(p.automation_ready, f"{key} postgame starter is selectable")
        expect(p.trigger_kind == "POSTGAME_BIRCH_STARTER",
               f"{key} uses postgame Birch starter trigger")
    for game in ("alpha_sapphire", "omega_ruby"):
        visible = {p.key for p in gift_profiles_for_game(game)}
        expect(set(keys).issubset(visible),
               f"all postgame Birch starters remain visible/selectable in {game}")

    text = (ROOT / "qt_ui" / "gift_worker.py").read_text(encoding="utf-8")
    expect('"prelude": {"down": 1, "a": 5}' in text,
           "postgame starter path records exact DOWN + five-A bootstrap")
    expect('inputs.pulse(("LEFT",)' in text and 'inputs.pulse(("RIGHT",)' in text,
           "postgame starter uses left/middle/right chooser navigation")
    expect("POSTGAME_STARTER_CONFIRM_GATE" in text,
           "postgame starter uses RAM-gated chooser confirmation")
    expect("_wait_for_gift(bridge, baseline, timeout_s=6.0)" in text,
           "postgame starter final authority is new Party PK6")


def test_gift_party_count_static_contract():
    path = ROOT / "qt_ui" / "gift_worker.py"
    text = path.read_text(encoding="utf-8")
    expect("@staticmethod\n    def _party_count(snap: dict) -> int:" in text,
           "gift _party_count is static so self._party_count(snapshot) accepts exactly one snapshot argument")


def test_gift_saved_field_settle_contract():
    path = ROOT / "qt_ui" / "gift_worker.py"
    text = path.read_text(encoding="utf-8")
    expect("def _wait_for_saved_field_authority" in text,
           "gift worker has bounded input-free saved-field settle helper")
    expect("@staticmethod\n    def _wait_for_saved_field_authority" not in text,
           "gift saved-field settle helper remains instance-bound because it uses self")
    expect("GIFT SAVED FIELD SETTLE READY" in text,
           "gift worker logs successful saved-field convergence")
    expect(text.count("self._wait_for_saved_field_authority(b, self.anchor, timeout_s=2.0)") == 2,
           "both gift reset paths use saved-field settle authority")


def test_gift_party_post_reset_remap_contract():
    path = ROOT / "qt_ui" / "gift_worker.py"
    text = path.read_text(encoding="utf-8")
    expect("def _wait_for_party_authority" in text, "gift worker has bounded live-party remap helper")
    expect("allow_unmapped=True" in text, "gift remap helper permits transient UNMAPPED_D25")
    expect("time.sleep(0.20)" in text, "gift remap helper uses bounded polling delay")
    expect("before = self._wait_for_party_authority(bridge, timeout_s=10.0)" in text,
           "gift baseline waits for live-party authority after reset")


def test_shiny_sound_on_detection_contract():
    path = ROOT / "qt_ui" / "main_window.py"
    text = path.read_text(encoding="utf-8")
    encounter_start = text.index("    def _encounter_update(self, payload):")
    encounter_end = text.index("\n    def ", encounter_start + 5)
    encounter = text[encounter_start:encounter_end]
    finish_start = text.index("    def _hunt_finished(self, status):")
    finish_end = text.index("\n    def ", finish_start + 5)
    finish = text[finish_start:finish_end]

    expect('if bool(event.get("is_shiny")):' in encounter,
           "shiny sound is triggered from RAM-confirmed encounter events")
    expect("play_shiny_sound(" in encounter,
           "encounter shiny path plays configured shiny sound")
    expect("_shiny_sound_seen" in encounter,
           "encounter shiny sound is deduplicated by encounter identity")
    expect("play_shiny_sound(" not in finish,
           "hunt-finished SHINY HOLD path does not double-play the alert")


def test_low_end_startup_guard_contract():
    stats = (ROOT / "qt_ui" / "stats_page.py").read_text(encoding="utf-8")
    appdata = (ROOT / "qt_ui" / "appdata_store.py").read_text(encoding="utf-8")
    main = (ROOT / "qt_ui" / "main_window.py").read_text(encoding="utf-8")
    app = (ROOT / "qt_ui" / "app.py").read_text(encoding="utf-8")

    # Stats must not synchronously parse all JSONL ledgers in __init__.
    stats_init = stats[stats.index('class StatsPage'):stats.index('    def _make_table', stats.index('class StatsPage'))]
    expect('self.refresh()' not in stats_init, "StatsPage startup must remain lazy")
    expect('with path.open("r", encoding="utf-8") as fh:' in stats, "Stats JSONL reads must stay streamed")
    expect('def _data_signature(self):' in stats, "Stats unchanged-ledger cache missing")

    expect('if not profile.migration_path.exists():' in appdata, "legacy migration must be marker-guarded")
    expect('QTimer.singleShot(350, self.run_probe)' in main, "startup connection probe must be deferred until UI paint")
    expect('self.party_refresh_timer.setInterval(1_000)' in main, "idle party QThread churn guard missing")
    expect(main.count('from .wild_worker import WildHuntWorker') == 1, "Wild worker should be lazy-imported once")
    expect(main.count('from .static_worker import StaticHuntWorker') == 1, "Static worker should be lazy-imported once")
    expect(main.count('from .gift_worker import GiftHuntWorker') == 1, "Gift worker should be lazy-imported once")
    expect('MAIN_WINDOW_CONSTRUCTED elapsed_ms=' in app, "startup timing breadcrumb missing")

def main():
    tests = [
        test_di_fossil_auto_capture_b_only_nickname_contract,
        test_dd_idle_party_qthread_deferred_shutdown_contract,
        test_discord_shutdown_drains_async_tasks_contract,
        test_wild_bridge_recovery_covers_transient_wifi_drop_contract,
        test_fossil_batch_only_ui_contract,
        test_dp_mixed_fossil_species_agnostic_state_contract,
        test_dq_adaptive_fossil_batch_1_to_5_contract,
        test_db_fossil_batch_five_policy,
        test_honey_any_items_slot_authority_contract,
        test_honey_slot1_direct_bag_shortcut_contract,
        test_horde_nonshiny_auto_attack_selector_contract,
        test_horde_three_discriminator_phase_fingerprint_contract,
        test_omega_ruby_surf_fishing_exposed,
        test_eon_ticket_run_reinteract_static_contract,
        test_static_u32_single_timeout_retry_contract,
        test_postgame_birch_starter_contract,
        test_gift_party_count_static_contract,
        test_gift_saved_field_settle_contract,
        test_gift_party_post_reset_remap_contract,
        test_shiny_sound_on_detection_contract,
        test_low_end_startup_guard_contract,
        test_oras_gift_party_slots_2_to_6_and_shared_static_labels,
        test_fishing_odds,
        test_cumulative_phase_probability_and_charm_contract,
        test_fishing_tracker,
        test_auto_capture_lock,
        test_party_wurmple_prediction_and_auto_capture_test_policy,
        test_oras_split_evolution_and_omega_ruby_capture_charm,
        test_static_framework,
        test_static_reset_first_bootstrap_authority,
        test_static_saved_field_authority,
        test_manual_stop_propagates_through_reset_contract,
        test_static_reset_adapter_callback_isolation,
        test_public_profile_fishing_defaults,
        test_single_capture_waits_for_exact_touch_ready_command_phase,
        test_or_bag_transition_grace_after_command_gate_drop,
        test_single_capture_dynamic_bag_owner_relocation,
        test_static_persistence_contract,
        test_static_regigigas_transport_and_zone_contract,
        test_soaring_rift_mapper_contract,
        test_dialga_state2_profile_policy,
        test_skytrip_runtime_object_mapper_contract,
        test_skytrip_runtime_state_delta_contract,
        test_skytrip_external_reference_state_mapper_contract,
        test_skytrip_external_reference_query_safe_segment_contract,
        test_skytrip_escaped_pointer_ownership_contract,
        test_skytrip_fast_cached_reference_and_motion_contract,
        test_free_party_post_capture_direct_return_contract,
        test_cu_final_party_slot_16e6_transition,
        test_shiny_phase_extrema_reset_contract,
        test_skytrip_heap_anchor_probe_contract,
        test_static_ring_family_promotion_and_party_contract,
        test_cw_cave_edge_start_contract,
    ]
    results = []
    for fn in tests:
        fn()
        results.append({"test": fn.__name__, "status": "PASS"})
    payload = {"status": "PASS", "tests": results}
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
