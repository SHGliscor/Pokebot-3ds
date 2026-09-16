#!/usr/bin/env python3
"""Apply the Pokebot NDS proof-of-concept patch to a pinned nds-bootstrap tree.

v0p2 adds a New-3DS RTCom/TwlBg hardware proof on top of v0p1:
- four-word read-only-RAM/input mailbox at sharedAddr[9..12];
- boot-time RTCom upload from nds-bootstrap's ARM7 bootloader;
- shocoman's New-3DS TwlBg runtime patch is installed before the game starts;
- ARM7 cardengine reads the TwlBg ZL/ZR byte from legacy RTC registers;
- a rising ZL edge synthesizes a short DS A press.

The RTCom installer is fail-open: failure leaves the game boot path intact.
No arbitrary game-RAM write command is exposed.

Pinned nds-bootstrap:
1585a242c80f78fc67cb49a7bfe55a24cc362785
"""

from __future__ import annotations

import argparse
from pathlib import Path

PINNED_COMMIT = "1585a242c80f78fc67cb49a7bfe55a24cc362785"

HEADER = r'''#ifndef POKEBOT_BRIDGE_H
#define POKEBOT_BRIDGE_H

#include <nds/ndstypes.h>

#define POKEBOT_PROTOCOL_VERSION 0x00020000u

/*
 * SDK1 card-engine shared block starts at 0x027FFA0C.
 * Slots 9..12 map to 0x027FFA30..0x027FFA3C.
 * 0x027FFA40 is UNPATCHED_FUNCTION_LOCATION, so Pokebot stops at slot 12.
 */
#define PB_MB_CONTROL 9
#define PB_MB_COMMAND 10
#define PB_MB_DATA0   11
#define PB_MB_DATA1   12

#define PB_TAG_BASE 0x504B4200u
#define PB_TAG_MASK 0xFFFFFF00u

#define PB_STATE_READY 0x01u
#define PB_STATE_REQ   0x02u
#define PB_STATE_BUSY  0x03u
#define PB_STATE_DONE  0x04u
#define PB_STATE_ERR   0x05u

#define PB_CONTROL(state) (PB_TAG_BASE | (state))

#define PB_PONG 0x504F4E47u
#define PB_BOOT_RTCOM_MARKER 0x52544332u /* "RTC2" */

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
#include <nds/arm7/clock.h>

#include "pokebot_bridge.h"

/* v0p2 still synthesizes only the ten REG_KEYINPUT buttons. */
static volatile u16 injectedKeys;
static volatile u16 injectedKeyFrames;
static bool rtcomAvailable;
static bool zlWasDown;
static u8 rtcomWarmup;

#define PB_CS_0    (1u << 6)
#define PB_CS_1    ((1u << 6) | (1u << 2))
#define PB_SCK_0   (1u << 5)
#define PB_SCK_1   ((1u << 5) | (1u << 1))
#define PB_SIO_1   ((1u << 4) | (1u << 0))
#define PB_SIO_OUT (1u << 4)
#define PB_SIO_IN  1u

#define PB_RTC_READ_COUNTER_EXT 0x71u
#define PB_ZL_BIT 0x04u

static void pb_wait(volatile int count) {
    while (--count > 0) {
    }
}

/*
 * Read the three-byte legacy RTC COUNTER extension using the reversed bit
 * order used by RTCom. TwlBg stores ZR in bit1 and ZL in bit2 of byte 2.
 */
static u8 pb_read_zlzr(void) {
    u8 command = PB_RTC_READ_COUNTER_EXT;
    u8 result = 0;

    RTC_CR8 = PB_CS_0 | PB_SCK_1 | PB_SIO_1;
    pb_wait(2);
    RTC_CR8 = PB_CS_1 | PB_SCK_1 | PB_SIO_1;
    pb_wait(2);

    for (u32 bit = 0; bit < 8; bit++) {
        RTC_CR8 = PB_CS_1 | PB_SCK_0 | PB_SIO_OUT | (command >> 7);
        pb_wait(1);
        RTC_CR8 = PB_CS_1 | PB_SCK_1 | PB_SIO_OUT | (command >> 7);
        pb_wait(1);
        command <<= 1;
    }

    for (u32 byte = 0; byte < 3; byte++) {
        u8 data = 0;
        for (u32 bit = 0; bit < 8; bit++) {
            RTC_CR8 = PB_CS_1 | PB_SCK_0;
            pb_wait(1);
            RTC_CR8 = PB_CS_1 | PB_SCK_1;
            pb_wait(1);
            data <<= 1;
            if (RTC_CR8 & PB_SIO_IN)
                data |= 1;
        }
        if (byte == 2)
            result = data;
    }

    RTC_CR8 = PB_CS_0 | PB_SCK_1;
    pb_wait(2);
    return result & 0x06u;
}

static void release_all(void) {
    injectedKeys = 0;
    injectedKeyFrames = 0;
}

static void poll_rtcom_test(void) {
    if (!rtcomAvailable)
        return;

    const bool zlDown = (pb_read_zlzr() & PB_ZL_BIT) != 0;

    if (rtcomWarmup) {
        rtcomWarmup--;
        zlWasDown = zlDown;
        return;
    }

    if (zlDown && !zlWasDown) {
        injectedKeys |= 1u; /* DS A */
        if (injectedKeyFrames < 3)
            injectedKeyFrames = 3;
    }

    zlWasDown = zlDown;
}

void pokebot_bridge_tick(volatile u32 *shared) {
    if (injectedKeyFrames && --injectedKeyFrames == 0)
        injectedKeys = 0;

    if ((shared[PB_MB_CONTROL] & PB_TAG_MASK) != PB_TAG_BASE) {
        if (shared[PB_MB_COMMAND] == PB_BOOT_RTCOM_MARKER) {
            rtcomAvailable = true;
            rtcomWarmup = 60;
        }

        release_all();
        shared[PB_MB_COMMAND] = 0;
        shared[PB_MB_DATA0] = rtcomAvailable ? PB_BOOT_RTCOM_MARKER : 0;
        shared[PB_MB_DATA1] = POKEBOT_PROTOCOL_VERSION;
        shared[PB_MB_CONTROL] = PB_CONTROL(PB_STATE_READY);
        poll_rtcom_test();
        return;
    }

    if (shared[PB_MB_CONTROL] == PB_CONTROL(PB_STATE_REQ)) {
        const u32 command = shared[PB_MB_COMMAND];
        const u32 data0 = shared[PB_MB_DATA0];
        u32 data1 = shared[PB_MB_DATA1];

        shared[PB_MB_CONTROL] = PB_CONTROL(PB_STATE_BUSY);

        if (command == PB_CMD_PING) {
            shared[PB_MB_DATA0] = PB_PONG;
            shared[PB_MB_DATA1] = POKEBOT_PROTOCOL_VERSION;
            shared[PB_MB_CONTROL] = PB_CONTROL(PB_STATE_DONE);
        } else if (command == PB_CMD_KEY_SET) {
            if (data1 == 0)
                data1 = 2;
            else if (data1 > 120)
                data1 = 120;

            injectedKeys = (u16)(data0 & 0x03FFu);
            injectedKeyFrames = (u16)data1;
            shared[PB_MB_DATA0] = injectedKeys;
            shared[PB_MB_DATA1] = data1;
            shared[PB_MB_CONTROL] = PB_CONTROL(PB_STATE_DONE);
        } else if (command == PB_CMD_RELEASE_ALL) {
            release_all();
            shared[PB_MB_DATA0] = 0;
            shared[PB_MB_DATA1] = 0;
            shared[PB_MB_CONTROL] = PB_CONTROL(PB_STATE_DONE);
        } else if (command == PB_CMD_READ32) {
            if ((data0 & 3u) || data0 < 0x02000000u || data0 > 0x023FFFFCu) {
                shared[PB_MB_DATA0] = PB_ERR_BAD_ADDRESS;
                shared[PB_MB_CONTROL] = PB_CONTROL(PB_STATE_ERR);
            } else {
                shared[PB_MB_DATA0] = *(volatile u32 *)data0;
                shared[PB_MB_DATA1] = POKEBOT_PROTOCOL_VERSION;
                shared[PB_MB_CONTROL] = PB_CONTROL(PB_STATE_DONE);
            }
        } else {
            shared[PB_MB_DATA0] = PB_ERR_BAD_COMMAND;
            shared[PB_MB_CONTROL] = PB_CONTROL(PB_STATE_ERR);
        }
    }

    poll_rtcom_test();
}

void pokebot_bridge_apply_keys(u16 *keyInput, u16 *extKeyInput) {
    (void)extKeyInput;
    *keyInput &= (u16)~(injectedKeys & 0x03FFu);
}
'''

