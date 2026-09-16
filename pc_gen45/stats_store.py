from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
import threading
from typing import Any


APP_FOLDER = "Pokebot-Gen45-PC"
STATS_FILE = "stats.json"
MAX_RECENT = 30


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def appdata_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        root = Path(base)
    else:
        root = Path.home() / "AppData" / "Local"
    path = root / APP_FOLDER
    path.mkdir(parents=True, exist_ok=True)
    return path


class StatsStore:
    """Persistent lifetime/session hunt statistics stored in AppData."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (appdata_dir() / STATS_FILE)
        self._lock = threading.RLock()
        self.data = self._load()
        self.start_new_session()

    def _default(self) -> dict[str, Any]:
        return {
            "version": 1,
            "lifetime": {
                "seen": 0,
                "shinies": 0,
                "resets": 0,
                "hunts_started": 0,
            },
            "session": {
                "started_at": _now_iso(),
                "seen": 0,
                "shinies": 0,
                "resets": 0,
                "sets": 0,
            },
            "last_hunt": None,
            "recently_seen": [],
        }

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._default()
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("stats root must be an object")
        except Exception:
            backup = self.path.with_suffix(".corrupt.json")
            try:
                self.path.replace(backup)
            except Exception:
                pass
            return self._default()

        default = self._default()
        lifetime = loaded.get("lifetime")
        if not isinstance(lifetime, dict):
            lifetime = {}
        session = loaded.get("session")
        if not isinstance(session, dict):
            session = {}

        default["lifetime"].update({
            key: int(lifetime.get(key, 0) or 0)
            for key in default["lifetime"]
        })
        default["session"].update({
            key: session.get(key, default["session"][key])
            for key in default["session"]
        })
        recent = loaded.get("recently_seen", [])
        if isinstance(recent, list):
            default["recently_seen"] = recent[:MAX_RECENT]
        default["last_hunt"] = loaded.get("last_hunt")
        return default

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)

    def start_new_session(self) -> None:
        with self._lock:
            self.data["session"] = {
                "started_at": _now_iso(),
                "seen": 0,
                "shinies": 0,
                "resets": 0,
                "sets": 0,
            }
            self.save()

    def start_hunt(self, hunt: dict[str, Any]) -> None:
        with self._lock:
            self.data["last_hunt"] = {
                **hunt,
                "started_at": _now_iso(),
            }
            self.data["lifetime"]["hunts_started"] += 1
            self.save()

    def record_set(self, mons: list[dict[str, Any]]) -> None:
        with self._lock:
            self.data["session"]["sets"] += 1
            self.data["session"]["seen"] += len(mons)
            self.data["lifetime"]["seen"] += len(mons)

            shiny_count = sum(1 for mon in mons if mon.get("shiny"))
            self.data["session"]["shinies"] += shiny_count
            self.data["lifetime"]["shinies"] += shiny_count

            recent = self.data.setdefault("recently_seen", [])
            for mon in mons:
                recent.insert(0, {
                    **mon,
                    "seen_at": _now_iso(),
                })
            del recent[MAX_RECENT:]
            self.save()

    def record_reset(self) -> None:
        with self._lock:
            self.data["session"]["resets"] += 1
            self.data["lifetime"]["resets"] += 1
            self.save()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self.data))
