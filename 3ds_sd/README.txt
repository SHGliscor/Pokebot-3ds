Pokebot3DS-CFW — 3DS SD files
=============================

This package contains ONE unified Pokebot-Luma boot.firm for the normal desktop bot.
Do not install a separate New 3DS program or a second boot.firm.

Included boot.firm:
  Pokebot-Luma v0p7-n3ds-fb1-cpad
  SHA256: 1b0cb91d9816ca805955dff41debdddd28fa47c39b43a94f5b0407a5311564f8

The firmware exposes the shared UDP/4952 services used by Pokebot3DS-CFW:
- read-only RAM bridge
- acknowledged HID/touch controller
- read-only top-screen framebuffer INFO/READ commands 11-12
- acknowledged Circle Pad pulse command 13

Command 13 is required by HF77's automatic DexNav tutorial Poochyena sneak.
The Circle Pad returns to neutral after every bounded pulse and RELEASE_ALL.

The firmware also retains the recovered New 3DS latency changes (0x20 worker
priority, non-blocking socket polling and 2 ms idle yield). These stay in the
same unified firmware path.

Install:
1. Back up your existing SD-root boot.firm.
2. Copy 3ds_sd\boot.firm to the root of the 3DS SD card as boot.firm.
3. Copy the correct title folder from 3ds_sd\luma\titles\ if code.ips is required.
4. Open Rosalina -> Pokebot3DS Bridge -> Enable Both.
5. Keep standard Rosalina InputRedirection OFF.
6. Run the normal RUN_BOT.bat on the PC.

See FIRST_CPAD_TEST.txt for the HF77 hardware test and UNIFIED_FIRMWARE.txt for provenance.
