from __future__ import annotations
from .xy_kalos import run as _run
NAME = "Chespin"
SPECIES = 650
STATUS = "XY_HARDWARE_TEST"
def run_from_field(ctx, raw_path):
    return _run(ctx, raw_path, "chespin")
