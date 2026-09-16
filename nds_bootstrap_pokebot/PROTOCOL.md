# Pokebot NDS protocol v0p2

## DS mailbox

NTR/SDK1 shared base: `0x027FFA0C`

| Index | DS address | Purpose |
|---:|---:|---|
| 9 | `0x027FFA30` | Control/state |
| 10 | `0x027FFA34` | Command |
| 11 | `0x027FFA38` | DATA0 |
| 12 | `0x027FFA3C` | DATA1 |

The mailbox stops at `0x027FFA3C` because nds-bootstrap reserves
`0x027FFA40` as `UNPATCHED_FUNCTION_LOCATION`.

Protocol version: `0x00020000`.

### Control values

- READY: `0x504B4201`
- REQ: `0x504B4202`
- BUSY: `0x504B4203`
- DONE: `0x504B4204`
- ERR: `0x504B4205`

### Commands

- `0x01 PING` -> DATA0 = `0x504F4E47`
- `0x02 KEY_SET` -> DATA0 key mask, DATA1 duration in VBlanks
- `0x03 RELEASE_ALL`
- `0x10 READ32` -> aligned, read-only DS RAM access

Allowed READ32 range:

`0x02000000..0x023FFFFC`

There is no arbitrary RAM-write command.

## v0p2 RTCom boot marker

If the bootloader completes all three TwlBg patch stages, it writes:

`0x52544332` (`RTC2`)

to `0x027FFA34` immediately before HeartGold starts.

The cardengine consumes this marker on its first mailbox initialization and
enables the v0p2 RTCom hardware proof. A failed RTCom install leaves the marker
clear and does not block game boot.

## v0p2 New-3DS test channel

The TwlBg runtime patch publishes New-3DS ZL/ZR/Nub state through the legacy
RTC COUNTER extension.

The ARM7 cardengine reads its third byte:

- bit 1: ZR
- bit 2: ZL

For v0p2 only, a rising ZL edge starts a three-VBlank synthetic A pulse.

This is a hardware validation command, not the final PC protocol.

## Safety

- Synthetic DS keys auto-release.
- ZL is edge-triggered, not repeated while held.
- The RTCom installer is fail-open.
- Normal TWiLight Release bootstrap is unchanged.
- The dedicated Pokebot build still disables the ARM7 TWiLight in-game-menu
  helper to remain inside the existing cardengine allocation.
