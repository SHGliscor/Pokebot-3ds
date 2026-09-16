# Pokebot NDS mailbox protocol v0p1

## Location

For NTR/SDK1 games the nds-bootstrap card-engine shared block starts at
`0x027FFA0C`.

Pokebot reserves only indexes 9 through 12:

| Index | DS address | Purpose |
|---:|---:|---|
| 9 | `0x027FFA30` | Control/tag + state |
| 10 | `0x027FFA34` | Command |
| 11 | `0x027FFA38` | DATA0 (argument/response) |
| 12 | `0x027FFA3C` | DATA1 (argument/response) |

This boundary is deliberate. In the pinned nds-bootstrap source,
`UNPATCHED_FUNCTION_LOCATION` starts at `0x027FFA40`, so the Pokebot
mailbox must end at `0x027FFA3C`.

## Control word

The upper 24 bits identify Pokebot:

`0x504B4200`

The low byte is the state:

| State | Value | Full control word |
|---|---:|---:|
| READY | 1 | `0x504B4201` |
| REQ | 2 | `0x504B4202` |
| BUSY | 3 | `0x504B4203` |
| DONE | 4 | `0x504B4204` |
| ERR | 5 | `0x504B4205` |

Protocol version: `0x00010001`.

A writer fills Command/DATA0/DATA1 and writes **REQ last**. ARM7 writes the
response fields first and writes DONE or ERR last.

## Commands

### 0x01 PING

Arguments: none.

Success:
- DATA0 = `0x504F4E47` (PONG)
- DATA1 = protocol version
- control = DONE

### 0x02 KEY_SET

Input:
- DATA0 = low ten DS key bits to press
- DATA1 = number of VBlanks to keep them pressed

If DATA1 is zero it defaults to two VBlanks. Values above 120 are clamped to
120, so a failed transport cannot leave a synthetic key held indefinitely.

The mask uses the normal REG_KEYINPUT order:

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

Input:
- DATA0 = aligned DS main-RAM address

Allowed v0p1 range:

`0x02000000 <= address <= 0x023FFFFC`

Success:
- DATA0 = 32-bit value
- DATA1 = protocol version
- control = DONE

Unaligned/out-of-range requests return:
- DATA0 = `0x00000002`
- control = ERR

v0p1 intentionally provides no arbitrary write command.

## Dedicated-build space trade-off

The stock ARM7 cardengine is packed into a `4 KiB + 0x80` linker region.
The first bridge build exceeded that region by 256 bytes. Instead of extending
the region into unknown neighbouring memory, the Pokebot build replaces the
ARM7 TWiLight in-game-menu helper with a no-op stub.

This affects only the dedicated Pokebot bootstrap. The normal TWiLight
release/nightly binaries are not changed.

## ARM11 mapping note

The next-stage New 3DS TWL ARM11 payload can address the mailbox through the
TWL DS-RAM mapping. The DS addresses above remain the protocol authority;
ARM11 virtual addresses should be derived by the ARM11 implementation rather
than hard-coded into PC software.
