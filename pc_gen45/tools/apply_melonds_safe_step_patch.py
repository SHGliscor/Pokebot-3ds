#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "melonDS-lua").resolve()


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one SAFE_STEP anchor, got {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


script_h = ROOT / "src/frontend/qt_sdl/ScriptManager.h"
cpp = ROOT / "src/frontend/qt_sdl/ScriptManager.cpp"

replace_once(
    script_h,
    """    std::intptr_t pokebotSocket = -1;
    bool pokebotWSAStarted = false;
    std::array<int, 12> pokebotPulseFrames{};
    bool pokebotDisplayOn = true;

    // reset variables, callbacks
""",
    """    std::intptr_t pokebotSocket = -1;
    bool pokebotWSAStarted = false;
    std::array<int, 12> pokebotPulseFrames{};
    bool pokebotDisplayOn = true;

    // One-tile overworld movement guard. The Python side validates that the
    // destination is encounter terrain before command 10 is allowed to press
    // a D-pad direction. melonDS then releases it on the first coordinate
    // transition so an unthrottled core cannot chain into a second tile.
    bool pokebotStepActive = false;
    int pokebotStepBit = -1;
    melonDS::u32 pokebotStepXAddr = 0;
    melonDS::u32 pokebotStepZAddr = 0;
    melonDS::u32 pokebotStepMapAddr = 0;
    melonDS::u32 pokebotStepExpectedMap = 0;
    melonDS::u32 pokebotStepStartX = 0;
    melonDS::u32 pokebotStepStartZ = 0;
    unsigned pokebotStepFramesLeft = 0;

    // reset variables, callbacks
""",
)

replace_once(
    cpp,
    """    // Frame-based pulse release. DS key bits are active-low.
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
""",
    """    // Frame-based pulse release. DS key bits are active-low.
    for (int bit = 0; bit < 12; ++bit)
    {
        if (pokebotPulseFrames[bit] <= 0)
            continue;
        --pokebotPulseFrames[bit];
        if (pokebotPulseFrames[bit] == 0)
            luaInputMask.fetch_or(1u << bit);
    }

    // SAFE_STEP guard runs before applying the next frame's input mask. Once
    // X/Z changes by the first tile, the direction is released immediately.
    // A map transition or frame-budget expiry also releases the key.
    if (pokebotStepActive && nds)
    {
        const melonDS::u32 currentMap = nds->ARM9Read32(pokebotStepMapAddr);
        const melonDS::u32 currentX = nds->ARM9Read32(pokebotStepXAddr);
        const melonDS::u32 currentZ = nds->ARM9Read32(pokebotStepZAddr);

        bool stopStep = currentMap != pokebotStepExpectedMap ||
                        currentX != pokebotStepStartX ||
                        currentZ != pokebotStepStartZ;

        if (!stopStep)
        {
            if (pokebotStepFramesLeft > 0)
                --pokebotStepFramesLeft;
            if (pokebotStepFramesLeft == 0)
                stopStep = true;
        }

        if (stopStep)
        {
            if (pokebotStepBit >= 0 && pokebotStepBit < 12)
                luaInputMask.fetch_or(1u << pokebotStepBit);
            pokebotStepActive = false;
            pokebotStepBit = -1;
        }
    }

    if (pokebotSocket == -1)
        return;
""",
)

replace_once(
    cpp,
    """        case 5: // RELEASE_ALL / give manual control back
            pokebotPulseFrames.fill(0);
            luaInputMask = 0xFFF;
            luaInputActive = false;
            break;
""",
    """        case 5: // RELEASE_ALL / give manual control back
            pokebotStepActive = false;
            pokebotStepBit = -1;
            pokebotStepFramesLeft = 0;
            pokebotPulseFrames.fill(0);
            luaInputMask = 0xFFF;
            luaInputActive = false;
            break;
""",
)

replace_once(
    cpp,
    """        case 6: // RESET
            pokebotPulseFrames.fill(0);
            luaInputMask = 0xFFF;
            luaInputActive = false;
            emuInstances[0]->fastForwardToggled = false;
            emuInstances[0]->getEmuThread()->emuReset();
            break;
""",
    """        case 6: // RESET
            pokebotStepActive = false;
            pokebotStepBit = -1;
            pokebotStepFramesLeft = 0;
            pokebotPulseFrames.fill(0);
            luaInputMask = 0xFFF;
            luaInputActive = false;
            emuInstances[0]->fastForwardToggled = false;
            emuInstances[0]->getEmuThread()->emuReset();
            break;
""",
)

replace_once(
    cpp,
    """        case 9: // AUDIO: u8 enabled
            if (n < 10)
            {
                fail("bad AUDIO");
                break;
            }
            emuInstances[0]->setPokebotAudioEnabled(req[9] != 0);
            break;

        default:
""",
    """        case 9: // AUDIO: u8 enabled
            if (n < 10)
            {
                fail("bad AUDIO");
                break;
            }
            emuInstances[0]->setPokebotAudioEnabled(req[9] != 0);
            break;

        case 10: // SAFE_STEP: bit, xAddr, zAddr, mapAddr, expectedMap, maxFrames
        {
            if (n < 28 || !nds)
            {
                fail("bad SAFE_STEP");
                break;
            }

            const unsigned bit = req[9];
            const melonDS::u32 xAddr = read32(req.data() + 10);
            const melonDS::u32 zAddr = read32(req.data() + 14);
            const melonDS::u32 mapAddr = read32(req.data() + 18);
            const melonDS::u32 expectedMap = read32(req.data() + 22);
            const unsigned maxFrames = read16(req.data() + 26);

            if (bit < 4 || bit > 7 || maxFrames < 1 || maxFrames > 600)
            {
                fail("bad SAFE_STEP args");
                break;
            }
            if (pokebotStepActive)
            {
                fail("SAFE_STEP busy");
                break;
            }
            if (nds->ARM9Read32(mapAddr) != expectedMap)
            {
                fail("SAFE_STEP map mismatch");
                break;
            }

            pokebotStepBit = static_cast<int>(bit);
            pokebotStepXAddr = xAddr;
            pokebotStepZAddr = zAddr;
            pokebotStepMapAddr = mapAddr;
            pokebotStepExpectedMap = expectedMap;
            pokebotStepStartX = nds->ARM9Read32(xAddr);
            pokebotStepStartZ = nds->ARM9Read32(zAddr);
            pokebotStepFramesLeft = maxFrames;
            pokebotStepActive = true;

            // Never permit diagonal leftovers from any previous automation.
            for (unsigned dpadBit = 4; dpadBit <= 7; ++dpadBit)
                luaInputMask.fetch_or(1u << dpadBit);
            luaInputMask.fetch_and(~(1u << bit));
            luaInputActive = true;
            pokebotPulseFrames[bit] = 0;
            break;
        }

        default:
""",
)

print("Applied Pokebot native SAFE_STEP one-tile coordinate guard")
