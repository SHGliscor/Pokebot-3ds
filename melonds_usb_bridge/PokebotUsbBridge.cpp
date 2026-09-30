#include "PokebotUsbBridge.h"

#include <cstdlib>
#include <cstring>

#include "NDS.h"

namespace PokebotUsbBridge
{
namespace
{
BridgePage* gBridge = nullptr;
constexpr char kMagic[] = "POKEBOT_MELONDS_USB_BRIDGE_V1";
}

void Init()
{
    if (gBridge)
        return;

    gBridge = static_cast<BridgePage*>(std::aligned_alloc(0x1000, sizeof(BridgePage)));
    if (!gBridge)
        return;

    std::memset(gBridge, 0, sizeof(BridgePage));
    std::memcpy(gBridge->magic, kMagic, sizeof(kMagic));
    gBridge->version = 1;
    gBridge->console_type = static_cast<std::uint32_t>(NDS::ConsoleType);
    gBridge->mainram_ptr = reinterpret_cast<std::uint64_t>(NDS::MainRAM);
    gBridge->mainram_size = NDS::MainRAMMaxSize;
}

void DeInit()
{
    if (!gBridge)
        return;

    std::memset(gBridge, 0, sizeof(BridgePage));
    std::free(gBridge);
    gBridge = nullptr;
}

const BridgePage* GetBridgePage()
{
    return gBridge;
}
}
