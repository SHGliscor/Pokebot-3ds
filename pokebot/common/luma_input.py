from __future__ import annotations

"""Pokebot-Luma acknowledged controller transport on the shared UDP/4952 bridge.

Pokebot-Luma exposes RAM and controller commands through the same bridge
endpoint.  This compatibility module keeps the public API used by the locked
starter and validated Wild modules, but replaces the old UDP/4950 state stream
with sequence-aware acknowledged commands:

    5  INPUT_PING
    6  INPUT_PULSE
    7  INPUT_STATUS
    8  RELEASE_ALL
    9  INPUT_TOUCH_PULSE
   10  INPUT_HID_LATCH
   13  INPUT_CPAD_PULSE

Game RAM remains read-only.  Controller completion acknowledges transport
execution; the existing RAM/state/PK6 gates remain authoritative for gameplay.
"""

import socket
import struct
import time

INPUT_PORT = 4952
RAM_PROBE_PORT = 4952
HID_NEUTRAL = 0x00000FFF
TOUCH_NEUTRAL = 0x02000000
CPAD_NEUTRAL = 0x007FF7FF

BUTTON_BITS = {
    'A': 0, 'B': 1, 'SELECT': 2, 'START': 3,
    'RIGHT': 4, 'LEFT': 5, 'UP': 6, 'DOWN': 7,
    'R': 8, 'L': 9, 'X': 10, 'Y': 11,
}

REQ_MAGIC = 0x5242524F
RESP_MAGIC = 0x5342524F
WIRE_VERSION = 1
REQ = struct.Struct('<IHHIII')
RESP = struct.Struct('<IHHIIiI')
INPUT_CAPS = struct.Struct('<IIIIII')
INPUT_STATUS = struct.Struct('<IIIII')

CMD_INPUT_PING = 5
CMD_INPUT_PULSE = 6
CMD_INPUT_STATUS = 7
CMD_RELEASE_ALL = 8
CMD_INPUT_TOUCH_PULSE = 9
CMD_INPUT_HID_LATCH = 10
CMD_INPUT_CPAD_PULSE = 13

STATUS_NAMES = {
    0: 'OK', 1: 'BAD_MAGIC', 2: 'BAD_VERSION', 3: 'BAD_COMMAND',
    4: 'GAME_NOT_FOUND', 5: 'OPEN_FAILED', 6: 'QUERY_FAILED',
    7: 'NOT_READABLE', 8: 'RANGE_INVALID', 9: 'LENGTH_INVALID',
    10: 'MAP_FAILED', 11: 'INTERNAL', 12: 'INPUT_INVALID',
    13: 'INPUT_BUSY', 14: 'INPUT_LEGACY_ACTIVE', 15: 'INPUT_PATCH_FAILED',
}

INPUT_STATE_IDLE = 0
INPUT_STATE_ACCEPTED = 1
INPUT_STATE_IN_PROGRESS = 2
INPUT_STATE_COMPLETED = 3
INPUT_STATE_ALREADY_COMPLETED = 4
INPUT_STATE_ABORTED = 5
INPUT_STATE_NOT_FOUND = 6
INPUT_STATE_NAMES = {
    0: 'IDLE', 1: 'ACCEPTED', 2: 'IN_PROGRESS', 3: 'COMPLETED',
    4: 'ALREADY_COMPLETED', 5: 'ABORTED', 6: 'NOT_FOUND',
}

CAP_HID_PULSE = 1 << 0
CAP_STATUS_COMPAT = 1 << 1
CAP_SEQUENCE_COMPAT = 1 << 2
CAP_RELEASE_ALL = 1 << 3
CAP_TOUCH_PULSE = 1 << 6
CAP_HID_LATCH = 1 << 7
CAP_CPAD_PULSE = 1 << 8
CAPABILITIES = (
    CAP_HID_PULSE | CAP_STATUS_COMPAT | CAP_SEQUENCE_COMPAT |
    CAP_RELEASE_ALL | CAP_TOUCH_PULSE | CAP_HID_LATCH | CAP_CPAD_PULSE
)


class LumaInputError(RuntimeError):
    pass


