# Pokebot nds-bootstrap PoC v0p2

This branch is the isolated Generation IV transport experiment for Pokebot. It
builds a separate Nightly-slot nds-bootstrap and does not alter the normal
TWiLight Menu++ Release bootstrap.

## Hardware baseline

- Console: New Nintendo 3DS
- Test game: Pokemon HeartGold (Europe)
- ROM SHA1: `EB47AB4BA0326AE842135F62C7EC68CF85C9785F`
- nds-bootstrap:
  `1585a242c80f78fc67cb49a7bfe55a24cc362785`
- RTCom/TwlBg reference:
  `shocoman/Analog-Controls-for-NDS-Games-on-3DS@58a5afd55275a565fe7ee0ad40e927df0e7b514e`

v0p1 was hardware-validated through HeartGold's Continue screen and into the
overworld.

## v0p2 milestone

v0p2 adds the first live New-3DS RTCom path:

```
nds-bootstrap ARM7 bootloader
        |
        | upload RTCom microcode before HeartGold starts
        v
TWL ARM11 / TwlBg
        |
        | ZL/ZR/Nub -> legacy RTC extension registers
        v
nds-bootstrap ARM7 cardengine
        |
        | rising ZL edge
        v
synthetic DS A press
        |
        v
HeartGold
```

The RTCom installation is fail-open. If the ARM11 upload or TwlBg patch fails,
the bootloader continues into HeartGold rather than deliberately stopping the
game.

## Hardware test

Keep your known-good v0p1 file backed up.

1. Replace only `/_nds/nds-bootstrap-nightly.nds` with the v0p2 file.
2. Keep HeartGold configured to use the Nightly bootstrap.
3. Boot to the overworld.
4. Confirm normal physical controls still work.
5. Face a sign, NPC, PC, door prompt, or another object where a normal **A**
   press has an obvious effect.
6. Press **ZL** on the New 3DS once.

Expected result:

`ZL -> one short synthetic DS A press`

Holding ZL should not spam A. The bridge reacts only to the rising edge.

If HeartGold boots but ZL does nothing, that is still useful: it means the
normal nds-bootstrap/cardengine path remains sound and the failure is inside
the RTCom/TwlBg side.

## DS-side mailbox retained from v0p1

The four-word mailbox remains at `0x027FFA30..0x027FFA3C` with:

- `PING`
- timed `KEY_SET`
- `RELEASE_ALL`
- aligned read-only `READ32` in `0x02000000..0x023FFFFC`

No arbitrary game-RAM write command exists.

## ARM7 space trade-off

The normal ARM7 cardengine is packed into a `4 KiB + 0x80` linker region.
The dedicated Pokebot build replaces the ARM7 TWiLight in-game-menu helper
with a stub rather than enlarging that reserved region.

This affects only the Pokebot Nightly test bootstrap.

## What v0p2 does not do yet

v0p2 does not expose the mailbox to the Windows bot over Wi-Fi. Its purpose is
to establish that the complete DS/TWL hardware chain works first.

After ZL -> A is confirmed, the next transport milestone is a bidirectional
ARM11/ARM7 command channel suitable for `PING`, `READ32`, and timed input
commands from the PC.
