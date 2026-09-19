from __future__ import annotations

"""Deterministic public ZIP builder with preflight integrity checks.

Run from an extracted release source tree.  It refuses to package if the
offline regression or local import/dependency audit fails, preventing the
v0p43BM-style missing-module regression.
"""

import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE_DIRS = {"__pycache__", ".git", ".venv", ".venv-build", "build", "dist", "runtime"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}


def run_check(script: str) -> None:
    subprocess.run([sys.executable, str(ROOT / "tools" / script)], cwd=ROOT, check=True)


def files_for_release():
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if path.suffix.lower() in EXCLUDE_SUFFIXES:
            continue
        yield path, rel


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv=None):
    argv = list(argv or sys.argv[1:])
    output = Path(argv[0]).expanduser().resolve() if argv else ROOT.parent / f"{ROOT.name}.zip"
    run_check("offline_regression.py")
    run_check("release_integrity.py")

    # Never ship interpreter caches from validation.
    for cache in ROOT.rglob("__pycache__"):
        if cache.is_dir():
            shutil.rmtree(cache, ignore_errors=True)

    file_rows = []
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr(ROOT.name.rstrip("/") + "/", b"")
        for path, rel in files_for_release():
            arc = f"{ROOT.name}/{rel.as_posix()}"
            zf.write(path, arc)
            file_rows.append({"path": rel.as_posix(), "size": path.stat().st_size, "sha256": sha256(path)})

    with zipfile.ZipFile(output) as zf:
        bad = zf.testzip()
        roots = {n.split("/", 1)[0] for n in zf.namelist() if n}
        if bad or roots != {ROOT.name}:
            raise RuntimeError(f"ZIP validation failed bad={bad!r} roots={sorted(roots)}")

    print(json.dumps({
        "status": "PASS",
        "output": str(output),
        "top_level": ROOT.name,
        "files": len(file_rows),
        "zip_sha256": sha256(output),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
