from __future__ import annotations

import json
import os
import sqlite3
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pokebot.common.bridge import Bridge
from pokebot.common.pk6 import parse_pk6
from pokebot.common.species_names import SPECIES_NAMES
from pokebot.wild.world_authority import WorldMap, nearest_grid

TOOL = "Pokebot3DS-CFW Route 113 Ash Grass Proof Probe v0p43AE"
AS_TITLE = "0x000400000011C500"
AS_PROCESS = "sango-2"
ZONE_ADDR = 0x08C6E884
PRIMARY_X_ADDR = 0x08C6E7B0
PRIMARY_Z_ADDR = 0x08C6E7B8
SECONDARY_X_ADDR = 0x08DA8568
SECONDARY_Z_ADDR = 0x08DA8570
BATTLE_ADDR = 0x081FB478
WILD_PK6_ADDR = 0x081FFA6C
BATTLE_INACTIVE = 0x00040000
BATTLE_ACTIVE = 0x00040001
EXPECTED_FULL_DB = "alpha_sapphire_world.sqlite"


def u32(br: Bridge, addr: int) -> int:
    return struct.unpack("<I", br.read(addr, 4))[0]


def f32(br: Bridge, addr: int) -> float:
    return struct.unpack("<f", br.read(addr, 4))[0]


def utc_local_stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def load_connection() -> tuple[str, int, float]:
    appdata = Path(os.environ.get("APPDATA", str(Path.home()))) / "Pokebot-3DS"
    p = appdata / "settings.json"
    data = {}
    try:
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    host = str(data.get("three_ds_ip") or "192.168.0.28").strip()
    try: port = int(data.get("ram_bridge_port", 4952))
    except Exception: port = 4952
    try: timeout = float(data.get("bridge_timeout_s", 2.0))
    except Exception: timeout = 2.0
    return host, port, min(5.0, max(0.5, timeout))


def output_dir() -> Path:
    root = Path(os.environ.get("APPDATA", str(Path.home()))) / "Pokebot-3DS" / "support"
    root.mkdir(parents=True, exist_ok=True)
    return root


def find_full_world_db() -> tuple[Path | None, list[str]]:
    """Find the *raw* W0 compiler database by exact filename.

    The packaged grass runtime DB has a different filename and is not accepted.
    Search likely locations only, with a bounded directory depth.
    """
    candidates = []
    env = os.environ
    home = Path.home()
    roots = [ROOT, ROOT.parent, home / "Desktop", home / "Downloads", home / "Documents"]
    roots.append(Path(os.environ.get("APPDATA", str(Path.home()))) / "Pokebot-3DS")
    seen = set()
    notes = []
    for root in roots:
        try:
            root = root.resolve()
        except Exception:
            continue
        if root in seen or not root.exists():
            continue
        seen.add(root)
        direct = root / EXPECTED_FULL_DB
        if direct.is_file():
            return direct, notes
        root_depth = len(root.parts)
        try:
            for base, dirs, files in os.walk(root):
                bp = Path(base)
                depth = len(bp.parts) - root_depth
                # Exact-filename search only; avoid giant caches/source trees.
                dirs[:] = [d for d in dirs if d.lower() not in {
                    "node_modules", ".git", "__pycache__", "windows", "$recycle.bin",
                    "program files", "program files (x86)", "packages", "cache", "caches"
                }]
                if depth >= 7:
                    dirs[:] = []
                if EXPECTED_FULL_DB in files:
                    return bp / EXPECTED_FULL_DB, notes
        except (PermissionError, OSError) as exc:
            notes.append(f"search skipped {root}: {exc}")
    return None, notes


