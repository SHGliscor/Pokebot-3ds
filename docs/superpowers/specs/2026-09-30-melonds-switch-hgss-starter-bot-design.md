# melonDS Switch HG/SS Shiny-Hunting Bot Design

Date: 2026-09-30

## 1. Purpose

Build a safe, RAM-driven shiny-hunting bot for Pokemon HeartGold and SoulSilver running in `trulymust/melonDS-switch-upscale` on Nintendo Switch. The PC-side bot communicates over USB using Koi USB botbase 3.33.

The first retained feature is HG/SS starter hunting. The bot must inspect all three starters in RAM, stop on any shiny, and only soft-reset when all three starter records are proven valid and non-shiny.

The architecture must be reusable for later HG/SS hunts and, later, D/P/Pt and Gen 5 without rebuilding the USB, emulator-memory, controller, diagnostics, or Pokemon parsing layers.

## 2. Constraints

- Emulator: `trulymust/melonDS-switch-upscale`.
- Switch transport: Koi USB botbase 3.33 over USB.
- Bot implementation: Python on the PC.
- No capture-card dependency.
- No Lua script inside melonDS.
- RAM access is read-only from the bot's public API.
- Controller input is the only way the bot changes game state.
- No Qt UI in v0.1.
- Safety takes priority over reset speed.
- Unknown or contradictory game/Pokemon state fails closed.

## 3. Technical references

### melonDS Switch fork

`trulymust/melonDS-switch-upscale` identifies itself as melonDS 0.9.2. On ARM64 its JIT is enabled by default. In the Switch JIT path, melonDS allocates a backing memory block with `aligned_alloc()`, creates a mapped alias with `svcMapProcessCodeMemory`, and sets `NDS::MainRAM` to the MainRAM portion of that mapped block.

This gives us two useful views of the same emulated backing storage:

1. the heap-backed allocation created by melonDS;
2. the mapped alias used by JIT/fastmem.

For v0.1, heap-backed discovery is preferred because Koi exposes heap-relative reads directly. We do not need the exact JIT alias if we can reliably identify and read the corresponding backing storage.

Relevant source files:

- `src/NDS.h`
- `src/NDS.cpp`
- `src/ARMJIT_Memory.cpp`
- `src/frontend/switch/main.cpp`

### Koi USB botbase

Required primitives include:

- `peek` / `peekMulti` for heap-relative reads;
- `peekAbsolute` / `peekAbsoluteMulti`;
- `peekMain`;
- pointer reads;
- `getHeapBase`;
- `getMainNsoBase`;
- `getBuildID`;
- `getTitleID`;
- controller `press`, `release`, `click`, and `clickSeq`.

Game and hunt code must never emit Koi protocol strings directly.

### Pokebot NDS

`wyanido/pokebot-nds` supplies the HG/SS game-side research to port:

- game/language markers at DS `0x023FFE08` and `0x023FFE0F`;
- HG/SS regional offsets;
- dynamic anchor at `0x021D4158 + regional_offset`;
- starter data at `anchor + 0x1BC00`;
- 236-byte Gen IV party-record spacing;
- Gen IV checksum/parsing logic;
- Gen IV shiny test.

## 4. Architecture

