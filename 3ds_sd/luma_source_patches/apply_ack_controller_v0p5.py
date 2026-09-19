from pathlib import Path

root = Path(__file__).resolve().parents[2] / "Luma3DS"
source_dir = root / "sysmodules" / "rosalina" / "source"
include_dir = root / "sysmodules" / "rosalina" / "include"
controller_c = source_dir / "pokebot_input_controller.c"
controller_h = include_dir / "pokebot_input_controller.h"
bridge_c = source_dir / "pokebot_ram_bridge.c"
menus_c = source_dir / "menus.c"
input_c = source_dir / "input_redirection.c"
input_h = include_dir / "input_redirection.h"

header = r'''/*
 * Pokebot-Luma v0p5 acknowledged input controller.
 * Uses Luma's HID/touch redirection storage; never writes game process RAM.
 */
#pragma once

#include <3ds.h>

extern bool pokebotInputControllerEnabled;
extern int pokebotInputControllerResult;
extern volatile u32 pokebotInputCommands;

Result PokebotInputController_SetEnabled(bool enable);
void PokebotInputController_Update(void);
void PokebotInputController_ReleaseAll(void);
u16 PokebotInputController_Handle(
    u16 command,
    u32 requestId,
    u32 argument,
    u32 aux,
    void *payload,
    u32 payloadCapacity,
    u32 *payloadLength,
    s32 *result);
'''

