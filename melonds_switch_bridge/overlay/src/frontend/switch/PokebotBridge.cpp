#include "PokebotBridge.h"

#include <switch.h>

#include <arpa/inet.h>
#include <atomic>
#include <cerrno>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include "NDS.h"

namespace PokebotBridge
{
namespace
{
constexpr u32 RequestMagic = 0x52425344;  // DSBR
constexpr u32 ResponseMagic = 0x53425344; // DSBS
constexpr u16 Version = 1;
constexpr u32 DSMainRAMBase = 0x02000000;
constexpr u32 DSMainRAMSize = 0x00400000;
constexpr u32 MaxRead = 4096;
constexpr size_t ThreadStackSize = 0x8000;
constexpr int ThreadPriority = 0x2C;

constexpr u16 CmdPing = 1;
constexpr u16 CmdInfo = 2;
constexpr u16 CmdRead = 3;

constexpr s32 StatusOK = 0;
constexpr s32 StatusBadRequest = -1;
constexpr s32 StatusBadCommand = -2;
constexpr s32 StatusBadRange = -3;
constexpr s32 StatusNotReady = -4;

#pragma pack(push, 1)
struct Request
{
    u32 magic;
    u16 version;
    u16 command;
    u32 request_id;
    u32 address;
    u32 length;
};

struct Response
{
    u32 magic;
    u16 version;
    u16 command;
    u32 request_id;
    s32 status;
    u32 length;
};

struct InfoPayload
{
    u32 mainram_size;
    u32 console_type;
};
#pragma pack(pop)

static_assert(sizeof(Request) == 20, "unexpected Pokebot bridge request size");
static_assert(sizeof(Response) == 20, "unexpected Pokebot bridge response size");
static_assert(sizeof(InfoPayload) == 8, "unexpected Pokebot bridge info size");

Thread BridgeThread;
std::atomic<bool> Running{false};
bool ThreadStarted = false;
int SocketFD = -1;

bool ValidReadRange(u32 address, u32 length)
{
    if (length == 0 || length > MaxRead)
        return false;

    const u64 start = address;
    const u64 end = start + length;
    return start >= DSMainRAMBase && end <= (u64)DSMainRAMBase + DSMainRAMSize;
}

void SendResponse(
    const sockaddr_in& peer,
    socklen_t peerLen,
    const Request& request,
    s32 status,
    const void* payload,
    u32 payloadLen)
{
    alignas(8) u8 buffer[sizeof(Response) + MaxRead];
    Response response{
        ResponseMagic,
        Version,
        request.command,
        request.request_id,
        status,
        payloadLen
    };

    memcpy(buffer, &response, sizeof(response));
    if (payloadLen && payload)
        memcpy(buffer + sizeof(response), payload, payloadLen);

    sendto(
        SocketFD,
        buffer,
        sizeof(response) + payloadLen,
        0,
        reinterpret_cast<const sockaddr*>(&peer),
        peerLen);
}

void HandleRequest(const Request& request, const sockaddr_in& peer, socklen_t peerLen)
{
    if (request.magic != RequestMagic || request.version != Version)
    {
        SendResponse(peer, peerLen, request, StatusBadRequest, nullptr, 0);
        return;
    }

    switch (request.command)
    {
    case CmdPing:
    {
        static constexpr char Name[] = "POKEBOT_MELONDS_BRIDGE_V1";
        SendResponse(peer, peerLen, request, StatusOK, Name, sizeof(Name) - 1);
        return;
    }

    case CmdInfo:
    {
        InfoPayload info{DSMainRAMSize, static_cast<u32>(NDS::ConsoleType)};
        SendResponse(peer, peerLen, request, StatusOK, &info, sizeof(info));
        return;
    }

    case CmdRead:
    {
        if (!ValidReadRange(request.address, request.length))
        {
            SendResponse(peer, peerLen, request, StatusBadRange, nullptr, 0);
            return;
        }
        if (!NDS::MainRAM)
        {
            SendResponse(peer, peerLen, request, StatusNotReady, nullptr, 0);
            return;
        }

        const u32 offset = request.address - DSMainRAMBase;
        SendResponse(
            peer,
            peerLen,
            request,
            StatusOK,
            NDS::MainRAM + offset,
            request.length);
        return;
    }

    default:
        SendResponse(peer, peerLen, request, StatusBadCommand, nullptr, 0);
        return;
    }
}

void ThreadMain(void*)
{
    while (Running.load(std::memory_order_relaxed))
    {
        Request request{};
        sockaddr_in peer{};
        socklen_t peerLen = sizeof(peer);
        const ssize_t received = recvfrom(
            SocketFD,
            &request,
            sizeof(request),
            0,
            reinterpret_cast<sockaddr*>(&peer),
            &peerLen);

        if (received < 0)
        {
            if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)
            {
                svcSleepThread(2000000); // 2 ms
                continue;
            }
            svcSleepThread(5000000); // 5 ms on unexpected socket errors
            continue;
        }

        if (received != static_cast<ssize_t>(sizeof(Request)))
            continue;

        HandleRequest(request, peer, peerLen);
    }
}
} // namespace

bool Init()
{
    if (Running.load(std::memory_order_relaxed))
        return true;

    SocketFD = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (SocketFD < 0)
    {
        printf("Pokebot bridge: socket() failed (%d)\n", errno);
        return false;
    }

    int reuse = 1;
    setsockopt(SocketFD, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));

    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_port = htons(Port);
    address.sin_addr.s_addr = htonl(INADDR_ANY);
    if (bind(SocketFD, reinterpret_cast<sockaddr*>(&address), sizeof(address)) < 0)
    {
        printf("Pokebot bridge: bind UDP %u failed (%d)\n", Port, errno);
        close(SocketFD);
        SocketFD = -1;
        return false;
    }

    const int flags = fcntl(SocketFD, F_GETFL, 0);
    if (flags >= 0)
        fcntl(SocketFD, F_SETFL, flags | O_NONBLOCK);

    Running.store(true, std::memory_order_relaxed);
    Result rc = threadCreate(
        &BridgeThread,
        ThreadMain,
        nullptr,
        nullptr,
        ThreadStackSize,
        ThreadPriority,
        -2);
    if (R_FAILED(rc))
    {
        Running.store(false, std::memory_order_relaxed);
        close(SocketFD);
        SocketFD = -1;
        printf("Pokebot bridge: threadCreate failed 0x%08X\n", rc);
        return false;
    }

    rc = threadStart(&BridgeThread);
    if (R_FAILED(rc))
    {
        Running.store(false, std::memory_order_relaxed);
        threadClose(&BridgeThread);
        close(SocketFD);
        SocketFD = -1;
        printf("Pokebot bridge: threadStart failed 0x%08X\n", rc);
        return false;
    }

    ThreadStarted = true;
    printf("Pokebot bridge: read-only DS RAM UDP bridge listening on port %u\n", Port);
    return true;
}

void DeInit()
{
    Running.store(false, std::memory_order_relaxed);

    if (ThreadStarted)
    {
        threadWaitForExit(&BridgeThread);
        threadClose(&BridgeThread);
        ThreadStarted = false;
    }

    if (SocketFD >= 0)
    {
        close(SocketFD);
        SocketFD = -1;
    }
}

bool IsRunning()
{
    return Running.load(std::memory_order_relaxed);
}
} // namespace PokebotBridge
