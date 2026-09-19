Pokebot3DS-CFW Capture Lifecycle Mapper v0p43AM

Purpose
-------
Capture the complete RAM lifecycle from battle command menu through one Ball throw and either:
- a visually confirmed real breakout, or
- successful capture -> Pokédex/nickname/Box -> stable overworld.

IMPORTANT
---------
This is an evidence mapper. It does not change live hunt behavior.
Master Ball is forbidden.
No game RAM writes are performed.

What changed in v0p43AM
-----------------------
The first successful v0p43AL trace proved that a successful Quick Ball capture can retain stale battle/menu RAM for around 20 seconds after Ball use. It also proved:
- Pokédex flow 0x1735 can be safely continued with B once settled.
- nickname prompt and Box message can share flow 0x176B; the outer object pointer transition is the useful authority.
- large evidence reads must be split into <=0x200-byte bridge requests.

Run
---
1. Start a normal single wild battle and wait at FIGHT/BAG/POKEMON/RUN.
2. Run CAPTURE_LIFECYCLE_MAPPER.bat.
3. The mapper throws exactly one non-Master best Ball.
4. If it asks whether FIGHT/BAG/POKEMON/RUN is visibly ready after a breakout, answer Y only for a real visible breakout.
5. Otherwise answer N and let it continue.
6. Upload the generated capture_lifecycle_mapper_*.json and .txt.

Next evidence wanted
--------------------
A genuine breakout trace is the highest priority. One corrected successful trace is also useful because v0p43AM now captures the broad RAM blocks that v0p43AL could not.
