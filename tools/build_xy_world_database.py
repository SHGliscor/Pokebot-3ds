from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import struct
from pathlib import Path

ZONE_SIZE = 0x38


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def garc_files(path: Path) -> list[bytes]:
    data = path.read_bytes()
    if len(data) < 0x1C:
        raise ValueError(f"GARC too short: {path}")
    magic = data[:4]
    if magic not in (b"CRAG", b"GARC"):
        raise ValueError(f"Not a GARC: {path} magic={magic!r}")
    header_size = struct.unpack_from('<I', data, 4)[0]
    data_offset = struct.unpack_from('<I', data, 0x10)[0]

    off = header_size
    if data[off:off+4] not in (b"OTAF", b"FATO"):
        raise ValueError(f"Missing FATO in {path}")
    fato_size = struct.unpack_from('<I', data, off + 4)[0]
    entry_count = struct.unpack_from('<H', data, off + 8)[0]
    entry_offsets = [struct.unpack_from('<I', data, off + 0x0C + i * 4)[0] for i in range(entry_count)]

    off += fato_size
    if data[off:off+4] not in (b"BTAF", b"FATB"):
        raise ValueError(f"Missing FATB in {path}")
    fatb_data = off + 0x0C

    out: list[bytes] = []
    for rel in entry_offsets:
        p = fatb_data + rel
        vector = struct.unpack_from('<I', data, p)[0]
        p += 4
        subfiles: list[bytes] = []
        for bit in range(32):
            if (vector >> bit) & 1:
                start, end, length = struct.unpack_from('<III', data, p)
                p += 12
                subfiles.append(data[data_offset + start:data_offset + start + length])
        if len(subfiles) != 1:
            raise ValueError(f"Expected one subfile per XY GARC entry, found {len(subfiles)}")
        out.append(subfiles[0])
    return out


def decompress_lz11(data: bytes) -> bytes:
    if not data or data[0] != 0x11:
        return data
    if len(data) < 4:
        raise ValueError("Truncated LZ11 stream")
    size = data[1] | (data[2] << 8) | (data[3] << 16)
    pos = 4
    if size == 0:
        if len(data) < 8:
            raise ValueError("Truncated extended LZ11 size")
        size = struct.unpack_from('<I', data, pos)[0]
        pos += 4

    out = bytearray()
    while len(out) < size:
        flags = data[pos]
        pos += 1
        for bit in range(8):
            if len(out) >= size:
                break
            if not (flags & (0x80 >> bit)):
                out.append(data[pos])
                pos += 1
                continue

            b1 = data[pos]
            b2 = data[pos + 1]
            pos += 2
            high = b1 >> 4
            if high == 0:
                b3 = data[pos]
                pos += 1
                length = (((b1 & 0x0F) << 4) | (b2 >> 4)) + 0x11
                disp = (((b2 & 0x0F) << 8) | b3) + 1
            elif high == 1:
                b3 = data[pos]
                b4 = data[pos + 1]
                pos += 2
                length = (((b1 & 0x0F) << 12) | (b2 << 4) | (b3 >> 4)) + 0x111
                disp = (((b3 & 0x0F) << 8) | b4) + 1
            else:
                length = high + 1
                disp = (((b1 & 0x0F) << 8) | b2) + 1
            if disp > len(out):
                raise ValueError(f"Invalid LZ11 displacement {disp} > {len(out)}")
            for _ in range(length):
                out.append(out[-disp])
                if len(out) >= size:
                    break
    return bytes(out[:size])


def unpack_mini(data: bytes, ident: bytes) -> list[bytes]:
    if len(data) < 8 or data[:2] != ident:
        raise ValueError(f"Mini identifier mismatch: expected {ident!r}, got {data[:2]!r}")
    count = struct.unpack_from('<H', data, 2)[0]
    offsets = [struct.unpack_from('<I', data, 4 + i * 4)[0] for i in range(count + 1)]
    if offsets != sorted(offsets) or offsets[-1] > len(data):
        raise ValueError("Invalid Mini offset table")
    return [data[offsets[i]:offsets[i + 1]] for i in range(count)]


def load_location_names(py_path: Path) -> dict[int, str]:
    ns: dict[str, object] = {}
    exec(py_path.read_text(encoding='utf-8'), ns)
    names = ns.get('XY_LOCATION_NAMES')
    if not isinstance(names, dict):
        raise ValueError(f"XY_LOCATION_NAMES missing from {py_path}")
    return {int(k): str(v) for k, v in names.items()}


