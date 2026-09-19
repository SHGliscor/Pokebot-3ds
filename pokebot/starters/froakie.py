from __future__ import annotations
from .xy_kalos import run as _run
NAME = "Froakie"
SPECIES = 656
STATUS = "XY_HARDWARE_TEST"
def run_from_field(ctx, raw_path):
    return _run(ctx, raw_path, "froakie")
