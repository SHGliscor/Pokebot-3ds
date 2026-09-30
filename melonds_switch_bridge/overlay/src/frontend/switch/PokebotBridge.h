#pragma once

namespace PokebotBridge
{
    constexpr unsigned short Port = 4953;

    bool Init();
    void DeInit();
    bool IsRunning();
}