def parse_zone_master(enc_files: list[bytes], location_names: dict[int, str]) -> list[dict]:
    masters = [x for x in enc_files if x and x[0] != 0x11 and len(x) % ZONE_SIZE == 0]
    if len(masters) != 1:
        raise ValueError(f"Expected one master ZoneData table; found {len(masters)}")
    master = masters[0]
    zone_count = len(master) // ZONE_SIZE
    zones = []
    for zone_id in range(zone_count):
        d = master[zone_id * ZONE_SIZE:(zone_id + 1) * ZONE_SIZE]
        parent_raw = struct.unpack_from('<H', d, 0x1C)[0]
        flags20 = struct.unpack_from('<I', d, 0x20)[0]
        parent_map = parent_raw & 0x03FF
        zones.append({
            'zone_id': zone_id,
            'xy_zone_number': zone_id,
            'map_type': d[0],
            'map_move': d[1],
            'map_area': struct.unpack_from('<H', d, 0x02)[0],
            'map_matrix_id': struct.unpack_from('<H', d, 0x04)[0],
            'text_file': struct.unpack_from('<H', d, 0x06)[0],
            'script_file': struct.unpack_from('<H', d, 0x18)[0],
            'town_map_group': struct.unpack_from('<H', d, 0x1A)[0],
            'parent_map': parent_map,
            'location_name': location_names.get(parent_map, f'Location {parent_map}'),
            'ol_value': parent_raw >> 10,
            'weather': struct.unpack_from('<H', d, 0x1E)[0] & 0x1F,
            'enable_roller_skates': bool((d[0x1E] >> 6) & 1),
            'enable_bicycle': bool((flags20 >> 10) & 1),
            'enable_running': bool((flags20 >> 11) & 1),
            'enable_escape_rope': bool((flags20 >> 12) & 1),
            'enable_fly': bool((flags20 >> 13) & 1),
            'raw_hex': d.hex(),
        })
    return zones


def parse_matrix_transitions(blob: bytes) -> list[tuple[int, float, float, float, float]]:
    # pk3DS MapMatrix.ParseUnk: repeated <Iffff> records until Direction == 0.
    out = []
    pos = 0
    while pos + 4 <= len(blob):
        direction = struct.unpack_from('<I', blob, pos)[0]
        if direction == 0:
            break
        if pos + 20 > len(blob):
            break
        direction, a, b, c, d = struct.unpack_from('<Iffff', blob, pos)
        out.append((direction, a, b, c, d))
        pos += 20
    return out


def parse_matrices(matrix_files: list[bytes]) -> tuple[list[dict], list[dict], list[dict]]:
    matrices = []
    cells = []
    transitions = []
    for matrix_id, raw in enumerate(matrix_files):
        parts = unpack_mini(decompress_lz11(raw), b'MM')
        if not parts:
            raise ValueError(f"Matrix {matrix_id} has no MM payload")
        p0 = parts[0]
        if len(p0) < 8:
            raise ValueError(f"Matrix {matrix_id} primary payload too short")
        u0, u1, width, height = struct.unpack_from('<HHHH', p0, 0)
        area = width * height
        need = 8 + area * 2
        if len(p0) < need:
            raise ValueError(f"Matrix {matrix_id}: {width}x{height} needs {need}, has {len(p0)}")
        entry_list = list(struct.unpack_from('<' + 'H' * area, p0, 8)) if area else []
        trans = parse_matrix_transitions(parts[1]) if len(parts) > 1 else []
        matrices.append({
            'matrix_id': matrix_id,
            'u0': u0,
            'u1': u1,
            'has_lod': 1 if trans else 0,
            'width': width,
            'height': height,
            'transition_count': len(trans),
            'raw_size': len(raw),
            'sha256': sha256_bytes(raw),
        })
        for idx, region_id in enumerate(entry_list):
            cells.append({
                'matrix_id': matrix_id,
                'matrix_x': idx % width if width else 0,
                'matrix_y': idx // width if width else 0,
                'region_id': -1 if region_id == 0xFFFF else int(region_id),
            })
        for n, (direction, a, b, c, d) in enumerate(trans):
            transitions.append({
                'matrix_id': matrix_id,
                'transition_index': n,
                'direction': int(direction),
                'p1': float(a), 'p2': float(b), 'p3': float(c), 'p4': float(d),
            })
    return matrices, cells, transitions


