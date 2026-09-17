#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import runpy

HERE = Path(__file__).resolve().parent

# Keep the already-proven bridge/turbo patch intact, then layer the wild-hunt
# one-tile SAFE_STEP guard on top. Both scripts receive the same sys.argv.
runpy.run_path(str(HERE / "apply_melonds_core_patch.py"), run_name="__main__")
runpy.run_path(str(HERE / "apply_melonds_safe_step_patch.py"), run_name="__main__")
