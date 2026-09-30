# melonDS Switch HG/SS Shiny-Hunting Bot Design

Date: 2026-09-30

## 1. Purpose

Build a safe, RAM-driven shiny-hunting bot for Pokemon HeartGold and SoulSilver running in `trulymust/melonDS-switch-upscale` on Nintendo Switch. The PC-side bot communicates over USB using Koi's USB botbase 3.33.

The first retained feature is HG/SS starter hunting. The bot must inspect all three starters in RAM, stop on any shiny, and only soft-reset when all three starter records are proven valid and non-shiny.

This design deliberately establishes reusable transport, emulator-memory, controller, Pokemon-data, diagnostics, and game-profile layers so later hunts and later DS games can reuse the same foundation.

## 2. Primary constraints

- Emulator: `trulymust/melonDS-switch-upscale`.
- Switch transport: Koi USB botbase 3.33 over USB.
- Bot implementation: Python on the PC.
- No capture card dependency.
- No Lua script loaded into melonDS.
- RAM access from the bot is read-only by design. The bot transport will not expose RAM-write methods.
- Controller input is allowed and is the only way the bot changes game state.
- No Qt UI in v0.1.
- Safety takes priority over reset speed.
- Unknown or contradictory game/Pokemon state must fail closed.

## 3. External technical references

### melonDS Switch fork

`trulymust/melonDS-switch-upscale` identifies itself as melonDS 0.9.2. On ARM64 the JIT is enabled by default. In the Switch JIT memory implementation, melonDS allocates and maps a backing memory block and assigns `NDS::MainRAM` to the MainRAM portion of that block.

Relevant source files:

- `src/NDS.h`
- `src/NDS.cpp`
- `src/ARMJIT_Memory.cpp`
- `src/frontend/switch/main.cpp`

### Koi USB botbase

The Koi command set exposes the primitives required by the PC bot:

- `peekAbsolute`
- `peekAbsoluteMulti`
- `peekMain`
- pointer reads
- `getHeapBase`
- `getMainNsoBase`
- `getBuildID`
- `getTitleID`
- controller `press`, `release`, `click`, `clickSeq`

The bot will wrap these commands; game and hunt code must never emit Koi protocol strings directly.

### Pokebot NDS reference logic

`wyanido/pokebot-nds` supplies proven HG/SS game-side research that should be ported rather than rediscovered:

- game/language identification around DS addresses `0x023FFE08` and `0x023FFE0F`
- HG/SS regional offsets
- dynamic HG/SS anchor at `0x021D4158 + regional_offset`
- starter data at `anchor + 0x1BC00`
- Gen IV Pokemon record parsing/checksum logic
- Gen IV shiny test

## 4. High-level architecture

```text
HGSS starter hunt
        |
        v
HGSS game profile / state machine
        |
        +-------------------+
        |                   |
        v                   v
NDSMemory              DSController
        |                   |
        v                   v
melonDS MainRAM       InputGate
resolver                   |
        |                   |
        +---------+---------+
                  |
                  v
             Koi USB
                  |
                  v
             Nintendo Switch
                  |
                  v
     melonDS-switch-upscale
                  |
                  v
            HG / SS ROM
```

Dependency direction is one-way. Higher layers may depend on lower layers, but lower layers do not know about hunts.

## 5. Repository/module layout

