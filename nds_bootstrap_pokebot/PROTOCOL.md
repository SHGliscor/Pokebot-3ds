# Pokebot NDS mailbox protocol v0p1

## Location

For NTR/SDK1 games the nds-bootstrap card-engine shared block starts at
`0x027FFA0C`.

Pokebot reserves indexes 9 through 15:

| Index | DS address | Purpose |
|---:|---:|---|
| 9 | `0x027FFA30` | Magic |
| 10 | `0x027FFA34` | Command |
| 11 | `0x027FFA38` | ARG0 |
| 12 | `0x027FFA3C` | ARG1 |
| 13 | `0x027FFA40` | RESP0 |
| 14 | `0x027FFA44` | RESP1 |
| 15 | `0x027FFA48` | Status |

The current pinned nds-bootstrap source uses indexes 0 through 8 for existing
card-engine/IGM state. v0p1 intentionally stays above those slots.

## Tags and status values

- Magic: `0x504B4254`
- PONG: `0x504F4E47`
- READY: `0x52445921`
- REQ: `0x52455121`
- BUSY: `0x42555359`
- DONE: `0x444F4E45`
- ERR: `0x45525221`
- Protocol version: `0x00010000`

A writer fills Command/ARG0/ARG1 and writes **REQ last**. The ARM7 handler
writes response fields and writes DONE or ERR last.

## Commands

### 0x01 PING

Arguments: none.

Success:
- RESP0 = PONG
- RESP1 = protocol version
- status = DONE

### 0x02 KEY_SET

- ARG0 = low ten DS key bits to press
- ARG1 = number of VBlanks to keep them pressed

If ARG1 is zero it defaults to two VBlanks. Values above 120 are clamped to
120.

The mask uses the normal REG_KEYINPUT bit order:

| Bit | Button |
|---:|---|
| 0 | A |
| 1 | B |
| 2 | Select |
| 3 | Start |
| 4 | Right |
| 5 | Left |
| 6 | Up |
| 7 | Down |
| 8 | R |
| 9 | L |

v0p1 does not synthesize X, Y, touch, lid state, or New-3DS-only buttons.

### 0x03 RELEASE_ALL

Clears every Pokebot-injected key immediately.

### 0x10 READ32

- ARG0 = aligned DS main-RAM address

Allowed v0p1 range:

`0x02000000 <= address <= 0x023FFFFC`

Success:
- RESP0 = 32-bit value
- status = DONE

Unaligned/out-of-range requests return:
- RESP0 = `0x00000002`
- status = ERR

v0p1 intentionally provides no arbitrary write command.

## ARM11 mapping note

The next-stage New 3DS TWL ARM11 payload can address the mailbox through the
TWL DS-RAM mapping. The DS address is the protocol authority; ARM11 virtual
addresses should be derived in the ARM11 implementation rather than hard-coded
into PC software.
