from __future__ import annotations
from .xy_kalos import run as _run
NAME = "Fennekin"
SPECIES = 653
STATUS = "XY_HARDWARE_PROVEN_CONTINUOUS"
def run_from_field(ctx, raw_path):
    return _run(ctx, raw_path, "fennekin")