```text
pokebot-melonds/
|
+-- run_probe.py
+-- run_hgss_starters.py
|
+-- pokebot/
|   +-- transport/
|   |   +-- koi_usb.py
|   |   +-- protocol.py
|   |
|   +-- emulator/
|   |   +-- melonds.py
|   |   +-- mainram_resolver.py
|   |   +-- nds_memory.py
|   |
|   +-- input/
|   |   +-- ds_controller.py
|   |   +-- input_gate.py
|   |
|   +-- pokemon/
|   |   +-- gen4_crypto.py
|   |   +-- pk4.py
|   |   +-- shiny.py
|   |
|   +-- games/
|   |   +-- hgss/
|   |       +-- profile.py
|   |       +-- pointers.py
|   |       +-- states.py
|   |       +-- starters.py
|   |
|   +-- hunts/
|   |   +-- hgss_starters.py
|   |
|   +-- diagnostics/
|       +-- logger.py
|       +-- dumps.py
|
+-- probes/
|   +-- 00_usb_probe.py
|   +-- 01_input_probe.py
|   +-- 02_reset_probe.py
|   +-- 03_mainram_probe.py
|   +-- 04_hgss_anchor_probe.py
|   +-- 05_starter_watch_probe.py
|   +-- 06_starter_reset_probe.py
|
+-- tests/
|   +-- test_gen4_crypto.py
|   +-- test_pk4.py
|   +-- test_hgss_offsets.py
|   +-- test_mainram_translation.py
|   +-- fixtures/
|
+-- docs/
    +-- superpowers/
```

Probes are permanent diagnostics, not throwaway scripts.

## 6. Koi USB transport

`KoiUSBTransport` owns the USB connection and Koi command protocol.

Responsibilities:

- connect/disconnect
- send a command and parse a reply
- bounded timeout and retry policy
- read absolute process memory
- read NSO-relative memory when required
- query title ID, build ID, heap base, NSO base, and botbase version
- send controller press/release/click operations
- cancel outstanding input sequences when entering a hold

The public RAM interface is read-only. No `poke`, pointer write, freeze, or equivalent API is exposed above the transport implementation.

## 7. melonDS MainRAM resolution

HG/SS DS addresses cannot be passed directly to Koi. Koi reads the Switch process's virtual address space; HG/SS addresses refer to the emulated NDS address space inside melonDS.

For normal NDS main RAM, logical translation is:

```text
offset = ds_address - 0x02000000
host_address = mainram_host + offset
```

`NDSMemory` exposes DS addresses only:

```python
class NDSMemory:
    def resolve_mainram(self) -> int: ...
    def read8(self, ds_addr: int) -> int: ...
    def read16(self, ds_addr: int) -> int: ...
    def read32(self, ds_addr: int) -> int: ...
    def read(self, ds_addr: int, size: int) -> bytes: ...
```

Hunt/game code must never receive or calculate Switch host addresses.

### 7.1 Resolver strategy

Use a three-tier strategy.

#### Tier A: runtime memory/signature discovery

Preferred first approach. Locate a candidate host memory window and validate it against multiple HG/SS values at known DS offsets.

A candidate is not accepted merely because its allocation size looks correct. Validation requires independent evidence, including:

- plausible HG/SS game code at DS `0x023FFE08`
- recognized language byte at DS `0x023FFE0F`
- a plausible HG/SS dynamic anchor at DS `0x021D4158 + language_offset`
- starter structures that validate when the starter screen is open

#### Tier B: stable NSO-relative pointer

During probing, determine whether the compiled melonDS fork exposes a stable module-relative pointer or pointer chain to `NDS::MainRAM`.

If stable across launches of the same build, use this as the fast resolver while retaining signature validation.

#### Tier C: tiny melonDS bridge marker

If A/B are unreliable across launches/builds, maintain a minimally modified build of this exact melonDS fork which exposes a stable marker structure, for example:

```text
POKEBOT_NDS_BRIDGE_V1
mainram_pointer
mainram_size
console_type
```

The Python-side `NDSMemory` API remains unchanged if this fallback is adopted.

### 7.2 Resolver invalidation

MainRAM resolution is invalidated after:

- melonDS process restart
- title/build ID change
- impossible game code/language
- invalid anchor after bounded retries
- USB reconnect where process metadata has changed

A soft reset does not automatically require a new host scan, but the previously resolved mapping must be revalidated before use.

## 8. HG/SS identification and regional offsets

Read the game/language markers through `NDSMemory`.

