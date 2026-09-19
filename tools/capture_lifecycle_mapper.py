from __future__ import annotations

"""Instrument an entire ORAS Ball -> capture/breakout -> field lifecycle.

v0p43AL is intentionally a standalone mapper.  It does NOT modify the live
hunt worker and never writes game RAM.  It throws exactly one non-Master Ball,
records RAM continuously, then:

* on a successful capture, follows only already hardware-mapped post-capture
  states (Pokédex / nickname / Box) with conservative readiness guards until
  the overworld is stable;
* on an apparent returned battle command menu, asks the operator to confirm
  what is visibly on the 3DS rather than reusing the live breakout classifier;
* on an unknown intermediate screen (EXP / level-up / move learning etc.),
  lets the operator request one instrumented A or B pulse while recording the
  exact RAM state immediately before and after it.

The purpose is to expose the first RAM state where real capture and real
breakout diverge.  No Master Ball is ever eligible in this mapper.
"""

import hashlib
import json
import math
import struct
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pokebot.wild.battle_bag_throw import (
    OWNER,
    BAG_OFF,
    BAG_STATE_OFF,
    BAG_CONTROLLER_OFF,
    HID_A,
    HID_B,
    POST_CAPTURE_SENTINEL,
    POST_CAPTURE_FLOW_POKEDEX,
    POST_CAPTURE_FLOW_NICKNAME_REGISTERED,
    POST_CAPTURE_FLOW_NICKNAME_UNREGISTERED,
    POST_CAPTURE_FLOW_DIRECT_PARTY_RETURN,
    POST_CAPTURE_FLOW_DIRECT_PARTY_RETURN_REGISTERED,
    POST_CAPTURE_FLOW_AFTER_BOX,
    throw_one_best_ball,
)
from pokebot.wild.validated_loader import load_walk_v0p23
from qt_ui.appdata_store import get_profile_paths
from qt_ui.settings_store import load_settings

AS_TITLE_ID = 0x000400000011C500
STATE_WINDOW_BASE = 0x081FB380
STATE_WINDOW_SIZE = 0x110
STATE_OFF_OUTER = 0x04
STATE_OFF_FLOW = 0x10
STATE_OFF_BATTLE = 0xF8
OUTER_TO_VIEW = 0x190
VIEW_STATE_OFF = 0x3C
VIEW_MASK_OFF = 0xA8
VIEW_READ_SIZE = 0xB0
HEAP_MIN = 0x08000000
HEAP_MAX = 0x10000000

PRIMARY_X_ADDR = 0x08C6E7B0
PRIMARY_Z_ADDR = 0x08C6E7B8
SECONDARY_X_ADDR = 0x08DA8568
SECONDARY_Z_ADDR = 0x08DA8570

SAMPLE_TARGET_SECONDS = 0.05
POST_THROW_MAX_SECONDS = 120.0
KNOWN_FLOW_STABLE_SECONDS = 3.0
NICKNAME_READY_SECONDS = 3.0
NICKNAME_RETRY_READY_SECONDS = 2.0
BOX_READY_SECONDS = 1.5
FIELD_STABLE_SECONDS = 2.0
BREAKOUT_PROMPT_GATE_STABLE_SECONDS = 3.0
UNKNOWN_OPERATOR_PROMPT_AFTER_SECONDS = 40.0
MAX_NICKNAME_B_ATTEMPTS = 3

# v0p43AL broad-memory evidence.  The RAM bridge response is capped below 4 KiB,
# so larger windows are deliberately read in bounded chunks.
BROAD_GLOBAL_BASE = 0x081FB000
BROAD_GLOBAL_SIZE = 0x5000
BROAD_OWNER_BASE = 0x0852FA00
BROAD_OWNER_SIZE = 0x1000
BROAD_READ_CHUNK = 0x0200
BROAD_MILESTONES_SECONDS = (
    0.00, 5.00, 10.00, 15.00, 20.00, 25.00, 30.00, 45.00, 60.00,
)

# Static reverse-engineering evidence from the supplied Alpha Sapphire base
# ExeFS.  EventBattleCall constructs EventBattleReturn with this vptr.  The
# object has an internal state field at +0x48 and an 8-way state switch.  This
# is evidence only until a live object is hardware-located; no production logic
# depends on these constants.
EVENT_BATTLE_RETURN_VPTR = 0x005DF09C
EVENT_BATTLE_RETURN_STATE_OFF = 0x48


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def hx(v: int | None) -> str | None:
    if v is None:
        return None
    return f"0x{int(v) & 0xFFFFFFFF:08X}"


def valid_ptr(v: int) -> bool:
    return HEAP_MIN <= int(v) < HEAP_MAX and (int(v) & 3) == 0