```text
HGSS starter hunt
        |
        v
HGSS profile / state machine
        |
        +-------------------+
        |                   |
        v                   v
NDSMemory              DSController
        |                   |
        v                   v
MainRAM resolver        InputGate
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

Higher layers depend on lower layers only. Hunt code never knows Switch addresses or Koi command syntax.

## 5. Module layout

```text
pokebot-melonds/
+-- run_probe.py
+-- run_hgss_starters.py
+-- pokebot/
|   +-- transport/
|   |   +-- koi_usb.py
|   |   +-- protocol.py
|   +-- emulator/
|   |   +-- melonds.py
|   |   +-- mainram_resolver.py
|   |   +-- nds_memory.py
|   +-- input/
|   |   +-- ds_controller.py
|   |   +-- input_gate.py
|   +-- pokemon/
|   |   +-- gen4_crypto.py
|   |   +-- pk4.py
|   |   +-- shiny.py
|   +-- games/hgss/
|   |   +-- profile.py
|   |   +-- pointers.py
|   |   +-- states.py
|   |   +-- starters.py
|   +-- hunts/
|   |   +-- hgss_starters.py
|   +-- diagnostics/
|       +-- logger.py
|       +-- dumps.py
+-- probes/
|   +-- 00_usb_probe.py
|   +-- 01_input_probe.py
|   +-- 02_reset_probe.py
|   +-- 03_mainram_probe.py
|   +-- 04_hgss_anchor_probe.py
|   +-- 05_starter_watch_probe.py
|   +-- 06_starter_reset_probe.py
+-- tests/
|   +-- test_gen4_crypto.py
|   +-- test_pk4.py
|   +-- test_hgss_offsets.py
|   +-- test_mainram_translation.py
|   +-- fixtures/
+-- docs/superpowers/
```

The probe programs are permanent diagnostics, not throwaway scripts.

## 6. Koi USB transport

`KoiUSBTransport` owns USB and Koi protocol details.

Responsibilities:

- connect/disconnect;
- command/reply parsing;
- bounded timeouts and retries;
- heap-relative and absolute reads;
- title/build/heap/NSO metadata;
- controller press/release/click operations;
- cancellation/release of outstanding inputs on hold.

The public RAM interface is read-only. No `poke`, freeze, or RAM-write operation is exposed to hunt code.

## 7. NDS MainRAM translation

HG/SS addresses refer to emulated NDS memory, not Switch process addresses.

For ordinary DS main RAM:

```text
offset = ds_address - 0x02000000
host_backing_address = mainram_backing_base + offset
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

### 7.1 Resolver tiers

#### Tier A: heap-backed signature discovery — preferred

Search the melonDS heap allocation space first, using Koi heap-relative reads. The Switch JIT path creates the backing block with `aligned_alloc()`, so the emulated MainRAM storage originates from the heap even though melonDS later maps a JIT alias.

A candidate base is accepted only when several independent DS offsets validate simultaneously:

- recognized HG/SS game code at DS `0x023FFE08`;
- recognized language at DS `0x023FFE0F`;
- plausible value at `0x021D4158 + regional_offset`;
- valid HG/SS starter structures at the expected derived address when the starter screen is open.

The probe should scan coarsely for the near-end-of-4-MB game/language signature first, then derive a candidate base and perform the remaining validations. It must not repeatedly dump the entire heap.

#### Tier B: stable NSO-relative pointer or host pointer chain

While probing Tier A, determine whether the specific melonDS build exposes a stable module-relative pointer or pointer chain to `NDS::MainRAM` or its backing allocation.

If one remains stable across cold launches of the same build, use it as a fast resolver but continue validating the HG/SS signatures before accepting it.

#### Tier C: tiny melonDS bridge marker — fallback

If A/B are unreliable, maintain a minimally modified build of this exact fork exposing a stable read-only marker such as:

```text
POKEBOT_NDS_BRIDGE_V1
mainram_pointer
mainram_size
console_type
```

The Python-side `NDSMemory` API does not change if this fallback is adopted.

### 7.2 Resolver invalidation

Invalidate and re-resolve after:

- melonDS process restart;
- title/build ID change;
- impossible game/language values;
- anchor failure after bounded retries;
- USB reconnect where process metadata changed.

A soft reset may keep the same host backing base, but the mapping and game identity must still be revalidated before the next hunt decision.

## 8. HG/SS identification and regional offsets

Read game/language through `NDSMemory`.

```text
HeartGold
JP -0x3B08
EN  0x0000
FR +0x0020
IT -0x0060
DE -0x0020
ES +0x0020

SoulSilver
JP -0x3B08
EN  0x0000
FR +0x0020
IT -0x0060
DE -0x0020
ES +0x0040
```

Unsupported or contradictory values cause `GAME_ID_MISMATCH` and disable automation.

## 9. HG/SS starter pointers

For the detected region:

```text
anchor = read32(0x021D4158 + regional_offset)
starter_data = anchor + 0x1BC00
```

Read three records at 236-byte spacing.

Expected species:

- 152 Chikorita
- 155 Cyndaquil
- 158 Totodile

Pokebot NDS uses its raw-data path for these starter structures. The Python parser therefore supports an explicit raw-starter mode instead of assuming all Gen IV in-memory records use the encrypted-box-data path.

