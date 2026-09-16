#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "melonDS-lua").resolve()


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one anchor, got {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


cpp = ROOT / "src/frontend/qt_sdl/ScriptManager.cpp"
mem = ROOT / "lua/core/memory.lua"
sol = ROOT / "sol/sol.hpp"
cmake_qt = ROOT / "src/frontend/qt_sdl/CMakeLists.txt"
script_h = ROOT / "src/frontend/qt_sdl/ScriptManager.h"
emu_thread = ROOT / "src/frontend/qt_sdl/EmuThread.cpp"
emu_lua = ROOT / "lua/core/emu.lua"

replace_once(
    cpp,
    """static int read_s32_le(u32 address) {
    if (address == -1) return -1;
    return nds->ARM9Read32(address);
}
""",
    """static int read_s32_le(u32 address) {
    if (address == -1) return -1;
    return nds->ARM9Read32(address);
}

static std::string read_block(u32 address, u32 size) {
    if (size == 0 || size > 0x400000)
        return {};

    std::string out;
    out.resize(size);
    for (u32 i = 0; i < size; i++)
        out[i] = static_cast<char>(nds->ARM9Read8(address + i));
    return out;
}
""",
)

replace_once(
    cpp,
    """    native.set_function("read_s32", &read_s32_le);
""",
    """    native.set_function("read_s32", &read_s32_le);
    native.set_function("read_block", &read_block);
""",
)

replace_once(
    cpp,
    """    // reset current game
    native.set_function("reset", []()
    {
        emuInstances[0]->getEmuThread()->emuReset();
    });

""",
    """    // reset current game
    native.set_function("reset", []()
    {
        emuInstances[0]->getEmuThread()->emuReset();
    });

    // Pokebot navigation can use melonDS' existing fast-forward path while
    // booting through title/text, then restore normal speed before evaluating
    // a generated target.
    native.set_function("set_fast_forward", [](bool enabled)
    {
        emuInstances[0]->fastForwardToggled = enabled;
    });

""",
)

replace_once(
    emu_lua,
    """-- Resets current running game
function emu.reset()
    native.reset()
end

""",
    """-- Resets current running game
function emu.reset()
    native.reset()
end

-- Enables/disables melonDS fast-forward.
---@param enabled boolean
function emu.setFastForward(enabled)
    native.set_fast_forward(enabled)
end

""",
)

replace_once(
    mem,
    """function memory.read_s32(addr)
    return native.read_s32(addr)
end
""",
    """function memory.read_s32(addr)
    return native.read_s32(addr)
end

---@param addr number Address
---@param size number Byte length (1..4 MiB)
---@return string
function memory.read_block(addr, size)
    return native.read_block(addr, size)
end
""",
)

replace_once(
    sol,
    """\t\t\t*this = nullopt;
\t\t\tthis->construct(std::forward<Args>(args)...);
\t\t}
""",
    """\t\t\t*this = nullopt;
\t\t\tnew (static_cast<void*>(this)) optional(std::in_place, std::forward<Args>(args)...);
\t\t\treturn **this;
\t\t}
""",
)

replace_once(
    cmake_qt,
    """target_link_libraries(melonDS PRIVATE ${QT_LINK_LIBS} ${CMAKE_DL_LIBS} dl m)
""",
    """if (WIN32)
    target_link_libraries(melonDS PRIVATE ${QT_LINK_LIBS} m)
else()
    target_link_libraries(melonDS PRIVATE ${QT_LINK_LIBS} ${CMAKE_DL_LIBS} dl m)
endif()
""",
)


replace_once(
    script_h,
    """    bool externalInputsBlocked() const { return luaInputActive; }
""",
    """    bool externalInputsBlocked() const { return luaInputActive; }
    melonDS::u32 externalInputMask() const { return luaInputMask.load(); }
""",
)

replace_once(
    emu_thread,
    """            // process input and hotkeys if not blocked
            if (!scriptManager.externalInputsBlocked())
                emuInstance->nds->SetKeyMask(emuInstance->inputMask);
""",
    """            // Apply Pokebot/Lua input every emulation frame while it owns
            // input. This makes injected controls independent of Qt window
            // focus and prevents focus-loss keyboard cleanup from cancelling
            // a bot-held key.
            if (scriptManager.externalInputsBlocked())
                emuInstance->nds->SetKeyMask(scriptManager.externalInputMask());
            else
                emuInstance->nds->SetKeyMask(emuInstance->inputMask);
""",
)


# Native localhost UDP bridge for Pokebot. Normal hunting uses this path and
# does not require a Lua script or per-frame filesystem polling.
replace_once(
    script_h,
    """#include <sol/sol.hpp>
""",
    """#include <sol/sol.hpp>
#include <array>
#include <cstdint>
""",
)

replace_once(
    script_h,
    """    ScriptManager();
    ~ScriptManager() { resetAll(); } // ?
""",
    """    ScriptManager();
    ~ScriptManager();

    void pollPokebotBridge();
""",
)

replace_once(
    script_h,
    """private:
    // reset variables, callbacks
""",
    """private:
    void initPokebotBridge();
    void shutdownPokebotBridge();

    std::intptr_t pokebotSocket = -1;
    bool pokebotWSAStarted = false;
    std::array<int, 12> pokebotPulseFrames{};

    // reset variables, callbacks
""",
)

replace_once(
    cpp,
    """#include <InputConfig/InputConfigDialog.h>

#include "ScriptManager.h"
""",
    """#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
#endif

#include <InputConfig/InputConfigDialog.h>

#include <algorithm>
#include <array>
#include <cstring>
#include <vector>

#include "ScriptManager.h"
""",
)

replace_once(
    cpp,
    """ScriptManager::ScriptManager() : running(false) {}
""",
    r"""ScriptManager::ScriptManager() : running(false)
{
    initPokebotBridge();
}

ScriptManager::~ScriptManager()
{
    shutdownPokebotBridge();
    resetAll();
}

void ScriptManager::initPokebotBridge()
{
#ifdef _WIN32
    WSADATA data{};
    if (WSAStartup(MAKEWORD(2, 2), &data) != 0)
        return;
    pokebotWSAStarted = true;

    SOCKET sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (sock == INVALID_SOCKET)
        return;

    u_long nonblocking = 1;
    ioctlsocket(sock, FIONBIO, &nonblocking);

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(4953);
    inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);

    if (bind(sock, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) == SOCKET_ERROR)
    {
        closesocket(sock);
        return;
    }

    pokebotSocket = static_cast<std::intptr_t>(sock);
    printf("[Pokebot] native UDP bridge listening on 127.0.0.1:4953\n");
#endif
}

void ScriptManager::shutdownPokebotBridge()
{
#ifdef _WIN32
    if (pokebotSocket != -1)
    {
        closesocket(static_cast<SOCKET>(pokebotSocket));
        pokebotSocket = -1;
    }
    if (pokebotWSAStarted)
    {
        WSACleanup();
        pokebotWSAStarted = false;
    }
#endif
}

void ScriptManager::pollPokebotBridge()
{
#ifdef _WIN32
    // Frame-based pulse release. DS key bits are active-low.
    for (int bit = 0; bit < 12; ++bit)
    {
        if (pokebotPulseFrames[bit] <= 0)
            continue;
        --pokebotPulseFrames[bit];
        if (pokebotPulseFrames[bit] == 0)
            luaInputMask.fetch_or(1u << bit);
    }

    if (pokebotSocket == -1)
        return;

    SOCKET sock = static_cast<SOCKET>(pokebotSocket);

    auto read16 = [](const unsigned char* p) -> unsigned {
        return unsigned(p[0]) | (unsigned(p[1]) << 8);
    };
    auto read32 = [](const unsigned char* p) -> melonDS::u32 {
        return melonDS::u32(p[0]) |
               (melonDS::u32(p[1]) << 8) |
               (melonDS::u32(p[2]) << 16) |
               (melonDS::u32(p[3]) << 24);
    };
    auto append32 = [](std::vector<unsigned char>& out, melonDS::u32 v) {
        out.push_back(static_cast<unsigned char>(v & 0xFF));
        out.push_back(static_cast<unsigned char>((v >> 8) & 0xFF));
        out.push_back(static_cast<unsigned char>((v >> 16) & 0xFF));
        out.push_back(static_cast<unsigned char>((v >> 24) & 0xFF));
    };

    // Drain a small bounded number per emulation frame. Python sends
    // synchronous request/response commands, so normally this is one packet.
    for (int packet = 0; packet < 8; ++packet)
    {
        std::array<unsigned char, 256> req{};
        sockaddr_in peer{};
        int peerLen = sizeof(peer);
        int n = recvfrom(
            sock,
            reinterpret_cast<char*>(req.data()),
            static_cast<int>(req.size()),
            0,
            reinterpret_cast<sockaddr*>(&peer),
            &peerLen
        );

        if (n == SOCKET_ERROR)
        {
            if (WSAGetLastError() == WSAEWOULDBLOCK)
                break;
            break;
        }

        if (n < 9 || std::memcmp(req.data(), "PKB1", 4) != 0)
            continue;

        melonDS::u32 seq = read32(req.data() + 4);
        unsigned cmd = req[8];

        std::vector<unsigned char> resp;
        resp.reserve(4096);
        resp.insert(resp.end(), {'P','K','R','1'});
        append32(resp, seq);
        resp.push_back(0); // status: 0 OK, 1 error

        auto fail = [&resp](const char* message) {
            resp[8] = 1;
            resp.insert(resp.end(), message, message + std::strlen(message));
        };

        switch (cmd)
        {
        case 1: // PING
        {
            const char* pong = "Pokebot-native-v1";
            resp.insert(resp.end(), pong, pong + std::strlen(pong));
            break;
        }

        case 2: // READ: u32 address, u16 length
        {
            if (n < 15)
            {
                fail("bad READ");
                break;
            }
            melonDS::u32 address = read32(req.data() + 9);
            unsigned length = read16(req.data() + 13);
            if (!nds || length < 1 || length > 4096)
            {
                fail("bad READ range");
                break;
            }
            resp.reserve(9 + length);
            for (unsigned i = 0; i < length; ++i)
                resp.push_back(nds->ARM9Read8(address + i));
            break;
        }

        case 3: // KEY: u8 bit, u8 pressed
        {
            if (n < 11 || req[9] >= 12)
            {
                fail("bad KEY");
                break;
            }
            unsigned bit = req[9];
            bool pressed = req[10] != 0;
            melonDS::u32 mask = luaInputMask.load();
            if (pressed) mask &= ~(1u << bit);
            else mask |= (1u << bit);
            luaInputMask = mask;
            luaInputActive = true;
            pokebotPulseFrames[bit] = 0;
            break;
        }

        case 4: // PULSE: u8 bit, u16 frames
        {
            if (n < 12 || req[9] >= 12)
            {
                fail("bad PULSE");
                break;
            }
            unsigned bit = req[9];
            unsigned frames = read16(req.data() + 10);
            if (frames < 1 || frames > 600)
            {
                fail("bad PULSE frames");
                break;
            }
            luaInputMask.fetch_and(~(1u << bit));
            luaInputActive = true;
            pokebotPulseFrames[bit] = static_cast<int>(frames);
            break;
        }

        case 5: // RELEASE_ALL / give manual control back
            pokebotPulseFrames.fill(0);
            luaInputMask = 0xFFF;
            luaInputActive = false;
            break;

        case 6: // RESET
            pokebotPulseFrames.fill(0);
            luaInputMask = 0xFFF;
            luaInputActive = false;
            emuInstances[0]->fastForwardToggled = false;
            emuInstances[0]->getEmuThread()->emuReset();
            break;

        case 7: // FAST_FORWARD: u8 enabled
            if (n < 10)
            {
                fail("bad FAST_FORWARD");
                break;
            }
            emuInstances[0]->fastForwardToggled = req[9] != 0;
            break;

        default:
            fail("unknown command");
            break;
        }

        sendto(
            sock,
            reinterpret_cast<const char*>(resp.data()),
            static_cast<int>(resp.size()),
            0,
            reinterpret_cast<sockaddr*>(&peer),
            peerLen
        );
    }
#endif
}
""",
)

# Poll native Pokebot commands before choosing the key mask for the next frame.
replace_once(
    emu_thread,
    """            // Apply Pokebot/Lua input every emulation frame while it owns
            // input. This makes injected controls independent of Qt window
            // focus and prevents focus-loss keyboard cleanup from cancelling
            // a bot-held key.
            if (scriptManager.externalInputsBlocked())
                emuInstance->nds->SetKeyMask(scriptManager.externalInputMask());
            else
                emuInstance->nds->SetKeyMask(emuInstance->inputMask);
""",
    """            scriptManager.pollPokebotBridge();

            // Apply Pokebot/Lua input every emulation frame while it owns
            // input. This makes injected controls independent of Qt window
            // focus and prevents focus-loss keyboard cleanup from cancelling
            // a bot-held key.
            if (scriptManager.externalInputsBlocked())
                emuInstance->nds->SetKeyMask(scriptManager.externalInputMask());
            else
                emuInstance->nds->SetKeyMask(emuInstance->inputMask);
""",
)

print("Applied Pokebot native UDP, RAM, background input, fast-forward, sol2 compiler, and Windows linker compatibility patches")
