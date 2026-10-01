#include "PokebotUsbBridge.h"

#include <switch.h>

#include <cstring>

#include "NDS.h"

namespace
{
constexpr std::size_t kBridgeReserveSize = 0x1000;
void* gOverrideHeapBase = nullptr;
std::size_t gOverrideHeapSize = 0;
}

extern "C" void __libnx_initheap(void)
{
    extern char* fake_heap_start;
    extern char* fake_heap_end;

    if (!envHasHeapOverride())
    {
        // This build is intended to run as an NRO under nx-hbloader, which
        // supplies an override heap. Refuse to create an ambiguous layout.
        fake_heap_start = nullptr;
        fake_heap_end = nullptr;
        return;
    }

    gOverrideHeapBase = envGetHeapOverrideAddr();
    gOverrideHeapSize = envGetHeapOverrideSize();
    if (!gOverrideHeapBase || gOverrideHeapSize <= kBridgeReserveSize)
    {
        fake_heap_start = nullptr;
        fake_heap_end = nullptr;
        return;
    }

    // Reserve the first page of the NRO heap override for Pokebot metadata.
    // nx-hbloader passes this override immediately after the mapped NRO, so
    // the page has a deterministic offset from Koi's process heap base.
    fake_heap_start = static_cast<char*>(gOverrideHeapBase) + kBridgeReserveSize;
    fake_heap_end = static_cast<char*>(gOverrideHeapBase) + gOverrideHeapSize;
}

namespace PokebotUsbBridge
{
namespace
{
volatile BridgePage* gBridge = nullptr;
constexpr char kMagic[] = "POKEBOT_MELONDS_USB_BRIDGE_V1";
}

void Init()
{
    if (!gOverrideHeapBase)
        return;

    if (!gBridge)
    {
        auto* page = static_cast<BridgePage*>(gOverrideHeapBase);
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
}

const BridgePage* GetBridgePage()
{
    return const_cast<const BridgePage*>(gBridge);
}
}