source = r'''/*
 * Pokebot-Luma v0p5 acknowledged controller.
 *
 * Commands on the existing Pokebot UDP 4952 bridge:
 *   5 INPUT_PING
 *   6 INPUT_PULSE
 *   7 INPUT_STATUS
 *   8 RELEASE_ALL
 *   9 TOUCH_PULSE
 *  10 HID_LATCH
 *  13 CPAD_PULSE
 *
 * Pulse timing is owned by the 3DS. Duplicate sequence IDs are observational
 * and never create a second gameplay action. RELEASE_ALL neutralises only the
 * injected HID/touch state. Physical buttons remain additive in the patched
 * Luma HID hook.
 */
#include <3ds.h>
#include <string.h>

#include "input_redirection.h"
#include "pokebot_input_controller.h"

#define POKEBOT_CMD_INPUT_PING   5
#define POKEBOT_CMD_INPUT_PULSE  6
#define POKEBOT_CMD_INPUT_STATUS 7
#define POKEBOT_CMD_RELEASE_ALL  8
#define POKEBOT_CMD_TOUCH_PULSE  9
#define POKEBOT_CMD_HID_LATCH   10
#define POKEBOT_CMD_CPAD_PULSE  13

#define POKEBOT_STATUS_OK                  0
#define POKEBOT_STATUS_BAD_COMMAND         3
#define POKEBOT_STATUS_INPUT_INVALID      12
#define POKEBOT_STATUS_INPUT_BUSY         13
#define POKEBOT_STATUS_INPUT_LEGACY_ACTIVE 14
#define POKEBOT_STATUS_INPUT_PATCH_FAILED 15

#define POKEBOT_INPUT_IDLE              0
#define POKEBOT_INPUT_ACCEPTED          1
#define POKEBOT_INPUT_IN_PROGRESS       2
#define POKEBOT_INPUT_COMPLETED         3
#define POKEBOT_INPUT_ALREADY_COMPLETED 4
#define POKEBOT_INPUT_ABORTED           5
#define POKEBOT_INPUT_NOT_FOUND         6

#define POKEBOT_KIND_NONE        0
#define POKEBOT_KIND_HID_PULSE   1
#define POKEBOT_KIND_TOUCH_PULSE 2
#define POKEBOT_KIND_HID_LATCH   3
#define POKEBOT_KIND_CPAD_PULSE  4

#define POKEBOT_PHASE_NONE    0
#define POKEBOT_PHASE_HELD    1
#define POKEBOT_PHASE_SETTLE  2
#define POKEBOT_PHASE_LATCHED 3

#define POKEBOT_HID_NEUTRAL   0x00000FFFUL
#define POKEBOT_TOUCH_NEUTRAL 0x02000000UL
#define POKEBOT_CPAD_NEUTRAL  0x007FF7FFUL
#define POKEBOT_INPUT_CAPS     0x000001CFUL
#define POKEBOT_MAX_HOLD_MS    5000UL
#define POKEBOT_MAX_SETTLE_MS  5000UL

#define POKEBOT_RUNTIME_PATCH_ACTIVE  (1UL << 0)
#define POKEBOT_RUNTIME_LEGACY_ACTIVE (1UL << 1)

#pragma pack(push, 1)
typedef struct PokebotInputCaps
{
    u32 protocolVersion;
    u32 capabilityFlags;
    u32 runtimeFlags;
    u32 neutralHid;
    u32 maxHoldMs;
    u32 maxSettleMs;
} PokebotInputCaps;

typedef struct PokebotInputStatusPayload
{
    u32 sequence;
    u32 state;
    u32 rawHid;
    u32 remainingMs;
    u32 runtimeFlags;
} PokebotInputStatusPayload;
#pragma pack(pop)

typedef struct PokebotInputRuntime
{
    u32 sequence;
    u16 command;
    u32 state;
    u32 rawHid;
    u32 touchState;
    u32 cpadState;
    u32 holdMs;
    u32 settleMs;
    u32 kind;
    u32 phase;
    u64 deadlineMs;
} PokebotInputRuntime;

bool pokebotInputControllerEnabled = false;
int pokebotInputControllerResult = 0;
volatile u32 pokebotInputCommands = 0;

static bool sPatchesInstalled = false;
static PokebotInputRuntime sInput;

static u32 runtimeFlags(void)
{
    u32 flags = 0;
    if (sPatchesInstalled && pokebotInputControllerEnabled)
        flags |= POKEBOT_RUNTIME_PATCH_ACTIVE;
    if (inputRedirectionEnabled)
        flags |= POKEBOT_RUNTIME_LEGACY_ACTIVE;
    return flags;
}

static bool active(void)
{
    return sInput.state == POKEBOT_INPUT_ACCEPTED ||
           sInput.state == POKEBOT_INPUT_IN_PROGRESS;
}

static void neutral(void)
{
    PokebotInput_ResetRemote();
    sInput.rawHid = POKEBOT_HID_NEUTRAL;
    sInput.touchState = POKEBOT_TOUCH_NEUTRAL;
    sInput.cpadState = POKEBOT_CPAD_NEUTRAL;
}

static u32 remainingMs(void)
{
    if (!active() || sInput.kind == POKEBOT_KIND_HID_LATCH)
        return 0;
    u64 now = osGetTime();
    if (now >= sInput.deadlineMs)
        return 0;
    u64 remain = sInput.deadlineMs - now;
    return remain > 0xFFFFFFFFULL ? 0xFFFFFFFFUL : (u32)remain;
}

static void fillStatus(PokebotInputStatusPayload *out, u32 stateOverride)
{
    memset(out, 0, sizeof(*out));
    out->sequence = sInput.sequence;
    out->state = stateOverride == 0xFFFFFFFFUL ? sInput.state : stateOverride;
    out->rawHid = sInput.rawHid;
    out->remainingMs = remainingMs();
    out->runtimeFlags = runtimeFlags();
}

void PokebotInputController_ReleaseAll(void)
{
    if (active())
        sInput.state = POKEBOT_INPUT_ABORTED;
    sInput.phase = POKEBOT_PHASE_NONE;
    sInput.deadlineMs = 0;
    neutral();
}

void PokebotInputController_Update(void)
{
    if (!pokebotInputControllerEnabled || !active())
        return;
    if (sInput.kind == POKEBOT_KIND_HID_LATCH)
        return;

    u64 now = osGetTime();
    if (now < sInput.deadlineMs)
        return;

    if (sInput.phase == POKEBOT_PHASE_HELD)
    {
        if (sInput.kind == POKEBOT_KIND_HID_PULSE)
            PokebotInput_SetRemoteHid(POKEBOT_HID_NEUTRAL);
        else if (sInput.kind == POKEBOT_KIND_TOUCH_PULSE)
            PokebotInput_SetRemoteTouch(POKEBOT_TOUCH_NEUTRAL);
        else if (sInput.kind == POKEBOT_KIND_CPAD_PULSE)
            PokebotInput_SetRemoteCircle(POKEBOT_CPAD_NEUTRAL);

        sInput.rawHid = POKEBOT_HID_NEUTRAL;
        sInput.touchState = POKEBOT_TOUCH_NEUTRAL;
        sInput.cpadState = POKEBOT_CPAD_NEUTRAL;

        if (sInput.settleMs != 0)
        {
            sInput.phase = POKEBOT_PHASE_SETTLE;
            sInput.deadlineMs = now + sInput.settleMs;
            sInput.state = POKEBOT_INPUT_IN_PROGRESS;
            return;
        }
    }

    sInput.phase = POKEBOT_PHASE_NONE;
    sInput.deadlineMs = 0;
    sInput.state = POKEBOT_INPUT_COMPLETED;
    neutral();
}

Result PokebotInputController_SetEnabled(bool enable)
{
    if (enable)
    {
        if (pokebotInputControllerEnabled)
            return 0;
        if (inputRedirectionEnabled)
            return (Result)-14;

        PokebotInput_ResetRemote();
        Result res = InputRedirection_DoOrUndoPatches();
        if (R_FAILED(res))
        {
            pokebotInputControllerResult = (int)res;
            return res;
        }

        sPatchesInstalled = true;
        memset(&sInput, 0, sizeof(sInput));
        sInput.rawHid = POKEBOT_HID_NEUTRAL;
        sInput.touchState = POKEBOT_TOUCH_NEUTRAL;
        sInput.cpadState = POKEBOT_CPAD_NEUTRAL;
        pokebotInputCommands = 0;
        pokebotInputControllerEnabled = true;
        pokebotInputControllerResult = 0;
        return 0;
    }

    if (!pokebotInputControllerEnabled && !sPatchesInstalled)
        return 0;

    PokebotInputController_ReleaseAll();
    pokebotInputControllerEnabled = false;

    if (sPatchesInstalled)
    {
        Result res = InputRedirection_DoOrUndoPatches();
        if (R_FAILED(res))
        {
            pokebotInputControllerResult = (int)res;
            return res;
        }
        sPatchesInstalled = false;
    }

    pokebotInputControllerResult = 0;
    return 0;
}

static bool validRawHid(u32 rawHid)
{
    return (rawHid & ~POKEBOT_HID_NEUTRAL) == 0;
}

static bool validCirclePad(u32 cpadState)
{
    u32 x = cpadState & 0xFFFUL;
    u32 y = (cpadState >> 12) & 0xFFFUL;
    return (cpadState & 0xFF000000UL) == 0 && x <= 0xFFFUL && y <= 0xFFFUL;
}

static bool parseTiming(u32 aux, u32 *holdMs, u32 *settleMs)
{
    *holdMs = aux & 0xFFFFUL;
    *settleMs = (aux >> 16) & 0xFFFFUL;
    return *holdMs > 0 && *holdMs <= POKEBOT_MAX_HOLD_MS &&
           *settleMs <= POKEBOT_MAX_SETTLE_MS;
}

static u16 available(void)
{
    if (inputRedirectionEnabled)
        return POKEBOT_STATUS_INPUT_LEGACY_ACTIVE;
    if (!pokebotInputControllerEnabled || !sPatchesInstalled)
        return POKEBOT_STATUS_INPUT_PATCH_FAILED;
    return POKEBOT_STATUS_OK;
}

static bool duplicate(u16 command, u32 sequence)
{
    return sInput.sequence != 0 && sequence == sInput.sequence && command == sInput.command;
}

static u16 startHidPulse(u16 command, u32 sequence, u32 rawHid, u32 aux)
{
    u32 holdMs = 0, settleMs = 0;
    if (!validRawHid(rawHid) || !parseTiming(aux, &holdMs, &settleMs))
        return POKEBOT_STATUS_INPUT_INVALID;
    if (active())
        return POKEBOT_STATUS_INPUT_BUSY;

    memset(&sInput, 0, sizeof(sInput));
    sInput.sequence = sequence;
    sInput.command = command;
    sInput.state = POKEBOT_INPUT_IN_PROGRESS;
    sInput.rawHid = rawHid;
    sInput.touchState = POKEBOT_TOUCH_NEUTRAL;
    sInput.cpadState = POKEBOT_CPAD_NEUTRAL;
    sInput.holdMs = holdMs;
    sInput.settleMs = settleMs;
    sInput.kind = POKEBOT_KIND_HID_PULSE;
    sInput.phase = POKEBOT_PHASE_HELD;
    sInput.deadlineMs = osGetTime() + holdMs;
    PokebotInput_SetRemoteTouch(POKEBOT_TOUCH_NEUTRAL);
    PokebotInput_SetRemoteCircle(POKEBOT_CPAD_NEUTRAL);
    PokebotInput_SetRemoteHid(rawHid);
    pokebotInputCommands++;
    return POKEBOT_STATUS_OK;
}

static u16 startTouchPulse(u16 command, u32 sequence, u32 touchState, u32 aux)
{
    u32 holdMs = 0, settleMs = 0;
    if (!parseTiming(aux, &holdMs, &settleMs) || touchState == POKEBOT_TOUCH_NEUTRAL)
        return POKEBOT_STATUS_INPUT_INVALID;
    if (active())
        return POKEBOT_STATUS_INPUT_BUSY;

    memset(&sInput, 0, sizeof(sInput));
    sInput.sequence = sequence;
    sInput.command = command;
    sInput.state = POKEBOT_INPUT_IN_PROGRESS;
    sInput.rawHid = POKEBOT_HID_NEUTRAL;
    sInput.touchState = touchState;
    sInput.cpadState = POKEBOT_CPAD_NEUTRAL;
    sInput.holdMs = holdMs;
    sInput.settleMs = settleMs;
    sInput.kind = POKEBOT_KIND_TOUCH_PULSE;
    sInput.phase = POKEBOT_PHASE_HELD;
    sInput.deadlineMs = osGetTime() + holdMs;
    PokebotInput_SetRemoteHid(POKEBOT_HID_NEUTRAL);
    PokebotInput_SetRemoteCircle(POKEBOT_CPAD_NEUTRAL);
    PokebotInput_SetRemoteTouch(touchState);
    pokebotInputCommands++;
    return POKEBOT_STATUS_OK;
}

static u16 startHidLatch(u16 command, u32 sequence, u32 rawHid, u32 aux)
{
    if (!validRawHid(rawHid) || aux != 0)
        return POKEBOT_STATUS_INPUT_INVALID;
    if (active())
        return POKEBOT_STATUS_INPUT_BUSY;

    memset(&sInput, 0, sizeof(sInput));
    sInput.sequence = sequence;
    sInput.command = command;
    sInput.state = POKEBOT_INPUT_IN_PROGRESS;
    sInput.rawHid = rawHid;
    sInput.touchState = POKEBOT_TOUCH_NEUTRAL;
    sInput.cpadState = POKEBOT_CPAD_NEUTRAL;
    sInput.kind = POKEBOT_KIND_HID_LATCH;
    sInput.phase = POKEBOT_PHASE_LATCHED;
    PokebotInput_SetRemoteTouch(POKEBOT_TOUCH_NEUTRAL);
    PokebotInput_SetRemoteCircle(POKEBOT_CPAD_NEUTRAL);
    PokebotInput_SetRemoteHid(rawHid);
    pokebotInputCommands++;
    return POKEBOT_STATUS_OK;
}

static u16 startCpadPulse(u16 command, u32 sequence, u32 cpadState, u32 aux)
{
    u32 holdMs = 0, settleMs = 0;
    if (!validCirclePad(cpadState) || cpadState == POKEBOT_CPAD_NEUTRAL ||
        !parseTiming(aux, &holdMs, &settleMs))
        return POKEBOT_STATUS_INPUT_INVALID;
    if (active())
        return POKEBOT_STATUS_INPUT_BUSY;

    memset(&sInput, 0, sizeof(sInput));
    sInput.sequence = sequence;
    sInput.command = command;
    sInput.state = POKEBOT_INPUT_IN_PROGRESS;
    sInput.rawHid = POKEBOT_HID_NEUTRAL;
    sInput.touchState = POKEBOT_TOUCH_NEUTRAL;
    sInput.cpadState = cpadState;
    sInput.holdMs = holdMs;
    sInput.settleMs = settleMs;
    sInput.kind = POKEBOT_KIND_CPAD_PULSE;
    sInput.phase = POKEBOT_PHASE_HELD;
    sInput.deadlineMs = osGetTime() + holdMs;
    PokebotInput_SetRemoteHid(POKEBOT_HID_NEUTRAL);
    PokebotInput_SetRemoteTouch(POKEBOT_TOUCH_NEUTRAL);
    PokebotInput_SetRemoteCircle(cpadState);
    pokebotInputCommands++;
    return POKEBOT_STATUS_OK;
}

u16 PokebotInputController_Handle(
    u16 command,
    u32 requestId,
    u32 argument,
    u32 aux,
    void *payload,
    u32 payloadCapacity,
    u32 *payloadLength,
    s32 *result)
{
    *payloadLength = 0;
    *result = 0;
    PokebotInputController_Update();

    if (command == POKEBOT_CMD_INPUT_PING)
    {
        if (payloadCapacity < sizeof(PokebotInputCaps))
            return POKEBOT_STATUS_INPUT_INVALID;
        PokebotInputCaps caps;
        caps.protocolVersion = 1;
        caps.capabilityFlags = POKEBOT_INPUT_CAPS;
        caps.runtimeFlags = runtimeFlags();
        caps.neutralHid = POKEBOT_HID_NEUTRAL;
        caps.maxHoldMs = POKEBOT_MAX_HOLD_MS;
        caps.maxSettleMs = POKEBOT_MAX_SETTLE_MS;
        memcpy(payload, &caps, sizeof(caps));
        *payloadLength = sizeof(caps);
        return available();
    }

    if (command == POKEBOT_CMD_RELEASE_ALL)
    {
        if (payloadCapacity < sizeof(PokebotInputStatusPayload))
            return POKEBOT_STATUS_INPUT_INVALID;
        PokebotInputController_ReleaseAll();
        PokebotInputStatusPayload status;
        memset(&status, 0, sizeof(status));
        status.sequence = requestId;
        status.state = POKEBOT_INPUT_COMPLETED;
        status.rawHid = POKEBOT_HID_NEUTRAL;
        status.remainingMs = 0;
        status.runtimeFlags = runtimeFlags();
        memcpy(payload, &status, sizeof(status));
        *payloadLength = sizeof(status);
        pokebotInputCommands++;
        return POKEBOT_STATUS_OK;
    }

    u16 ready = available();
    if (ready != POKEBOT_STATUS_OK)
        return ready;

    if (command == POKEBOT_CMD_INPUT_STATUS)
    {
        if (payloadCapacity < sizeof(PokebotInputStatusPayload))
            return POKEBOT_STATUS_INPUT_INVALID;
        PokebotInputStatusPayload status;
        if (argument == 0 || argument != sInput.sequence)
        {
            memset(&status, 0, sizeof(status));
            status.sequence = argument;
            status.state = POKEBOT_INPUT_NOT_FOUND;
            status.rawHid = POKEBOT_HID_NEUTRAL;
            status.runtimeFlags = runtimeFlags();
        }
        else
            fillStatus(&status, 0xFFFFFFFFUL);
        memcpy(payload, &status, sizeof(status));
        *payloadLength = sizeof(status);
        return POKEBOT_STATUS_OK;
    }

    if (duplicate(command, requestId))
    {
        if (payloadCapacity < sizeof(PokebotInputStatusPayload))
            return POKEBOT_STATUS_INPUT_INVALID;
        PokebotInputStatusPayload status;
        u32 override = sInput.state == POKEBOT_INPUT_COMPLETED ?
                       POKEBOT_INPUT_ALREADY_COMPLETED : 0xFFFFFFFFUL;
        fillStatus(&status, override);
        memcpy(payload, &status, sizeof(status));
        *payloadLength = sizeof(status);
        return POKEBOT_STATUS_OK;
    }

    u16 statusCode = POKEBOT_STATUS_BAD_COMMAND;
    if (command == POKEBOT_CMD_INPUT_PULSE)
        statusCode = startHidPulse(command, requestId, argument, aux);
    else if (command == POKEBOT_CMD_TOUCH_PULSE)
        statusCode = startTouchPulse(command, requestId, argument, aux);
    else if (command == POKEBOT_CMD_HID_LATCH)
        statusCode = startHidLatch(command, requestId, argument, aux);
    else if (command == POKEBOT_CMD_CPAD_PULSE)
        statusCode = startCpadPulse(command, requestId, argument, aux);

    if (statusCode != POKEBOT_STATUS_OK)
        return statusCode;

    if (payloadCapacity < sizeof(PokebotInputStatusPayload))
        return POKEBOT_STATUS_INPUT_INVALID;
    PokebotInputStatusPayload status;
    fillStatus(&status, 0xFFFFFFFFUL);
    memcpy(payload, &status, sizeof(status));
    *payloadLength = sizeof(status);
    return POKEBOT_STATUS_OK;
}
'''

