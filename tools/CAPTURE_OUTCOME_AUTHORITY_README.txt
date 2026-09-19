Pokebot3DS-CFW v0p43AO - Capture Epoch Authority Validator
===========================================================

Purpose
-------
Validate the new read-only capture-vs-breakout candidate before it is allowed to
control the live shiny Auto-Catch path.

Hardware evidence currently available
-------------------------------------
Successful capture, no level-up:
  0x081FB518[16] stayed exactly equal to its pre-throw value until teardown.

Successful capture with a level-up delay:
  0x081FB518[16] again stayed exactly equal to its pre-throw value through the
  long stale-battle interval, then the battle tore down normally.

Operator-confirmed real breakout:
  0x081FB518[16] changed before the visible command menu returned.
  0x081FB528[16] retained the pre-throw value in the same snapshots.

The validator therefore requires ALL of:
  * current 16-byte epoch differs from its exact pre-throw token;
  * change is stable for multiple samples;
  * battle is still active;
  * FLOW exactly equals the pre-throw battle FLOW;
  * command menu is stable;
  * original opponent PK6 identity still matches.

Only then may another diagnostic Ball be attempted.

Safety
------
* Refuses to run if the initial opponent is shiny.
* Master Ball is forbidden regardless of target.
* No game RAM writes.
* Live hunt code is unchanged from v0p43AJ/AM baseline.
* Ball 1 is forced to a weak 1x Ball to encourage a breakout.
* Ball 2+ uses normal non-Master ranking.

Run
---
1. Start an ordinary NON-SHINY single wild battle.
2. Leave the game on FIGHT / BAG / POKEMON / RUN.
3. Run CAPTURE_OUTCOME_AUTHORITY_TEST.bat.
4. Let it proceed automatically.
5. Upload capture_epoch_authority_*.json and matching .txt from the support folder.

A high-value pass is:
  weak Ball 1 breaks -> epoch authority proves breakout -> Ball 2 opens Bag and
  throws -> eventual capture -> post-capture cleanup/field.