def u32(blob: bytes, off: int) -> int:
    return struct.unpack_from("<I", blob, off)[0]


def f32(br, addr: int) -> float:
    return struct.unpack("<f", br.read(addr, 4))[0]


def read_position(br, core) -> dict:
    zone_raw = br.u32(core.ZONE_ADDR)
    return {
        "zone_raw": hx(zone_raw),
        "zone_id": zone_raw & 0xFFFF,
        "world_primary": [f32(br, PRIMARY_X_ADDR), f32(br, PRIMARY_Z_ADDR)],
        "world_secondary": [f32(br, SECONDARY_X_ADDR), f32(br, SECONDARY_Z_ADDR)],
    }


def read_chunked(br, base: int, size: int, chunk: int = BROAD_READ_CHUNK) -> bytes:
    out = bytearray()
    offset = 0
    while offset < int(size):
        take = min(int(chunk), int(size) - offset)
        out.extend(br.read(int(base) + offset, take))
        offset += take
    return bytes(out)


def decode_pk6(core, raw: bytes, tid: int, sid: int) -> dict:
    try:
        p = core.decode_stored_pk6(raw, tid, sid)
        return {
            "valid": bool(p.get("valid")),
            "species": p.get("species"),
            "pid": p.get("pid"),
            "identity": p.get("identity"),
            "shiny_xor": p.get("shiny_xor"),
            "is_shiny": p.get("is_shiny"),
            "reason": p.get("reason"),
        }
    except Exception as exc:
        return {"valid": False, "error": f"{type(exc).__name__}: {exc}"}


class Recorder:
    def __init__(self, host: str, timeout: float, core, tid: int, sid: int):
        self.core = core
        self.tid = tid
        self.sid = sid
        self.br = core.Bridge(host, timeout=timeout)
        # Keep sampler request ids away from the input bridge sequence range.
        try:
            self.br._seq = (self.br._seq ^ 0x40000000) & 0x7FFFFFFF
        except Exception:
            pass
        self.samples: list[dict] = []
        self.transitions: list[dict] = []
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._t0 = time.monotonic()
        self._last_key = None
        self._last_pk6_hash = None
        self._last_pk6_decoded = None

    def rel(self) -> float:
        return round(time.monotonic() - self._t0, 3)

    def snapshot(self, label: str | None = None, *, include_blobs: bool = False,
                 include_position: bool = False) -> dict:
        with self._lock:
            t = self.rel()
            out: dict = {"t": t, "time": now_iso()}
            if label:
                out["label"] = label
            try:
                sw = self.br.read(STATE_WINDOW_BASE, STATE_WINDOW_SIZE)
                outer = u32(sw, STATE_OFF_OUTER)
                flow = u32(sw, STATE_OFF_FLOW)
                battle = u32(sw, STATE_OFF_BATTLE)
                out.update({
                    "battle": hx(battle),
                    "battle_u32": battle,
                    "flow": hx(flow),
                    "flow_u32": flow,
                    "outer_ptr": hx(outer),
                    "outer_ptr_u32": outer,
                    "state_window_sha256": hashlib.sha256(sw).hexdigest(),
                })
                if include_blobs:
                    out["state_window_hex"] = sw.hex()

                gate = False
                view = 0
                view_state = None
                view_mask = None
                view_blob = None
                if battle == self.core.BATTLE_ACTIVE and valid_ptr(outer):
                    try:
                        view = self.br.u32(outer + OUTER_TO_VIEW)
                        if valid_ptr(view):
                            view_blob = self.br.read(view, VIEW_READ_SIZE)
                            view_state = u32(view_blob, VIEW_STATE_OFF)
                            view_mask = u32(view_blob, VIEW_MASK_OFF)
                            gate = (
                                view_state == self.core.READY_STATE
                                and view_mask == self.core.READY_MASK
                            )
                    except Exception as exc:
                        out["gate_error"] = f"{type(exc).__name__}: {exc}"
                out.update({
                    "view_ptr": hx(view),
                    "view_state": hx(view_state),
                    "view_mask": hx(view_mask),
                    "command_gate": bool(gate),
                })
                if view_blob is not None:
                    out["view_sha256"] = hashlib.sha256(view_blob).hexdigest()
                    if include_blobs:
                        out["view_hex"] = view_blob.hex()

                try:
                    bag = OWNER + BAG_OFF
                    bag_blob = self.br.read(bag, 0x24)
                    out["bag_state"] = bag_blob[BAG_STATE_OFF]
                    out["bag_controller"] = hx(u32(bag_blob, BAG_CONTROLLER_OFF))
                    out["bag_sha256"] = hashlib.sha256(bag_blob).hexdigest()
                    if include_blobs:
                        out["bag_hex"] = bag_blob.hex()
                except Exception as exc:
                    out["bag_error"] = f"{type(exc).__name__}: {exc}"

                try:
                    raw = self.br.read(self.core.WILD_PK6_ADDR, self.core.PK6_STORED_SIZE)
                    ph = hashlib.sha256(raw).hexdigest()
                    out["wild_pk6_sha256"] = ph
                    if ph != self._last_pk6_hash:
                        self._last_pk6_hash = ph
                        self._last_pk6_decoded = decode_pk6(self.core, raw, self.tid, self.sid)
                    out["wild_pk6"] = self._last_pk6_decoded
                    if include_blobs:
                        out["wild_pk6_hex"] = raw.hex()
                except Exception as exc:
                    out["wild_pk6_error"] = f"{type(exc).__name__}: {exc}"

                if include_position:
                    try:
                        out["position"] = read_position(self.br, self.core)
                    except Exception as exc:
                        out["position_error"] = f"{type(exc).__name__}: {exc}"

                key = (
                    out.get("battle"), out.get("flow"), out.get("outer_ptr"),
                    out.get("view_ptr"), out.get("view_state"), out.get("view_mask"),
                    out.get("command_gate"), out.get("bag_state"),
                    out.get("bag_controller"), out.get("wild_pk6_sha256"),
                )
                out["transition"] = key != self._last_key
                if out["transition"]:
                    self._last_key = key
                    # Transition records carry raw object windows so unknown phases
                    # can be diffed after hardware testing.
                    tr = dict(out)
                    try:
                        if valid_ptr(outer):
                            ob = self.br.read(outer, 0x200)
                            tr["outer_blob_sha256"] = hashlib.sha256(ob).hexdigest()
                            tr["outer_blob_hex"] = ob.hex()
                    except Exception as exc:
                        tr["outer_blob_error"] = f"{type(exc).__name__}: {exc}"
                    self.transitions.append(tr)
                return out
            except Exception as exc:
                out["sample_error"] = f"{type(exc).__name__}: {exc}"
                return out

    def _loop(self):
        next_tick = time.monotonic()
        while not self._stop.is_set():
            started = time.monotonic()
            s = self.snapshot()
            self.samples.append(s)
            next_tick += SAMPLE_TARGET_SECONDS
            sleep_for = next_tick - time.monotonic()
            if sleep_for > 0:
                self._stop.wait(sleep_for)
            else:
                # Reads took longer than target cadence; preserve actual times
                # instead of spawning overlapping requests.
                next_tick = time.monotonic()

    def start(self):
        self._t0 = time.monotonic()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="capture-lifecycle-recorder", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)