controller_h.write_text(header, encoding="utf-8")
controller_c.write_text(source, encoding="utf-8")

text = input_c.read_text(encoding="utf-8")
if "void PokebotInput_SetRemoteHid(u32 rawHid)" not in text:
    marker = 'static u32 irData[] = { 0x80800081 }; // Default: C-Stick at the center, no buttons.\n'
    if marker not in text:
        raise SystemExit("input_redirection hidData marker not found")
    helper = r'''

/* Pokebot-Luma v0p5 acknowledged-controller accessors. */
void PokebotInput_SetRemoteHid(u32 rawHid)
{
    volatile u32 *remote = PA_FROM_VA_PTR(hidData);
    remote[5] = rawHid & 0x00000FFF;
}

void PokebotInput_SetRemoteTouch(u32 touchState)
{
    volatile u32 *remote = PA_FROM_VA_PTR(hidData);
    remote[6] = touchState;
}

void PokebotInput_SetRemoteCircle(u32 circleState)
{
    volatile u32 *remote = PA_FROM_VA_PTR(hidData);
    remote[7] = circleState & 0x00FFFFFF;
}

void PokebotInput_ResetRemote(void)
{
    volatile u32 *remote = PA_FROM_VA_PTR(hidData);
    remote[5] = 0x00000FFF;
    remote[6] = 0x02000000;
    remote[7] = 0x007FF7FF;
}
'''
    text = text.replace(marker, marker + helper, 1)
    input_c.write_text(text, encoding="utf-8")

