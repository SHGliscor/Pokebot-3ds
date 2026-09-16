# Pokebot Gen4/5 PC — melonDS proof

Temporary PC backend for Generation IV/V automation while the Switch CFW backend
is unavailable.

## First target

- Pokemon HeartGold Europe v10
- SHA1: `EB47AB4BA0326AE842135F62C7EC68CF85C9785F`
- emulator: pinned melonDS-Lua fork
- authority: checksum-valid Pokemon structures in emulated ARM9 RAM
- game-memory writes: none

## Why this architecture

The game logic does not know or care whether the emulator is on PC or Switch.
It talks to an `EmulatorBackend` with operations such as RAM reads, key pulses
and reset. The current backend is melonDS on Windows; a Switch backend can
replace it later.

For HeartGold Europe we deliberately do **not** copy pret's US runtime
addresses. pret is used for structures, constants, map/encounter data and game
logic. Runtime Pokemon discovery uses a full 4 MiB ARM9 RAM snapshot plus the
Gen-IV checksum/encryption rules.

## Sources

Gen IV data/structure source:
- pret/pokeheartgold
- pret/pokeplatinum
- pret/pokediamond

pret currently does not host a main-series Gen-V decomp. The Gen-V adapter is
kept separate so Black/White data can come from a compatible decomp/tooling
source without coupling the hunt logic to it.

The PK4 parser mirrors the documented/decompiled HGSS layout:
- BoxPokemon size: 0x88
- party Pokemon size: 0xEC
- four shuffled 0x20-byte blocks
- box encryption seed: checksum
- encryption LCRNG: seed = seed * 1103515245 + 24691
- shiny test: TID ^ SID ^ PIDlo ^ PIDhi < 8

## Running the proof

1. Start the packaged `melonDS.exe`.
2. Boot your clean HeartGold Europe ROM.
3. In melonDS load/run `lua/pokebot_bridge.lua`.
4. Leave melonDS running.
5. In a Command Prompt from this folder:

```
python pokebot_gen45.py ping
python pokebot_gen45.py scan
```

For the starter-address proof, save in Elm's lab immediately before choosing:

```
python pokebot_gen45.py starter-probe
```

The tool takes a RAM snapshot, asks you to choose one starter, then takes a
second snapshot. Any newly-created checksum-valid Chikorita/Cyndaquil/Totodile
is printed with PID, shiny value, nature, ability, IVs and Hidden Power.

## Current scope

v0p1 proves:
- direct ARM9 bulk RAM read from melonDS
- direct button forcing through the melonDS Lua API
- emulator reset
- checksum-valid PK4 parsing
- full-RAM PK4 discovery with no region-specific address
- starter before/after probe
- pret-backed Gen-IV species table

Next after the hardware/software probe passes:
- lock the live party/starter addresses for HeartGold Europe
- automate the complete starter reset loop
- session/lifetime statistics
- party viewer
- wild encounter reader
- map + encounter database import from pret
- Platinum
- Gen V PK5 parser/backend profile