class Mapper:
    def __init__(self, br, core, recorder: Recorder, report: dict):
        self.br = br
        self.core = core
        self.rec = recorder
        self.report = report
        self.input_events: list[dict] = report.setdefault("input_events", [])

    def main_snapshot(self, label: str, *, position=False) -> dict:
        # Use the recorder's second bridge so the snapshot has exactly the same
        # schema as timeline samples.  Main thread calls are serialized with the
        # recorder thread only by UDP itself; each request is stateless.
        return self.rec.snapshot(label, include_blobs=True, include_position=position)

    def broad_dump(self, label: str) -> dict:
        rec = {
            "label": label,
            "t": self.rec.rel(),
            "time": now_iso(),
            "global_base": hx(BROAD_GLOBAL_BASE),
            "global_size": BROAD_GLOBAL_SIZE,
            "owner_base": hx(BROAD_OWNER_BASE),
            "owner_size": BROAD_OWNER_SIZE,
            "event_battle_return_vptr_static": hx(EVENT_BATTLE_RETURN_VPTR),
            "event_battle_return_state_off": hx(EVENT_BATTLE_RETURN_STATE_OFF),
        }
        try:
            blob = read_chunked(self.br, BROAD_GLOBAL_BASE, BROAD_GLOBAL_SIZE)
            rec["global_sha256"] = hashlib.sha256(blob).hexdigest()
            rec["global_hex"] = blob.hex()
            pat = struct.pack("<I", EVENT_BATTLE_RETURN_VPTR)
            rec["event_battle_return_vptr_hits_global"] = [
                hx(BROAD_GLOBAL_BASE + i) for i in range(0, len(blob) - 3, 4)
                if blob[i:i+4] == pat
            ]
        except Exception as exc:
            rec["global_error"] = f"{type(exc).__name__}: {exc}"
        try:
            blob = read_chunked(self.br, BROAD_OWNER_BASE, BROAD_OWNER_SIZE)
            rec["owner_sha256"] = hashlib.sha256(blob).hexdigest()
            rec["owner_hex"] = blob.hex()
            pat = struct.pack("<I", EVENT_BATTLE_RETURN_VPTR)
            rec["event_battle_return_vptr_hits_owner"] = [
                hx(BROAD_OWNER_BASE + i) for i in range(0, len(blob) - 3, 4)
                if blob[i:i+4] == pat
            ]
        except Exception as exc:
            rec["owner_error"] = f"{type(exc).__name__}: {exc}"
        try:
            rec["anchor"] = self.main_snapshot(label + "_ANCHOR")
            anchor = rec["anchor"]
            # Capture the live dynamic UI/event objects too.  These heap objects
            # are where a class vptr such as EventBattleReturn is more likely to
            # live than in a fixed global block.
            for key, ptr_key in (("outer_object", "outer_ptr_u32"), ("view_object", "view_ptr")):
                ptr = anchor.get(ptr_key)
                if isinstance(ptr, str):
                    try:
                        ptr = int(ptr, 0)
                    except Exception:
                        ptr = 0
                if isinstance(ptr, int) and valid_ptr(ptr):
                    try:
                        blob = read_chunked(self.br, ptr, 0x400)
                        rec[key + "_base"] = hx(ptr)
                        rec[key + "_size"] = len(blob)
                        rec[key + "_sha256"] = hashlib.sha256(blob).hexdigest()
                        rec[key + "_hex"] = blob.hex()
                        pat = struct.pack("<I", EVENT_BATTLE_RETURN_VPTR)
                        rec[key + "_event_battle_return_vptr_hits"] = [
                            hx(ptr + i) for i in range(0, len(blob) - 3, 4)
                            if blob[i:i+4] == pat
                        ]
                    except Exception as exc:
                        rec[key + "_error"] = f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            rec["anchor_error"] = f"{type(exc).__name__}: {exc}"
        self.report.setdefault("broad_snapshots", []).append(rec)
        return rec

    def pulse(self, raw_hid: int, label: str, hold_ms=180, settle_ms=500) -> dict:
        # Preserve a broad RAM image immediately around every instrumented input.
        broad_before = self.broad_dump(label + "_BROAD_BEFORE")
        before = self.main_snapshot(label + "_BEFORE")
        result = self.br.hid_pulse_no_retransmit(raw_hid, hold_ms, settle_ms)
        after = self.main_snapshot(label + "_AFTER")
        broad_after = self.broad_dump(label + "_BROAD_AFTER")
        ev = {"label": label, "before": before, "input": result, "after": after,
              "broad_before_label": broad_before.get("label"),
              "broad_after_label": broad_after.get("label")}
        self.input_events.append(ev)
        if not result.get("completed"):
            raise RuntimeError(label + " did not reach acknowledged COMPLETED")
        return ev

    def read_key(self) -> dict:
        return self.rec.snapshot()

    def wait_stable(self, predicate, stable_seconds: float, timeout: float, label: str) -> dict:
        deadline = time.monotonic() + timeout
        stable_since = None
        stable_key = None
        last = None
        while time.monotonic() < deadline:
            cur = self.read_key()
            last = cur
            if predicate(cur):
                key = (cur.get("battle"), cur.get("flow"), cur.get("outer_ptr"), cur.get("command_gate"))
                if key != stable_key:
                    stable_key = key
                    stable_since = time.monotonic()
                elif stable_since is not None and time.monotonic() - stable_since >= stable_seconds:
                    marked = self.main_snapshot(label, position=(label == "OVERWORLD_STABLE"))
                    marked["stable_seconds"] = round(time.monotonic() - stable_since, 3)
                    return marked
            else:
                stable_since = None
                stable_key = None
            time.sleep(0.05)
        raise RuntimeError(f"timeout waiting for {label}; last={last}")

    def drive_success_to_field(self, first: dict) -> dict:
        post = {
            "started_t": self.rec.rel(),
            "first_mapped_state": first,
            "branch": None,
            "events": [],
        }
        flow = int(first.get("flow_u32") or 0)

        if flow == POST_CAPTURE_FLOW_POKEDEX:
            post["branch"] = "UNREGISTERED"
            pokedex_outer = int(first.get("outer_ptr_u32") or 0)
            print("Mapped Pokédex flow 0x1735 seen. Waiting 3 s for exact settled object before B...")
            self.wait_stable(
                lambda s: int(s.get("flow_u32") or 0) == POST_CAPTURE_FLOW_POKEDEX
                and int(s.get("outer_ptr_u32") or 0) == pokedex_outer,
                KNOWN_FLOW_STABLE_SECONDS, 15.0, "POKEDEX_INPUT_READY",
            )
            post["events"].append(self.pulse(HID_B, "POKEDEX_B"))
            nick = self.wait_stable(
                lambda s: int(s.get("flow_u32") or 0) == POST_CAPTURE_FLOW_NICKNAME_UNREGISTERED
                and int(s.get("outer_ptr_u32") or 0) not in (0, pokedex_outer),
                0.30, 8.0, "NICKNAME_UNREGISTERED_APPEARED",
            )
        elif flow == POST_CAPTURE_FLOW_NICKNAME_REGISTERED:
            post["branch"] = "REGISTERED"
            nick = first
        elif flow == POST_CAPTURE_FLOW_NICKNAME_UNREGISTERED:
            post["branch"] = "UNREGISTERED_NICKNAME_ALREADY_VISIBLE"
            nick = first
        else:
            raise RuntimeError("drive_success_to_field called without mapped first flow")

        nickname_flow = int(nick.get("flow_u32") or 0)
        nickname_outer = int(nick.get("outer_ptr_u32") or 0)
        print("Nickname flow seen. Waiting 3 s for exact settled object before B...")
        self.wait_stable(
            lambda s: int(s.get("flow_u32") or 0) == nickname_flow
            and int(s.get("outer_ptr_u32") or 0) == nickname_outer,
            NICKNAME_READY_SECONDS, 18.0, "NICKNAME_INPUT_READY",
        )

        box = None
        for attempt in range(1, MAX_NICKNAME_B_ATTEMPTS + 1):
            if attempt > 1:
                print(f"B attempt {attempt-1} was ignored. Re-proving nickname readiness before retry {attempt}...")
                self.wait_stable(
                    lambda s: int(s.get("flow_u32") or 0) == nickname_flow
                    and int(s.get("outer_ptr_u32") or 0) == nickname_outer,
                    NICKNAME_RETRY_READY_SECONDS, 12.0, f"NICKNAME_RETRY_{attempt}_READY",
                )
            post["events"].append(self.pulse(HID_B, f"NICKNAME_B_{attempt}"))
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                cur = self.read_key()
                cf = int(cur.get("flow_u32") or 0)
                co = int(cur.get("outer_ptr_u32") or 0)
                if cf == nickname_flow and co not in (0, nickname_outer):
                    box = cur
                    break
                if cf == nickname_flow and co == nickname_outer:
                    time.sleep(0.05)
                    continue
                if (
                    cf in {POST_CAPTURE_FLOW_DIRECT_PARTY_RETURN, POST_CAPTURE_FLOW_DIRECT_PARTY_RETURN_REGISTERED}
                    or (
                        int(cur.get("battle_u32") or 0) == self.core.BATTLE_INACTIVE
                        and cf == 0x00000005
                    )
                ):
                    post["branch"] = "FREE_PARTY_SLOT_NO_BOX_MESSAGE"
                    post["direct_return"] = cur
                    field = self.wait_stable(
                        lambda s: int(s.get("battle_u32") or 0) == self.core.BATTLE_INACTIVE
                        and int(s.get("flow_u32") or 0) == 0x00000005,
                        FIELD_STABLE_SECONDS, 15.0, "OVERWORLD_STABLE_DIRECT_RETURN",
                    )
                    post["overworld"] = field
                    post["finished_t"] = self.rec.rel()
                    return post
                raise RuntimeError(f"unexpected state after nickname B: {cur}")
            if box is not None:
                break
        if box is None:
            raise RuntimeError("nickname B remained ignored after 3 guarded attempts; no A sent")

        box_outer = int(box.get("outer_ptr_u32") or 0)
        print("Box-message object transition seen. Waiting 1.5 s before A...")
        self.wait_stable(
            lambda s: int(s.get("flow_u32") or 0) == nickname_flow
            and int(s.get("outer_ptr_u32") or 0) == box_outer,
            BOX_READY_SECONDS, 12.0, "BOX_MESSAGE_INPUT_READY",
        )
        post["events"].append(self.pulse(HID_A, "BOX_MESSAGE_A"))

        # Record 0x669 if it appears, but ultimately require actual field state.
        after_box_deadline = time.monotonic() + 12.0
        saw_669 = False
        while time.monotonic() < after_box_deadline:
            cur = self.read_key()
            if int(cur.get("flow_u32") or 0) == POST_CAPTURE_FLOW_AFTER_BOX:
                saw_669 = True
            if (
                int(cur.get("battle_u32") or 0) == self.core.BATTLE_INACTIVE
                and int(cur.get("flow_u32") or 0) == 0x00000005
            ):
                break
            time.sleep(0.05)
        post["saw_flow_0x669"] = saw_669
        field = self.wait_stable(
            lambda s: int(s.get("battle_u32") or 0) == self.core.BATTLE_INACTIVE
            and int(s.get("flow_u32") or 0) == 0x00000005,
            FIELD_STABLE_SECONDS, 15.0, "OVERWORLD_STABLE",
        )
        post["overworld"] = field
        post["finished_t"] = self.rec.rel()
        post["result"] = "SUCCESS_TO_OVERWORLD"
        return post