def parse_field_regions(mapgr_files: list[bytes]) -> list[dict]:
    out = []
    for region_id, raw in enumerate(mapgr_files):
        dec = decompress_lz11(raw)
        sig = dec[:4].decode('ascii', errors='replace') if len(dec) >= 4 else ''
        out.append({
            'region_id': region_id,
            'garc_size': len(raw),
            'decoded_size': len(dec),
            'decoded_signature': sig,
            'sha256': sha256_bytes(raw),
        })
    return out


def build_database(source_dir: Path, output: Path, xy_locations_py: Path) -> dict:
    enc_path = source_dir / 'a' / '0' / '1' / '2'
    mapgr_path = source_dir / 'a' / '0' / '4' / '1'
    matrix_path = source_dir / 'a' / '0' / '4' / '2'
    for p in (enc_path, mapgr_path, matrix_path):
        if not p.is_file():
            raise FileNotFoundError(p)

    names = load_location_names(xy_locations_py)
    enc_files = garc_files(enc_path)
    mapgr_files = garc_files(mapgr_path)
    matrix_files = garc_files(matrix_path)

    zones = parse_zone_master(enc_files, names)
    matrices, matrix_cells, transitions = parse_matrices(matrix_files)
    field_regions = parse_field_regions(mapgr_files)

    missing_parent_ids = sorted({z['parent_map'] for z in zones if z['parent_map'] not in names})
    bad_matrix_refs = sorted({z['map_matrix_id'] for z in zones if z['map_matrix_id'] >= len(matrices)})
    used_regions = {c['region_id'] for c in matrix_cells if c['region_id'] >= 0}
    missing_region_refs = sorted(x for x in used_regions if x >= len(field_regions))

    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()
    db = sqlite3.connect(output)
    try:
        db.executescript('''
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE zones(
            zone_id INTEGER PRIMARY KEY,
            xy_zone_number INTEGER NOT NULL,
            parent_map INTEGER NOT NULL,
            location_name TEXT NOT NULL,
            map_matrix_id INTEGER NOT NULL,
            map_type INTEGER NOT NULL,
            map_move INTEGER NOT NULL,
            map_area INTEGER NOT NULL,
            text_file INTEGER NOT NULL,
            script_file INTEGER NOT NULL,
            town_map_group INTEGER NOT NULL,
            ol_value INTEGER NOT NULL,
            weather INTEGER NOT NULL,
            enable_roller_skates INTEGER NOT NULL,
            enable_bicycle INTEGER NOT NULL,
            enable_running INTEGER NOT NULL,
            enable_escape_rope INTEGER NOT NULL,
            enable_fly INTEGER NOT NULL,
            raw_hex TEXT NOT NULL
        );
        CREATE INDEX zones_parent_map_idx ON zones(parent_map);
        CREATE INDEX zones_matrix_idx ON zones(map_matrix_id);
        CREATE TABLE map_matrices(
            matrix_id INTEGER PRIMARY KEY,
            u0 INTEGER NOT NULL,
            u1 INTEGER NOT NULL,
            has_lod INTEGER NOT NULL,
            width INTEGER NOT NULL,
            height INTEGER NOT NULL,
            transition_count INTEGER NOT NULL,
            raw_size INTEGER NOT NULL,
            sha256 TEXT NOT NULL
        );
        CREATE TABLE matrix_regions(
            matrix_id INTEGER NOT NULL,
            matrix_x INTEGER NOT NULL,
            matrix_y INTEGER NOT NULL,
            region_id INTEGER NOT NULL,
            PRIMARY KEY(matrix_id,matrix_x,matrix_y)
        ) WITHOUT ROWID;
        CREATE INDEX matrix_regions_region_idx ON matrix_regions(region_id);
        CREATE TABLE matrix_transitions(
            matrix_id INTEGER NOT NULL,
            transition_index INTEGER NOT NULL,
            direction INTEGER NOT NULL,
            p1 REAL NOT NULL,
            p2 REAL NOT NULL,
            p3 REAL NOT NULL,
            p4 REAL NOT NULL,
            PRIMARY KEY(matrix_id,transition_index)
        ) WITHOUT ROWID;
        CREATE TABLE field_regions(
            region_id INTEGER PRIMARY KEY,
            garc_size INTEGER NOT NULL,
            decoded_size INTEGER NOT NULL,
            decoded_signature TEXT NOT NULL,
            sha256 TEXT NOT NULL
        );
        ''')
        meta = {
            'schema': 'pokebot3ds.xy_world.v1',
            'game_family': 'xy',
            'games': 'Pokemon X / Pokemon Y',
            'source_encdata': 'a/0/1/2',
            'source_mapgr': 'a/0/4/1',
            'source_mapmatrix': 'a/0/4/2',
            'zone_count': str(len(zones)),
            'map_matrix_count': str(len(matrices)),
            'field_region_count': str(len(field_regions)),
            'location_parent_count': str(len({z['parent_map'] for z in zones})),
            'source_encdata_sha256': sha256_bytes(enc_path.read_bytes()),
            'source_mapgr_sha256': sha256_bytes(mapgr_path.read_bytes()),
            'source_mapmatrix_sha256': sha256_bytes(matrix_path.read_bytes()),
            'compile_status': 'PASS_GAME_FILE_DERIVED_ZONE_MATRIX_COMPILE',
        }
        db.executemany('INSERT INTO meta(key,value) VALUES(?,?)', meta.items())
        db.executemany('''INSERT INTO zones VALUES(
            :zone_id,:xy_zone_number,:parent_map,:location_name,:map_matrix_id,
            :map_type,:map_move,:map_area,:text_file,:script_file,:town_map_group,
            :ol_value,:weather,:enable_roller_skates,:enable_bicycle,:enable_running,
            :enable_escape_rope,:enable_fly,:raw_hex)''', zones)
        db.executemany('''INSERT INTO map_matrices VALUES(
            :matrix_id,:u0,:u1,:has_lod,:width,:height,:transition_count,:raw_size,:sha256)''', matrices)
        db.executemany('INSERT INTO matrix_regions VALUES(:matrix_id,:matrix_x,:matrix_y,:region_id)', matrix_cells)
        db.executemany('INSERT INTO matrix_transitions VALUES(:matrix_id,:transition_index,:direction,:p1,:p2,:p3,:p4)', transitions)
        db.executemany('INSERT INTO field_regions VALUES(:region_id,:garc_size,:decoded_size,:decoded_signature,:sha256)', field_regions)
        db.commit()
    finally:
        db.close()

    report = {
        'schema': 'pokebot3ds.xy_world_build_report.v1',
        'output': str(output),
        'zones': len(zones),
        'unique_parent_maps': len({z['parent_map'] for z in zones}),
        'map_matrices': len(matrices),
        'matrix_cells': len(matrix_cells),
        'matrix_transitions': len(transitions),
        'field_regions': len(field_regions),
        'missing_parent_ids': missing_parent_ids,
        'bad_matrix_refs': bad_matrix_refs,
        'missing_region_refs': missing_region_refs,
        'aquacorde_zone_ids': [z['zone_id'] for z in zones if z['parent_map'] == 10],
        'source_sha256': {
            'a/0/1/2': sha256_bytes(enc_path.read_bytes()),
            'a/0/4/1': sha256_bytes(mapgr_path.read_bytes()),
            'a/0/4/2': sha256_bytes(matrix_path.read_bytes()),
        },
    }
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description='Compile the Pokémon X/Y world/location database from decrypted RomFS GARCs.')
    ap.add_argument('source_dir', type=Path, help='Folder containing a/0/1/2, a/0/4/1 and a/0/4/2')
    ap.add_argument('--output', type=Path, default=Path('pokemon_xy_world_runtime.sqlite'))
    ap.add_argument('--locations-py', type=Path, default=Path(__file__).resolve().parents[1] / 'pokebot' / 'common' / 'xy_locations.py')
    ap.add_argument('--report', type=Path, default=None)
    args = ap.parse_args()
    report = build_database(args.source_dir, args.output, args.locations_py)
    text = json.dumps(report, indent=2, ensure_ascii=False)
    print(text)
    if args.report:
        args.report.write_text(text + '\n', encoding='utf-8')
    if report['missing_parent_ids'] or report['bad_matrix_refs'] or report['missing_region_refs']:
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
