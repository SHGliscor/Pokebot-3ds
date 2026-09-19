from __future__ import annotations
import hashlib, json, sys, zipfile
from pathlib import Path
from datetime import datetime

REQUIRED = {
    "encdata_zonedata": "a/0/1/2",
    "mapGR": "a/0/4/1",
    "mapMatrix": "a/0/4/2",
}

def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def norm(name: str) -> str:
    return name.replace('\\','/').strip('/')

def find_member(z: zipfile.ZipFile, suffix: str):
    suffix = norm(suffix)
    hits = [n for n in z.namelist() if norm(n).endswith(suffix)]
    # Require a path-component boundary before suffix where possible.
    exactish = [n for n in hits if norm(n) == suffix or norm(n).endswith('/'+suffix)]
    hits = exactish or hits
    if not hits:
        return None
    hits.sort(key=lambda n: (len(norm(n)), norm(n)))
    return hits[0]

def read_member(zip_path: Path, suffix: str):
    with zipfile.ZipFile(zip_path, 'r') as z:
        m = find_member(z, suffix)
        if not m:
            return None
        return m, z.read(m)

def ask_path(label: str, optional=False) -> Path|None:
    while True:
        raw = input(label).strip().strip('"')
        if optional and not raw:
            return None
        p = Path(raw)
        if p.is_file(): return p
        print(f"Not found: {p}")

def main():
    print("Pokebot3DS X/Y world-data source extractor")
    print("Read-only: extracts only the three small X/Y world archives needed for DB compilation.\n")
    base = ask_path("Path to Y base.zip (required): ")
    update = ask_path("Path to Y update.zip (optional; press Enter to skip): ", optional=True)

    found = {}
    manifest = {
        "created": datetime.now().isoformat(timespec='seconds'),
        "game": "Pokemon Y",
        "source_base": str(base),
        "source_update": str(update) if update else None,
        "required": REQUIRED,
        "files": {},
        "notes": [
            "X/Y GARC layout: encdata/zonedata a/0/1/2; mapGR a/0/4/1; mapMatrix a/0/4/2.",
            "If the update contains a/0/1/2, it overrides the base copy to match the installed update.",
            "No game data is modified.",
        ],
    }

    for key, rel in REQUIRED.items():
        source = "base"
        res = read_member(base, rel)
        # Update overrides only when it actually contains the archive.
        if update:
            u = read_member(update, rel)
            if u is not None:
                res = u
                source = "update"
        if res is None:
            print(f"ERROR: could not find {rel} in supplied ZIP(s)")
            return 2
        member, data = res
        found[rel] = data
        manifest["files"][key] = {
            "romfs_path": rel,
            "zip_member": member,
            "selected_source": source,
            "size": len(data),
            "sha256": sha256(data),
        }
        print(f"FOUND {rel}  {len(data):,} bytes  [{source}]")

    out = Path.cwd() / "XY_WORLD_SOURCE_FILES.zip"
    with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel, data in found.items():
            z.writestr(rel, data)
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
        z.writestr("README.txt",
            "Upload this ZIP back to ChatGPT. It contains only the three X/Y RomFS archives needed to compile the world/location database.\n"
            "Required: a/0/1/2, a/0/4/1, a/0/4/2.\n")
    print(f"\nDONE: {out}")
    print("Upload XY_WORLD_SOURCE_FILES.zip back to the chat.")
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