def save(profile, report: dict, code: int) -> int:
    report["finished"] = now_iso()
    out_dir = profile.root / "support"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"capture_lifecycle_mapper_{stamp}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    transitions = report.get("timeline_transitions") or []
    lines = [
        "Pokebot3DS-CFW Capture Lifecycle Mapper v0p43AL",
        f"Result: {report.get('result')}",
        f"Throw: {(report.get('throw') or {}).get('final_selection')}",
        f"Samples: {len(report.get('timeline_samples') or [])}",
        f"Transitions: {len(transitions)}",
        f"Broad snapshots: {len(report.get('broad_snapshots') or [])}",
        f"RAM writes: False",
        f"Master Ball eligible: False",
        "",
        "Transition timeline:",
    ]
    for tr in transitions:
        lines.append(
            f"T+{tr.get('t')} battle={tr.get('battle')} flow={tr.get('flow')} "
            f"outer={tr.get('outer_ptr')} gate={tr.get('command_gate')} "
            f"state={tr.get('view_state')} mask={tr.get('view_mask')} "
            f"bag={tr.get('bag_state')} pk6={(tr.get('wild_pk6') or {}).get('identity')}"
        )
    lines += ["", f"JSON: {path}"]
    txt = path.with_suffix(".txt")
    txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nFull lifecycle report: {path}")
    print(f"Summary: {txt}")
    return code