Recognized HG/SS language offsets are ported from Pokebot NDS:

```text
HeartGold:
JP -0x3B08
EN  0x0000
FR +0x0020
IT -0x0060
DE -0x0020
ES +0x0020

SoulSilver:
JP -0x3B08
EN  0x0000
FR +0x0020
IT -0x0060
DE -0x0020
ES +0x0040
```

Unsupported or contradictory game/language values produce `GAME_ID_MISMATCH` and disable automation.

## 9. HG/SS pointers and starter data

For the detected regional offset:

```text
anchor = read32(0x021D4158 + regional_offset)
starter_data = anchor + 0x1BC00
```

The three starter records are read at 236-byte spacing.

Expected species set:

- Chikorita: 152
- Cyndaquil: 155
- Totodile: 158

The existing Pokebot NDS starter routine passes the starter structures through its raw-data path. The Python parser therefore has an explicit starter/raw structure mode rather than assuming all in-memory Gen IV structures require the normal encrypted-box-data path.

## 10. Gen IV Pokemon validation and shiny logic

No Pokemon record may influence reset decisions until it validates.

Validation includes:

- correct record length
- valid checksum
- expected species
- sensible structural values
- stable repeated read

For starter stability, read all three starters at least twice and require the following to remain unchanged between accepted samples:

- PID
- checksum
- species

Gen IV shiny value:

```text
sv = TID XOR SID XOR PID_high XOR PID_low
shiny = sv < 8
```

Any shiny starter is sufficient to enter `SHINY_HOLD`.

## 11. DS controller abstraction

The HG/SS hunt talks in DS controls, not Switch controls.

Default mapping in this melonDS Switch fork:

```text
DS A      -> Switch A
DS B      -> Switch B
DS X      -> Switch X
DS Y      -> Switch Y
DS Start  -> Switch Plus
DS Select -> Switch Minus
DS L      -> Switch L
DS R      -> Switch R
D-pad     -> Switch D-pad / supported left-stick mapping
```

Soft reset is exposed as one semantic operation:

```python
ds.soft_reset()
```

and implemented as an overlapping hold of:

```text
L + R + PLUS + MINUS
```

The initial reset probe establishes a conservative reliable hold duration. It is optimized only after correctness is proven.

## 12. InputGate

All controller output passes through a single `InputGate`.

States:

```text
DISABLED
PROBE_ONLY
AUTOMATION
SHINY_HOLD
SAFETY_HOLD
```

`SHINY_HOLD` and `SAFETY_HOLD`:

- cancel queued input sequences
- release any held controls
- reject all subsequent hunt-generated input

This is an independent protection in addition to the hunt state machine. A later logic bug calling `soft_reset()` cannot reset a shiny if the gate is already holding.

## 13. Starter hunt state machine

```text
DISCONNECTED
    |
    v
USB_CONNECTED
    |
    v
MELONDS_IDENTIFIED
    |
    v
MAINRAM_RESOLVED
    |
    v
HGSS_IDENTIFIED
    |
    v
LOAD/CONTINUE
    |
    v
WAITING_FOR_STARTER_SCREEN
    |
    v
READ_3_STARTERS
    |
    v
VALIDATE_3_STARTERS
    |---------------- invalid/ambiguous ----------------> SAFETY_HOLD
    |
    +---------------- any shiny ------------------------> SHINY_HOLD
    |
    +---------------- all valid non-shiny
    v
SOFT_RESET
    |
    v
REVALIDATE HGSS/RAM
    |
    +----------------------------------------------------> repeat
```

Only the explicit `ALL_VALID_NON_SHINY` decision may request a soft reset.

## 14. Reset and startup synchronization

The bot must not rely on a long blind sleep followed by button mashing.

After soft reset:

1. send the overlapping reset combination
2. enter `RESETTING`
3. stop issuing further controls
4. observe RAM transition away from the previous state
5. wait until a valid HG/SS identity is visible again
6. revalidate the dynamic anchor
7. proceed through the continue/startup sequence
8. interact with the starter machine only when the expected state is established