def raw_hid_for_buttons(buttons) -> int:
    raw = HID_NEUTRAL
    for item in buttons:
        key = str(item).strip().upper()
        if key not in BUTTON_BITS:
            raise ValueError(f'Unsupported button: {key}')
        raw &= ~(1 << BUTTON_BITS[key])
    return raw


CPAD_BOUND = 0x5D0


def encode_circle_pad(x: float, y: float) -> int:
    """Encode normalized Circle Pad axes using Luma InputRedirection format.

    x/y are clamped to [-1.0, 1.0]. Neutral is 0x7FF7FF. ORAS DexNav
    sneaking uses deliberately partial tilt, never a full-stick value.
    """
    x = max(-1.0, min(1.0, float(x)))
    y = max(-1.0, min(1.0, float(y)))
    if x == 0.0 and y == 0.0:
        return CPAD_NEUTRAL
    raw_x = int(x * CPAD_BOUND + 0x800)
    raw_y = int(y * CPAD_BOUND + 0x800)
    if raw_x >= 0xFFF:
        raw_x = 0x000 if x < 0 else 0xFFF
    if raw_y >= 0xFFF:
        raw_y = 0x000 if y < 0 else 0xFFF
    raw_x = max(0, min(0xFFF, raw_x))
    raw_y = max(0, min(0xFFF, raw_y))
    return ((raw_y & 0xFFF) << 12) | (raw_x & 0xFFF)


def _state_payload(payload: bytes):
    if len(payload) != INPUT_STATUS.size:
        raise LumaInputError(
            f'input status payload size {len(payload)}, expected {INPUT_STATUS.size}'
        )
    sequence, state, raw_hid, remaining_ms, runtime_flags = INPUT_STATUS.unpack(payload)
    return {
        'sequence': int(sequence),
        'state': int(state),
        'state_name': INPUT_STATE_NAMES.get(int(state), f'UNKNOWN_{int(state)}'),
        'raw_hid': int(raw_hid),
        'remaining_ms': int(remaining_ms),
        'runtime_flags': int(runtime_flags),
        'acknowledged': True,
    }


