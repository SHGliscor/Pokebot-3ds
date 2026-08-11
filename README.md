# ORAS RAM Bot

Capture-free Pokémon Omega Ruby and Alpha Sapphire automation using Luma3DS
InputRedirection for controls and checksum-valid PK6 memory reads for encounter
identity and shiny authority.

## Development Status

![Overall Progress](https://img.shields.io/badge/Overall%20Progress-55%25-2ea44f)
![Current Build](https://img.shields.io/badge/Current%20Build-v0p36-1688f0)
![Platform](https://img.shields.io/badge/Platform-New%20Nintendo%203DS-e60012)
![Authority](https://img.shields.io/badge/Encounter%20Authority-PK6%20RAM-7b2cbf)

`███████████░░░░░░░░░ 55%`

### Hardware-validated

- [x] Luma3DS InputRedirection UDP controller input
- [x] Alpha Sapphire 1.4 process discovery through Luma3DS GDB
- [x] Checksum-valid stored PK6 decryption and parsing
- [x] RAM-authoritative species, PID, Encryption Constant and shiny value
- [x] Treecko capture-free starter resets
- [x] Torchic capture-free starter resets
- [x] Mudkip capture-free starter resets
- [x] Separate calibrated starter-selection macros
- [x] Route 101 normal left/right wild encounter loop
- [x] RAM-confirmed battle entry and exit states
- [x] Normal wild battle escape using native touchscreen coordinates
- [x] Immediate input hold for shinies, invalid RAM and repeated identities
- [x] Low-lag bounded RAM reads with no continuous background polling

### Implemented

- [x] Unlimited starter and Route 101 production hunts
- [x] Finite starter and wild reliability tests
- [x] Persistent lifetime, phase, species and shiny statistics
- [x] Exact-once encounter identity protection
- [x] Automatic wild-area resolution using PK6 met location
- [x] 66 packaged Alpha Sapphire wild-location profiles
- [x] Wild-area groups for land, water, caves, desert and special locations
- [x] Fixed-height scrollable encounter rosters
- [x] Collapsible embedded n3DS_view panel
- [x] Clean location-only wild-area labels
- [x] One-shot six-slot party RAM reader
- [x] Party sprites with hover details for IVs, EVs, nature, ability, held item,
  Pokérus, Hidden Power, PID and shiny value
- [x] Local normal and shiny ORAS sprites for National Dex 1–721
- [x] Shiny sound alert
- [x] Support ZIP export and optional 4 FPS recording
- [x] Hosted read-only dashboard publisher foundation
- [x] Fail-closed controls: unexpected state or invalid data stops input

### In progress — hardware validation required

- [ ] Hold B + left/right running encounters
- [ ] Clockwise spin encounters
- [ ] Hold B + clockwise spin encounters
- [ ] Stationary Acro Bike bunny-hop encounters
- [ ] Moving Acro Bike bunny-hop encounters
- [ ] Reusable grass and ordinary land-route movement profiles
- [ ] Cave and interior movement profiles
- [ ] Sand and desert movement profiles
- [ ] Surf and open-water movement profiles
- [ ] Automatic movement assignment after met-location resolution
- [ ] Live six-slot party reader validation on occupied and empty parties
- [ ] Reliability testing with n3DS_view enabled and disabled

### Planned ORAS support

- [ ] Fishing encounters
- [ ] Rock Smash encounters and obstacle recovery
- [ ] Horde encounters
- [ ] DexNav encounters
- [ ] Underwater areas
- [ ] Soaring movement-trigger encounters
- [ ] RAM-authoritative Static encounters
- [ ] Gift Pokémon and fossils
- [ ] Breeding and egg hatching
- [ ] Story Latios/Latias encounter
- [ ] Random Starter mode
- [ ] Shiny Discord notifications
- [ ] Full cross-mode regression suite
- [ ] Installer and public release package

### Future game support

- [ ] Pokémon X and Y using the Generation VI RAM and sprite architecture
- [ ] Pokémon Sun, Moon, Ultra Sun and Ultra Moon using Generation VII models
- [ ] Generation II Virtual Console integration
- [ ] Generation IV/V feasibility research using RTCOM

## Safety model

- RAM is the sole authority for encounter identity and shiny status.
- The bot never writes Pokémon data or game memory.
- Controller input is sent only through Luma3DS InputRedirection UDP.
- Every combined direction/B action is fully released before a RAM read.
- Shiny Pokémon, invalid checksums, unknown states and repeated encounter
  identities stop all further automated input.
- Unvalidated terrain modes perform one encounter and stop after safely leaving
  a normal battle. Only the proven Route 101 normal pendulum is unlimited.

## Current release

The latest development build is `v0p36_RAMPartyHoverCards`.

It is a slim runtime package containing one launcher, the required bot/viewer
files, the Alpha Sapphire encounter tables and local Generation VI ORAS sprite
assets. Tests, historical patch notes and legacy launchers are excluded from
the release ZIP.

## Technical scope

| Component | Current state |
|---|---|
| Input | Luma3DS InputRedirection UDP |
| Memory transport | Luma3DS GDB connection |
| Supported game | Alpha Sapphire 1.4 |
| Encounter format | Stored Generation VI PK6 |
| Shiny authority | TID/SID/PID shiny value from validated RAM |
| Wild location | PK6 met-location resolver |
| Sprites | Local Gen VI ORAS normal/shiny sprites |
| Background polling | Disabled |
| Memory writes | None |

## Project direction

The immediate goal is to validate reusable movement profiles by terrain type
instead of building a separate macro for every route. Once grass, cave,
desert, Surf and special movement are reliable, the same RAM-authoritative
encounter system can cover the packaged Alpha Sapphire location tables without
visual or OCR shiny detection.
