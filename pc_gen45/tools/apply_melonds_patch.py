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

print("Applied Pokebot melonDS RAM, background input, sol2 compiler, and Windows linker compatibility patches")
