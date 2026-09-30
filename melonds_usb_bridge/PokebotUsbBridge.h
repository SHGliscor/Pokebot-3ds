#pragma once

#include <cstdint>

namespace PokebotUsbBridge
{
struct alignas(0x1000) BridgePage
{
    char magic[32];
    std::uint32_t version;
    std::uint32_t console_type;
    std::uint64_t mainram_ptr;
    std::uint32_t mainram_size;
    std::uint32_t reserved;
    std::uint8_t padding[0x1000 - 56];
};

void Init();
void DeInit();
const BridgePage* GetBridgePage();
}
