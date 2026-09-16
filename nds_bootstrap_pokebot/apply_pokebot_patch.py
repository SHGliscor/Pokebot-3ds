#!/usr/bin/env python3
"""Apply the Pokebot NDS proof-of-concept patch to a pinned nds-bootstrap tree.

This patch is intentionally small and NTR-only for v0p1:
- reserves sharedAddr[9..12] as a four-word Pokebot mailbox;
- adds PING, timed KEY_SET/RELEASE_ALL and read-only READ32;
- merges injected DS key bits into nds-bootstrap's existing key-input hook;
- forces that generic key-input hook on for this dedicated build;
- replaces the ARM7 TWiLight in-game-menu helper with a stub to reclaim
  cardengine space. The normal/release bootstrap is not modified.

Target upstream commit:
1585a242c80f78fc67cb49a7bfe55a24cc362785
"""

from __future__ import annotations

import argparse
from pathlib import Path

PINNED_COMMIT = "1585a242c80f78fc67cb49a7bfe55a24cc362785"

HEADER = r'''#ifndef POKEBOT_BRIDGE_H
#define POKEBOT_BRIDGE_H

#include <nds/ndstypes.h>

#define POKEBOT_PROTOCOL_VERSION 0x00010001u

/*
 * SDK1 card-engine shared block starts at 0x027FFA0C.
 * Slots 9..12 map to 0x027FFA30..0x027FFA3C.
 * 0x027FFA40 is UNPATCHED_FUNCTION_LOCATION, so v0p1 must not extend past 12.
 */
#define PB_MB_CONTROL 9
#define PB_MB_COMMAND 10
#define PB_MB_DATA0   11
#define PB_MB_DATA1   12

#define PB_TAG_BASE 0x504B4200u /* "PKB" + one-byte state */
#define PB_TAG_MASK 0xFFFFFF00u

#define PB_STATE_READY 0x01u
#define PB_STATE_REQ   0x02u
#define PB_STATE_BUSY  0x03u
#define PB_STATE_DONE  0x04u
#define PB_STATE_ERR   0x05u

#define PB_CONTROL(state) (PB_TAG_BASE | (state))

#define PB_PONG 0x504F4E47u

#define PB_CMD_PING        0x00000001u
#define PB_CMD_KEY_SET     0x00000002u
#define PB_CMD_RELEASE_ALL 0x00000003u
#define PB_CMD_READ32      0x00000010u

#define PB_ERR_BAD_COMMAND 0x00000001u
#define PB_ERR_BAD_ADDRESS 0x00000002u

void pokebot_bridge_tick(volatile u32 *shared);
void pokebot_bridge_apply_keys(u16 *keyInput, u16 *extKeyInput);

#endif
'''

SOURCE = r'''#include <nds/ndstypes.h>

#include "pokebot_bridge.h"

/* v0p1 synthesizes only the ten REG_KEYINPUT buttons. */
static volatile u16 injectedKeys;
static volatile u16 injectedKeyFrames;

static void release_all(void) {
    injectedKeys = 0;
    injectedKeyFrames = 0;
}

void pokebot_bridge_tick(volatile u32 *shared) {
    if (injectedKeyFrames && --injectedKeyFrames == 0)
        injectedKeys = 0;

    if ((shared[PB_MB_CONTROL] & PB_TAG_MASK) != PB_TAG_BASE) {
        release_all();
        shared[PB_MB_COMMAND] = 0;
        shared[PB_MB_DATA0] = 0;
        shared[PB_MB_DATA1] = POKEBOT_PROTOCOL_VERSION;
        shared[PB_MB_CONTROL] = PB_CONTROL(PB_STATE_READY);
        return;
    }

    if (shared[PB_MB_CONTROL] != PB_CONTROL(PB_STATE_REQ))
        return;

    const u32 command = shared[PB_MB_COMMAND];
    const u32 data0 = shared[PB_MB_DATA0];
    u32 data1 = shared[PB_MB_DATA1];

    shared[PB_MB_CONTROL] = PB_CONTROL(PB_STATE_BUSY);

    if (command == PB_CMD_PING) {
        shared[PB_MB_DATA0] = PB_PONG;
        shared[PB_MB_DATA1] = POKEBOT_PROTOCOL_VERSION;
        shared[PB_MB_CONTROL] = PB_CONTROL(PB_STATE_DONE);
        return;
    }

    if (command == PB_CMD_KEY_SET) {
        if (data1 == 0)
            data1 = 2;
        else if (data1 > 120)
            data1 = 120;

        injectedKeys = (u16)(data0 & 0x03FFu);
        injectedKeyFrames = (u16)data1;
        shared[PB_MB_DATA0] = injectedKeys;
        shared[PB_MB_DATA1] = data1;
        shared[PB_MB_CONTROL] = PB_CONTROL(PB_STATE_DONE);
        return;
    }

    if (command == PB_CMD_RELEASE_ALL) {
        release_all();
        shared[PB_MB_DATA0] = 0;
        shared[PB_MB_DATA1] = 0;
        shared[PB_MB_CONTROL] = PB_CONTROL(PB_STATE_DONE);
        return;
    }

    if (command == PB_CMD_READ32) {
        if ((data0 & 3u) || data0 < 0x02000000u || data0 > 0x023FFFFCu) {
            shared[PB_MB_DATA0] = PB_ERR_BAD_ADDRESS;
            shared[PB_MB_CONTROL] = PB_CONTROL(PB_STATE_ERR);
            return;
        }

        shared[PB_MB_DATA0] = *(volatile u32 *)data0;
        shared[PB_MB_DATA1] = POKEBOT_PROTOCOL_VERSION;
        shared[PB_MB_CONTROL] = PB_CONTROL(PB_STATE_DONE);
        return;
    }

    shared[PB_MB_DATA0] = PB_ERR_BAD_COMMAND;
    shared[PB_MB_CONTROL] = PB_CONTROL(PB_STATE_ERR);
}

void pokebot_bridge_apply_keys(u16 *keyInput, u16 *extKeyInput) {
    (void)extKeyInput;
    *keyInput &= (u16)~(injectedKeys & 0x03FFu);
}
'''

