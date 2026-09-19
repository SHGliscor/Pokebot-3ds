from __future__ import annotations

import argparse
import json
import socket
import struct
import time
import traceback
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Pokebot3DS-CFW — Causal Auto-Run Controller v0p14
# Alpha Sapphire 1.4 only.
#
# State machine:
#   overworld -> proven 1-tile movement -> battle -> menu gate
#   -> ONE authoritative PK6 read -> shiny/invalid HOLD
#   -> proven native Run touch -> verified overworld -> repeat
# ---------------------------------------------------------------------------

REQ_MAGIC = 0x5242524F
RESP_MAGIC = 0x5342524F
VERSION = 1
PORT = 4952

CMD_PING = 1
CMD_GAME_INFO = 2
CMD_READ = 4
CMD_INPUT_PING = 5
CMD_INPUT_PULSE = 6
CMD_INPUT_STATUS = 7
CMD_RELEASE_ALL = 8
CMD_INPUT_TOUCH_PULSE = 9

STATUS_OK = 0

INPUT_STATE_IDLE = 0
INPUT_STATE_ACCEPTED = 1
INPUT_STATE_IN_PROGRESS = 2
INPUT_STATE_COMPLETED = 3
INPUT_STATE_ALREADY_COMPLETED = 4
INPUT_STATE_ABORTED = 5
INPUT_STATE_NOT_FOUND = 6

INPUT_CAP_HID_PULSE = 1 << 0
INPUT_CAP_TOUCH_PULSE = 1 << 6

AS_TITLE_ID = 0x000400000011C500
BRIDGE_MAX_READ = 0x200

REQ = struct.Struct("<IHHIII")
RESP = struct.Struct("<IHHIIiI")
GAME_INFO = struct.Struct("<QI8sI")
INPUT_CAPS = struct.Struct("<IIIIII")
INPUT_STATUS = struct.Struct("<IIIII")

# Proven Alpha Sapphire 1.4 RAM.
BATTLE_ADDR = 0x081FB478
FLOW_ADDR = 0x081FB390
OUTER_PTR_ADDR = 0x081FB384
OUTER_TO_VIEW = 0x190
WILD_PK6_ADDR = 0x081FFA6C
TRAINER_IDS_ADDR = 0x08C81340
ZONE_ADDR = 0x08C6E884

BATTLE_INACTIVE = 0x00040000
BATTLE_ACTIVE = 0x00040001
BATTLE_TRANSITION = 0x00000000

HEAP_MIN = 0x08000000
HEAP_MAX = 0x10000000

# v0p8 hardware-derived candidate command-menu gate.
VIEW_STATE_OFF = 0x3C
VIEW_MASK_OFF = 0xA8
VIEW_READ_SIZE = 0xB0
READY_STATE = 0x00000002
READY_MASK = 0x00000100

GATE_CONFIRM_SAMPLES = 3
GATE_POLL_SEC = 0.08
MENU_WAIT_SEC = 20.0

# Proven exact one-tile movement profile.
# 3DS raw HID is active-low across the low 12 key bits.
HID_NEUTRAL = 0x00000FFF
HID_UP = HID_NEUTRAL & ~(1 << 6)     # 0xFBF
HID_DOWN = HID_NEUTRAL & ~(1 << 7)   # 0xF7F

MOVE_HOLD_MS = 160
MOVE_SETTLE_MS = 140
MOVE_POST_SETTLE_SEC = 0.70

# Movement command is NEVER retransmitted.
MOVE_STATUS_TIMEOUT_SEC = 2.5
MAX_MOVEMENT_PULSES_PER_ENCOUNTER = 240

# Hardware-proven native Run touch.
RUN_TOUCH_STATE = 0x01EA97FF
RUN_TOUCH_XY = (160, 220)
TOUCH_HOLD_MS = 90
TOUCH_SETTLE_MS = 60
RUN_TAPS = 3
INPUT_STATUS_POLL_SEC = 0.04
INPUT_STATUS_TIMEOUT_SEC = 2.5

BATTLE_SETTLE_TIMEOUT_SEC = 8.0
POST_ESCAPE_FIELD_SETTLE_SEC = 0.9

PK6_STORED_SIZE = 232
MAX_GEN6_SPECIES = 721

# Finite first automatic proof.
ENCOUNTERS = 5

# Gen6/7 block positioning used by the canonical PKM format.
# 32 shuffle values are listed because sv = (EC >> 13) & 31.
BLOCK_POSITION = (
    (0,1,2,3), (0,1,3,2), (0,2,1,3), (0,3,1,2),
    (0,2,3,1), (0,3,2,1), (1,0,2,3), (1,0,3,2),
    (2,0,1,3), (3,0,1,2), (2,0,3,1), (3,0,2,1),
    (1,2,0,3), (1,3,0,2), (2,1,0,3), (3,1,0,2),
    (2,3,0,1), (3,2,0,1), (1,2,3,0), (1,3,2,0),
    (2,1,3,0), (3,1,2,0), (2,3,1,0), (3,2,1,0),
    (0,1,2,3), (0,1,3,2), (0,2,1,3), (0,3,1,2),
    (0,2,3,1), (0,3,2,1), (1,0,2,3), (1,0,3,2),
)


class BridgeError(RuntimeError):
    pass


class SafetyHold(RuntimeError):
    pass


def hx(v: int | None):
    return None if v is None else f"0x{v:08X}"


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def valid_heap_ptr(v: int) -> bool:
    return HEAP_MIN <= v < HEAP_MAX and (v & 3) == 0


def u32_at(blob: bytes, off: int) -> int:
    return struct.unpack_from("<I", blob, off)[0]


def u16_at(blob: bytes, off: int) -> int:
    return struct.unpack_from("<H", blob, off)[0]


