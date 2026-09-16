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
emu = ROOT / "lua/core/emu.lua"

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
    cpp,
    """    native.set_function("reset_input", [this]()
    {
        this->luaInputMask = 0xFFF;
        this->luaInputActive = true;

        nds->SetKeyMask(0xFFF);
    });
""",
    """    native.set_function("reset_input", [this]()
    {
        this->luaInputMask = 0xFFF;
        this->luaInputActive = true;

        nds->SetKeyMask(0xFFF);
    });

    native.set_function("touch_screen", [](int x, int y)
    {
        if (x < 0) x = 0;
        if (x > 255) x = 255;
        if (y < 0) y = 0;
        if (y > 191) y = 191;
        emuInstances[0]->touchScreen(x, y);
    });

    native.set_function("release_screen", []()
    {
        emuInstances[0]->releaseScreen();
    });
""",
)

replace_once(
    emu,
    """function emu.resetInput()
    return native.reset_input()
end
""",
    """function emu.resetInput()
    return native.reset_input()
end

-- Forces touchscreen contact at DS bottom-screen coordinates.
---@param x number 0..255
---@param y number 0..191
function emu.touch(x, y)
    return native.touch_screen(x, y)
end

-- Releases the DS touchscreen.
function emu.releaseTouch()
    return native.release_screen()
end
""",
)

print("Applied Pokebot melonDS bulk-read + touchscreen patch")
