Pokebot3DS-CFW v0p43AE - Route 113 Ash Grass Proof Probe
==========================================================

Purpose
-------
Prove that the ash-covered terrain on Route 113 is wild-encounter terrain
without weakening the production movement safety classifier.

How to use
----------
1. Start Pokemon Alpha Sapphire and stand on Route 113.
2. For the first run, stand WELL INSIDE the ash-covered grass patch.
3. Run ROUTE113_ASH_GRASS_PROOF.bat.
4. Choose I = interior ash-covered encounter grass.
5. Choose Y for the encounter proof.
6. Use only the physical 3DS controls and move around inside that same visible
   ash-grass patch until a wild encounter begins.
7. The probe detects the battle transition from RAM and stops automatically.
8. Upload both output files from:
      %APPDATA%\Pokebot-3DS\support\
      route113_ash_grass_proof_*.json
      route113_ash_grass_proof_*.txt

Recommended additional samples
------------------------------
Run it again on:
- E = edge of the ash-covered grass patch
- P = nearby ordinary path/non-grass

Raw permission proof
--------------------
The probe searches your PC for the previous whole-game compiler output named:
  alpha_sapphire_world.sqlite

If found, it discovers the raw-tile table by schema and records the exact raw
permission for the Route 113 tile under the player and all four neighbours.
If the file is no longer present, the probe still records the exact Route 113
zone/grid/region/local tile and the RAM-observed encounter transition. That is
useful proof of the missing production classification, but a raw-permission
mapping step will still be needed before promoting the terrain into movement
authority.

Safety
------
- No game RAM writes.
- No controller input is sent by this probe.
- It never runs, catches, resets, or leaves a battle.
- Encounter movement is manual using the physical 3DS controls.
- The live Auto-Catch and movement code are not modified.
- boot.firm is unchanged.
