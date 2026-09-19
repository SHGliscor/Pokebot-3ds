Pokebot3DS-CFW — Pokémon X/Y RAM Probe

Purpose
-------
This is the first fail-closed XY backend validation. It does not send controller input.
It verifies GAME_INFO is Pokémon X or Y before reading any XY-specific RAM addresses.

Run
---
Double-click tools\XY_RAM_PROBE.bat and enter the 3DS IP address, or run:
  tools\XY_RAM_PROBE.bat 192.168.x.x

Expected
--------
- Pokémon X: process kujira-1, title 0004000000055D00
- Pokémon Y: process kujira-2, title 0004000000055E00
- TID/SID read from 0x08C79C3C
- Party slot 1 base 0x08CE1CF8
- 484-byte party slot stride
- PK6 stored size 232 bytes

The probe prints each checksum-valid party Pokémon and writes xy_ram_probe_*.json
in the bot root. Send that JSON back after testing.

Reference basis
---------------
Initial XY live-RAM addresses are from zaksabeast/PokeReader Gen 6 reader.
PK6 structure/decryption semantics use the bot's shared Gen 6 parser, cross-checked
against PKHeX Gen 6 PKM conventions. Hardware validation remains authoritative.
