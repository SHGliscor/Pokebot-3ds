Pokebot3DS-CFW v0p43AR — Multi-Ball Capture Completion Validator

Run CAPTURE_OUTCOME_AUTHORITY_TEST.bat in an ordinary NON-SHINY wild battle on FIGHT / BAG / POKEMON / RUN.

Ball 1 is forced to a weak 1x non-Master Ball. Every Ball after that uses Best Ball selection.

A rethrow is allowed only after the game itself consumes a BAG touch and the embedded Bag reaches state 1. Firmware acknowledgement, command-gate return, PK6 persistence and the 0x081FB518 token are not rethrow authority.

The test continues through repeated failed captures (up to 10 Balls). If a Ball captures the target, it runs the guarded post-capture flow toward overworld.

Initial shiny: refused.
Master Ball: forbidden.
Game RAM writes: none.
Live hunt logic: unchanged.
