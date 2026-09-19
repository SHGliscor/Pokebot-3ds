from pathlib import Path
import sqlite3, sys
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DB = ROOT / "pokebot" / "wild" / "world" / "pokemon_y_grass_runtime.sqlite"
if not DB.is_file():
    raise SystemExit("FAIL: runtime DB missing")
con = sqlite3.connect(DB)
try:
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    counts = {
        t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("zones","map_matrices","matrix_regions","matrix_zone_switches","encounter_cells")
    }
    assert counts == {
        "zones": 360, "map_matrices": 227, "matrix_regions": 4586,
        "matrix_zone_switches": 63696, "encounter_cells": 6924
    }, counts
    r2 = con.execute("SELECT zone_id,map_matrix_id FROM zones WHERE location_name='Route 2'").fetchall()
    assert r2 == [(259,0)], r2
finally:
    con.close()
from pokebot.wild.world_authority import WorldMap
w = WorldMap(DB)
assert 259 in w.zones and w.zones[259]["location_name"] == "Route 2"
assert w.zones[259]["enable_running"] is True
print("XY_WORLD_RUNTIME_SELF_TEST_PASS")
print(counts)