def main() -> int:
    profile = get_profile_paths()
    settings = load_settings(profile.settings_path)
    host = settings["three_ds_ip"]
    timeout = min(float(settings.get("bridge_timeout_s", 1.5)), 1.5)
    _, core, _ = load_walk_v0p23()
    br = core.Bridge(host, timeout=timeout)

    report: dict = {
        "tool": "Pokebot3DS-CFW Capture Lifecycle Mapper v0p43AL",
        "started": now_iso(),
        "ram_writes": False,
        "live_hunt_modified": False,
        "master_ball_eligible": False,
        "sample_target_seconds": SAMPLE_TARGET_SECONDS,
        "broad_global_window": {"base": hx(BROAD_GLOBAL_BASE), "size": BROAD_GLOBAL_SIZE},
        "broad_owner_window": {"base": hx(BROAD_OWNER_BASE), "size": BROAD_OWNER_SIZE},
        "static_re_candidates": {
            "event_battle_return_vptr": hx(EVENT_BATTLE_RETURN_VPTR),
            "event_battle_return_state_offset": hx(EVENT_BATTLE_RETURN_STATE_OFF),
            "status": "STATIC_EVIDENCE_ONLY_NOT_PRODUCTION_AUTHORITY",
        },
        "notes": [
            "throws exactly one non-Master best Ball",
            "operator confirms a visually real breakout; stale returned gate alone is never accepted",
            "mapped post-capture states are driven with long readiness guards",
            "unknown intermediate screens can be advanced by one operator-requested instrumented A/B pulse",
            "50 ms target scalar timeline plus scheduled chunked 20 KiB battle-global and 4 KiB battle-owner snapshots (<=0x200 per bridge read)",
            "static EventBattleReturn vptr/state candidate is recorded but never trusted without hardware proof",
            "v0p43AM: successful hardware trace proved Pokédex B -> nickname and nickname B keeps flow 0x176B while outer_ptr changes to Box-message object",
        ],
    }

    main._last_report = report
    main._last_code = 2

    print("=" * 78)
    print("Pokebot3DS-CFW CAPTURE LIFECYCLE MAPPER v0p43AL")
    print("Alpha Sapphire 1.4 | NO RAM WRITES | MASTER BALL FORBIDDEN")
    print("=" * 78)
    print("Use an ordinary SINGLE wild battle and leave the game on FIGHT / BAG / POKEMON / RUN.")
    print("For the Pokédex branch, use a species that is not registered if practical.")
    print("The mapper throws ONE Ball. If it genuinely breaks out, rerun for a successful-catch trace.")
    print()

    gi = br.game_info()
    report["game_info"] = gi
    if int(gi.get("title_id", 0)) != AS_TITLE_ID:
        report["result"] = "REFUSED_WRONG_GAME"
        return save(profile, report, 2)
    caps = br.input_ping()
    report["input_caps"] = caps
    if not (caps.get("hid_pulse") and caps.get("touch_pulse")):
        report["result"] = "REFUSED_INPUT_CAPS"
        return save(profile, report, 2)
    br.release_all()

    ids = br.read(core.TRAINER_IDS_ADDR, 4)
    tid, sid = struct.unpack("<HH", ids)
    report["trainer_ids"] = {"tid": tid, "sid": sid}

    # Ground the actual opponent for the report.  Probe Ball policy deliberately
    # uses species=0/moves=[] below so Master Ball is impossible regardless of
    # the encountered species.
    raw_pk6 = br.read(core.WILD_PK6_ADDR, core.PK6_STORED_SIZE)
    actual_pk6 = decode_pk6(core, raw_pk6, tid, sid)
    report["initial_wild_pk6"] = actual_pk6
    if not actual_pk6.get("valid"):
        report["result"] = "REFUSED_INVALID_WILD_PK6"
        return save(profile, report, 2)

    gate = core.read_gate(br)
    report["initial_gate"] = gate
    if not gate.get("gate"):
        report["result"] = "REFUSED_COMMAND_MENU_NOT_READY"
        return save(profile, report, 2)

    recorder = Recorder(host, timeout, core, tid, sid)
    mapper = Mapper(br, core, recorder, report)
    recorder.start()
    code = 2
    try:
        report["timeline_anchor_before_throw"] = mapper.main_snapshot("BEFORE_THROW_PATH", position=True)

        # The probe never supplies actual legendary/self-KO eligibility to the
        # chooser.  species=0 means the production Master policy must forbid it.
        probe_target = {
            "species": 0,
            "species_name": "CAPTURE_LIFECYCLE_PROBE_MASTER_FORBIDDEN",
            "moves": [],
            "tid": tid,
            "sid": sid,
            "pid": actual_pk6.get("pid"),
            "pokemon_pid": actual_pk6.get("pid"),
        }
        throw = throw_one_best_ball(
            core,
            br,
            target=probe_target,
            throw_index=1,
            method_key="",
            environment="",
            check_stop=lambda: None,
            log=lambda m: print(m, flush=True),
        )
        report["throw"] = throw
        chosen = (throw.get("final_selection") or {}).get("item_id")
        if chosen == 1:
            raise RuntimeError("SAFETY: mapper selected Master Ball despite hard prohibition")
        report["timeline_anchor_after_throw"] = mapper.main_snapshot("AFTER_THROW_USE", position=False)
        mapper.broad_dump("POST_THROW_T_0.000")

        print("\nBall thrown. Recording complete RAM lifecycle...")
        started_outcome = time.monotonic()
        milestone_index = 1  # T+0.000 already captured above
        gate_stable_since = None
        gate_prompted = False
        unknown_prompted_at = 0.0
        known_first = None

        while time.monotonic() - started_outcome < POST_THROW_MAX_SECONDS:
            cur = mapper.read_key()
            elapsed_now = time.monotonic() - started_outcome
            while (
                milestone_index < len(BROAD_MILESTONES_SECONDS)
                and elapsed_now >= BROAD_MILESTONES_SECONDS[milestone_index]
            ):
                mark = BROAD_MILESTONES_SECONDS[milestone_index]
                mapper.broad_dump(f"POST_THROW_T_{mark:.3f}")
                milestone_index += 1
            battle = int(cur.get("battle_u32") or 0)
            flow = int(cur.get("flow_u32") or 0)
            outer = int(cur.get("outer_ptr_u32") or 0)

            if battle == POST_CAPTURE_SENTINEL or flow in {
                POST_CAPTURE_FLOW_POKEDEX,
                POST_CAPTURE_FLOW_NICKNAME_REGISTERED,
                POST_CAPTURE_FLOW_NICKNAME_UNREGISTERED,
                POST_CAPTURE_FLOW_AFTER_BOX,
            }:
                if not report.get("capture_authority_seen"):
                    report["capture_authority_seen"] = [mapper.main_snapshot("FIRST_CAPTURE_AUTHORITY")]
                    mapper.broad_dump("FIRST_CAPTURE_AUTHORITY_BROAD")
                # 0x669 can occur after Box; first useful driver state must be
                # Pokédex/nickname.  Keep recording if sentinel appears earlier.
                if flow in {
                    POST_CAPTURE_FLOW_POKEDEX,
                    POST_CAPTURE_FLOW_NICKNAME_REGISTERED,
                    POST_CAPTURE_FLOW_NICKNAME_UNREGISTERED,
                }:
                    known_first = mapper.main_snapshot("FIRST_MAPPED_POST_CAPTURE_SCREEN")
                    break

            if cur.get("command_gate"):
                if gate_stable_since is None:
                    gate_stable_since = time.monotonic()
                elif (
                    not gate_prompted
                    and time.monotonic() - gate_stable_since >= BREAKOUT_PROMPT_GATE_STABLE_SECONDS
                ):
                    gate_prompted = True
                    snap = mapper.main_snapshot("RETURNED_GATE_OPERATOR_CHECK", position=False)
                    report.setdefault("operator_checks", []).append(snap)
                    print("\nRAM has shown a returned command gate continuously for 3 seconds.")
                    print("Look at the 3DS. Is FIGHT / BAG / POKEMON / RUN visibly ready NOW?")
                    ans = input("Type Y for a real breakout, N to keep recording: ").strip().lower()
                    report.setdefault("operator_answers", []).append({"t": recorder.rel(), "question": "visible_real_breakout", "answer": ans})
                    if ans.startswith("y"):
                        report["result"] = "OPERATOR_CONFIRMED_REAL_BREAKOUT"
                        report["breakout"] = mapper.main_snapshot("REAL_BREAKOUT_CONFIRMED", position=False)
                        mapper.broad_dump("REAL_BREAKOUT_CONFIRMED_BROAD")
                        code = 0
                        return code
                    gate_stable_since = None
                    gate_prompted = False
            else:
                gate_stable_since = None
                gate_prompted = False

            elapsed = time.monotonic() - started_outcome
            # If an unrecognised interactive phase is blocking progress, let the
            # operator request exactly one instrumented pulse.  This is valuable
            # for EXP/level-up/move-learn screens whose RAM flow is not mapped yet.
            if elapsed >= UNKNOWN_OPERATOR_PROMPT_AFTER_SECONDS and elapsed - unknown_prompted_at >= 10.0:
                unknown_prompted_at = elapsed
                print("\nNo mapped Pokédex/nickname state yet.")
                print("If the 3DS is waiting for input on an EXP/level-up/other screen,")
                print("you can have the mapper send ONE recorded input now.")
                ans = input("Type A, B, or W to keep waiting: ").strip().lower()
                report.setdefault("operator_answers", []).append({"t": recorder.rel(), "question": "unknown_intermediate_input", "answer": ans})
                if ans == "a":
                    mapper.pulse(HID_A, "OPERATOR_UNKNOWN_STATE_A")
                elif ans == "b":
                    mapper.pulse(HID_B, "OPERATOR_UNKNOWN_STATE_B")
            time.sleep(0.05)

        if known_first is None:
            report["result"] = "TIMEOUT_NO_MAPPED_POST_CAPTURE_OR_CONFIRMED_BREAKOUT"
            raise RuntimeError(report["result"])

        print("\nSuccessful-capture post-capture branch found. Driving mapped sequence to overworld...")
        report["post_capture"] = mapper.drive_success_to_field(known_first)
        report["result"] = "SUCCESSFUL_CAPTURE_LIFECYCLE_TO_OVERWORLD"
        code = 0
        return code
    except Exception as exc:
        if report.get("result") not in {
            "OPERATOR_CONFIRMED_REAL_BREAKOUT",
            "SUCCESSFUL_CAPTURE_LIFECYCLE_TO_OVERWORLD",
        }:
            report["result"] = report.get("result") or "MAPPER_FAILED"
            report["error"] = f"{type(exc).__name__}: {exc}"
            print("\n" + report["error"])
        return code
    finally:
        # Give the sampler one final observation after the final input/state.
        try:
            time.sleep(0.20)
            report["timeline_final"] = mapper.main_snapshot("FINAL", position=True)
        except Exception as exc:
            report["timeline_final_error"] = f"{type(exc).__name__}: {exc}"
        recorder.stop()
        report["timeline_samples"] = recorder.samples
        report["timeline_transitions"] = recorder.transitions
        report["timeline_sample_count"] = len(recorder.samples)
        report["timeline_transition_count"] = len(recorder.transitions)
        # We cannot call save from inside finally if returning above unless we
        # restructure; stash for the wrapper below.
        main._last_report = report
        main._last_code = code


if __name__ == "__main__":
    # main() intentionally returns after the hardware run while its finally
    # stores the complete recorder output.  Save exactly once here.
    try:
        rc = main()
    except BaseException as exc:
        # Preflight exceptions before recorder construction still get a report
        # through a small fallback rather than losing evidence.
        print(f"Fatal mapper error: {type(exc).__name__}: {exc}")
        raise
    rep = getattr(main, "_last_report", None)
    if rep is None:
        raise SystemExit(rc)
    prof = get_profile_paths()
    raise SystemExit(save(prof, rep, getattr(main, "_last_code", rc)))
