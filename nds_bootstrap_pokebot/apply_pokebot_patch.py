#!/usr/bin/env python3
"""Apply the Pokebot NDS proof-of-concept patch to a pinned nds-bootstrap tree.

This patch is intentionally small:
- reserves sharedAddr[9..15] as a Pokebot mailbox;
- adds PING, timed KEY_SET/RELEASE_ALL and read-only READ32;
- merges injected DS key bits into nds-bootstrap's existing key-input hook;
- forces that generic key-input hook on for this dedicated build.

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

#define POKEBOT_PROTOCOL_VERSION 0x00010000u

/* sharedAddr[] mailbox slots. 0..8 are already used by nds-bootstrap. */
#define PB_MB_MAGIC   9
#define PB_MB_COMMAND 10
#define PB_MB_ARG0    11
#define PB_MB_ARG1    12
#define PB_MB_RESP0   13
#define PB_MB_RESP1   14
#define PB_MB_STATUS  15

#define PB_MAGIC 0x504B4254u /* "PKBT" as a protocol tag */
#define PB_PONG  0x504F4E47u /* "PONG" */

#define PB_STATUS_READY 0x52445921u /* "RDY!" */
#define PB_STATUS_REQ   0x52455121u /* "REQ!" */
#define PB_STATUS_BUSY  0x42555359u /* "BUSY" */
#define PB_STATUS_DONE  0x444F4E45u /* "DONE" */
#define PB_STATUS_ERR   0x45525221u /* "ERR!" */

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

/*
 * v0p1 is deliberately limited to the ten REG_KEYINPUT buttons:
 * A, B, Select, Start, Right, Left, Up, Down, R, L.
 *
 * KEY_SET is timed. A command with ARG1 == 0 defaults to two VBlanks.
 * Requests above 120 VBlanks are clamped so a dead transport cannot leave
 * a key held indefinitely.
 */
static volatile u16 injectedKeys = 0;
static volatile u16 injectedKeyFrames = 0;

static bool valid_read32_address(u32 address) {
    if (address & 3u) {
        return false;
    }

    return address >= 0x02000000u && address <= 0x023FFFFCu;
}

static void release_all(void) {
    injectedKeys = 0;
    injectedKeyFrames = 0;
}

void pokebot_bridge_tick(volatile u32 *shared) {
    if (injectedKeyFrames > 0) {
        injectedKeyFrames--;
        if (injectedKeyFrames == 0) {
            injectedKeys = 0;
        }
    }

    if (shared[PB_MB_MAGIC] != PB_MAGIC) {
        release_all();
        shared[PB_MB_COMMAND] = 0;
        shared[PB_MB_ARG0] = 0;
        shared[PB_MB_ARG1] = 0;
        shared[PB_MB_RESP0] = 0;
        shared[PB_MB_RESP1] = POKEBOT_PROTOCOL_VERSION;
        shared[PB_MB_MAGIC] = PB_MAGIC;
        shared[PB_MB_STATUS] = PB_STATUS_READY;
        return;
    }

    if (shared[PB_MB_STATUS] != PB_STATUS_REQ) {
        return;
    }

    const u32 command = shared[PB_MB_COMMAND];
    const u32 arg0 = shared[PB_MB_ARG0];
    const u32 arg1 = shared[PB_MB_ARG1];

    shared[PB_MB_STATUS] = PB_STATUS_BUSY;
    shared[PB_MB_RESP0] = 0;
    shared[PB_MB_RESP1] = POKEBOT_PROTOCOL_VERSION;

    switch (command) {
        case PB_CMD_PING:
            shared[PB_MB_RESP0] = PB_PONG;
            shared[PB_MB_STATUS] = PB_STATUS_DONE;
            break;

        case PB_CMD_KEY_SET: {
            u32 frames = arg1;
            if (frames == 0) {
                frames = 2;
            } else if (frames > 120) {
                frames = 120;
            }

            injectedKeys = (u16)(arg0 & 0x03FFu);
            injectedKeyFrames = (u16)frames;

            shared[PB_MB_RESP0] = injectedKeys;
            shared[PB_MB_RESP1] = frames;
            shared[PB_MB_STATUS] = PB_STATUS_DONE;
            break;
        }

        case PB_CMD_RELEASE_ALL:
            release_all();
            shared[PB_MB_STATUS] = PB_STATUS_DONE;
            break;

        case PB_CMD_READ32:
            if (!valid_read32_address(arg0)) {
                shared[PB_MB_RESP0] = PB_ERR_BAD_ADDRESS;
                shared[PB_MB_STATUS] = PB_STATUS_ERR;
                break;
            }

            shared[PB_MB_RESP0] = *(volatile u32 *)arg0;
            shared[PB_MB_STATUS] = PB_STATUS_DONE;
            break;

        default:
            shared[PB_MB_RESP0] = PB_ERR_BAD_COMMAND;
            shared[PB_MB_STATUS] = PB_STATUS_ERR;
            break;
    }
}

void pokebot_bridge_apply_keys(u16 *keyInput, u16 *extKeyInput) {
    (void)extKeyInput;

    /*
     * REG_KEYINPUT is active-low. Clear requested bits so the game sees
     * those buttons as pressed. v0p1 intentionally does not synthesize
     * X/Y/touch yet.
     */
    *keyInput &= (u16)~(injectedKeys & 0x03FFu);
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
    bridge_h = root / "retail/cardengine/arm7/include/pokebot_bridge.h"
    bridge_c = root / "retail/cardengine/arm7/source/pokebot_bridge.c"

    for required in (cardengine, patch_arm9):
        if not required.is_file():
            raise FileNotFoundError(required)

    bridge_h.write_text(HEADER, encoding="utf-8")
    bridge_c.write_text(SOURCE, encoding="utf-8")

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
    print("Mailbox: sharedAddr[9..15]")
    print("Commands: PING, KEY_SET (timed), RELEASE_ALL, READ32")


if __name__ == "__main__":
    main()
