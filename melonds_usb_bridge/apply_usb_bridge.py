from pathlib import Path
import shutil
import sys

root = Path(sys.argv[1]).resolve()
here = Path(__file__).resolve().parent
switch_dir = root / "src" / "frontend" / "switch"
main_cpp = switch_dir / "main.cpp"
cmake = switch_dir / "CMakeLists.txt"

shutil.copy2(here / "PokebotUsbBridge.cpp", switch_dir / "PokebotUsbBridge.cpp")
shutil.copy2(here / "PokebotUsbBridge.h", switch_dir / "PokebotUsbBridge.h")

text = cmake.read_text(encoding="utf-8")
if "    PokebotUsbBridge.cpp\n" not in text:
    text = text.replace("    main.cpp\n", "    main.cpp\n    PokebotUsbBridge.cpp\n", 1)
cmake.write_text(text, encoding="utf-8")

text = main_cpp.read_text(encoding="utf-8")
if '#include "PokebotUsbBridge.h"' not in text:
    text = text.replace('#include "InputConfig.h"\n', '#include "InputConfig.h"\n#include "PokebotUsbBridge.h"\n', 1)
if "PokebotUsbBridge::Init();" not in text:
    text = text.replace("    Emulation::Init();\n\n    bool argvLoaded = false;", "    Emulation::Init();\n    PokebotUsbBridge::Init();\n\n    bool argvLoaded = false;", 1)
if "PokebotUsbBridge::DeInit();" not in text:
    text = text.replace("    Emulation::DeInit();\n    Frontend::DeInit_ROM();", "    PokebotUsbBridge::DeInit();\n    Emulation::DeInit();\n    Frontend::DeInit_ROM();", 1)
main_cpp.write_text(text, encoding="utf-8")
print("Applied USB-only Pokebot melonDS bridge")