text = input_h.read_text(encoding="utf-8")
if "void PokebotInput_SetRemoteHid(u32 rawHid);" not in text:
    text = text.rstrip() + r'''

/* Pokebot-Luma v0p5 acknowledged-controller helpers. */
void PokebotInput_SetRemoteHid(u32 rawHid);
void PokebotInput_SetRemoteTouch(u32 touchState);
void PokebotInput_SetRemoteCircle(u32 circleState);
void PokebotInput_ResetRemote(void);
''' + "\n"
    input_h.write_text(text, encoding="utf-8")

text = bridge_c.read_text(encoding="utf-8")
if '#include "pokebot_input_controller.h"' not in text:
    marker = '#include "pokebot_ram_bridge.h"\n'
    if marker not in text:
        raise SystemExit("bridge include marker not found")
    text = text.replace(marker, marker + '#include "pokebot_input_controller.h"\n', 1)

text = text.replace('"Pokebot3DS-Luma-v0p4"', '"Pokebot3DS-Luma-v0p5"')

route_marker = '    PokebotTarget target;\n'
if 'PokebotInputController_Handle(' not in text:
    if route_marker not in text:
        raise SystemExit("bridge route marker not found")
    route = r'''    if ((req->command >= 5 && req->command <= 10) || req->command == 13)
    {
        u8 inputPayload[32];
        u32 inputPayloadLength = 0;
        s32 inputResult = 0;
        u16 inputStatus = PokebotInputController_Handle(
            req->command,
            req->requestId,
            req->argument,
            req->aux,
            inputPayload,
            sizeof(inputPayload),
            &inputPayloadLength,
            &inputResult);
        Pokebot_SendResponse(
            sock, remote, remoteLen, req,
            (PokebotStatus)inputStatus,
            (Result)inputResult,
            inputPayloadLength != 0 ? inputPayload : NULL,
            inputPayloadLength);
        return;
    }

'''
    text = text.replace(route_marker, route + route_marker, 1)

