from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


def _extract_console_profile(argv):
    """Consume Pokebot's own profile switch before Qt sees argv."""
    profile = None
    cleaned = [argv[0]]
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--console-profile":
            if i + 1 >= len(argv):
                raise SystemExit("--console-profile requires a profile name")
            profile = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--console-profile="):
            profile = arg.split("=", 1)[1]
            i += 1
            continue
        cleaned.append(arg)
        i += 1
    return profile, cleaned


def _safe_profile_slug(name: str) -> str:
    text = str(name or "").strip()
    # Keep Windows folder names predictable and block path traversal.
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._-")
    if not slug:
        slug = "console"
    return slug[:64]


def _configure_console_profile(profile_name: str | None) -> None:
    if not profile_name:
        return

    display_name = str(profile_name).strip() or "Console"
    slug = _safe_profile_slug(display_name)

    existing_override = os.environ.get("POKEBOT_APPDATA_ROOT") or os.environ.get("POKEBOT_DATA_ROOT")
    if existing_override:
        base = Path(existing_override).expanduser().resolve()
        # If a caller supplied a root explicitly, keep named instances beneath it.
        root = base / "instances" / slug
    else:
        appdata = os.environ.get("APPDATA")
        if appdata:
            base = Path(appdata).expanduser().resolve() / "Pokebot-3DS"
        else:
            base = Path.home() / ".pokebot-3ds"
        root = base / "instances" / slug

    root.mkdir(parents=True, exist_ok=True)

    # Named console profiles deliberately start clean instead of importing
    # package-local legacy runtime state from another console/build.  Once the
    # marker exists, ensure_profile() creates only the missing baseline files.
    marker = root / "migration_v1.json"
    if not marker.exists():
        marker.write_text(
            json.dumps(
                {
                    "mode": "multi_console_fresh_profile",
                    "profile": display_name,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    os.environ["POKEBOT_APPDATA_ROOT"] = str(root)
    os.environ["POKEBOT_MULTI_INSTANCE"] = "1"
    os.environ["POKEBOT_INSTANCE_NAME"] = display_name
    os.environ["POKEBOT_INSTANCE_SLUG"] = slug


_profile_name, _qt_argv = _extract_console_profile(sys.argv)
sys.argv[:] = _qt_argv
_configure_console_profile(_profile_name)

from qt_ui.app import main


if __name__ == '__main__':
    raise SystemExit(main())