IGM_STUB = r'''/*
 * Pokebot nds-bootstrap v0p2 space trade-off.
 *
 * The stock ARM7 cardengine is packed into a 4 KiB + 0x80 region. The
 * Pokebot mailbox/input/RTCom reader uses that space, so this dedicated
 * test build gives up the ARM7 TWiLight in-game-menu helper. The normal
 * release bootstrap is not modified.
 */
void inGameMenu(void) {
}
'''

BOOT_H = r'''#ifndef POKEBOT_RTCOM_BOOT_H
#define POKEBOT_RTCOM_BOOT_H

#include <nds/ndstypes.h>
#include <stdbool.h>

bool pokebot_rtcom_install(void);

#endif
'''

BOOT_C = r'''#include <nds.h>

#include "pokebot_rtcom_boot.h"
#include "pokebot_rtcom_ucode.h"

#define PB_RTCOM_STAT_READY 0x00u
#define PB_RTCOM_STAT_ACK   0x80u
#define PB_RTCOM_STAT_DONE  0x82u

#define PB_RTCOM_REQ_UPLOAD_UCODE  0x41u
#define PB_RTCOM_REQ_FINISH_UCODE  0x42u
#define PB_RTCOM_REQ_EXECUTE_UCODE 0x44u
#define PB_RTCOM_REQ_NEXT          0x81u
#define PB_RTCOM_REQ_KEEPALIVE     0x83u

#define PB_RTC_READ_112  0x6Du
#define PB_RTC_WRITE_112 0x6Cu
#define PB_RTC_READ_113  0x6Fu
#define PB_RTC_WRITE_113 0x6Eu

#define PB_CS_0    (1u << 6)
#define PB_CS_1    ((1u << 6) | (1u << 2))
#define PB_SCK_0   (1u << 5)
#define PB_SCK_1   ((1u << 5) | (1u << 1))
#define PB_SIO_1   ((1u << 4) | (1u << 0))
#define PB_SIO_OUT (1u << 4)
#define PB_SIO_IN  1u

#define PB_BOOT_RTCOM_MARKER_ADDR ((vu32 *)0x027FFA34)
#define PB_BOOT_RTCOM_MARKER 0x52544332u

static void pb_wait(volatile int count) {
    while (--count > 0) {
    }
}

static void pb_rtc_transfer(u8 *cmd, u32 cmdLen, u8 *result, u32 resultLen) {
    RTC_CR8 = PB_CS_0 | PB_SCK_1 | PB_SIO_1;
    pb_wait(2);
    RTC_CR8 = PB_CS_1 | PB_SCK_1 | PB_SIO_1;
    pb_wait(2);

    u8 data = *cmd++;
    for (u32 bit = 0; bit < 8; bit++) {
        RTC_CR8 = PB_CS_1 | PB_SCK_0 | PB_SIO_OUT | (data >> 7);
        pb_wait(9);
        RTC_CR8 = PB_CS_1 | PB_SCK_1 | PB_SIO_OUT | (data >> 7);
        pb_wait(9);
        data <<= 1;
    }

    for (; cmdLen > 1; cmdLen--) {
        data = *cmd++;
        for (u32 bit = 0; bit < 8; bit++) {
            RTC_CR8 = PB_CS_1 | PB_SCK_0 | PB_SIO_OUT | (data >> 7);
            pb_wait(9);
            RTC_CR8 = PB_CS_1 | PB_SCK_1 | PB_SIO_OUT | (data >> 7);
            pb_wait(9);
            data <<= 1;
        }
    }

    for (; resultLen > 0; resultLen--) {
        data = 0;
        for (u32 bit = 0; bit < 8; bit++) {
            RTC_CR8 = PB_CS_1 | PB_SCK_0;
            pb_wait(9);
            RTC_CR8 = PB_CS_1 | PB_SCK_1;
            pb_wait(9);
            data <<= 1;
            if (RTC_CR8 & PB_SIO_IN)
                data |= 1;
        }
        *result++ = data;
    }

    pb_wait(2);
    RTC_CR8 = PB_CS_0 | PB_SCK_1;
    pb_wait(2);
}

static u8 pb_read112(void) {
    u8 command = PB_RTC_READ_112;
    u8 value = 0;
    pb_rtc_transfer(&command, 1, &value, 1);
    return value;
}

static u8 pb_read113(void) {
    u8 command = PB_RTC_READ_113;
    u8 value = 0;
    pb_rtc_transfer(&command, 1, &value, 1);
    return value;
}

static void pb_write112(u8 value) {
    u8 command[2] = {PB_RTC_WRITE_112, value};
    pb_rtc_transfer(command, 2, 0, 0);
}

static void pb_write113(u8 value) {
    u8 command[2] = {PB_RTC_WRITE_113, value};
    pb_rtc_transfer(command, 2, 0, 0);
}

static u16 pb_begin(void) {
    const u16 oldRcnt = REG_RCNT;
    REG_IF = IRQ_NETWORK;
    REG_RCNT = 0x8100;
    REG_IF = IRQ_NETWORK;
    return oldRcnt;
}

static void pb_end(u16 oldRcnt) {
    REG_IF = IRQ_NETWORK;
    REG_RCNT = oldRcnt;
}

static bool pb_wait_status(u8 status) {
    int timeout = 2062500;
    do {
        if (!(REG_IF & IRQ_NETWORK))
            continue;

        REG_IF = IRQ_NETWORK;
        return pb_read113() == status;
    } while (--timeout);

    REG_IF = IRQ_NETWORK;
    return false;
}

static void pb_request_async(u8 request) {
    pb_write113(request);
}

static void pb_request_async_param(u8 request, u8 param) {
    pb_write112(param);
    pb_write113(request);
}

static bool pb_request(u8 request) {
    pb_request_async(request);
    return pb_wait_status(PB_RTCOM_STAT_ACK);
}

static bool pb_request_param(u8 request, u8 param) {
    pb_request_async_param(request, param);
    return pb_wait_status(PB_RTCOM_STAT_ACK);
}

static bool pb_upload_ucode(void) {
    const u32 length = pokebot_rtcom_ucode_size;

    if (!pb_request_param(PB_RTCOM_REQ_UPLOAD_UCODE, length & 0xFF))
        return false;
    if (!pb_request_param(PB_RTCOM_REQ_NEXT, (length >> 8) & 0xFF))
        return false;
    if (!pb_request_param(PB_RTCOM_REQ_NEXT, (length >> 16) & 0xFF))
        return false;
    if (!pb_request_param(PB_RTCOM_REQ_NEXT, (length >> 24) & 0xFF))
        return false;

    for (u32 i = 0; i < length; i++) {
        if (!pb_request_param(PB_RTCOM_REQ_NEXT, pokebot_rtcom_ucode[i]))
            return false;
    }

    pb_request_async(PB_RTCOM_REQ_KEEPALIVE);
    if (!pb_wait_status(PB_RTCOM_STAT_DONE))
        return false;

    return pb_request(PB_RTCOM_REQ_FINISH_UCODE);
}

static bool pb_execute_stage(u8 stage) {
    if (!pb_request_param(PB_RTCOM_REQ_EXECUTE_UCODE, stage))
        return false;

    /* Let TwlBg settle between self-modifying-code stages. */
    swiDelay(250000);
    return true;
}

bool pokebot_rtcom_install(void) {
    *PB_BOOT_RTCOM_MARKER_ADDR = 0;

    const int savedIrq = enterCriticalSection();
    const u16 oldRcnt = pb_begin();

    bool ok = false;
    int tries = 10;

    do {
        pb_request_async(1);
        if (pb_wait_status(PB_RTCOM_STAT_DONE)) {
            ok = true;
            break;
        }
    } while (--tries);

    if (ok) {
        ok = false;
        tries = 5;
        do {
            if (pb_upload_ucode()) {
                ok = true;
                break;
            }
        } while (--tries);
    }

    if (ok)
        ok = pb_execute_stage(1);
    if (ok)
        ok = pb_execute_stage(2);
    if (ok)
        ok = pb_execute_stage(3);

    pb_end(oldRcnt);
    leaveCriticalSection(savedIrq);

    if (ok)
        *PB_BOOT_RTCOM_MARKER_ADDR = PB_BOOT_RTCOM_MARKER;

    return ok;
}
'''


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one patch anchor, found {count}: {old!r}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def make_ucode_header(data: bytes) -> str:
    lines = []
    for i in range(0, len(data), 12):
        chunk = ", ".join(f"0x{b:02X}" for b in data[i:i + 12])
        lines.append(f"    {chunk},")
    body = "\n".join(lines)
    return (
        "#ifndef POKEBOT_RTCOM_UCODE_H\n"
        "#define POKEBOT_RTCOM_UCODE_H\n\n"
        "#include <nds/ndstypes.h>\n\n"
        "static const u8 pokebot_rtcom_ucode[] = {\n"
        f"{body}\n"
        "};\n"
        f"static const u32 pokebot_rtcom_ucode_size = {len(data)}u;\n\n"
        "#endif\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("tree", type=Path, help="Path to the nds-bootstrap checkout")
    parser.add_argument("--ucode", type=Path, required=True, help="Compiled New-3DS RTCom .uc11 payload")
    args = parser.parse_args()

    root = args.tree.resolve()
    ucode_path = args.ucode.resolve()

    cardengine = root / "retail/cardengine/arm7/source/cardengine.c"
    patch_arm9 = root / "retail/bootloader/source/arm7/patch_arm9.c"
    in_game_menu = root / "retail/cardengine/arm7/source/inGameMenu.c"
    bridge_h = root / "retail/cardengine/arm7/include/pokebot_bridge.h"
    bridge_c = root / "retail/cardengine/arm7/source/pokebot_bridge.c"

    boot_main = root / "retail/bootloader/source/arm7/main.arm7.c"
    boot_h = root / "retail/bootloader/source/arm7/pokebot_rtcom_boot.h"
    boot_c = root / "retail/bootloader/source/arm7/pokebot_rtcom_boot.c"
    ucode_h = root / "retail/bootloader/source/arm7/pokebot_rtcom_ucode.h"

    for required in (cardengine, patch_arm9, in_game_menu, boot_main, ucode_path):
        if not required.is_file():
            raise FileNotFoundError(required)

    ucode = ucode_path.read_bytes()
    if not ucode or len(ucode) > 0x20000:
        raise RuntimeError(f"unexpected RTCom ucode size: {len(ucode)}")

    bridge_h.write_text(HEADER, encoding="utf-8")
    bridge_c.write_text(SOURCE, encoding="utf-8")
    in_game_menu.write_text(IGM_STUB, encoding="utf-8")

    boot_h.write_text(BOOT_H, encoding="utf-8")
    boot_c.write_text(BOOT_C, encoding="utf-8")
    ucode_h.write_text(make_ucode_header(ucode), encoding="utf-8")

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

    replace_once(
        boot_main,
        '#include "unpatched_funcs.h"\n',
        '#include "unpatched_funcs.h"\n#include "pokebot_rtcom_boot.h"\n',
    )

    replace_once(
        boot_main,
        '\tREG_SCFG_EXT = 0x12A03000;\n\n\tstartBinary_ARM7();\n',
        '\t/* Pokebot v0p2: fail-open RTCom/TwlBg install for New 3DS. */\n'
        '\t(void)pokebot_rtcom_install();\n\n'
        '\tREG_SCFG_EXT = 0x12A03000;\n\n'
        '\tstartBinary_ARM7();\n',
    )

    print("Pokebot NDS v0p2 patch applied")
    print(f"Pinned upstream: {PINNED_COMMIT}")
    print(f"RTCom ucode bytes: {len(ucode)}")
    print("Mailbox: sharedAddr[9..12] / 0x027FFA30..0x027FFA3C")
    print("Hardware proof: New-3DS ZL rising edge -> synthetic DS A")
    print("RTCom install is fail-open; normal game boot continues on failure")


if __name__ == "__main__":
    main()