class LumaInputTransport:
    """Sequence-aware acknowledged Pokebot-Luma controller on UDP/4952."""

    def __init__(self, host, port=INPUT_PORT, timeout=1.0):
        self.host = str(host)
        self.port = int(port)
        self.timeout = max(0.1, float(timeout))
        self.remote = (self.host, self.port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(self.timeout)
        self._sequence = int(time.time_ns() & 0x7FFFFFFF) or 1
        self._caps_cache = None

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass

    def next_sequence(self):
        self._sequence = (self._sequence + 1) & 0x7FFFFFFF
        if self._sequence == 0:
            self._sequence = 1
        return self._sequence

    def _request(self, command, *, argument=0, aux=0, request_id=None, retries=1):
        request_id = int(request_id or self.next_sequence()) & 0xFFFFFFFF
        if request_id == 0:
            request_id = 1
        packet = REQ.pack(
            REQ_MAGIC, WIRE_VERSION, int(command), request_id,
            int(argument) & 0xFFFFFFFF, int(aux) & 0xFFFFFFFF,
        )

        last_error = None
        for attempt in range(int(retries) + 1):
            try:
                self.sock.sendto(packet, self.remote)
                deadline = time.monotonic() + self.timeout
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('Pokebot-Luma response timeout')
                    self.sock.settimeout(remaining)
                    data, remote = self.sock.recvfrom(4096)
                    if len(data) < RESP.size:
                        continue
                    magic, version, status, echoed_id, echoed_arg, result, payload_len = RESP.unpack_from(data)
                    if magic != RESP_MAGIC or version != WIRE_VERSION:
                        continue
                    if echoed_id != request_id:
                        # Ignore a late datagram from another request on this socket.
                        continue
                    payload = data[RESP.size:]
                    if len(payload) != payload_len:
                        raise LumaInputError(
                            f'payload mismatch header={payload_len} actual={len(payload)}'
                        )
                    if status != 0:
                        name = STATUS_NAMES.get(int(status), f'UNKNOWN_{int(status)}')
                        raise LumaInputError(
                            f'Pokebot-Luma command {command} failed: {name} '
                            f'result=0x{int(result) & 0xFFFFFFFF:08X}'
                        )
                    return {
                        'remote': f'{remote[0]}:{remote[1]}',
                        'request_id': request_id,
                        'status': int(status),
                        'argument': int(echoed_arg),
                        'result': int(result),
                        'payload': payload,
                    }
            except (socket.timeout, TimeoutError) as exc:
                last_error = exc
                if attempt >= int(retries):
                    break
                # Same request ID is deliberately retransmitted. Commands 6/9/10
                # are sequence-deduplicated by Pokebot-Luma, so this cannot create
                # a second gameplay edge when only the ACK was lost.
                continue
        raise LumaInputError(
            f'Pokebot-Luma UDP/{self.port} timeout after {int(retries)+1} attempt(s)'
        ) from last_error

    def input_ping(self):
        r = self._request(CMD_INPUT_PING, retries=1)
        payload = r['payload']
        if len(payload) != INPUT_CAPS.size:
            raise LumaInputError(
                f'INPUT_PING payload size {len(payload)}, expected {INPUT_CAPS.size}'
            )
        protocol, caps, runtime, neutral, max_hold, max_settle = INPUT_CAPS.unpack(payload)
        info = {
            'protocol': int(protocol),
            'capabilities': int(caps),
            'runtime_flags': int(runtime),
            'neutral_hid': int(neutral),
            'max_hold_ms': int(max_hold),
            'max_settle_ms': int(max_settle),
            'transport': 'Pokebot-Luma acknowledged bridge',
            'udp_port': self.port,
            'acknowledged': True,
            'authority': 'controller ACK + existing RAM/state/PK6 gates',
        }
        required = CAP_HID_PULSE | CAP_STATUS_COMPAT | CAP_SEQUENCE_COMPAT | CAP_RELEASE_ALL
        if (int(caps) & required) != required:
            raise LumaInputError(
                f'Pokebot-Luma controller missing required capabilities: '
                f'caps=0x{int(caps):08X} required=0x{required:08X}'
            )
        if int(neutral) != HID_NEUTRAL:
            raise LumaInputError(
                f'Pokebot-Luma neutral HID mismatch: 0x{int(neutral):03X}'
            )
        self._caps_cache = info
        return info

    def _status(self, sequence):
        r = self._request(
            CMD_INPUT_STATUS,
            argument=int(sequence),
            aux=0,
            retries=1,
        )
        return _state_payload(r['payload'])

    def _wait_terminal(self, sequence, initial, *, hold_ms, settle_ms, poll_ms=25):
        current = dict(initial)
        if current['state'] in (INPUT_STATE_COMPLETED, INPUT_STATE_ALREADY_COMPLETED):
            return current
        if current['state'] in (INPUT_STATE_ABORTED, INPUT_STATE_NOT_FOUND):
            raise LumaInputError(
                f'Pokebot-Luma sequence {sequence} {current["state_name"]}'
            )

        deadline = time.monotonic() + max(
            self.timeout * 3.0,
            (max(0, int(hold_ms)) + max(0, int(settle_ms))) / 1000.0 + 2.0,
        )
        interval = max(0.010, min(0.100, float(poll_ms) / 1000.0))
        samples = []
        status_timeouts = 0
        max_status_timeouts = 2
        while time.monotonic() < deadline:
            # Avoid polling faster than needed while still giving the bridge
            # regular service opportunities for hold->settle->terminal state.
            remaining_ms = int(current.get('remaining_ms', 0) or 0)
            if remaining_ms > 80:
                time.sleep(min(0.050, max(interval, (remaining_ms - 40) / 1000.0)))
            else:
                time.sleep(interval)

            try:
                # INPUT_STATUS is read-only. If a status reply is lost after the
                # gameplay command was already ACKed, keep querying this exact
                # gameplay sequence; never replay the button/touch command.
                current = self._status(sequence)
            except LumaInputError as exc:
                if 'timeout' not in str(exc).lower():
                    raise
                status_timeouts += 1
                samples.append({
                    'event': 'STATUS_TIMEOUT',
                    'sequence': int(sequence),
                    'timeout_count': status_timeouts,
                })
                if status_timeouts > max_status_timeouts:
                    break
                # Extend only by one bounded transport timeout per lost read-only
                # status reply. The gameplay command itself is not reissued.
                deadline += min(self.timeout, 1.0)
                continue

            samples.append({
                'state': current['state'],
                'state_name': current['state_name'],
                'remaining_ms': current['remaining_ms'],
                'raw_hid': current['raw_hid'],
            })
            if current['state'] in (INPUT_STATE_COMPLETED, INPUT_STATE_ALREADY_COMPLETED):
                current['samples'] = samples
                return current
            if current['state'] in (INPUT_STATE_ABORTED, INPUT_STATE_NOT_FOUND):
                raise LumaInputError(
                    f'Pokebot-Luma sequence {sequence} {current["state_name"]}'
                )

        # Fail closed and force neutral before surfacing the timeout.
        try:
            self.release_all()
        except Exception:
            pass
        raise LumaInputError(
            f'Pokebot-Luma sequence {sequence} did not reach COMPLETED in time'
        )

    def release_all(self, duration_ms=0):
        # duration_ms remains accepted for API compatibility; command 8 owns
        # immediate neutralisation in firmware.
        r = self._request(CMD_RELEASE_ALL, retries=1)
        status = _state_payload(r['payload'])
        status.update({
            'request_id': r['request_id'],
            'transport': f'Pokebot-Luma acknowledged UDP {self.port}',
        })
        return status

    def pulse_raw(self, raw_hid, *, hold_ms, settle_ms, interval_ms=20):
        raw_hid = int(raw_hid) & HID_NEUTRAL
        if raw_hid == HID_NEUTRAL:
            raise LumaInputError('refusing neutral HID as a pulse')
        hold_ms = int(hold_ms)
        settle_ms = int(settle_ms)
        if not (0 <= settle_ms <= 0xFFFF and 0 <= hold_ms <= 0xFFFF):
            raise LumaInputError('hold/settle outside wire range')
        sequence = self.next_sequence()
        aux = (hold_ms & 0xFFFF) | ((settle_ms & 0xFFFF) << 16)
        r = self._request(
            CMD_INPUT_PULSE,
            argument=raw_hid,
            aux=aux,
            request_id=sequence,
            retries=1,
        )
        initial = _state_payload(r['payload'])
        terminal = self._wait_terminal(
            sequence, initial,
            hold_ms=hold_ms,
            settle_ms=settle_ms,
            poll_ms=max(10, int(interval_ms or 20)),
        )
        return {
            **terminal,
            'sequence': sequence,
            'requested_raw_hid': raw_hid,
            'hold_ms': hold_ms,
            'settle_ms': settle_ms,
            'initial': initial,
            'terminal': terminal,
            'completed': True,
            'acknowledged': True,
            'transport': f'Pokebot-Luma acknowledged UDP {self.port}',
        }

    def pulse_buttons(self, buttons, *, hold_ms, settle_ms, interval_ms=20):
        return self.pulse_raw(
            raw_hid_for_buttons(buttons),
            hold_ms=hold_ms,
            settle_ms=settle_ms,
            interval_ms=interval_ms,
        )

    def touch_pulse(self, touch_state, *, hold_ms, settle_ms, interval_ms=20):
        caps = self._caps_cache or self.input_ping()
        if not (int(caps['capabilities']) & CAP_TOUCH_PULSE):
            raise LumaInputError('Pokebot-Luma does not advertise native touch pulse')
        sequence = self.next_sequence()
        hold_ms = int(hold_ms)
        settle_ms = int(settle_ms)
        aux = (hold_ms & 0xFFFF) | ((settle_ms & 0xFFFF) << 16)
        r = self._request(
            CMD_INPUT_TOUCH_PULSE,
            argument=int(touch_state) & 0xFFFFFFFF,
            aux=aux,
            request_id=sequence,
            retries=1,
        )
        initial = _state_payload(r['payload'])
        terminal = self._wait_terminal(
            sequence, initial,
            hold_ms=hold_ms,
            settle_ms=settle_ms,
            poll_ms=max(10, int(interval_ms or 20)),
        )
        return {
            **terminal,
            'sequence': sequence,
            'touch_state': int(touch_state) & 0xFFFFFFFF,
            'hold_ms': hold_ms,
            'settle_ms': settle_ms,
            'initial': initial,
            'terminal': terminal,
            'completed': True,
            'acknowledged': True,
            'transport': f'Pokebot-Luma acknowledged UDP {self.port}',
        }

    def cpad_pulse(self, x, y, *, hold_ms, settle_ms, interval_ms=20):
        caps = self._caps_cache or self.input_ping()
        if not (int(caps['capabilities']) & CAP_CPAD_PULSE):
            raise LumaInputError(
                'Pokebot-Luma does not advertise acknowledged Circle Pad pulse; '
                'copy the bundled HF77 boot.firm to the SD root'
            )
        cpad_state = encode_circle_pad(x, y)
        if cpad_state == CPAD_NEUTRAL:
            raise LumaInputError('refusing neutral Circle Pad as a pulse')
        hold_ms = int(hold_ms)
        settle_ms = int(settle_ms)
        if not (0 < hold_ms <= 0xFFFF and 0 <= settle_ms <= 0xFFFF):
            raise LumaInputError('Circle Pad hold/settle outside wire range')
        sequence = self.next_sequence()
        aux = (hold_ms & 0xFFFF) | ((settle_ms & 0xFFFF) << 16)
        r = self._request(
            CMD_INPUT_CPAD_PULSE,
            argument=cpad_state,
            aux=aux,
            request_id=sequence,
            retries=1,
        )
        initial = _state_payload(r['payload'])
        terminal = self._wait_terminal(
            sequence, initial,
            hold_ms=hold_ms,
            settle_ms=settle_ms,
            poll_ms=max(10, int(interval_ms or 20)),
        )
        return {
            **terminal,
            'sequence': sequence,
            'cpad_state': cpad_state,
            'x': float(x),
            'y': float(y),
            'hold_ms': hold_ms,
            'settle_ms': settle_ms,
            'initial': initial,
            'terminal': terminal,
            'completed': True,
            'acknowledged': True,
            'transport': f'Pokebot-Luma acknowledged CPAD UDP {self.port}',
        }

    def latch_raw(self, raw_hid):
        caps = self._caps_cache or self.input_ping()
        if not (int(caps['capabilities']) & CAP_HID_LATCH):
            raise LumaInputError('Pokebot-Luma does not advertise retained HID latch')
        raw_hid = int(raw_hid) & HID_NEUTRAL
        if raw_hid == HID_NEUTRAL:
            raise LumaInputError('refusing neutral HID as a latch')
        sequence = self.next_sequence()
        r = self._request(
            CMD_INPUT_HID_LATCH,
            argument=raw_hid,
            aux=0,
            request_id=sequence,
            retries=1,
        )
        initial = _state_payload(r['payload'])
        if initial['state'] in (INPUT_STATE_ABORTED, INPUT_STATE_NOT_FOUND):
            raise LumaInputError(
                f'Pokebot-Luma latch sequence {sequence} {initial["state_name"]}'
            )
        # One status query proves the latched sequence is still owned by the
        # bridge. It intentionally remains IN_PROGRESS until RELEASE_ALL.
        active = self._status(sequence)
        if active['state'] not in (INPUT_STATE_ACCEPTED, INPUT_STATE_IN_PROGRESS):
            raise LumaInputError(
                f'Pokebot-Luma latch sequence {sequence} unexpected {active["state_name"]}'
            )
        return {
            **active,
            'sequence': sequence,
            'active': True,
            'requested_raw_hid': raw_hid,
            'initial': initial,
            'acknowledged': True,
            'transport': f'Pokebot-Luma acknowledged UDP {self.port}',
        }


def self_test():
    assert REQ.size == 20
    assert RESP.size == 24
    assert INPUT_CAPS.size == 24
    assert INPUT_STATUS.size == 20
    assert raw_hid_for_buttons(('A',)) == 0xFFE
    assert raw_hid_for_buttons(('START',)) == 0xFF7
    assert raw_hid_for_buttons(('L', 'R', 'START', 'SELECT')) == 0xCF3
    assert INPUT_PORT == 4952
    assert encode_circle_pad(0.0, 0.0) == CPAD_NEUTRAL
    assert encode_circle_pad(-0.40, 0.0) != CPAD_NEUTRAL
    return True
