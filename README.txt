Pokebot3DS-CFW v0p43EJ HF94 — DexNav Fluid Steering + Fast Dialogue

HF94 is based directly on HF93.

DexNav tutorial Poochyena changes:
- Keeps the HF89-probed live DexNav target block at 0x08D3B560.
- Keeps HF93 Fang display and identity gate: Poochyena #261, Lv.5, plus Thunder Fang / Ice Fang / Fire Fang.
- Keeps automatic reset/retry for accidental non-target Route 101 encounters.
- Keeps accidental-shiny protection: a shiny random encounter is held instead of reset over.
- Rival/tutorial dialogue now uses adaptive fast A presses: 650 ms normal cadence, 850 ms cleanup cadence, and stops immediately when RAM proves the DexNav target has spawned.
- CPAD no longer deliberately tapers to a slow crawl near contact.
- Longer RAM-guided correction holds reduce the number of unavoidable pulse boundaries.
- Removes the extra 120 ms post-correction battle wait; battle is checked immediately at the next steering read.

UI / telemetry:
- The raw main-MT RNG tracker has been removed from the dashboard.
- The persistent RNG telemetry worker is disabled during normal use. This frees UDP/RAM bandwidth for hunt-state reads and DexNav steering.
- The underlying RNG tracker modules remain packaged for future diagnostics if needed.

Firmware:
- No new boot.firm protocol is required over HF93.
- The bundled CPAD-capable boot.firm remains required for automatic DexNav Circle Pad control.
- Keep standard Rosalina InputRedirection OFF and use the Pokebot3DS bridge/controller mode.