Where a useful RAM state marker has not yet been identified, conservative timing may be used temporarily during probing, but the following step must be validated before further input is sent.

## 15. Manual verification mode

Before automatic resetting is enabled, `05_starter_watch_probe.py` runs with the InputGate unable to send controller commands.

The user manually opens the HG/SS starter selector. The probe displays all three decoded records and repeatedly validates them.

This mode establishes that:

- MainRAM translation is correct
- the HG/SS anchor is correct
- starter addresses are correct
- raw starter parsing is correct
- shiny calculation is correct

Only after this probe is independently verified may automatic reset logic be enabled.

## 16. Probe sequence

### `00_usb_probe.py`

No game automation.

Report:

- botbase version
- title ID
- build ID
- NSO base
- heap base

### `01_input_probe.py`

Interactive manual controller test for:

- A/B/X/Y
- D-pad
- Plus/Minus
- L/R

### `02_reset_probe.py`

Tests only the HG/SS soft-reset chord. No RAM automation is required for the first pass.

### `03_mainram_probe.py`

No controller output.

Find MainRAM and report:

- host MainRAM address
- HG/SS version
- language
- validation evidence used

### `04_hgss_anchor_probe.py`

No automatic hunt loop.

Report:

- regional offset
- anchor address/value
- derived starter data address

### `05_starter_watch_probe.py`

Zero controller output.

When the user opens the starter selector, display all three starters including:

- species
- PID
- checksum
- TID/SID
- shiny value
- shiny state
- nature
- IVs

### `06_starter_reset_probe.py`

One-cycle automation only:

```text
read -> validate -> all non-shiny -> one soft reset -> stop
```

A shiny or any uncertainty holds instead.

### `07_hgss_starter_hunter.py`

Continuous automation enabled only after probes 00-06 pass.

## 17. Failure handling

Failure classes:

```text
USB_LOST
MAINRAM_LOST
GAME_ID_MISMATCH
ANCHOR_INVALID
STARTER_DATA_INVALID
STARTER_DATA_UNSTABLE
RESET_TIMEOUT
INPUT_ERROR
```

### Recoverable transport/resolver faults

`USB_LOST`:

- disable input
- attempt bounded reconnect
- re-read botbase/title/build/process metadata
- re-resolve/revalidate MainRAM if process identity changed
- hold if recovery cannot prove state

`MAINRAM_LOST`:

- disable input
- rerun resolver
- revalidate HG/SS identity/language
- resume only after proof succeeds

### Ambiguous game/Pokemon faults

`GAME_ID_MISMATCH` -> immediate `SAFETY_HOLD`.

`ANCHOR_INVALID` -> bounded rereads, then resolver retry; hold if unresolved.

`STARTER_DATA_INVALID` -> wait/re-read within a bounded window; hold on timeout.

`STARTER_DATA_UNSTABLE` -> immediate hold once bounded stabilization fails.

`RESET_TIMEOUT` -> stop all input; revalidate RAM; hold if a valid known state cannot be recovered.

General rule:

```text
uncertain transport -> bounded retry
uncertain RAM mapping -> reacquire and prove
uncertain game state -> hold
uncertain Pokemon state -> hold
possible shiny -> hold
```

## 18. Diagnostics and support bundles

Internally log events as JSONL.

Example:

```json
{"t":123.456,"event":"state","from":"HGSS_IDENTIFIED","to":"WAIT_STARTERS"}
{"t":124.112,"event":"ram","addr":"0x021D4158","value":"0x0221AB00"}
{"t":130.445,"event":"starter","slot":1,"species":152,"pid":"8F32A106","shiny":false}
{"t":130.447,"event":"starter","slot":2,"species":155,"pid":"3A641C20","shiny":false}
{"t":130.449,"event":"starter","slot":3,"species":158,"pid":"22F91230","shiny":false}
{"t":130.600,"event":"decision","value":"RESET"}
```

