from __future__ import annotations

import argparse
import hashlib
import json
import struct
import zipfile
from pathlib import Path


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def decompress_lz11(data: bytes) -> bytes:
    if not data or data[0] != 0x11:
        return data
    size = data[1] | data[2] << 8 | data[3] << 16
    pos = 4
    if size == 0:
        size = struct.unpack_from("<I", data, pos)[0]
        pos += 4
    out = bytearray()
    while len(out) < size:
        flags = data[pos]
        pos += 1
        for bit in range(8):
            if len(out) >= size:
                break
            if not flags & (0x80 >> bit):
                out.append(data[pos])
                pos += 1
                continue
            b1, b2 = data[pos], data[pos + 1]
            pos += 2
            high = b1 >> 4
            if high == 0:
                b3 = data[pos]
                pos += 1
                length = ((b1 & 0xF) << 4 | b2 >> 4) + 0x11
                distance = ((b2 & 0xF) << 8 | b3) + 1
            elif high == 1:
                b3, b4 = data[pos], data[pos + 1]
                pos += 2
                length = ((b1 & 0xF) << 12 | b2 << 4 | b3 >> 4) + 0x111
                distance = ((b3 & 0xF) << 8 | b4) + 1
            else:
                length = high + 1
                distance = ((b1 & 0xF) << 8 | b2) + 1
            if distance > len(out):
                raise ValueError("invalid LZ11 distance")
            for _ in range(length):
                out.append(out[-distance])
                if len(out) == size:
                    break
    return bytes(out)


def garc_subfiles(data: bytes) -> list[bytes]:
    if data[:4] not in (b"CRAG", b"GARC"):
        return []
    header_size = struct.unpack_from("<I", data, 4)[0]
    data_offset = struct.unpack_from("<I", data, 0x10)[0]
    off = header_size
    fato_size = struct.unpack_from("<I", data, off + 4)[0]
    count = struct.unpack_from("<H", data, off + 8)[0]
    offsets = [
        struct.unpack_from("<I", data, off + 0x0C + 4 * i)[0]
        for i in range(count)
    ]
    off += fato_size
    out = []
    for relative in offsets:
        cursor = off + 0x0C + relative
        vector = struct.unpack_from("<I", data, cursor)[0]
        cursor += 4
        for bit in range(32):
            if vector & (1 << bit):
                start, _end, length = struct.unpack_from("<III", data, cursor)
                cursor += 12
                out.append(data[data_offset + start:data_offset + start + length])
    return out


def inspect_resource(relative: str, data: bytes) -> dict:
    raw_parts = garc_subfiles(data)
    decoded = []
    decode_failures = 0
    for part in raw_parts:
        try:
            decoded.append(decompress_lz11(part))
        except (IndexError, struct.error, ValueError):
            decoded.append(part)
            decode_failures += 1
    signatures = {}
    for part in decoded:
        signature = part[:2].decode("latin1", errors="replace")
        if part[:4] in (b"coll", b"term"):
            signature = part[:4].decode("latin1")
        signatures[signature] = signatures.get(signature, 0) + 1
    record_kind = None
    if relative == "a/0/1/3":
        record_kind = "ZO"
    elif relative == "a/0/4/0":
        record_kind = "MM"
    return {
        "path": relative,
        "sha256": sha256(data),
        "garc_entries": len(raw_parts),
        "decoded_bytes": sum(len(part) for part in decoded),
        "decode_failures": decode_failures,
        "signatures": signatures,
        "record_kind": record_kind,
    }


def archive_resources(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        out = {}
        for info in archive.infolist():
            marker = "/romfs/"
            if marker not in info.filename:
                continue
            relative = info.filename.split(marker, 1)[1].replace("\\", "/")
            if relative.startswith("a/"):
                out[relative] = archive.read(info)
        return out


def build_manifest(base_path: Path, update_path: Path) -> dict:
    base = archive_resources(base_path)
    update = archive_resources(update_path)
    all_paths = sorted(set(base) | set(update))
    resources = []
    for path in all_paths:
        base_record = inspect_resource(path, base[path]) if path in base else None
        update_record = inspect_resource(path, update[path]) if path in update else None
        resources.append({
            "path": path,
            "base": base_record,
            "update_1_4": update_record,
            "effective": "update_1_4" if update_record else "base",
            "changed": bool(
                base_record and update_record
                and base_record["sha256"] != update_record["sha256"]
            ),
        })
    return {
        "schema": "pokebot3ds.oras_map_manifest.v1",
        "game": "Pokemon Alpha Sapphire",
        "base_archive": str(base_path),
        "update_archive": str(update_path),
        "resources": resources,
        "summary": {
            "base_a_resources": len(base),
            "update_1_4_a_resources": len(update),
            "overridden_resources": sum(1 for r in resources if r["changed"]),
            "zone_archive": next(
                (r for r in resources if r["path"] == "a/0/1/3"), None
            ),
            "map_archive": next(
                (r for r in resources if r["path"] == "a/0/4/0"), None
            ),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a read-only ORAS base + 1.4 map resource manifest."
    )
    parser.add_argument("base_zip", type=Path)
    parser.add_argument("update_zip", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = build_manifest(args.base_zip, args.update_zip)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
