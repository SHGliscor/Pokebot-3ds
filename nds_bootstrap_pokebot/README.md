# Pokebot nds-bootstrap PoC v0p1

This directory builds a separate experimental nds-bootstrap binary for the
Pokebot NDS transport work. It does not modify the normal release build in
TWiLight Menu++ unless you deliberately copy the test file over the Nightly
bootstrap slot.

## Pinned target

- Console: New Nintendo 3DS
- Test game: Pokemon HeartGold (Europe)
- ROM SHA1: `EB47AB4BA0326AE842135F62C7EC68CF85C9785F`
- nds-bootstrap commit:
  `1585a242c80f78fc67cb49a7bfe55a24cc362785`

## v0p1 scope

The first build adds only the DS-side primitives needed for the transport:

- a small mailbox at `sharedAddr[9..15]`;
- `PING`;
- timed `KEY_SET` with automatic release;
- `RELEASE_ALL`;
- aligned, read-only `READ32` inside `0x02000000..0x023FFFFC`;
- injection through nds-bootstrap's existing generic key-input patch hook.

There are no RAM writes.

The key fail-safe is deliberately strict: requests are clamped to at most
120 VBlanks (about two seconds), and `ARG1=0` defaults to two VBlanks.

## Important limitation

v0p1 is the **DS-side half** of the bridge. It does not yet contain the live
ARM11/PC transport. The next stage connects this mailbox to the New 3DS
TWL ARM11 side and then exposes the commands to the PC.

This separation is intentional: it lets us compile and validate the
nds-bootstrap changes before adding the much more sensitive TwlBg/RTCom
payload.

## Build

GitHub Actions builds this branch remotely using the same devkitARM container
family used by upstream nds-bootstrap. The resulting artifact contains:

- `nds-bootstrap-pokebot-v0p1.nds`
- `INSTALL/_nds/nds-bootstrap-nightly.nds`
- this README
- `PROTOCOL.md`
- `BUILD_MANIFEST.txt`

## Installing for a later hardware test

Do not overwrite your normal **release** bootstrap.

When the ARM11 transport is ready for testing:

1. Back up `/_nds/nds-bootstrap-nightly.nds` from the SD card if it exists.
2. Copy `INSTALL/_nds/nds-bootstrap-nightly.nds` from the artifact to
   `/_nds/nds-bootstrap-nightly.nds`.
3. In TWiLight Menu++ per-game settings for HeartGold, select **Nightly**
   nds-bootstrap.
4. Keep the normal Release bootstrap selected for every other game.

Restoring the backed-up Nightly file returns TWiLight Menu++ to its previous
state.

## Source strategy

The repository does not vendor nds-bootstrap. The workflow clones the exact
pinned upstream commit and runs `apply_pokebot_patch.py`. This makes every
change reviewable and prevents an upstream update from silently changing the
test binary.
