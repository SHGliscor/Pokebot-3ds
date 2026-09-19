from __future__ import annotations

import argparse
import json
import struct
import zipfile
from pathlib import Path

from build_oras_map_manifest import decompress_lz11, garc_subfiles


def archive_resource(path: Path, relative: str) -> bytes:
    with zipfile.ZipFile(path) as archive:
        suffix = f"/romfs/{relative}"
        for info in archive.infolist():
            if info.filename.replace("\\", "/").endswith(suffix):
                return archive.read(info)
    raise FileNotFoundError(f"{relative} not found in {path}")


def read_u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def read_u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def inspect_zo(data: bytes) -> dict:
    if data[:2] != b"ZO":
        raise ValueError("not a ZO record")
    version = read_u16(data, 2)
    header_size = read_u32(data, 4)
    offsets = [read_u32(data, 8 + 4 * i) for i in range(5)]
    valid_offsets = (
        header_size <= offsets[0] <= offsets[1] <= offsets[2]
        <= offsets[3] <= offsets[4] <= len(data)
    )
    return {
        "kind": "ZO",
        "version": version,
        "header_size": header_size,
        "section_offsets": offsets,
        "section_sizes": [
            offsets[i + 1] - offsets[i] for i in range(4)
        ],
        "length": len(data),
        "offsets_valid": valid_offsets,
    }


def inspect_mm(data: bytes) -> dict:
    if data[:2] != b"MM":
        raise ValueError("not an MM record")
    version = read_u16(data, 2)
    header_size = read_u32(data, 4)
    total_size = read_u32(data, 8)
    width = read_u16(data, 0x14)
    height = read_u16(data, 0x16)
    tile_count = width * height
    tile_bytes = tile_count * 4
    return {
        "kind": "MM",
        "version": version,
        "header_size": header_size,
        "declared_size": total_size,
        "length": len(data),
        "width": width,
        "height": height,
        "tile_count": tile_count,
        "tile_bytes": tile_bytes,
        "tile_payload_fits": header_size + tile_bytes <= len(data),
        "declared_size_matches": total_size == len(data),
    }


def inspect_archive(path: Path, relative: str) -> dict:
    raw = archive_resource(path, relative)
    records = []
    failures = []
    for index, part in enumerate(garc_subfiles(raw)):
        try:
            decoded = decompress_lz11(part)
            if relative == "a/0/1/3" and decoded[:2] == b"ZO":
                records.append({"index": index, **inspect_zo(decoded)})
            elif relative == "a/0/4/0" and decoded[:2] == b"MM":
                records.append({"index": index, **inspect_mm(decoded)})
        except (IndexError, struct.error, ValueError) as exc:
            failures.append({"index": index, "error": str(exc)})
    return {
        "archive": str(path),
        "resource": relative,
        "garc_entries": len(garc_subfiles(raw)),
        "records": records,
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect ORAS ZO zone and MM map record headers."
    )
    parser.add_argument("base_zip", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = {
        "schema": "pokebot3ds.oras_map_records.v1",
        "zone_records": inspect_archive(args.base_zip, "a/0/1/3"),
        "map_records": inspect_archive(args.base_zip, "a/0/4/0"),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "zone_records": len(result["zone_records"]["records"]),
        "map_records": len(result["map_records"]["records"]),
        "zone_failures": len(result["zone_records"]["failures"]),
        "map_failures": len(result["map_records"]["failures"]),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
