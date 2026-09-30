from __future__ import annotations

import argparse
from pathlib import Path
import shutil

HERE = Path(__file__).resolve().parent
OVERLAY = HERE / "overlay" / "src" / "frontend" / "switch"


def insert_once(text: str, anchor: str, replacement: str, label: str) -> str:
    if replacement in text:
        return text
    if anchor not in text:
        raise RuntimeError(f"Could not find {label} anchor")
    return text.replace(anchor, anchor + replacement, 1)


def patch_source(source_root: Path) -> list[Path]:
    switch_dir = source_root / "src" / "frontend" / "switch"
    main_cpp = switch_dir / "main.cpp"
    cmake = switch_dir / "CMakeLists.txt"
    if not main_cpp.is_file() or not cmake.is_file():
        raise FileNotFoundError(
            "Expected trulymust/melonDS-switch-upscale source tree with "
            "src/frontend/switch/main.cpp and CMakeLists.txt"
        )

    for name in ("PokebotBridge.cpp", "PokebotBridge.h"):
        shutil.copy2(OVERLAY / name, switch_dir / name)

    main_text = main_cpp.read_text(encoding="utf-8")
    main_text = insert_once(
        main_text,
        '#include "InputConfig.h"\n',
        '#include "PokebotBridge.h"\n',
        "main include",
    )
    main_text = insert_once(
        main_text,
        '    Emulation::Init();\n',
        '    PokebotBridge::Init();\n',
        "bridge init",
    )
    deinit_anchor = '    Emulation::DeInit();\n'
    if '    PokebotBridge::DeInit();\n' not in main_text:
        if deinit_anchor not in main_text:
            raise RuntimeError("Could not find bridge deinit anchor")
        main_text = main_text.replace(
            deinit_anchor,
            '    PokebotBridge::DeInit();\n' + deinit_anchor,
            1,
        )
    main_cpp.write_text(main_text, encoding="utf-8")

    cmake_text = cmake.read_text(encoding="utf-8")
    cmake_text = insert_once(
        cmake_text,
        '    RATracker.cpp\n',
        '    PokebotBridge.cpp\n',
        "Switch source list",
    )
    cmake.write_text(cmake_text, encoding="utf-8")

    return [switch_dir / "PokebotBridge.cpp", switch_dir / "PokebotBridge.h", main_cpp, cmake]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Add the read-only Pokebot DS RAM UDP bridge to melonDS Switch"
    )
    parser.add_argument("source_root", type=Path)
    args = parser.parse_args()
    changed = patch_source(args.source_root.resolve())
    print("Pokebot melonDS bridge source installed:")
    for path in changed:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