def identify_raw_tile_table(db_path: Path) -> dict | None:
    """Discover the W0 raw-tile table by schema, not by guessed table name."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            safe_table = table.replace('"', '""')
            info = list(con.execute(f'PRAGMA table_info("{safe_table}")'))
            names = {str(r[1]).lower(): str(r[1]) for r in info}
            rcol = next((names[k] for k in ("region_id", "field_region_id", "region") if k in names), None)
            xcol = next((names[k] for k in ("tile_x", "local_x", "x") if k in names), None)
            ycol = next((names[k] for k in ("tile_y", "local_y", "y", "z") if k in names), None)
            rawcol = next((names[k] for k in ("raw_permission", "permission", "raw", "raw_u32") if k in names), None)
            rawhex = next((names[k] for k in ("raw_permission_hex", "raw_hex", "permission_hex") if k in names), None)
            rawbytes = next((names[k] for k in ("raw_bytes_hex", "permission_bytes_hex", "bytes_hex") if k in names), None)
            if rcol and xcol and ycol and (rawcol or rawhex):
                return {
                    "table": table, "region": rcol, "x": xcol, "y": ycol,
                    "raw": rawcol, "raw_hex": rawhex, "raw_bytes_hex": rawbytes,
                    "columns": [str(r[1]) for r in info],
                }
    finally:
        con.close()
    return None


def raw_tile_lookup(db_path: Path | None, schema: dict | None, region: int, tx: int, ty: int) -> dict | None:
    if not db_path or not schema:
        return None
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        t = schema["table"].replace('"', '""')
        rc = schema["region"].replace('"', '""')
        xc = schema["x"].replace('"', '""')
        yc = schema["y"].replace('"', '""')
        row = con.execute(
            f'SELECT * FROM "{t}" WHERE "{rc}"=? AND "{xc}"=? AND "{yc}"=? LIMIT 1',
            (int(region), int(tx), int(ty)),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        raw_val = d.get(schema["raw"]) if schema.get("raw") else None
        raw_hex = d.get(schema["raw_hex"]) if schema.get("raw_hex") else None
        raw_bytes = d.get(schema["raw_bytes_hex"]) if schema.get("raw_bytes_hex") else None
        if raw_val is not None:
            raw_val = int(raw_val) & 0xFFFFFFFF
            raw_hex = raw_hex or f"0x{raw_val:08X}"
            raw_bytes = raw_bytes or raw_val.to_bytes(4, "little").hex()
        return {
            "raw_permission": raw_val,
            "raw_permission_hex": str(raw_hex) if raw_hex is not None else None,
            "raw_bytes_hex": str(raw_bytes) if raw_bytes is not None else None,
            "source_table": schema["table"],
        }
    finally:
        con.close()


def read_position(br: Bridge, world: WorldMap, raw_db: Path | None, raw_schema: dict | None) -> dict:
    zone_raw = u32(br, ZONE_ADDR)
    zone_id = zone_raw & 0xFFFF
    ax, az = f32(br, PRIMARY_X_ADDR), f32(br, PRIMARY_Z_ADDR)
    bx, bz = f32(br, SECONDARY_X_ADDR), f32(br, SECONDARY_Z_ADDR)
    gx, gy = nearest_grid(ax), nearest_grid(az)
    r = world.resolve(zone_id, (gx, gy))
    raw = None
    if r.get("resolved") and r.get("region_id") is not None and r.get("local_tile"):
        raw = raw_tile_lookup(raw_db, raw_schema, int(r["region_id"]), int(r["local_tile"][0]), int(r["local_tile"][1]))
    return {
        "time": utc_local_stamp(),
        "zone_raw": f"0x{zone_raw:08X}", "zone_id": zone_id,
        "world_primary": [ax, az], "world_secondary": [bx, bz],
        "duplicates_match": abs(ax-bx) <= 0.25 and abs(az-bz) <= 0.25,
        "grid": [gx, gy], "world_resolve": r, "raw_tile": raw,
    }


def neighbour_samples(pos: dict, world: WorldMap, raw_db: Path | None, raw_schema: dict | None) -> dict:
    zone = pos["zone_id"]
    gx, gy = pos["grid"]
    out = {}
    for name, (dx, dy) in {"CENTER":(0,0), "RIGHT":(1,0), "DOWN":(0,1), "LEFT":(-1,0), "UP":(0,-1)}.items():
        r = world.resolve(zone, (gx+dx, gy+dy))
        raw = None
        if r.get("resolved") and r.get("region_id") is not None and r.get("local_tile"):
            raw = raw_tile_lookup(raw_db, raw_schema, int(r["region_id"]), int(r["local_tile"][0]), int(r["local_tile"][1]))
        out[name] = {"grid":[gx+dx, gy+dy], "world_resolve":r, "raw_tile":raw}
    return out


def save_report(report: dict, stem: str) -> tuple[Path, Path]:
    out = output_dir()
    jp = out / f"{stem}.json"
    tp = out / f"{stem}.txt"
    jp.write_text(json.dumps(report, indent=2), encoding="utf-8")
    lines = [
        TOOL,
        f"Result: {report.get('result')}",
        f"Sample label: {report.get('operator_label')}",
        f"Route: {report.get('initial_position',{}).get('world_resolve',{}).get('location_name')}",
        f"Grid: {report.get('initial_position',{}).get('grid')}",
        f"Region/local: {report.get('initial_position',{}).get('world_resolve',{}).get('region_id')} / {report.get('initial_position',{}).get('world_resolve',{}).get('local_tile')}",
        f"Raw world DB: {report.get('raw_world_database',{}).get('path') or 'NOT FOUND'}",
        f"Raw permission: {(report.get('initial_position',{}).get('raw_tile') or {}).get('raw_permission_hex')}",
        f"Encounter observed: {bool(report.get('encounter_proof'))}",
        f"Trigger field grid: {(report.get('encounter_proof') or {}).get('last_field_position',{}).get('grid')}",
        f"Trigger raw permission: {((report.get('encounter_proof') or {}).get('last_field_position',{}).get('raw_tile') or {}).get('raw_permission_hex')}",
        f"Wild PK6: {(report.get('encounter_proof') or {}).get('wild_pk6')}",
        "RAM writes: False",
        "Controller inputs: False (manual physical movement only)",
        f"JSON: {jp}",
    ]
    tp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jp, tp


def ask_label() -> tuple[str, str]:
    print("\nWHAT ARE YOU VISIBLY STANDING ON?")
    print("  I = interior ash-covered encounter grass")
    print("  E = edge of ash-covered encounter grass")
    print("  P = ordinary path / non-grass")
    print("  O = other / unsure")
    while True:
        v = input("Choice [I/E/P/O]: ").strip().upper()
        if v in {"I","E","P","O"}:
            return v, {"I":"INTERIOR_ASH_GRASS","E":"EDGE_ASH_GRASS","P":"PATH_NON_GRASS","O":"OTHER_UNSURE"}[v]


def wait_for_encounter(br: Bridge, world: WorldMap, raw_db: Path | None, raw_schema: dict | None, timeout_s: float = 120.0) -> dict | None:
    print("\nENCOUNTER WATCH ACTIVE (max 120 seconds)")
    print("Use the 3DS physical controls and move ONLY inside the same visible ash-grass patch.")
    print("The probe sends NO controller input. It will stop as soon as battle RAM becomes active.")
    deadline = time.monotonic() + timeout_s
    trail = []
    last_grid = None
    last_field = None
    active_streak = 0
    while time.monotonic() < deadline:
        try:
            battle = u32(br, BATTLE_ADDR)
            if battle == BATTLE_ACTIVE:
                active_streak += 1
                if active_streak >= 2:
                    break
            else:
                active_streak = 0
                if battle == BATTLE_INACTIVE:
                    p = read_position(br, world, raw_db, raw_schema)
                    last_field = p
                    g = tuple(p["grid"])
                    if g != last_grid:
                        trail.append(p)
                        last_grid = g
                        print(f"  field tile {p['grid']} raw={(p.get('raw_tile') or {}).get('raw_permission_hex') or 'unknown'}")
            time.sleep(0.08)
        except KeyboardInterrupt:
            return None
    else:
        return None

    print("Battle transition detected. Reading one valid wild PK6 when available...")
    mon = None
    pk_deadline = time.monotonic() + 12.0
    while time.monotonic() < pk_deadline:
        try:
            raw = br.read(WILD_PK6_ADDR, 232)
            parsed = parse_pk6(raw, SPECIES_NAMES)
            if parsed.get("valid"):
                mon = {k: parsed.get(k) for k in ("valid","species","species_name","pid","tid","sid","shiny_xor","is_shiny","moves","raw_sha256")}
                break
        except Exception:
            pass
        time.sleep(0.20)
    return {
        "detected_at": utc_local_stamp(),
        "last_field_position": last_field,
        "field_tile_trail": trail,
        "wild_pk6": mon,
        "battle_state": "0x00040001",
    }


def main() -> int:
    print("="*76)
    print(TOOL)
    print("READ-ONLY PROOF TOOL — no RAM writes, no controller input")
    print("="*76)
    host, port, timeout = load_connection()
    print(f"Using dashboard connection: {host}:{port} timeout={timeout:.2f}s")

    world_path = ROOT / "pokebot" / "wild" / "world" / "alpha_sapphire_grass_runtime.sqlite"
    world = WorldMap(world_path)
    raw_db, search_notes = find_full_world_db()
    raw_schema = identify_raw_tile_table(raw_db) if raw_db else None

    if raw_db:
        print(f"Found previous raw world database: {raw_db}")
        if raw_schema:
            print(f"Raw tile table discovered: {raw_schema['table']}")
        else:
            print("WARNING: raw DB found but no raw-tile table schema was recognised.")
    else:
        print("Raw W0 compiler database not found locally; coordinate/encounter proof will still be recorded.")

    br = Bridge(host=host, port=port, timeout=timeout)
    report = {
        "tool": TOOL, "started": utc_local_stamp(), "ram_writes": False,
        "controller_inputs": False,
        "connection": {"host":host,"port":port,"timeout_s":timeout},
        "raw_world_database": {
            "path": str(raw_db) if raw_db else None,
            "schema": raw_schema,
            "search_notes": search_notes,
            "expected_filename": EXPECTED_FULL_DB,
        },
    }
    try:
        gi = br.game_info(); report["game_info"] = gi
        print(f"GAME_INFO: {gi.get('title_id')} pid={gi.get('pid')} process={gi.get('process_name')}")
        if gi.get("status") != 0 or gi.get("title_id") != AS_TITLE or gi.get("process_name") != AS_PROCESS:
            raise RuntimeError("Alpha Sapphire 1.4 process is not the active bridge target")

        pos = read_position(br, world, raw_db, raw_schema)
        report["initial_position"] = pos
        report["neighbours"] = neighbour_samples(pos, world, raw_db, raw_schema)
        r = pos["world_resolve"]
        print(f"Position: zone={pos['zone_id']} grid={pos['grid']} location={r.get('location_name')} region={r.get('region_id')} local={r.get('local_tile')}")
        print(f"Production classifier currently says encounter_terrain={r.get('encounter_terrain')}")
        print(f"Raw permission: {(pos.get('raw_tile') or {}).get('raw_permission_hex') or 'not available yet'}")
        if not r.get("resolved") or r.get("location_name") != "Route 113":
            raise RuntimeError("Stand on Route 113 before running this proof probe")

        _, label = ask_label(); report["operator_label"] = label
        stem = "route113_ash_grass_proof_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        report["result"] = "ROUTE113_SAMPLE_RECORDED"
        save_report(report, stem)

        answer = input("\nProve a real wild encounter from this terrain now? [Y/N]: ").strip().upper()
        if answer == "Y":
            ep = wait_for_encounter(br, world, raw_db, raw_schema)
            if ep:
                report["encounter_proof"] = ep
                trigger_raw = ((ep.get("last_field_position") or {}).get("raw_tile") or {}).get("raw_permission_hex")
                initial_raw = (pos.get("raw_tile") or {}).get("raw_permission_hex")
                if trigger_raw:
                    report["result"] = "ENCOUNTER_PROVED_ON_ROUTE113_RAW_PERMISSION"
                else:
                    report["result"] = "ENCOUNTER_PROVED_ON_ROUTE113_COORDINATE_RAW_DB_NOT_FOUND"
                report["raw_permission_consistency"] = {
                    "initial": initial_raw,
                    "trigger": trigger_raw,
                    "same": bool(initial_raw and trigger_raw and initial_raw == trigger_raw),
                }
            else:
                report["result"] = "ROUTE113_SAMPLE_RECORDED_NO_ENCOUNTER_WITHIN_WATCH"
        report["finished"] = utc_local_stamp()
        jp, tp = save_report(report, stem)
        print("\n" + "="*76)
        print(f"Result: {report['result']}")
        print(f"JSON: {jp}")
        print(f"TXT : {tp}")
        print("Upload BOTH files back to ChatGPT.")
        print("="*76)
        return 0
    except Exception as exc:
        report["result"] = "PROBE_ERROR"
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["finished"] = utc_local_stamp()
        stem = "route113_ash_grass_proof_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        jp, tp = save_report(report, stem)
        print(f"\nERROR: {exc}")
        print(f"Saved diagnostic JSON: {jp}")
        print(f"Saved diagnostic TXT : {tp}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
