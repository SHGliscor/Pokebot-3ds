from __future__ import annotations

"""Compatibility facade for the existing hunt modules.

The class name is retained so the locked starter/reset modules do not change,
and transport is now the Pokebot-Luma acknowledged controller on shared UDP/4952.
Controller completion is acknowledged; existing RAM state and PK6 checksum gates
remain authoritative for gameplay.
"""

import time

from .luma_input import (
    INPUT_PORT as BRIDGE_PORT,
    HID_NEUTRAL,
    BUTTON_BITS,
    CAPABILITIES,
    CAP_HID_LATCH,
    CAP_CPAD_PULSE,
    LumaInputError,
    LumaInputTransport,
    raw_hid_for_buttons,
)


class AcknowledgedInputError(RuntimeError):
    pass


class AcknowledgedInput:
    def __init__(self, host, port=BRIDGE_PORT, timeout=1.0):
        self.host = str(host)
        self.port = int(port)
        self.timeout = float(timeout)
        self.transport = LumaInputTransport(self.host, port=self.port, timeout=self.timeout)

    def close(self):
        self.transport.close()

    @staticmethod
    def _raw_hid(buttons):
        return raw_hid_for_buttons(buttons)

    def input_ping(self):
        try:
            return self.transport.input_ping()
        except Exception as exc:
            raise AcknowledgedInputError(str(exc)) from exc

    def release_all(self):
        try:
            return self.transport.release_all()
        except Exception as exc:
            raise AcknowledgedInputError(str(exc)) from exc

    def pulse(
        self,
        buttons,
        *,
        hold_ms,
        resume_settle_ms,
        packet_interval_ms,
        release_ms,
    ):
        # Preserve the locked module's pre-input timing exactly.
        if resume_settle_ms:
            time.sleep(float(resume_settle_ms) / 1000.0)
        try:
            return self.transport.pulse_buttons(
                buttons,
                hold_ms=int(hold_ms),
                settle_ms=int(release_ms),
                interval_ms=max(5, int(packet_interval_ms or 20)),
            )
        except Exception as exc:
            raise AcknowledgedInputError(str(exc)) from exc

    def touch_pulse(
        self,
        touch_state,
        *,
        hold_ms=180,
        resume_settle_ms=0,
        packet_interval_ms=20,
        release_ms=220,
    ):
        """Send one acknowledged native touchscreen pulse over Pokebot UDP/4952."""
        if resume_settle_ms:
            time.sleep(float(resume_settle_ms) / 1000.0)
        try:
            return self.transport.touch_pulse(
                int(touch_state) & 0xFFFFFFFF,
                hold_ms=int(hold_ms),
                settle_ms=int(release_ms),
                interval_ms=max(5, int(packet_interval_ms or 20)),
            )
        except Exception as exc:
            raise AcknowledgedInputError(str(exc)) from exc

    def circle_pad_pulse(
        self,
        x,
        y,
        *,
        hold_ms=260,
        resume_settle_ms=0,
        packet_interval_ms=20,
        release_ms=80,
    ):
        """Send one acknowledged partial Circle Pad pulse over UDP/4952."""
        if resume_settle_ms:
            time.sleep(float(resume_settle_ms) / 1000.0)
        try:
            caps = self.transport._caps_cache or self.transport.input_ping()
            if not (int(caps.get('capabilities', 0)) & CAP_CPAD_PULSE):
                raise AcknowledgedInputError(
                    f'CPAD-capable Pokebot-Luma boot.firm is not active '
                    f'(controller capabilities=0x{int(caps.get("capabilities", 0)):08X}; '
                    f'required bit=0x{CAP_CPAD_PULSE:08X})'
                )
            return self.transport.cpad_pulse(
                float(x), float(y),
                hold_ms=int(hold_ms),
                settle_ms=int(release_ms),
                interval_ms=max(5, int(packet_interval_ms or 20)),
            )
        except Exception as exc:
            if isinstance(exc, AcknowledgedInputError):
                raise
            raise AcknowledgedInputError(str(exc)) from exc

    def hold_chord_latched(
        self,
        buttons,
        *,
        hold_ms,
        resume_settle_ms=0,
        release_ms=0,
    ):
        """Assert one multi-button HID chord with command 10 until explicit release.

        This is intentionally separate from ``pulse`` so the locked starter
        choreography continues to use the proven timed-pulse path. The reset
        route uses this only for the ORAS soft-reset chord, where hardware logs
        showed occasional game-level misses even though command 6 reached
        COMPLETED.
        """
        if resume_settle_ms:
            time.sleep(float(resume_settle_ms) / 1000.0)

        raw_hid = self._raw_hid(buttons)
        try:
            caps = self.transport._caps_cache or self.transport.input_ping()
            if not (int(caps.get('capabilities', 0)) & CAP_HID_LATCH):
                raise AcknowledgedInputError(
                    'Pokebot-Luma retained HID latch capability is required for reset'
                )

            active = self.transport.latch_raw(raw_hid)
            time.sleep(max(0, int(hold_ms)) / 1000.0)
            released = self.transport.release_all()

            if int(released.get('raw_hid', HID_NEUTRAL)) != HID_NEUTRAL:
                raise AcknowledgedInputError(
                    f'reset chord release did not return neutral HID: '
                    f'0x{int(released.get("raw_hid", 0)):03X}'
                )

            if release_ms:
                time.sleep(float(release_ms) / 1000.0)

            return {
                'mode': 'retained_hid_latch',
                'buttons': tuple(str(x).upper() for x in buttons),
                'requested_raw_hid': raw_hid,
                'hold_ms': int(hold_ms),
                'pre_neutral_ms': int(resume_settle_ms),
                'post_release_ms': int(release_ms),
                'sequence': active.get('sequence'),
                'active_state': active.get('state_name'),
                'active_raw_hid': active.get('raw_hid'),
                'release_state': released.get('state_name'),
                'release_raw_hid': released.get('raw_hid'),
                'acknowledged': True,
                'transport': f'Pokebot-Luma retained HID latch UDP {self.port}',
            }
        except Exception as exc:
            # A reset chord must never escape this helper still asserted.
            try:
                self.transport.release_all()
            except Exception:
                pass
            if isinstance(exc, AcknowledgedInputError):
                raise
            raise AcknowledgedInputError(str(exc)) from exc