loop_marker = '    while (pokebotRamBridgeEnabled && !preTerminationRequested)\n    {\n'
if '        PokebotInputController_Update();\n' not in text:
    if loop_marker not in text:
        raise SystemExit("bridge loop marker not found")
    text = text.replace(loop_marker, loop_marker + '        PokebotInputController_Update();\n\n', 1)

exit_marker = '    pokebotRamBridgeEnabled = false;\n    socClose(sock);\n'
if '    PokebotInputController_ReleaseAll();\n    pokebotRamBridgeEnabled = false;' not in text:
    if exit_marker not in text:
        raise SystemExit("bridge exit marker not found")
    text = text.replace(exit_marker, '    PokebotInputController_ReleaseAll();\n' + exit_marker, 1)

disable_marker = 'Result PokebotRamBridge_Disable(s64 timeout)\n{\n    if (!pokebotRamBridgeEnabled)\n        return 0;\n'
if '    if (pokebotInputControllerEnabled)\n        return (Result)-13;\n' not in text:
    if disable_marker not in text:
        raise SystemExit("bridge disable marker not found")
    text = text.replace(disable_marker, disable_marker + '    if (pokebotInputControllerEnabled)\n        return (Result)-13;\n', 1)

bridge_c.write_text(text, encoding="utf-8")