class Bridge:
    def __init__(self, host: str, timeout: float = 1.0):
        self.host = host
        self.timeout = timeout
        self._seq = (int(time.time() * 1000) & 0x7FFFFFFF) or 1

    def next_sequence(self) -> int:
        self._seq = (self._seq + 1) & 0x7FFFFFFF
        if self._seq == 0:
            self._seq = 1
        return self._seq

    def request(
        self,
        command: int,
        argument: int = 0,
        aux: int = 0,
        *,
        request_id: int | None = None,
        retries: int = 2,
    ) -> dict:
        last = None

        for attempt in range(retries):
            rid = request_id if request_id is not None else self.next_sequence()
            pkt = REQ.pack(
                REQ_MAGIC,
                VERSION,
                command,
                rid,
                argument & 0xFFFFFFFF,
                aux & 0xFFFFFFFF,
            )

            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.settimeout(self.timeout)
                    s.sendto(pkt, (self.host, PORT))
                    data, _ = s.recvfrom(4096)
            except socket.timeout as exc:
                last = exc
                if attempt + 1 < retries:
                    time.sleep(0.04)
                    continue
                raise BridgeError(
                    f"UDP timeout command={command} request_id={rid}"
                ) from exc

            if len(data) < RESP.size:
                raise BridgeError("short bridge response")

            magic, version, status, echoed_id, echoed_arg, result, payload_len = RESP.unpack_from(data)
            payload = data[RESP.size:]

            if magic != RESP_MAGIC:
                raise BridgeError("bad response magic")
            if version != VERSION:
                raise BridgeError("bad response version")
            if echoed_id != rid:
                raise BridgeError(
                    f"response sequence mismatch expected={rid} got={echoed_id}"
                )
            if len(payload) != payload_len:
                raise BridgeError("response payload length mismatch")

            return {
                "status": status,
                "result": result,
                "request_id": rid,
                "argument": echoed_arg,
                "payload": payload,
            }

        raise BridgeError(str(last) if last else "bridge request failed")

    def ping(self) -> str:
        r = self.request(CMD_PING)
        if r["status"] != STATUS_OK:
            raise BridgeError(f"PING status={r['status']} result={r['result']}")
        return r["payload"].decode("ascii", errors="replace")

    def game_info(self) -> dict:
        r = self.request(CMD_GAME_INFO)
        if r["status"] != STATUS_OK or len(r["payload"]) != GAME_INFO.size:
            raise BridgeError(
                f"GAME_INFO status={r['status']} result={r['result']}"
            )
        title, pid, name, flags = GAME_INFO.unpack(r["payload"])
        return {
            "title_id": title,
            "title_id_hex": f"0x{title:016X}",
            "pid": pid,
            "process": name.rstrip(b"\0").decode("ascii", errors="replace"),
            "flags": flags,
        }

    def input_ping(self) -> dict:
        r = self.request(CMD_INPUT_PING)
        if r["status"] != STATUS_OK or len(r["payload"]) != INPUT_CAPS.size:
            raise BridgeError(
                f"INPUT_PING status={r['status']} result={r['result']} "
                f"payload={len(r['payload'])}"
            )
        protocol, caps, runtime, neutral, max_hold, max_settle = INPUT_CAPS.unpack(r["payload"])
        return {
            "protocol_version": protocol,
            "capability_flags": caps,
            "capability_flags_hex": hx(caps),
            "runtime_flags": runtime,
            "runtime_flags_hex": hx(runtime),
            "neutral_hid": neutral,
            "neutral_hid_hex": hx(neutral),
            "max_hold_ms": max_hold,
            "max_settle_ms": max_settle,
            "hid_pulse": bool(caps & INPUT_CAP_HID_PULSE),
            "touch_pulse": bool(caps & INPUT_CAP_TOUCH_PULSE),
        }

    def read(self, address: int, length: int, retries: int = 2) -> bytes:
        if length <= 0 or length > BRIDGE_MAX_READ:
            raise BridgeError(
                f"read length 0x{length:X} exceeds bridge max 0x{BRIDGE_MAX_READ:X}"
            )
        r = self.request(CMD_READ, address, length, retries=retries)
        if r["status"] != STATUS_OK:
            raise BridgeError(
                f"READ 0x{address:08X}+0x{length:X}: "
                f"status={r['status']} result=0x{r['result'] & 0xFFFFFFFF:08X}"
            )
        if len(r["payload"]) != length:
            raise BridgeError(
                f"READ returned {len(r['payload'])} bytes, expected {length}"
            )
        return r["payload"]

    def read_span(self, address: int, length: int) -> bytes:
        parts = []
        done = 0
        while done < length:
            n = min(BRIDGE_MAX_READ, length - done)
            parts.append(self.read(address + done, n))
            done += n
        return b"".join(parts)

    def u32(self, address: int, retries: int = 2) -> int:
        return struct.unpack("<I", self.read(address, 4, retries=retries))[0]

    def release_all(self) -> dict:
        r = self.request(CMD_RELEASE_ALL, retries=2)
        return {
            "status": r["status"],
            "result": r["result"],
            "request_id": r["request_id"],
        }

    def decode_input_status(self, r: dict) -> dict:
        decoded = {
            "bridge_status": r["status"],
            "bridge_result": r["result"],
            "request_id": r["request_id"],
        }
        if len(r["payload"]) >= INPUT_STATUS.size:
            seq, state, raw_hid, remaining, runtime = INPUT_STATUS.unpack(
                r["payload"][:INPUT_STATUS.size]
            )
            decoded.update({
                "sequence_id": seq,
                "state": state,
                "raw_hid": hx(raw_hid),
                "remaining_ms": remaining,
                "runtime_flags": hx(runtime),
            })
        return decoded

    def wait_input_terminal(self, sequence_id: int, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        samples = []

        while time.monotonic() < deadline:
            r = self.request(
                CMD_INPUT_STATUS,
                sequence_id,
                0,
                retries=2,  # status query can retry; the input pulse itself never does.
            )
            s = self.decode_input_status(r)
            samples.append(s)

            if r["status"] != STATUS_OK:
                raise BridgeError(
                    f"INPUT_STATUS bridge status={r['status']} seq={sequence_id}"
                )

            state = s.get("state")
            if state in (INPUT_STATE_COMPLETED, INPUT_STATE_ALREADY_COMPLETED):
                return {
                    "completed": True,
                    "terminal_state": state,
                    "samples": samples,
                }
            if state in (INPUT_STATE_ABORTED, INPUT_STATE_NOT_FOUND):
                return {
                    "completed": False,
                    "terminal_state": state,
                    "samples": samples,
                }

            time.sleep(INPUT_STATUS_POLL_SEC)

        return {
            "completed": False,
            "terminal_state": None,
            "samples": samples,
            "timeout": True,
        }

    def hid_pulse_no_retransmit(
        self, raw_hid: int, hold_ms: int, settle_ms: int
    ) -> dict:
        seq = self.next_sequence()
        aux = (hold_ms & 0xFFFF) | ((settle_ms & 0xFFFF) << 16)

        # CRITICAL: retries=1. Movement pulse is never retransmitted.
        r = self.request(
            CMD_INPUT_PULSE,
            raw_hid,
            aux,
            request_id=seq,
            retries=1,
        )
        initial = self.decode_input_status(r)

        if r["status"] != STATUS_OK:
            raise BridgeError(
                f"HID pulse rejected status={r['status']} result={r['result']}"
            )

        terminal = self.wait_input_terminal(seq, MOVE_STATUS_TIMEOUT_SEC)
        return {
            "sequence_id": seq,
            "raw_hid": hx(raw_hid),
            "hold_ms": hold_ms,
            "settle_ms": settle_ms,
            "initial": initial,
            "terminal": terminal,
            "completed": terminal["completed"],
        }

    def touch_pulse_no_retransmit(
        self, touch_state: int, hold_ms: int, settle_ms: int
    ) -> dict:
        seq = self.next_sequence()
        aux = (hold_ms & 0xFFFF) | ((settle_ms & 0xFFFF) << 16)

        # CRITICAL: command 9 is sent exactly ONCE.
        #
        # A UDP response timeout is ambiguous: the gameplay pulse may have
        # reached firmware and only the acknowledgement may have been lost.
        # In that case we MUST NOT resend the touch. Instead, query command 7
        # (INPUT_STATUS) for the exact same firmware sequence ID.
        try:
            r = self.request(
                CMD_INPUT_TOUCH_PULSE,
                touch_state,
                aux,
                request_id=seq,
                retries=1,
            )
        except BridgeError as exc:
            expected = (
                f"UDP timeout command={CMD_INPUT_TOUCH_PULSE} "
                f"request_id={seq}"
            )
            if str(exc) != expected:
                raise

            # Status queries are observation-only and may retry. By the time
            # the 1-second UDP reply timeout has elapsed, an accepted 120/120
            # pulse should already be terminal. Firmware sequence state is the
            # authority; there is still NO command-9 retransmission here.
            terminal = self.wait_input_terminal(
                seq,
                INPUT_STATUS_TIMEOUT_SEC,
            )
            return {
                "sequence_id": seq,
                "touch_state": hx(touch_state),
                "hold_ms": hold_ms,
                "settle_ms": settle_ms,
                "initial": {
                    "response_timeout": True,
                    "error": str(exc),
                    "sequence_id": seq,
                    "retransmitted": False,
                },
                "terminal": terminal,
                "completed": terminal["completed"],
                "response_timeout_recovery": True,
                "status_recovered": terminal["completed"],
                "touch_retransmitted": False,
            }

        initial = self.decode_input_status(r)

        if r["status"] != STATUS_OK:
            raise BridgeError(
                f"touch pulse rejected status={r['status']} result={r['result']}"
            )

        terminal = self.wait_input_terminal(seq, INPUT_STATUS_TIMEOUT_SEC)
        return {
            "sequence_id": seq,
            "touch_state": hx(touch_state),
            "hold_ms": hold_ms,
            "settle_ms": settle_ms,
            "initial": initial,
            "terminal": terminal,
            "completed": terminal["completed"],
            "response_timeout_recovery": False,
            "status_recovered": False,
            "touch_retransmitted": False,
        }


# ---------------------------------------------------------------------------
# Gen6 PK6 decode / validation
# ---------------------------------------------------------------------------

def crypt_pk6_payload(payload: bytearray, seed: int) -> None:
    for off in range(0, len(payload), 2):
        seed = (0x41C64E6D * seed + 0x6073) & 0xFFFFFFFF
        word = struct.unpack_from("<H", payload, off)[0]
        word ^= (seed >> 16) & 0xFFFF
        struct.pack_into("<H", payload, off, word)


def unshuffle_pk6_payload(payload: bytearray, sv: int) -> bytearray:
    """
    Matches PKHeX PokeCrypto Shuffle67 behavior used after decryption.
    Each Gen6 stored block is 56 bytes; payload is four blocks = 224 bytes.
    """
    block_size = 56
    order = BLOCK_POSITION[sv & 31]

    data = bytearray(payload)
    perm = [0, 1, 2, 3]
    slot_of = [0, 1, 2, 3]

    for i in range(3):
        desired = order[i]
        j = slot_of[desired]
        if j == i:
            continue

        a = i * block_size
        b = j * block_size
        ba = data[a:a + block_size]
        bb = data[b:b + block_size]
        data[a:a + block_size] = bb
        data[b:b + block_size] = ba

        block_at_i = perm[i]
        perm[j] = block_at_i
        slot_of[block_at_i] = j

    return data


def checksum16(data: bytes) -> int:
    total = 0
    for off in range(0, len(data), 2):
        total = (total + struct.unpack_from("<H", data, off)[0]) & 0xFFFF
    return total


def decode_stored_pk6(raw: bytes, save_tid: int, save_sid: int) -> dict:
    if len(raw) != PK6_STORED_SIZE:
        return {
            "valid": False,
            "reason": f"wrong length {len(raw)}",
        }

    ec = u32_at(raw, 0x00)
    sanity = u16_at(raw, 0x04)
    stored_checksum = u16_at(raw, 0x06)

    encrypted_payload = bytearray(raw[8:232])
    crypt_pk6_payload(encrypted_payload, ec)

    calculated_checksum = checksum16(encrypted_payload)

    sv = (ec >> 13) & 31
    canonical_payload = unshuffle_pk6_payload(encrypted_payload, sv)
    dec = bytearray(raw[:8]) + canonical_payload

    species = u16_at(dec, 0x08)
    tid = u16_at(dec, 0x0C)
    sid = u16_at(dec, 0x0E)
    pid = u32_at(dec, 0x18)
    nature = dec[0x1C]
    ability = dec[0x14]
    # Telemetry-only Gen VI gender decode from the already-decrypted PK6.
    # This adds no RAM read and does not alter movement/battle authority.
    gender_form = dec[0x1D]
    gender_code = (gender_form >> 1) & 0x03
    gender = {0: "♂", 1: "♀", 2: "—"}.get(gender_code, "—")
    iv32 = u32_at(dec, 0x74)

    ivs = {
        "hp": (iv32 >> 0) & 0x1F,
        "attack": (iv32 >> 5) & 0x1F,
        "defense": (iv32 >> 10) & 0x1F,
        "speed": (iv32 >> 15) & 0x1F,
        "sp_attack": (iv32 >> 20) & 0x1F,
        "sp_defense": (iv32 >> 25) & 0x1F,
    }

    shiny_xor = (
        tid
        ^ sid
        ^ (pid & 0xFFFF)
        ^ ((pid >> 16) & 0xFFFF)
    ) & 0xFFFF

    checksum_ok = calculated_checksum == stored_checksum
    sanity_ok = sanity == 0
    species_ok = 1 <= species <= MAX_GEN6_SPECIES
    trainer_ok = tid == save_tid and sid == save_sid
    shiny = shiny_xor < 16

    reasons = []
    if not sanity_ok:
        reasons.append(f"sanity={sanity:#06x}")
    if not checksum_ok:
        reasons.append(
            f"checksum stored={stored_checksum:#06x} calc={calculated_checksum:#06x}"
        )
    if not species_ok:
        reasons.append(f"species={species}")
    if not trainer_ok:
        reasons.append(
            f"TID/SID {tid}/{sid} != save {save_tid}/{save_sid}"
        )

    valid = sanity_ok and checksum_ok and species_ok and trainer_ok

    return {
        "valid": valid,
        "reason": "valid stored PK6" if valid else "; ".join(reasons),
        "ec": hx(ec),
        "sanity": f"0x{sanity:04X}",
        "checksum": f"0x{stored_checksum:04X}",
        "calculated_checksum": f"0x{calculated_checksum:04X}",
        "checksum_valid": checksum_ok,
        "shuffle_value": sv,
        "species": species,
        "tid": tid,
        "sid": sid,
        "pid": hx(pid),
        "nature_id": nature,
        "ability_id": ability,
        "gender": gender,
        "ivs": ivs,
        "iv_sum": sum(ivs.values()),
        "shiny_xor": shiny_xor,
        "is_shiny": shiny,
        "identity": f"{ec:08X}:{pid:08X}:{species}",
    }


# ---------------------------------------------------------------------------
# Battle/menu state
# ---------------------------------------------------------------------------

def read_gate(br: Bridge) -> dict:
    battle = br.u32(BATTLE_ADDR)

    if battle != BATTLE_ACTIVE:
        return {
            "time": now_iso(),
            "battle": hx(battle),
            "outer": None,
            "view": None,
            "state": None,
            "mask": None,
            "gate": False,
            "valid_view": False,
        }

    outer = br.u32(OUTER_PTR_ADDR)
    if not valid_heap_ptr(outer):
        return {
            "time": now_iso(),
            "battle": hx(battle),
            "outer": hx(outer),
            "view": None,
            "state": None,
            "mask": None,
            "gate": False,
            "valid_view": False,
        }

    view = br.u32(outer + OUTER_TO_VIEW)
    if not valid_heap_ptr(view):
        return {
            "time": now_iso(),
            "battle": hx(battle),
            "outer": hx(outer),
            "view": hx(view),
            "state": None,
            "mask": None,
            "gate": False,
            "valid_view": False,
        }

    blob = br.read(view, VIEW_READ_SIZE)
    state = u32_at(blob, VIEW_STATE_OFF)
    mask = u32_at(blob, VIEW_MASK_OFF)

    return {
        "time": now_iso(),
        "battle": hx(battle),
        "outer": hx(outer),
        "view": hx(view),
        "state": hx(state),
        "mask": hx(mask),
        "gate": state == READY_STATE and mask == READY_MASK,
        "valid_view": True,
    }


def wait_for_gate(br: Bridge, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    samples = []
    consecutive = 0
    first_false = None
    first_true = None

    while time.monotonic() < deadline:
        s = read_gate(br)
        samples.append(s)

        if s["battle"] != hx(BATTLE_ACTIVE):
            return {
                "status": "BATTLE_ENDED_BEFORE_GATE",
                "samples": samples,
                "first_false": first_false,
                "first_true": first_true,
            }

        if s["valid_view"]:
            if not s["gate"]:
                consecutive = 0
                if first_false is None:
                    first_false = s
            else:
                if first_true is None:
                    first_true = s
                consecutive += 1

                if consecutive >= GATE_CONFIRM_SAMPLES:
                    return {
                        "status": "GATE_STABLE_TRUE",
                        "samples": samples,
                        "first_false": first_false,
                        "first_true": first_true,
                        "confirmed_samples": samples[-GATE_CONFIRM_SAMPLES:],
                    }

        time.sleep(GATE_POLL_SEC)

    return {
        "status": "GATE_TIMEOUT",
        "samples": samples,
        "first_false": first_false,
        "first_true": first_true,
    }


def wait_for_battle_active_after_transition(br: Bridge, timeout: float = 7.0) -> dict:
    deadline = time.monotonic() + timeout
    states = []

    while time.monotonic() < deadline:
        battle = br.u32(BATTLE_ADDR)
        states.append({"time": now_iso(), "battle": hx(battle)})

        if battle == BATTLE_ACTIVE:
            return {"active": True, "states": states}
        if battle == BATTLE_INACTIVE:
            return {"active": False, "states": states, "returned_inactive": True}

        time.sleep(0.08)

    return {"active": False, "states": states, "timeout": True}


def verify_escape(br: Bridge, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    states = []

    while time.monotonic() < deadline:
        battle = br.u32(BATTLE_ADDR)
        states.append({"time": now_iso(), "battle": hx(battle)})

        if battle == BATTLE_INACTIVE:
            return {"escaped": True, "states": states}

        time.sleep(0.10)

    return {"escaped": False, "states": states}


# ---------------------------------------------------------------------------
# Fully automated movement -> encounter
# ---------------------------------------------------------------------------

def move_until_encounter(br: Bridge, start_direction: str) -> dict:
    direction = start_direction
    pulses = []
    battle_log = []

    for index in range(1, MAX_MOVEMENT_PULSES_PER_ENCOUNTER + 1):
        battle_before = br.u32(BATTLE_ADDR)

        if battle_before == BATTLE_ACTIVE:
            return {
                "encounter": True,
                "next_direction": direction,
                "pulses": pulses,
                "battle_log": battle_log,
                "battle_was_already_active": True,
            }

        if battle_before == BATTLE_TRANSITION:
            trans = wait_for_battle_active_after_transition(br)
            battle_log.extend(trans["states"])
            if trans["active"]:
                return {
                    "encounter": True,
                    "next_direction": direction,
                    "pulses": pulses,
                    "battle_log": battle_log,
                    "transition_detected": True,
                }
            if trans.get("timeout"):
                raise SafetyHold("battle transition did not resolve")

        if battle_before != BATTLE_INACTIVE:
            raise SafetyHold(f"unexpected battle state before movement: {hx(battle_before)}")

        raw = HID_UP if direction == "UP" else HID_DOWN
        rec = br.hid_pulse_no_retransmit(
            raw,
            MOVE_HOLD_MS,
            MOVE_SETTLE_MS,
        )
        rec["index"] = index
        rec["direction"] = direction
        rec["time"] = now_iso()
        pulses.append(rec)

        if not rec["completed"]:
            raise SafetyHold(
                f"movement pulse {index} {direction} did not complete"
            )

        # Toggle immediately. After a successful Up, next step returns Down.
        direction = "DOWN" if direction == "UP" else "UP"

        # Exact proven post-settle delay for the one-tile profile.
        time.sleep(MOVE_POST_SETTLE_SEC)

        battle_after = br.u32(BATTLE_ADDR)
        battle_log.append({
            "time": now_iso(),
            "after_pulse": index,
            "battle": hx(battle_after),
        })

        if battle_after == BATTLE_ACTIVE:
            return {
                "encounter": True,
                "next_direction": direction,
                "pulses": pulses,
                "battle_log": battle_log,
            }

        if battle_after == BATTLE_TRANSITION:
            trans = wait_for_battle_active_after_transition(br)
            battle_log.extend(trans["states"])
            if trans["active"]:
                return {
                    "encounter": True,
                    "next_direction": direction,
                    "pulses": pulses,
                    "battle_log": battle_log,
                    "transition_detected": True,
                }
            if trans.get("timeout"):
                raise SafetyHold("battle transition did not resolve after movement")

        if battle_after != BATTLE_INACTIVE:
            raise SafetyHold(f"unexpected battle state after movement: {hx(battle_after)}")

    return {
        "encounter": False,
        "next_direction": direction,
        "pulses": pulses,
        "battle_log": battle_log,
        "reason": "movement pulse limit reached",
    }


def run_touch_sequence(br: Bridge) -> list[dict]:
    taps = []

    for i in range(1, RUN_TAPS + 1):
        rec = br.touch_pulse_no_retransmit(
            RUN_TOUCH_STATE,
            TOUCH_HOLD_MS,
            TOUCH_SETTLE_MS,
        )
        rec["tap_index"] = i
        rec["time"] = now_iso()
        taps.append(rec)

        if not rec["completed"]:
            break

        # The pulse completion already includes settle; a tiny gap avoids overlap.
        if i < RUN_TAPS:
            time.sleep(0.06)

    return taps


def save_report(report: dict, suffix: str = "") -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    extra = f"_{suffix}" if suffix else ""
    out = Path(f"CausalAutoRun_v0p14_{stamp}{extra}.json")
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return out


# Causal Run controller: no final RAM readiness gate.
CAUSAL_ENCOUNTERS = 3
MAX_RUN_TAPS_PER_ENCOUNTER = 24
POST_TAP_OBSERVE_SEC = 0.30
POST_TAP_BATTLE_POLL_SEC = 0.035
FIELD_STABLE_SAMPLES = 2
FIELD_STABLE_POLL_SEC = 0.06
FIELD_RETURN_TIMEOUT_SEC = 10.0

# Exact manually-settled battle-menu profile, derived from the hardware
# v0p4b menu snapshot and cross-checked against v0p12 late state.
SETTLED_PROFILE_READ_SIZE = 0xD8
SETTLED_PROFILE_POLL_SEC = 0.04
SETTLED_PROFILE_TIMEOUT_SEC = 20.0

DIAG_VIEW_SIZE = 0x400
DIAG_OUTER_SIZE = 0x400
EARLY_EXIT_CHECK_SEC = 1.20
LATE_DIAGNOSTIC_DELAY_SEC = 2.00
DIAG_SAMPLE_OFFSETS_SEC = (0.00, 0.25, 0.50, 1.00, 2.00)


def read_menu_profile(br: Bridge) -> dict:
    battle = br.u32(BATTLE_ADDR)

    rec = {
        "time": now_iso(),
        "battle": hx(battle),
        "outer": None,
        "view": None,
        "state_3C": None,
        "profile_match": False,
        "checks": {},
    }

    if battle != BATTLE_ACTIVE:
        return rec

    outer = br.u32(OUTER_PTR_ADDR)
    rec["outer"] = hx(outer)
    if not valid_heap_ptr(outer):
        return rec

    view = br.u32(outer + OUTER_TO_VIEW)
    rec["view"] = hx(view)
    if not valid_heap_ptr(view):
        return rec

    blob = br.read(view, SETTLED_PROFILE_READ_SIZE)
    u = lambda off: u32_at(blob, off)

    checks = {
        "state_3C_eq_2": u(0x3C) == 2,
        "v9C_eq_0x80": u(0x9C) == 0x80,
        "vA0_eq_0x40": u(0xA0) == 0x40,
        "vA4_eq_outer_plus_0x30": u(0xA4) == ((outer + 0x30) & 0xFFFFFFFF),
        "vA8_eq_0x100": u(0xA8) == 0x100,
        "vAC_eq_view_plus_0x10C": u(0xAC) == ((view + 0x10C) & 0xFFFFFFFF),
        "vB0_eq_view_plus_0x9C": u(0xB0) == ((view + 0x9C) & 0xFFFFFFFF),
        "vB4_eq_view_plus_0xB0": u(0xB4) == ((view + 0xB0) & 0xFFFFFFFF),
        "vB8_eq_view_plus_0xB0": u(0xB8) == ((view + 0xB0) & 0xFFFFFFFF),
        "vD4_eq_0": u(0xD4) == 0,
    }

    # 2026-08-29 Dialga hardware proof showed vA8/vB4/vB8/vD4 can
    # legitimately differ at the exact state=2 PK6-ready boundary. Keep every
    # field in diagnostics, but do not let those volatile values veto authority.
    authority_keys = (
        "state_3C_eq_2",
        "v9C_eq_0x80",
        "vA0_eq_0x40",
        "vA4_eq_outer_plus_0x30",
    )
    authority_checks = {key: checks[key] for key in authority_keys}
    rec.update({
        "state_3C": hx(u(0x3C)),
        "v9C": hx(u(0x9C)),
        "vA0": hx(u(0xA0)),
        "vA4": hx(u(0xA4)),
        "vA8": hx(u(0xA8)),
        "vAC": hx(u(0xAC)),
        "vB0": hx(u(0xB0)),
        "vB4": hx(u(0xB4)),
        "vB8": hx(u(0xB8)),
        "vD0": hx(u(0xD0)),
        "vD4": hx(u(0xD4)),
        "checks": checks,
        "authority_checks": authority_checks,
        "profile_policy": "state_3C=2 + stable action-select owner/view; vA8/vB4/vB8/vD4 diagnostic-only",
        "legacy_exact_profile_match": all(checks.values()),
        "profile_match": all(authority_checks.values()),
    })
    return rec


def wait_for_state2_for_pk6(br: Bridge, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    samples = []
    while time.monotonic() < deadline:
        s = read_menu_profile(br)
        samples.append(s)
        if s["battle"] != hx(BATTLE_ACTIVE):
            return {"ready_for_pk6": False, "status": "BATTLE_ENDED", "samples": samples}
        if s.get("state_3C") == "0x00000002":
            return {"ready_for_pk6": True, "status": "STATE2_SEEN", "samples": samples}
        time.sleep(SETTLED_PROFILE_POLL_SEC)
    return {"ready_for_pk6": False, "status": "STATE2_TIMEOUT", "samples": samples}


def wait_for_exact_settled_profile(br: Bridge, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    samples = []
    while time.monotonic() < deadline:
        s = read_menu_profile(br)
        samples.append(s)

        if s["battle"] != hx(BATTLE_ACTIVE):
            return {
                "matched": False,
                "status": "BATTLE_ENDED_BEFORE_PROFILE",
                "samples": samples,
            }

        if s["profile_match"]:
            return {
                "matched": True,
                "status": "EXACT_SETTLED_PROFILE_MATCH",
                "match": s,
                "samples": samples,
            }

        time.sleep(SETTLED_PROFILE_POLL_SEC)

    return {
        "matched": False,
        "status": "EXACT_SETTLED_PROFILE_TIMEOUT",
        "samples": samples,
    }


def capture_diag_snapshot(br: Bridge, label: str) -> dict:
    battle = br.u32(BATTLE_ADDR)
    flow = br.u32(FLOW_ADDR)
    outer = br.u32(OUTER_PTR_ADDR)

    rec = {
        "label": label,
        "time": now_iso(),
        "battle": hx(battle),
        "flow": hx(flow),
        "outer": hx(outer),
        "view": None,
        "gate": None,
        "state": None,
        "mask": None,
        "outer_0x400_hex": None,
        "view_0x400_hex": None,
    }

    if not valid_heap_ptr(outer):
        rec["capture_error"] = f"invalid outer {hx(outer)}"
        return rec

    try:
        view = br.u32(outer + OUTER_TO_VIEW)
    except Exception as exc:
        rec["capture_error"] = f"view pointer read: {type(exc).__name__}: {exc}"
        return rec

    rec["view"] = hx(view)

    if not valid_heap_ptr(view):
        rec["capture_error"] = f"invalid view {hx(view)}"
        return rec

    try:
        outer_blob = br.read_span(outer, DIAG_OUTER_SIZE)
        view_blob = br.read_span(view, DIAG_VIEW_SIZE)
    except Exception as exc:
        rec["capture_error"] = f"snapshot read: {type(exc).__name__}: {exc}"
        return rec

    state = u32_at(view_blob, VIEW_STATE_OFF)
    mask = u32_at(view_blob, VIEW_MASK_OFF)

    rec.update({
        "state": hx(state),
        "mask": hx(mask),
        "gate": state == READY_STATE and mask == READY_MASK,
        "outer_0x400_hex": outer_blob.hex(),
        "view_0x400_hex": view_blob.hex(),
    })
    return rec


def diff_snapshot_pair(a: dict, b: dict) -> dict:
    result = {
        "from": a.get("label"),
        "to": b.get("label"),
        "same_outer": a.get("outer") == b.get("outer"),
        "same_view": a.get("view") == b.get("view"),
        "regions": {},
    }

    for region_name, key, base_key in (
        ("outer", "outer_0x400_hex", "outer"),
        ("view", "view_0x400_hex", "view"),
    ):
        ah = a.get(key)
        bh = b.get(key)
        if not ah or not bh:
            result["regions"][region_name] = {"available": False}
            continue

        if a.get(base_key) != b.get(base_key):
            result["regions"][region_name] = {
                "available": False,
                "reason": "base address changed",
            }
            continue

        abase = int(a[base_key], 16)
        ab = bytes.fromhex(ah)
        bb = bytes.fromhex(bh)
        n = min(len(ab), len(bb))

        byte_changes = []
        for off in range(n):
            av, bv = ab[off], bb[off]
            if av != bv:
                byte_changes.append({
                    "address": hx(abase + off),
                    "offset": f"0x{off:X}",
                    "from": av,
                    "to": bv,
                    "small_state_like": max(av, bv) <= 0x20,
                })

        dword_changes = []
        for off in range(0, n - 3, 4):
            av = u32_at(ab, off)
            bv = u32_at(bb, off)
            if av != bv:
                dword_changes.append({
                    "address": hx(abase + off),
                    "offset": f"0x{off:X}",
                    "from": hx(av),
                    "to": hx(bv),
                    "small_state_like": max(av, bv) <= 0x1000,
                    "pointerish": (
                        (0x00100000 <= av < 0x10000000)
                        or (0x00100000 <= bv < 0x10000000)
                    ),
                })

        dword_changes.sort(
            key=lambda x: (
                not x["small_state_like"],
                x["pointerish"],
                int(x["address"], 16),
            )
        )
        byte_changes.sort(
            key=lambda x: (
                not x["small_state_like"],
                int(x["address"], 16),
            )
        )

        result["regions"][region_name] = {
            "available": True,
            "byte_change_count": len(byte_changes),
            "dword_change_count": len(dword_changes),
            "top_byte_changes": byte_changes[:100],
            "top_dword_changes": dword_changes[:100],
        }

    return result


def verify_escape_short(br: Bridge, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    states = []
    while time.monotonic() < deadline:
        battle = br.u32(BATTLE_ADDR)
        states.append({"time": now_iso(), "battle": hx(battle)})
        if battle == BATTLE_INACTIVE:
            return {"escaped": True, "states": states}
        time.sleep(0.08)
    return {"escaped": False, "states": states}


def bounded_late_snapshots(br: Bridge, origin_monotonic: float) -> list[dict]:
    snaps = []
    for offset in DIAG_SAMPLE_OFFSETS_SEC:
        target = origin_monotonic + offset
        remaining = target - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        # If battle already ended there is no need for further diagnostics.
        if br.u32(BATTLE_ADDR) == BATTLE_INACTIVE:
            snaps.append({
                "label": f"late_plus_{offset:.2f}s",
                "time": now_iso(),
                "battle": hx(BATTLE_INACTIVE),
                "note": "battle already inactive",
            })
            break
        snaps.append(capture_diag_snapshot(br, f"late_plus_{offset:.2f}s"))
    return snaps



def one_run_touch(br: Bridge) -> dict:
    rec = br.touch_pulse_no_retransmit(
        RUN_TOUCH_STATE,
        TOUCH_HOLD_MS,
        TOUCH_SETTLE_MS,
    )
    rec["time"] = now_iso()
    return rec


def observe_after_touch(br: Bridge, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    states = []
    while time.monotonic() < deadline:
        b = br.u32(BATTLE_ADDR)
        states.append({"time": now_iso(), "battle": hx(b)})

        # Any departure from ACTIVE proves the game accepted an action which is
        # taking us out of the current command state. For Run, continue through
        # to verified field inactive before declaring success.
        if b != BATTLE_ACTIVE:
            return {
                "left_active": True,
                "first_nonactive": hx(b),
                "states": states,
            }

        time.sleep(POST_TAP_BATTLE_POLL_SEC)

    return {
        "left_active": False,
        "first_nonactive": None,
        "states": states,
    }


def wait_field_stable(br: Bridge, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    consecutive = 0
    samples = []

    while time.monotonic() < deadline:
        b = br.u32(BATTLE_ADDR)
        samples.append({"time": now_iso(), "battle": hx(b)})

        if b == BATTLE_INACTIVE:
            consecutive += 1
            if consecutive >= FIELD_STABLE_SAMPLES:
                return {
                    "stable": True,
                    "samples": samples,
                    "consecutive_inactive": consecutive,
                }
        else:
            consecutive = 0

        time.sleep(FIELD_STABLE_POLL_SEC)

    return {
        "stable": False,
        "samples": samples,
        "consecutive_inactive": consecutive,
    }


def causal_run_until_field(br: Bridge) -> dict:
    attempts = []

    for attempt in range(1, MAX_RUN_TAPS_PER_ENCOUNTER + 1):
        before = br.u32(BATTLE_ADDR)
        if before != BATTLE_ACTIVE:
            field = wait_field_stable(br, FIELD_RETURN_TIMEOUT_SEC)
            return {
                "success": field["stable"],
                "attempts": attempts,
                "field": field,
                "note": "battle had already left active before next touch",
            }

        touch = one_run_touch(br)
        if not touch["completed"]:
            return {
                "success": False,
                "attempts": attempts + [{
                    "attempt": attempt,
                    "before_battle": hx(before),
                    "touch": touch,
                    "status": "TOUCH_NOT_COMPLETED",
                }],
                "reason": "touch pulse did not complete",
            }

        observed = observe_after_touch(br, POST_TAP_OBSERVE_SEC)
        item = {
            "attempt": attempt,
            "before_battle": hx(before),
            "touch": touch,
            "observe": observed,
        }
        attempts.append(item)

        if observed["left_active"]:
            field = wait_field_stable(br, FIELD_RETURN_TIMEOUT_SEC)
            return {
                "success": field["stable"],
                "accepted_attempt": attempt,
                "attempts": attempts,
                "field": field,
                "first_nonactive": observed["first_nonactive"],
            }

        # No guessed readiness sleep here. The next attempt starts immediately
        # after the bounded outcome observation. The touch pulse itself already includes the bounded press/settle profile.
        # HF97 shortens that profile, while each retry still re-reads battle RAM.

    return {
        "success": False,
        "attempts": attempts,
        "reason": f"battle stayed active through {MAX_RUN_TAPS_PER_ENCOUNTER} causal Run taps",
    }



def main():
    ap = argparse.ArgumentParser(
        description="Pokebot3DS-CFW causal automatic wild Run controller v0p14"
    )
    ap.add_argument("host", nargs="?", default="192.168.0.28")
    args = ap.parse_args()

    br = Bridge(args.host)

    print("Pokebot3DS-CFW — Causal Auto-Run Controller v0p14")
    print()
    print("THREE fully automatic wild encounters.")
    print("There is NO final RAM menu-readiness gate.")
    print()
    print("START POSITION:")
    print("  - Alpha Sapphire 1.4")
    print("  - overworld, NOT in battle")
    print("  - stand on a grass tile")
    print("  - tile directly ABOVE must also be grass")
    print("  - do not touch the 3DS after starting")
    print()
    print("After one valid non-shiny PK6 read, the bot repeatedly taps RUN")
    print("and uses the battle state itself as the authority.")
    print()

    report = {
        "tool": "Pokebot3DS-CFW Causal Auto-Run Controller v0p14",
        "started": datetime.now().astimezone().isoformat(timespec="seconds"),
        "finished": None,
        "host": args.host,
        "finite_encounters": CAUSAL_ENCOUNTERS,
        "strategy": {
            "final_ram_readiness_gate": False,
            "authority": "causal native Run outcome: battle ACTIVE must leave ACTIVE and settle INACTIVE",
            "max_run_taps_per_encounter": MAX_RUN_TAPS_PER_ENCOUNTER,
            "post_touch_observe_seconds": POST_TAP_OBSERVE_SEC,
            "extra_readiness_delay_seconds": 0,
        },
        "movement": {
            "profile": "proven one-tile alternating Up/Down",
            "hold_ms": MOVE_HOLD_MS,
            "settle_ms": MOVE_SETTLE_MS,
            "post_settle_seconds": MOVE_POST_SETTLE_SEC,
            "retransmit": False,
        },
        "pk6_safety": {
            "boundary": "first valid live battle view with state+0x3C == 2",
            "reads_per_encounter": 1,
            "invalid_or_shiny": "ABSOLUTE HOLD; NO RUN TOUCH",
        },
        "run_touch": {
            "command": CMD_INPUT_TOUCH_PULSE,
            "screen_xy": list(RUN_TOUCH_XY),
            "encoded": hx(RUN_TOUCH_STATE),
            "hold_ms": TOUCH_HOLD_MS,
            "settle_ms": TOUCH_SETTLE_MS,
            "taps_per_probe": 1,
            "retransmit": False,
        },
        "encounters": [],
    }

    try:
        print("PING:", br.ping())
        gi = br.game_info()
        report["game_info"] = gi
        print("GAME:", gi["title_id_hex"], "PID", gi["pid"], gi["process"])

        if gi["title_id"] != AS_TITLE_ID:
            raise SafetyHold("wrong title; Alpha Sapphire only")

        caps = br.input_ping()
        report["input_capabilities"] = caps
        if not caps["hid_pulse"]:
            raise SafetyHold("CFW bridge lacks acknowledged HID pulse")
        if not caps["touch_pulse"]:
            raise SafetyHold("CFW bridge lacks native touch pulse")
        if caps["neutral_hid"] != HID_NEUTRAL:
            raise SafetyHold(f"unexpected neutral HID {caps['neutral_hid_hex']}")

        report["preflight_release_all"] = br.release_all()

        if br.u32(BATTLE_ADDR) != BATTLE_INACTIVE:
            raise SafetyHold("start in overworld, not battle")

        ids = br.read(TRAINER_IDS_ADDR, 4)
        save_tid, save_sid = struct.unpack("<HH", ids)
        report["trainer_ids"] = {"tid": save_tid, "sid": save_sid}
        report["start_zone"] = br.u32(ZONE_ADDR)

        previous_identity = None
        direction = "UP"

        for enc_index in range(1, CAUSAL_ENCOUNTERS + 1):
            print()
            print("=" * 78)
            print(f"ENCOUNTER {enc_index}/{CAUSAL_ENCOUNTERS}")
            print("=" * 78)
            print("Moving automatically...")

            enc = {
                "encounter": enc_index,
                "started": now_iso(),
            }

            movement = move_until_encounter(br, direction)
            enc["movement"] = movement
            direction = movement["next_direction"]

            if not movement["encounter"]:
                report["encounters"].append(enc)
                raise SafetyHold(
                    f"no encounter after {len(movement['pulses'])} movement pulses"
                )

            print(f"Battle detected after {len(movement['pulses'])} movement pulse(s).")
            print("Waiting for the established state=2 PK6 safety boundary...")

            boundary = wait_for_state2_for_pk6(br)
            enc["pk6_boundary"] = boundary

            if not boundary["ready_for_pk6"]:
                report["encounters"].append(enc)
                raise SafetyHold(f"PK6 boundary failed: {boundary['status']}")

            # Exactly ONE authoritative Pokémon read.
            raw_pk6 = br.read(WILD_PK6_ADDR, PK6_STORED_SIZE)
            pk6 = decode_stored_pk6(raw_pk6, save_tid, save_sid)
            enc["pk6"] = pk6

            if not pk6["valid"]:
                report["encounters"].append(enc)
                raise SafetyHold(f"invalid wild PK6: {pk6['reason']}")

            if previous_identity is not None and pk6["identity"] == previous_identity:
                report["encounters"].append(enc)
                raise SafetyHold("duplicate/stale wild PK6 identity")

            previous_identity = pk6["identity"]

            print(
                f"PK6: species #{pk6['species']} PID {pk6['pid']} "
                f"XOR {pk6['shiny_xor']} shiny={pk6['is_shiny']}"
            )

            if pk6["is_shiny"]:
                report["encounters"].append(enc)
                raise SafetyHold(
                    f"SHINY FOUND species #{pk6['species']} PID {pk6['pid']}"
                )

            print("Valid non-shiny. Starting causal Run taps...")
            causal = causal_run_until_field(br)
            enc["causal_run"] = causal

            if not causal["success"]:
                report["encounters"].append(enc)
                raise SafetyHold(
                    "causal Run failed: " + causal.get("reason", "field did not become stable")
                )

            accepted = causal.get("accepted_attempt")
            enc["result"] = {
                "status": "PASS",
                "pass": True,
                "accepted_run_attempt": accepted,
            }
            enc["finished"] = now_iso()
            report["encounters"].append(enc)

            print(
                f"PASS {enc_index}/{CAUSAL_ENCOUNTERS}: "
                f"Run accepted on causal tap {accepted} -> overworld"
            )

            # Field is already proven inactive for three consecutive samples.
            time.sleep(0.45)

        report["result"] = {
            "status": "PASS_CAUSAL_AUTORUN_3_OF_3",
            "pass": True,
            "detail": (
                "Three fresh wild encounters completed with automatic movement, "
                "one authoritative valid non-shiny PK6 read each, repeated native "
                "Run taps without a final RAM readiness gate, and causal verified "
                "return to stable battle-inactive overworld."
            ),
        }

        br.release_all()
        report["finished"] = datetime.now().astimezone().isoformat(timespec="seconds")
        out = save_report(report, "PASS")

        print()
        print("=" * 80)
        print("PASS_CAUSAL_AUTORUN_3_OF_3")
        print("No battle-menu readiness RAM gate was needed.")
        print("Saved:", out.resolve())
        print("=" * 80)
        return 0

    except KeyboardInterrupt:
        try:
            rel = br.release_all()
        except Exception as release_exc:
            rel = {"error": f"{type(release_exc).__name__}: {release_exc}"}
        report["emergency_release"] = rel
        report["result"] = {
            "status": "MANUAL_STOP",
            "pass": False,
            "detail": "Ctrl+C received; RELEASE_ALL sent.",
        }
        report["finished"] = datetime.now().astimezone().isoformat(timespec="seconds")
        out = save_report(report, "MANUAL_STOP")
        print()
        print("MANUAL_STOP — inputs released.")
        print("Saved:", out.resolve())
        return 130

    except (SafetyHold, BridgeError) as exc:
        try:
            rel = br.release_all()
        except Exception as release_exc:
            rel = {"error": f"{type(release_exc).__name__}: {release_exc}"}
        report["emergency_release"] = rel
        report["result"] = {
            "status": "SAFETY_HOLD",
            "pass": False,
            "reason": str(exc),
            "detail": "Automation stopped; RELEASE_ALL requested.",
        }
        report["finished"] = datetime.now().astimezone().isoformat(timespec="seconds")
        out = save_report(report, "HOLD")
        print()
        print("=" * 80)
        print("SAFETY_HOLD")
        print(str(exc))
        print("Inputs released.")
        print("Saved:", out.resolve())
        print("=" * 80)
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as exc:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        crash_path = Path(f"CausalAutoRun_v0p14_{stamp}_CRASH.json")
        crash = {
            "tool": "Pokebot3DS-CFW Causal Auto-Run Controller v0p14",
            "status": "PYTHON_CRASH",
            "exception_type": type(exc).__name__,
            "exception": str(exc),
            "traceback": traceback.format_exc(),
        }
        try:
            crash_path.write_text(json.dumps(crash, indent=2), encoding="utf-8")
        except Exception:
            pass
        print()
        print("=" * 80)
        print("PYTHON_CRASH")
        print(type(exc).__name__ + ":", exc)
        print(traceback.format_exc())
        print("Crash report:", crash_path.resolve())
        print("=" * 80)
        raise