IGM_STUB = r'''/*
 * Pokebot nds-bootstrap v0p1 space trade-off.
 *
 * The stock ARM7 cardengine is packed into a 4 KiB + 0x80 region. The
 * Pokebot mailbox/input reader needs some of that space, so the dedicated
 * test build gives up the ARM7 TWiLight in-game-menu helper. This does not
 * affect the normal release bootstrap.
 */
void inGameMenu(void) {
}
'''


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one patch anchor, found {count}: {old!r}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("tree", type=Path, help="Path to the nds-bootstrap checkout")
    args = parser.parse_args()

    root = args.tree.resolve()
    cardengine = root / "retail/cardengine/arm7/source/cardengine.c"
    patch_arm9 = root / "retail/bootloader/source/arm7/patch_arm9.c"
    in_game_menu = root / "retail/cardengine/arm7/source/inGameMenu.c"
    bridge_h = root / "retail/cardengine/arm7/include/pokebot_bridge.h"
    bridge_c = root / "retail/cardengine/arm7/source/pokebot_bridge.c"

    for required in (cardengine, patch_arm9, in_game_menu):
        if not required.is_file():
            raise FileNotFoundError(required)

    bridge_h.write_text(HEADER, encoding="utf-8")
    bridge_c.write_text(SOURCE, encoding="utf-8")
    in_game_menu.write_text(IGM_STUB, encoding="utf-8")

    replace_once(
        cardengine,
        '#include "tonccpy.h"\n',
        '#include "tonccpy.h"\n#include "pokebot_bridge.h"\n',
    )

    replace_once(
        cardengine,
        'void myIrqHandlerVBlank(void) {\n',
        'void myIrqHandlerVBlank(void) {\n\tpokebot_bridge_tick(sharedAddr);\n',
    )

    replace_once(
        cardengine,
        '\tu32 dst = (u32)extKeyInputDst;\n',
        '\tpokebot_bridge_apply_keys(&keyInput, &extKeyInput);\n\n\tu32 dst = (u32)extKeyInputDst;\n',
    )

    replace_once(
        patch_arm9,
        '\tif (buttonsRemapped) {\n\t\tpatchKeyInputs(ndsHeader, moduleParams);\n\t}\n',
        '\t/* Dedicated Pokebot build: always install the generic key hook. */\n'
        '\t(void)buttonsRemapped;\n'
        '\tpatchKeyInputs(ndsHeader, moduleParams);\n',
    )

    print("Pokebot NDS v0p1 patch applied")
    print(f"Pinned upstream: {PINNED_COMMIT}")
    print("Mailbox: sharedAddr[9..12] / 0x027FFA30..0x027FFA3C")
    print("Commands: PING, KEY_SET (timed), RELEASE_ALL, READ32")
    print("Dedicated-build trade-off: ARM7 TWiLight in-game menu helper disabled")


if __name__ == "__main__":
    main()