text = menus_c.read_text(encoding="utf-8")
if '#include "pokebot_input_controller.h"' not in text:
    marker = '#include "pokebot_ram_bridge.h"\n'
    if marker not in text:
        raise SystemExit("menu controller include marker not found")
    text = text.replace(marker, marker + '#include "pokebot_input_controller.h"\n', 1)

start = text.find('static Result PokebotBridge_SetInputEnabled(bool enable)\n{')
end = text.find('static void PokebotBridge_ToggleRam(void)\n{', start)
if start < 0 or end < 0:
    raise SystemExit("menu input-enable block not found")
replacement = r'''static Result PokebotBridge_SetInputEnabled(bool enable)
{
    if (enable && !pokebotRamBridgeEnabled)
    {
        Result res = PokebotRamBridge_Start();
        if (R_FAILED(res))
            return res;
    }
    return PokebotInputController_SetEnabled(enable);
}

'''
text = text[:start] + replacement + text[end:]

text = text.replace('!inputRedirectionEnabled);', '!pokebotInputControllerEnabled);')
text = text.replace('if (!inputRedirectionEnabled)\n        sPokebotInputResult = PokebotBridge_SetInputEnabled(true);', 'if (!pokebotInputControllerEnabled)\n        sPokebotInputResult = PokebotBridge_SetInputEnabled(true);')

