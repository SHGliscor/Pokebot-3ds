from pathlib import Path

root = Path(__file__).resolve().parents[2] / "Luma3DS"
source_dir = root / "sysmodules" / "rosalina" / "source"
include_dir = root / "sysmodules" / "rosalina" / "include"
source_path = source_dir / "pokebot_ram_bridge.c"
header_path = include_dir / "pokebot_ram_bridge.h"

header = r'''/*
 * Pokebot3DS read-only RAM bridge for Pokebot-Luma.
 *
 * This file is added to a Luma3DS-derived build and remains subject to the
 * licensing terms of that build. The bridge exposes only bounded QUERY/READ
 * operations; it deliberately provides no game-memory write command.
 */
#pragma once

#include <3ds.h>
#include "MyThread.h"

extern bool pokebotRamBridgeEnabled;
extern Handle pokebotRamBridgeThreadStartedEvent;
extern int pokebotRamBridgeStartResult;
extern volatile u32 pokebotRamBridgePackets;
extern volatile u32 pokebotRamBridgeReads;

MyThread *PokebotRamBridge_CreateThread(void);
void PokebotRamBridge_ThreadMain(void);
Result PokebotRamBridge_Start(void);
Result PokebotRamBridge_Disable(s64 timeout);
'''

source = r'''/*
 * Pokebot3DS read-only RAM bridge for Pokebot-Luma v0p4.
 *
 * Protocol compatibility target: the previously proven Pokebot bridge
 * request/response framing used by the earlier Pokebot bridge prototypes.
 *
 * Security/safety invariant: QUERY and bounded READ only. There is no RAM
 * write command in this module.
 */
#include <3ds.h>
#include <arpa/inet.h>
#include <poll.h>
#include <string.h>

#include "csvc.h"
#include "menus.h"
#include "minisoc.h"
#include "pmdbgext.h"
#include "pokebot_ram_bridge.h"
#include "sleep.h"

#define POKEBOT_RAM_PORT       4952
#define POKEBOT_REQ_MAGIC      0x5242524FUL
#define POKEBOT_RESP_MAGIC     0x5342524FUL
#define POKEBOT_PROTOCOL_VER   1
#define POKEBOT_MAX_READ       0x200
#define POKEBOT_MAP_ADDR       0x00100000UL

#define POKEBOT_X_TID  0x0004000000055D00ULL
#define POKEBOT_Y_TID  0x0004000000055E00ULL
#define POKEBOT_OR_TID 0x000400000011C400ULL
#define POKEBOT_AS_TID 0x000400000011C500ULL

typedef enum PokebotCommand
{
    POKEBOT_CMD_PING      = 1,
    POKEBOT_CMD_GAME_INFO = 2,
    POKEBOT_CMD_QUERY     = 3,
    POKEBOT_CMD_READ      = 4,
} PokebotCommand;

typedef enum PokebotStatus
{
    POKEBOT_STATUS_OK             = 0,
    POKEBOT_STATUS_BAD_MAGIC      = 1,
    POKEBOT_STATUS_BAD_VERSION    = 2,
    POKEBOT_STATUS_BAD_COMMAND    = 3,
    POKEBOT_STATUS_GAME_NOT_FOUND = 4,
    POKEBOT_STATUS_OPEN_FAILED    = 5,
    POKEBOT_STATUS_QUERY_FAILED   = 6,
    POKEBOT_STATUS_NOT_READABLE   = 7,
    POKEBOT_STATUS_RANGE_INVALID  = 8,
    POKEBOT_STATUS_LENGTH_INVALID = 9,
    POKEBOT_STATUS_MAP_FAILED     = 10,
    POKEBOT_STATUS_INTERNAL       = 11,
} PokebotStatus;

#pragma pack(push, 1)
typedef struct PokebotRequest
{
    u32 magic;
    u16 version;
    u16 command;
    u32 requestId;
    u32 argument;
    u32 aux;
} PokebotRequest;

typedef struct PokebotResponse
{
    u32 magic;
    u16 version;
    u16 status;
    u32 requestId;
    u32 argument;
    s32 result;
    u32 payloadLength;
} PokebotResponse;

typedef struct PokebotGameInfo
{
    u64 titleId;
    u32 processId;
    char processName[8];
    u32 flags;
} PokebotGameInfo;

typedef struct PokebotQueryInfo
{
    u32 base;
    u32 size;
    u32 perm;
    u32 state;
    u32 pageFlags;
} PokebotQueryInfo;
#pragma pack(pop)

typedef struct PokebotTarget
{
    Handle process;
    u64 titleId;
    u32 pid;
    u32 launchFlags;
} PokebotTarget;

bool pokebotRamBridgeEnabled = false;
Handle pokebotRamBridgeThreadStartedEvent;
int pokebotRamBridgeStartResult = 0;
volatile u32 pokebotRamBridgePackets = 0;
volatile u32 pokebotRamBridgeReads = 0;

static MyThread sPokebotRamThread;
static u8 CTR_ALIGN(8) sPokebotRamThreadStack[0x4000];

static bool Pokebot_IsSupportedTitle(u64 tid)
{
    return tid == POKEBOT_X_TID || tid == POKEBOT_Y_TID ||
           tid == POKEBOT_OR_TID || tid == POKEBOT_AS_TID;
}

static const char *Pokebot_ProcessName(u64 tid)
{
    if (tid == POKEBOT_X_TID)
        return "kujira-1";
    if (tid == POKEBOT_Y_TID)
        return "kujira-2";
    if (tid == POKEBOT_OR_TID)
        return "sango-1";
    if (tid == POKEBOT_AS_TID)
        return "sango-2";
    return "unknown";
}

static Result Pokebot_OpenCurrentTarget(PokebotTarget *target)
{
    FS_ProgramInfo info;
    u32 pid = 0;
    u32 launchFlags = 0;
    memset(target, 0, sizeof(*target));

    Result res = PMDBG_GetCurrentAppInfo(&info, &pid, &launchFlags);
    if (R_FAILED(res))
        return res;
    if (!Pokebot_IsSupportedTitle(info.programId))
        return (Result)-4;

    Handle process = 0;
    res = svcOpenProcess(&process, pid);
    if (R_FAILED(res))
        return res;

    target->process = process;
    target->titleId = info.programId;
    target->pid = pid;
    target->launchFlags = launchFlags;
    return 0;
}

static Result Pokebot_QueryTarget(Handle process, u32 address, PokebotQueryInfo *out)
{
    MemInfo info;
    PageInfo page;
    Result res = svcQueryProcessMemory(&info, &page, process, address);
    if (R_FAILED(res))
        return res;

    out->base = info.base_addr;
    out->size = info.size;
    out->perm = info.perm;
    out->state = info.state;
    out->pageFlags = page.flags;
    return 0;
}

static PokebotStatus Pokebot_ReadTarget(
    Handle process,
    u32 address,
    u32 length,
    void *out,
    Result *outResult)
{
    *outResult = 0;
    if (length == 0 || length > POKEBOT_MAX_READ)
        return POKEBOT_STATUS_LENGTH_INVALID;

    u64 end = (u64)address + (u64)length;
    if (end > 0x100000000ULL)
        return POKEBOT_STATUS_RANGE_INVALID;

    PokebotQueryInfo query;
    Result res = Pokebot_QueryTarget(process, address, &query);
    if (R_FAILED(res))
    {
        *outResult = res;
        return POKEBOT_STATUS_QUERY_FAILED;
    }

    u64 regionEnd = (u64)query.base + (u64)query.size;
    if (address < query.base || end > regionEnd)
        return POKEBOT_STATUS_RANGE_INVALID;
    if ((query.perm & MEMPERM_READ) == 0)
        return POKEBOT_STATUS_NOT_READABLE;

    u32 sourceBase = address & ~0xFFFUL;
    u32 sourceEnd = (u32)((end + 0xFFFULL) & ~0xFFFULL);
    u32 mapSize = sourceEnd - sourceBase;
    if (mapSize == 0)
        mapSize = 0x1000;

    res = svcMapProcessMemoryEx(
        CUR_PROCESS_HANDLE,
        POKEBOT_MAP_ADDR,
        process,
        sourceBase,
        mapSize,
        0);
    if (R_FAILED(res))
    {
        *outResult = res;
        return POKEBOT_STATUS_MAP_FAILED;
    }

    memcpy(out, (const void *)(POKEBOT_MAP_ADDR + (address - sourceBase)), length);
    Result unmapRes = svcUnmapProcessMemoryEx(CUR_PROCESS_HANDLE, POKEBOT_MAP_ADDR, mapSize);
    if (R_FAILED(unmapRes))
    {
        *outResult = unmapRes;
        return POKEBOT_STATUS_INTERNAL;
    }

    pokebotRamBridgeReads++;
    return POKEBOT_STATUS_OK;
}

static void Pokebot_SendResponse(
    int sock,
    const struct sockaddr_in *remote,
    socklen_t remoteLen,
    const PokebotRequest *req,
    PokebotStatus status,
    Result result,
    const void *payload,
    u32 payloadLength)
{
    u8 buffer[sizeof(PokebotResponse) + POKEBOT_MAX_READ + 32];
    PokebotResponse response;
    memset(&response, 0, sizeof(response));
    response.magic = POKEBOT_RESP_MAGIC;
    response.version = POKEBOT_PROTOCOL_VER;
    response.status = (u16)status;
    response.requestId = req->requestId;
    response.argument = req->argument;
    response.result = (s32)result;
    response.payloadLength = payloadLength;

    memcpy(buffer, &response, sizeof(response));
    if (payload != NULL && payloadLength != 0)
        memcpy(buffer + sizeof(response), payload, payloadLength);

    socSendto(sock, buffer, sizeof(response) + payloadLength, 0,
              (const struct sockaddr *)remote, remoteLen);
}

static void Pokebot_HandleRequest(
    int sock,
    const struct sockaddr_in *remote,
    socklen_t remoteLen,
    const PokebotRequest *req)
{
    if (req->magic != POKEBOT_REQ_MAGIC)
    {
        Pokebot_SendResponse(sock, remote, remoteLen, req, POKEBOT_STATUS_BAD_MAGIC, 0, NULL, 0);
        return;
    }
    if (req->version != POKEBOT_PROTOCOL_VER)
    {
        Pokebot_SendResponse(sock, remote, remoteLen, req, POKEBOT_STATUS_BAD_VERSION, 0, NULL, 0);
        return;
    }

    if (req->command == POKEBOT_CMD_PING)
    {
        static const char payload[] = "Pokebot3DS-Luma-v0p4";
        Pokebot_SendResponse(sock, remote, remoteLen, req, POKEBOT_STATUS_OK, 0,
                             payload, sizeof(payload) - 1);
        return;
    }

    PokebotTarget target;
    Result res = Pokebot_OpenCurrentTarget(&target);
    if (R_FAILED(res))
    {
        PokebotStatus status = res == (Result)-4 ? POKEBOT_STATUS_GAME_NOT_FOUND : POKEBOT_STATUS_OPEN_FAILED;
        Pokebot_SendResponse(sock, remote, remoteLen, req, status, res, NULL, 0);
        return;
    }

    if (req->command == POKEBOT_CMD_GAME_INFO)
    {
        PokebotGameInfo info;
        memset(&info, 0, sizeof(info));
        info.titleId = target.titleId;
        info.processId = target.pid;
        strncpy(info.processName, Pokebot_ProcessName(target.titleId), sizeof(info.processName));
        info.flags = 0x00030001UL;
        svcCloseHandle(target.process);
        Pokebot_SendResponse(sock, remote, remoteLen, req, POKEBOT_STATUS_OK, 0,
                             &info, sizeof(info));
        return;
    }

    if (req->command == POKEBOT_CMD_QUERY)
    {
        PokebotQueryInfo info;
        res = Pokebot_QueryTarget(target.process, req->argument, &info);
        svcCloseHandle(target.process);
        if (R_FAILED(res))
        {
            Pokebot_SendResponse(sock, remote, remoteLen, req, POKEBOT_STATUS_QUERY_FAILED, res, NULL, 0);
            return;
        }
        Pokebot_SendResponse(sock, remote, remoteLen, req, POKEBOT_STATUS_OK, 0,
                             &info, sizeof(info));
        return;
    }

    if (req->command == POKEBOT_CMD_READ)
    {
        u8 data[POKEBOT_MAX_READ];
        Result readResult = 0;
        PokebotStatus status = Pokebot_ReadTarget(
            target.process, req->argument, req->aux, data, &readResult);
        svcCloseHandle(target.process);
        Pokebot_SendResponse(sock, remote, remoteLen, req, status, readResult,
                             status == POKEBOT_STATUS_OK ? data : NULL,
                             status == POKEBOT_STATUS_OK ? req->aux : 0);
        return;
    }

    svcCloseHandle(target.process);
    Pokebot_SendResponse(sock, remote, remoteLen, req, POKEBOT_STATUS_BAD_COMMAND, 0, NULL, 0);
}

MyThread *PokebotRamBridge_CreateThread(void)
{
    if (R_FAILED(MyThread_Create(&sPokebotRamThread, PokebotRamBridge_ThreadMain,
                                 sPokebotRamThreadStack, sizeof(sPokebotRamThreadStack),
                                 0x24, CORE_SYSTEM)))
        svcBreak(USERBREAK_PANIC);
    return &sPokebotRamThread;
}

void PokebotRamBridge_ThreadMain(void)
{
    Result res = 0;
    pokebotRamBridgeStartResult = 0;

    res = miniSocInit();
    if (R_FAILED(res))
    {
        pokebotRamBridgeStartResult = res;
        svcSignalEvent(pokebotRamBridgeThreadStartedEvent);
        return;
    }

    int sock = socSocket(AF_INET, SOCK_DGRAM, 0);
    u32 tries = 15;
    while (sock < 0 && --tries > 0)
    {
        svcSleepThread(100 * 1000 * 1000LL);
        sock = socSocket(AF_INET, SOCK_DGRAM, 0);
    }

    if (sock < 0)
    {
        pokebotRamBridgeStartResult = -1;
        miniSocExit();
        svcSignalEvent(pokebotRamBridgeThreadStartedEvent);
        return;
    }

    struct sockaddr_in bindAddr;
    memset(&bindAddr, 0, sizeof(bindAddr));
    bindAddr.sin_family = AF_INET;
    bindAddr.sin_port = htons(POKEBOT_RAM_PORT);
    bindAddr.sin_addr.s_addr = socGethostid();
    res = socBind(sock, (struct sockaddr *)&bindAddr, sizeof(bindAddr));
    if (res != 0)
    {
        pokebotRamBridgeStartResult = res;
        socClose(sock);
        miniSocExit();
        svcSignalEvent(pokebotRamBridgeThreadStartedEvent);
        return;
    }

    pokebotRamBridgePackets = 0;
    pokebotRamBridgeReads = 0;
    pokebotRamBridgeEnabled = true;
    svcSignalEvent(pokebotRamBridgeThreadStartedEvent);

    while (pokebotRamBridgeEnabled && !preTerminationRequested)
    {
        if (Sleep__Status())
        {
            while (!Wifi__IsConnected() && pokebotRamBridgeEnabled && !preTerminationRequested)
                svcSleepThread(1000000000ULL);
        }

        struct pollfd pfd;
        pfd.fd = sock;
        pfd.events = POLLIN;
        pfd.revents = 0;
        int pollres = socPoll(&pfd, 1, 20);
        if (pollres > 0 && (pfd.revents & POLLIN))
        {
            PokebotRequest req;
            struct sockaddr_in remote;
            socklen_t remoteLen = sizeof(remote);
            ssize_t n = socRecvfrom(sock, &req, sizeof(req), 0,
                                    (struct sockaddr *)&remote, &remoteLen);
            if (n < 0)
                break;
            if ((u32)n != sizeof(req))
                continue;

            pokebotRamBridgePackets++;
            Pokebot_HandleRequest(sock, &remote, remoteLen, &req);
        }
        else if (pollres < -10000)
            break;
    }

    pokebotRamBridgeEnabled = false;
    socClose(sock);
    miniSocExit();
}

Result PokebotRamBridge_Start(void)
{
    if (pokebotRamBridgeEnabled)
        return 0;

    pokebotRamBridgeStartResult = 0;
    Result res = svcCreateEvent(&pokebotRamBridgeThreadStartedEvent, RESET_STICKY);
    if (R_FAILED(res))
        return res;

    PokebotRamBridge_CreateThread();
    res = svcWaitSynchronization(pokebotRamBridgeThreadStartedEvent,
                                 10 * 1000 * 1000 * 1000LL);
    if (res == 0)
        res = (Result)pokebotRamBridgeStartResult;

    if (res != 0)
    {
        svcCloseHandle(pokebotRamBridgeThreadStartedEvent);
        pokebotRamBridgeEnabled = false;
    }
    pokebotRamBridgeStartResult = (int)res;
    return res;
}

Result PokebotRamBridge_Disable(s64 timeout)
{
    if (!pokebotRamBridgeEnabled)
        return 0;

    pokebotRamBridgeEnabled = false;
    Result res = MyThread_Join(&sPokebotRamThread, timeout);
    svcCloseHandle(pokebotRamBridgeThreadStartedEvent);
    return res;
}
'''

header_path.write_text(header, encoding="utf-8")
source_path.write_text(source, encoding="utf-8")