Every hold writes a support bundle containing at least:

```text
summary.txt
session.json
koi_transport.log
ram_resolution.json
hgss_state.json
starter_reads.bin
starter_reads.json
recent_events.log
```

Include:

- botbase version
- Switch title/build ID
- heap and NSO base
- MainRAM host address
- resolver strategy/evidence
- HG/SS version/language
- anchor
- starter base
- recent raw reads
- decoded starter values
- recent state transitions
- recent controller commands
- exact hold reason

## 19. Testing strategy

### Unit tests

Use captured fixtures to test without hardware:

- Gen IV checksum
- raw starter parsing
- encrypted PK4 parsing where reused later
- shiny calculation, including boundary SV 7 vs 8
- IV decoding
- regional offsets
- DS-to-host address translation
- state-machine decision table
- InputGate refusal after hold

### Hardware gates

Gate 1: 50 manual starter reads with zero bad accepted decodes.

Gate 2: 20 single-reset probe runs with 20/20 successful restart and RAM reacquisition.

Gate 3: 100 automated starter cycles with zero false shiny, unsafe reset, or stuck startup.

Gate 4: 500 automated resets with zero input after hold and zero corrupted starter records accepted.

Gate 5: long-session soak including deliberate USB reconnect, melonDS restart, and bot restart tests.

Primary safety metrics:

```text
unsafe resets = 0
false shiny holds = 0
accepted invalid starter records = 0
input after hold = 0
```

Reset speed is optimized only after these remain zero.

## 20. v0.1 scope

Included:

- Koi USB transport
- melonDS MainRAM resolver
- DS address translation
- HG/SS and language detection
- Gen IV/raw starter parser
- shiny calculation
- DS controller abstraction
- InputGate
- permanent probes 00-06
- continuous HG/SS starter hunter
- shiny/safety holds
- JSONL event logs
- diagnostic support bundles
- session/reset counters

Explicitly excluded from v0.1:

- wild encounters
- static encounters
- eggs
- auto capture
- touch-driven game menus
- Qt UI
- D/P/Pt
- Black/White
- Black 2/White 2
- Discord
- RNG tracking
- savestate automation

## 21. v0.1 acceptance criteria

1. Correctly detects HG or SS and language after every supported launch.
2. Reliably resolves logical NDS MainRAM even when the Switch host address changes.
3. Correctly reads Chikorita, Cyndaquil, and Totodile as validated starter records.
4. Never resets unless all three starters pass checksum/species/stability validation and are non-shiny.
5. Enters `SHINY_HOLD` immediately when any starter is shiny.
6. Sends no hunt input after `SHINY_HOLD` or `SAFETY_HOLD`.
7. Revalidates RAM/game state after every soft reset.
8. Passes the 500-reset safety soak with zero unsafe resets.
9. Produces a complete diagnostic bundle on each hold.
10. User stop always releases held buttons/sticks and disables the InputGate.

## 22. First implementation milestone

Do not begin starter automation immediately.

Implement and hardware-test these first:

```text
00_usb_probe.py
01_input_probe.py
02_reset_probe.py
03_mainram_probe.py
```

Only after all four pass should implementation proceed to the HG/SS anchor, Pokemon parser, starter watcher, and reset automation.

## 23. Future extension path

After v0.1 is proven stable, retain the same lower layers and add features as separate designs/plans:

1. HG/SS wild encounters and safe fleeing
2. HG/SS statics/gifts
3. fishing/headbutt
4. eggs
5. Qt UI and persisted statistics
6. D/P/Pt profiles
7. B/W and B2/W2 profiles

Each later game/hunt should reuse `KoiUSBTransport`, `NDSMemory`, `DSController`, `InputGate`, diagnostics, and common Pokemon parsing rather than adding transport or emulator-specific logic to hunt modules.