old_disable = r'''static void PokebotBridge_DisableBoth(void)
{
    if (pokebotRamBridgeEnabled)
        sPokebotRamResult = PokebotRamBridge_Disable(5 * 1000 * 1000 * 1000LL);
    if (inputRedirectionEnabled)
        sPokebotInputResult = PokebotBridge_SetInputEnabled(false);
}
'''
new_disable = r'''static void PokebotBridge_DisableBoth(void)
{
    if (pokebotInputControllerEnabled)
        sPokebotInputResult = PokebotBridge_SetInputEnabled(false);
    if (pokebotRamBridgeEnabled)
        sPokebotRamResult = PokebotRamBridge_Disable(5 * 1000 * 1000 * 1000LL);
}
'''
if old_disable not in text:
    raise SystemExit("menu Disable Both block not found")
text = text.replace(old_disable, new_disable, 1)

text = text.replace('"Pokebot-Luma v0p4\\n\\n"', '"Pokebot-Luma v0p5\\n\\n"')
text = text.replace('"Input UDP:        4950\\n\\n",', '"Input UDP:        4952\\n"\n            "Native IR 4950:   %s\\n\\n",')
text = text.replace('            inputRedirectionEnabled ? "ON" : "OFF",\n            (u32)sPokebotInputResult);', '            pokebotInputControllerEnabled ? "ON" : "OFF",\n            (u32)sPokebotInputResult,\n            inputRedirectionEnabled ? "ACTIVE - DISABLE" : "OFF");')
text = text.replace('"Controller: v0p3 additive path unchanged.\\n\\n"', '"Controller: ACK/status/dedupe/touch/latch.\\n"\n            "Physical buttons remain additive.\\n\\n"')

menus_c.write_text(text, encoding="utf-8")
