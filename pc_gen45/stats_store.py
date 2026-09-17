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
MAX_RECENT_SHINIES = 20


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
                "best_iv_sum": None,
                "worst_iv_sum": None,
                "best_sv": None,
                "worst_sv": None,
            },
            "session": {
                "started_at": _now_iso(),
                "seen": 0,
                "shinies": 0,
                "resets": 0,
                "sets": 0,
                "best_iv_sum": None,
                "worst_iv_sum": None,
                "best_sv": None,
                "worst_sv": None,
            },
            "last_hunt": None,
            "recently_seen": [],
            "recent_shinies": [],
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

        for key in ("seen", "shinies", "resets", "hunts_started"):
            default["lifetime"][key] = int(lifetime.get(key, 0) or 0)
        for key in ("best_iv_sum", "worst_iv_sum", "best_sv", "worst_sv"):
            value = lifetime.get(key)
            default["lifetime"][key] = int(value) if value is not None else None

        default["session"]["started_at"] = session.get(
            "started_at", default["session"]["started_at"]
        )
        for key in ("seen", "shinies", "resets", "sets"):
            default["session"][key] = int(session.get(key, 0) or 0)
        for key in ("best_iv_sum", "worst_iv_sum", "best_sv", "worst_sv"):
            value = session.get(key)
            default["session"][key] = int(value) if value is not None else None

        recent = loaded.get("recently_seen", [])
        if isinstance(recent, list):
            default["recently_seen"] = recent[:MAX_RECENT]

        recent_shinies = loaded.get("recent_shinies")
        if isinstance(recent_shinies, list):
            default["recent_shinies"] = recent_shinies[:MAX_RECENT_SHINIES]
        else:
            # Migration for builds that only stored a rolling encounter list.
            default["recent_shinies"] = [
                mon for mon in default["recently_seen"] if mon.get("shiny")
            ][:MAX_RECENT_SHINIES]

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
                "best_iv_sum": None,
                "worst_iv_sum": None,
                "best_sv": None,
                "worst_sv": None,
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

            for mon in mons:
                ivs = mon.get("ivs") or []
                if len(ivs) >= 6:
                    iv_sum = sum(int(v) for v in ivs[:6])
                    for scope in ("session", "lifetime"):
                        best = self.data[scope].get("best_iv_sum")
                        worst = self.data[scope].get("worst_iv_sum")
                        self.data[scope]["best_iv_sum"] = iv_sum if best is None else max(best, iv_sum)
                        self.data[scope]["worst_iv_sum"] = iv_sum if worst is None else min(worst, iv_sum)

                if mon.get("sv") is not None:
                    sv = int(mon["sv"])
                    for scope in ("session", "lifetime"):
                        best = self.data[scope].get("best_sv")
                        worst = self.data[scope].get("worst_sv")
                        # Smaller SV is closer to the Gen-IV shiny threshold (<8).
                        self.data[scope]["best_sv"] = sv if best is None else min(best, sv)
                        self.data[scope]["worst_sv"] = sv if worst is None else max(worst, sv)

            recent = self.data.setdefault("recently_seen", [])
            recent_shinies = self.data.setdefault("recent_shinies", [])
            for mon in mons:
                stamped = {
                    **mon,
                    "seen_at": _now_iso(),
                }
                recent.insert(0, stamped)
                if mon.get("shiny"):
                    recent_shinies.insert(0, stamped)
            del recent[MAX_RECENT:]
            del recent_shinies[MAX_RECENT_SHINIES:]
            self.save()

    def record_reset(self) -> None:
        with self._lock:
            self.data["session"]["resets"] += 1
            self.data["lifetime"]["resets"] += 1
            self.save()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self.data))