## 10. Pokemon validation and shiny logic

A record may affect a reset decision only after validation.

Validation includes:

- expected record size/layout;
- checksum success;
- expected species;
- structurally sane values;
- stability across repeated reads.

For accepted starter samples, PID, checksum, and species must agree across at least two consecutive reads.

Gen IV shiny value:

```text
sv = TID XOR SID XOR PID_high XOR PID_low
shiny = sv < 8
```

Any shiny starter enters `SHINY_HOLD`.

## 11. DS controller abstraction

HG/SS hunt code uses DS controls, not Switch controls.

Default mapping in this melonDS fork:

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

`ds.soft_reset()` sends an overlapping hold of:

```text
L + R + PLUS + MINUS
```

The reset probe determines a conservative reliable hold time before any speed optimization.

## 12. InputGate

Every controller command passes through one gate.

States:

```text
DISABLED
PROBE_ONLY
AUTOMATION
SHINY_HOLD
SAFETY_HOLD
```

On either hold:

- cancel queued sequences;
- release held buttons/sticks;
- reject all further hunt-generated input.

This independently prevents a later logic error from resetting a shiny.

## 13. Starter hunt state machine

```text
DISCONNECTED
    v
USB_CONNECTED
    v
MELONDS_IDENTIFIED
    v
MAINRAM_RESOLVED
    v
HGSS_IDENTIFIED
    v
LOAD_CONTINUE
    v
WAIT_STARTER_SCREEN
    v
READ_3_STARTERS
    v
VALIDATE_3_STARTERS
    +-- invalid/ambiguous --> SAFETY_HOLD
    +-- any shiny ---------> SHINY_HOLD
    +-- all valid non-shiny
            v
        SOFT_RESET
            v
       REVALIDATE
            |
            +-------------> repeat
```

Only explicit `ALL_VALID_NON_SHINY` may request a reset.

## 14. Reset/startup synchronization

Do not use a long blind sleep followed by unconditional button mashing.

After reset:

1. send the overlapping reset chord;
2. enter `RESETTING`;
3. stop further controller output;
4. observe RAM transition away from the old state;
5. wait for valid HG/SS identity again;
6. revalidate anchor/memory;
7. proceed through continue/startup;
8. interact with the starter machine only when the expected state is established.

Where no useful RAM flag has yet been identified, conservative timing is allowed during probing, but the next step must be validated before further input.

## 15. Manual verification mode

Before automatic resetting, `05_starter_watch_probe.py` runs with input disabled.

The user manually opens the starter selector. The probe repeatedly displays and validates all three starters.

This proves:

- MainRAM translation;
- HG/SS anchor;
- starter addresses;
- raw starter parsing;
- shiny calculation.

Automatic reset remains disabled until this is independently verified.

## 16. Permanent probes

### `00_usb_probe.py`

Reports botbase version, title ID, build ID, NSO base, and heap base. No game automation.

### `01_input_probe.py`

Manual A/B/X/Y, D-pad, Plus/Minus, L/R test.

### `02_reset_probe.py`

Tests only the HG/SS reset chord.

### `03_mainram_probe.py`

No controller output. Resolves backing MainRAM and reports host base, resolver tier/evidence, HG/SS version, and language.

### `04_hgss_anchor_probe.py`

Reports regional offset, anchor address/value, and starter data address.

### `05_starter_watch_probe.py`

Zero controller output. Displays species, PID, checksum, TID/SID, shiny value/state, nature, and IVs for all three starters.

### `06_starter_reset_probe.py`

One-cycle automation only:

```text
read -> validate -> all non-shiny -> one reset -> stop
```

A shiny or any uncertainty holds instead.

### `07_hgss_starter_hunter.py`

Continuous loop enabled only after probes 00-06 pass.

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

Rules:

```text
uncertain transport -> bounded retry
uncertain RAM mapping -> reacquire and prove
uncertain game state -> hold
uncertain Pokemon state -> hold
possible shiny -> hold
```

`USB_LOST`: disable input, bounded reconnect, re-read process metadata, re-resolve if identity changed, otherwise hold.

