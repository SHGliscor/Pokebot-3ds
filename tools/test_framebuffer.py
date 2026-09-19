from __future__ import annotations

import argparse
from pathlib import Path
from pokebot.common.framebuffer import capture_top_screen

def main():
    parser = argparse.ArgumentParser(description="Pokebot-Luma unified top-screen test")
    parser.add_argument("host", help="3DS IP address")
    parser.add_argument("--output", default="framebuffer_test_top.png")
    args = parser.parse_args()
    meta = capture_top_screen(args.host, Path(args.output), port=4952, timeout=1.0)
    print(
        f"PASS {meta['width']}x{meta['height']} "
        f"{meta['source']} -> {meta['path']}"
    )

if __name__ == "__main__":
    main()
