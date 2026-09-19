Pokebot3DS-CFW v0p42ZV - POST-CAPTURE MAPPER PROBE
===================================================

PURPOSE
-------
Map the real Alpha Sapphire 1.4 post-capture RAM states using ORDINARY catches.
No shiny is required. The live shiny Auto-Catch state machine is NOT changed.

CONFIRMED SCREEN ORDER
----------------------
REGISTERED SPECIES:
  Catch -> Nickname Yes/No -> No -> sent-to-Box/post-capture message -> Field

UNREGISTERED SPECIES:
  Catch -> Pokedex registration -> A -> Nickname Yes/No -> No
        -> sent-to-Box/post-capture message -> Field

There is no separate Pokédex "exit" step in the unregistered branch: one A on
Pokédex advances directly to the nickname prompt.

SAFETY / AUTHORITY
------------------
- Alpha Sapphire 1.4 only for these probes.
- READS RAM and QUERYs mapped CROs. DOES NOT WRITE game RAM.
- Uses acknowledged Pokebot-Luma controller pulses.
- Every expected visible screen is operator-confirmed before the probe advances.
- If an expected transition is not visible, the probe stops and saves evidence
  instead of guessing another input.

TEST 1 - REGISTERED SPECIES
---------------------------
1. Run the bridge/bot normally so the saved 3DS IP is correct.
2. Manually catch a normal species ALREADY registered in the Pokedex.
3. Leave the game on the Nickname Yes/No screen with Yes selected.
4. Run POST_CAPTURE_PROBE_REGISTERED.bat.
5. The probe snapshots Nickname/Yes, sends DOWN, snapshots Nickname/No, asks
   you to confirm the cursor moved, then sends A.
6. It snapshots the transition to the sent-to-Box/post-capture message.
7. After you confirm that message is visible, it sends one A and maps Field.

TEST 2 - UNREGISTERED SPECIES
-----------------------------
1. Manually catch a normal species NOT registered in the Pokedex.
2. Leave the game on the FIRST Pokedex registration screen.
3. Run POST_CAPTURE_PROBE_UNREGISTERED.bat.
4. The probe snapshots Pokédex, sends ONE A, takes fast transition snapshots,
   then asks you to confirm the Nickname Yes/No prompt appeared.
5. It then maps the same Nickname -> No -> Box message -> Field path as the
   registered probe.

OUTPUT
------
Each run writes files under:
  %APPDATA%\Pokebot-3DS\support\

Names:
  post_capture_mapper_registered_YYYYMMDD_HHMMSS.json
  post_capture_mapper_unregistered_YYYYMMDD_HHMMSS.json

The JSON records:
- operator-verified screen labels
- exact acknowledged controller inputs
- battle/global anchor values
- bounded snapshots around the known battle RAM anchors
- bounded heap-pointer snapshots
- loaded CRO evidence at full checkpoints
- fast snapshots immediately after Pokédex A / Nickname A / Box A
- stable byte changes between checkpoints

Upload BOTH JSON files after one successful registered test and one successful
unregistered test. Those two traces are what we will use to derive RAM gates for
POKEDEX, NICKNAME, BOX MESSAGE and FIELD before combining them into Auto-Catch.