`MAINRAM_LOST`: disable input, rerun resolver, revalidate HG/SS, resume only on proof.

`GAME_ID_MISMATCH`: immediate `SAFETY_HOLD`.

`ANCHOR_INVALID`: bounded rereads then resolver retry; hold if unresolved.

`STARTER_DATA_INVALID`: bounded rereads; hold on timeout.

`STARTER_DATA_UNSTABLE`: hold if stabilization fails.

`RESET_TIMEOUT`: stop input, revalidate RAM/game, hold if no known state returns.

## 18. Diagnostics

Use JSONL for internal event logs and human-readable console output derived from them.

Example:

```json
{"t":123.456,"event":"state","from":"HGSS_IDENTIFIED","to":"WAIT_STARTERS"}
{"t":124.112,"event":"ram","addr":"0x021D4158","value":"0x0221AB00"}
{"t":130.445,"event":"starter","slot":1,"species":152,"pid":"8F32A106","shiny":false}
{"t":130.600,"event":"decision","value":"RESET"}
```

Every hold creates a support bundle containing at least:

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

Include botbase version, title/build ID, heap/NSO base, MainRAM backing base, resolver evidence, HG/SS version/language, anchor, starter base, raw/decoded starter reads, recent transitions/inputs, and exact hold reason.

## 19. Testing strategy

### Unit tests

Use fixtures to test without hardware:

- Gen IV checksum;
- raw starter parsing;
- encrypted PK4 parsing for later reuse;
- shiny boundary `SV 7` vs `SV 8`;
- IV decoding;
- HG/SS offsets;
- DS-to-host translation;
- state-machine decision table;
- InputGate refusal after hold.

### Hardware gates

1. 50 manual starter reads, zero bad accepted decodes.
2. 20 single-reset runs, 20/20 restart + reacquisition.
3. 100 automated cycles, zero false shiny/unsafe reset/stuck startup.
4. 500 automated resets, zero input after hold and zero corrupted records accepted.
5. Long soak with deliberate USB reconnect, melonDS restart, and bot restart tests.

Primary metrics:

```text
unsafe resets = 0
false shiny holds = 0
accepted invalid starter records = 0
input after hold = 0
```

## 20. v0.1 scope

Included:

- Koi USB transport;
- heap-first melonDS MainRAM resolver;
- DS address translation;
- HG/SS/language detection;
- Gen IV/raw starter parser;
- shiny calculation;
- DS controller abstraction;
- InputGate;
- probes 00-06;
- continuous HG/SS starter hunter;
- shiny/safety holds;
- JSONL logs and support bundles;
- session/reset counters.

Excluded:

- wild encounters;
- statics;
- eggs;
- auto capture;
- touch-driven menus;
- Qt UI;
- D/P/Pt;
- B/W and B2/W2;
- Discord;
- RNG tracking;
- savestate automation.

## 21. Acceptance criteria

1. Correct HG/SS + language detection after supported launches.
2. Reliable MainRAM backing discovery even if host addresses change.
3. Correct validated reads of Chikorita, Cyndaquil, and Totodile.
4. No reset unless all three pass checksum/species/stability validation and are non-shiny.
5. Immediate `SHINY_HOLD` on any shiny.
6. No hunt input after `SHINY_HOLD` or `SAFETY_HOLD`.
7. RAM/game revalidation after every reset.
8. Pass 500-reset safety soak with zero unsafe resets.
9. Complete diagnostic bundle on each hold.
10. User stop releases held controls and disables InputGate.

## 22. First implementation milestone

Implement and hardware-test these before starter parsing or automation:

```text
00_usb_probe.py
01_input_probe.py
02_reset_probe.py
03_mainram_probe.py
```

Only after all four pass should implementation continue to the HG/SS anchor, parser, watcher, and reset loop.

## 23. Future extension path

After v0.1 is stable, add separate planned increments for:

1. HG/SS wild encounters and safe fleeing;
2. HG/SS statics/gifts;
3. fishing/headbutt;
4. eggs;
5. Qt UI/statistics;
6. D/P/Pt profiles;
7. B/W and B2/W2 profiles.

All later work should reuse `KoiUSBTransport`, `NDSMemory`, `DSController`, `InputGate`, diagnostics, and common Pokemon parsing.