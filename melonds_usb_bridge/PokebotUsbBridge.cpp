#include "PokebotUsbBridge.h"

#include <cstdlib>
#include <cstring>

#include "NDS.h"

namespace PokebotUsbBridge
{
namespace
{
volatile BridgePage* gBridge = nullptr;
constexpr char kMagic[] = "POKEBOT_MELONDS_USB_BRIDGE_V1";
}

void Init()
{
    if (!gBridge)
    {
        auto* page = static_cast<BridgePage*>(std::aligned_alloc(0x1000, sizeof(BridgePage)));
        if (!page)
            return;

        std::memset(page, 0, sizeof(BridgePage));
        gBridge = page;
        for (std::size_t i = 0; i < sizeof(kMagic); ++i)
            gBridge->magic[i] = kMagic[i];
        gBridge->version = 1;
    }

    gBridge->console_type = static_cast<std::uint32_t>(NDS::ConsoleType);
    gBridge->mainram_ptr = reinterpret_cast<std::uint64_t>(NDS::MainRAM);
    gBridge->mainram_size = NDS::MainRAMMaxSize;
}

void DeInit()
{
    if (!gBridge)
        return;

    auto* page = const_cast<BridgePage*>(gBridge);
    std::memset(page, 0, sizeof(BridgePage));
    gBridge = nullptr;
    std::free(page);
}

const BridgePage* GetBridgePage()
{
    return const_cast<const BridgePage*>(gBridge);
}
}
