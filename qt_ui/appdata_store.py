
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from pokebot.common.evolution_prediction import predict_split_evolution

# Public release: persistent user state lives outside the extracted package.
PUBLIC_APPDATA_DIRNAME = "Pokebot-3DS"

STARTER_DEFAULTS = {
    "treecko": {"starter": "Treecko", "species": 252},
    "torchic": {"starter": "Torchic", "species": 255},
    "mudkip": {"starter": "Mudkip", "species": 258},
}

WILD_DEFAULTS = {
    "run": {
        "hunt_type": "Wild",
        "method": "run",
        "lifetime_seen": 0,
        "lifetime_shinies": 0,
        "phase_seen": 0,
        "last_phase_seen": 0,
        "phase_log_miss": None,
        "phase_cumulative_probability": 0.0,
        "last_phase_cumulative_probability": None,
        "last_shiny": None,
        "highest_sv": None,
        "lowest_sv": None,
        "highest_iv_sum": None,
        "lowest_iv_sum": None,
        "lifetime_hunt_seconds": 0.0,
        "lifetime_fastest_rate": None,
    },
    "walk": {
        "hunt_type": "Wild",
        "method": "walk",
        "lifetime_seen": 0,
        "lifetime_shinies": 0,
        "phase_seen": 0,
        "last_phase_seen": 0,
        "phase_log_miss": None,
        "phase_cumulative_probability": 0.0,
        "last_phase_cumulative_probability": None,
        "last_shiny": None,
        "highest_sv": None,
        "lowest_sv": None,
        "highest_iv_sum": None,
        "lowest_iv_sum": None,
        "lifetime_hunt_seconds": 0.0,
        "lifetime_fastest_rate": None,
    },
    "acro_bunny": {
        "hunt_type": "Wild",
        "method": "acro_bunny",
        "lifetime_seen": 0,
        "lifetime_shinies": 0,
        "phase_seen": 0,
        "last_phase_seen": 0,
        "phase_log_miss": None,
        "phase_cumulative_probability": 0.0,
        "last_phase_cumulative_probability": None,
        "last_shiny": None,
        "highest_sv": None,
        "lowest_sv": None,
        "highest_iv_sum": None,
        "lowest_iv_sum": None,
        "lifetime_hunt_seconds": 0.0,
        "lifetime_fastest_rate": None,
    },
    "horde": {
        "hunt_type": "Wild",
        "method": "horde",
        "lifetime_seen": 0,
        "lifetime_shinies": 0,
        "phase_seen": 0,
        "last_phase_seen": 0,
        "phase_log_miss": None,
        "phase_cumulative_probability": 0.0,
        "last_phase_cumulative_probability": None,
        "last_shiny": None,
        "highest_sv": None,
        "lowest_sv": None,
        "highest_iv_sum": None,
        "lowest_iv_sum": None,
        "lifetime_hunt_seconds": 0.0,
        "lifetime_fastest_rate": None,
    },
    "surf": {
        "hunt_type": "Wild",
        "method": "surf",
        "lifetime_seen": 0,
        "lifetime_shinies": 0,
        "phase_seen": 0,
        "last_phase_seen": 0,
        "phase_log_miss": None,
        "phase_cumulative_probability": 0.0,
        "last_phase_cumulative_probability": None,
        "last_shiny": None,
        "highest_sv": None,
        "lowest_sv": None,
        "highest_iv_sum": None,
        "lowest_iv_sum": None,
        "lifetime_hunt_seconds": 0.0,
        "lifetime_fastest_rate": None,
    },
    "fishing": {
        "hunt_type": "Wild",
        "method": "fishing",
        "lifetime_seen": 0,
        "lifetime_shinies": 0,
        "phase_seen": 0,
        "last_phase_seen": 0,
        "phase_log_miss": None,
        "phase_cumulative_probability": 0.0,
        "last_phase_cumulative_probability": None,
        "last_shiny": None,
        "highest_sv": None,
        "lowest_sv": None,
        "highest_iv_sum": None,
        "lowest_iv_sum": None,
        "lifetime_hunt_seconds": 0.0,
        "lifetime_fastest_rate": None,
        "fishing_peak_chain": 0,
        "fishing_chain_breaks": 0,
        "fishing_hooked": 0,
        "fishing_no_bites": 0,
        "fishing_missed_hooks": 0,
        "fishing_current_chain": 0,
        "fishing_next_shiny_rolls": 1,
    },
    "cave": {
        "hunt_type": "Wild",
        "method": "cave",
        "lifetime_seen": 0,
        "lifetime_shinies": 0,
        "phase_seen": 0,
        "last_phase_seen": 0,
        "phase_log_miss": None,
        "phase_cumulative_probability": 0.0,
        "last_phase_cumulative_probability": None,
        "last_shiny": None,
        "highest_sv": None,
        "lowest_sv": None,
        "highest_iv_sum": None,
        "lowest_iv_sum": None,
        "lifetime_hunt_seconds": 0.0,
        "lifetime_fastest_rate": None,
    },
}

class ProfilePaths:
    def __init__(self, root):
        self.root = Path(root)
        self.stats_dir = self.root / "stats"
        self.history_dir = self.root / "history"
        self.encounters_dir = self.history_dir / "encounters"
        self.session_stats_dir = self.history_dir / "session_stats"
        self.settings_path = self.root / "settings.json"
        self.last_seen_path = self.history_dir / "last_seen_oras.json"
        self.recent_shinies_path = self.history_dir / "recent_shinies.json"
        self.migration_path = self.root / "migration_v1.json"
        self.species_shiny_totals_path = self.stats_dir / "species_shiny_totals.json"
        self.shiny_blocklist_path = self.root / "shiny_blocklist.json"
        self.screenshots_dir = self.history_dir / "screenshots"
        self.pokerus_history_path = self.history_dir / "pokerus_events.jsonl"

def _profile_root():
    """Return the persistent public-release profile directory.

    Windows releases use %APPDATA%/Pokebot-3DS so settings, statistics,
    history, support evidence and calibration survive replacing the app folder.
    POKEBOT_APPDATA_ROOT remains available for controlled portable/test runs.
    """
    override = os.environ.get("POKEBOT_APPDATA_ROOT") or os.environ.get("POKEBOT_DATA_ROOT")
    if override:
        return Path(override).expanduser().resolve()

    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata).expanduser().resolve() / PUBLIC_APPDATA_DIRNAME

    # Non-Windows/source fallback. Production Windows always has APPDATA.
    return Path.home() / ".pokebot-3ds"

def get_profile_paths():
    return ProfilePaths(_profile_root())

def _default_stats(key):
    meta = STARTER_DEFAULTS[key]
    return {
        "starter": meta["starter"],
        "species": meta["species"],
        "lifetime_seen": 0,
        "lifetime_shinies": 0,
        "completed_sessions": 0,
        "phase_seen": 0,
        "last_phase_seen": 0,
        "phase_log_miss": None,
        "phase_cumulative_probability": 0.0,
        "last_phase_cumulative_probability": None,
        "last_shiny": None,
        "last_result": None,
        "best_shiny_xor": None,
        "highest_shiny_xor": None,
        "lifetime_highest_iv_sum": None,
        "lifetime_lowest_iv_sum": None,
        "lifetime_highest_sv": None,
        "lifetime_lowest_sv": None,
        "highest_iv_record": None,
        "lowest_iv_record": None,
        "highest_sv_record": None,
        "lowest_sv_record": None,
        "last_session_summary": None,
        "lifetime_hunt_seconds": 0.0,
        "lifetime_fastest_rate": None,
    }

def _load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default

def _atomic_write(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)

def load_shiny_blocklist(profile=None):
    """Return the persistent Wild shiny blocklist.

    Fail-safe rule: unreadable/corrupt data means an EMPTY blocklist, which
    preserves the normal shiny HOLD behaviour.
    """
    profile = profile or get_profile_paths()
    data = _load_json(profile.shiny_blocklist_path, {})
    if not isinstance(data, dict):
        return {"version": 1, "species": {}}

    species = data.get("species")
    if not isinstance(species, dict):
        species = {}

    clean = {}
    for key, value in species.items():
        try:
            sid = int(key)
        except Exception:
            continue
        if sid <= 0:
            continue
        if isinstance(value, dict):
            enabled = bool(value.get("enabled", False))
            name = str(value.get("species_name") or f"Species #{sid}")
        else:
            enabled = bool(value)
            name = f"Species #{sid}"
        if enabled:
            clean[str(sid)] = {
                "species": sid,
                "species_name": name,
                "enabled": True,
            }
    return {"version": 1, "species": clean}


def blocked_shiny_species(profile=None):
    data = load_shiny_blocklist(profile)
    result = set()
    for key, value in (data.get("species") or {}).items():
        if isinstance(value, dict) and bool(value.get("enabled")):
            try:
                result.add(int(key))
            except Exception:
                pass
    return result


def is_species_shiny_blocked(species, profile=None):
    try:
        species = int(species)
    except Exception:
        return False
    return species in blocked_shiny_species(profile)


def set_species_shiny_block(
    species,
    species_name,
    enabled,
    profile=None,
):
    """Atomically update one species.

    ON means: a RAM-confirmed Wild shiny of this species is still recorded and
    counted, but its automatic action changes from HOLD to causal Run.
    """
    profile = profile or get_profile_paths()
    species = int(species)
    if species <= 0:
        raise ValueError("species must be positive")

    data = load_shiny_blocklist(profile)
    entries = dict(data.get("species") or {})
    key = str(species)

    if bool(enabled):
        entries[key] = {
            "species": species,
            "species_name": str(species_name or f"Species #{species}"),
            "enabled": True,
        }
    else:
        entries.pop(key, None)

    payload = {
        "version": 1,
        "species": entries,
    }
    _atomic_write(profile.shiny_blocklist_path, payload)
    return payload


def _legacy_runtime_dirs(base_dir):
    base_dir = Path(base_dir).resolve()
    found = []

    current = base_dir / "runtime"
    if current.exists():
        found.append(current)

    try:
        siblings = sorted(
            (
                p for p in base_dir.parent.iterdir()
                if p.is_dir() and (
                    p.name.startswith("Pokebot3DS-CFW")
                    or p.name.startswith("Pokebot" + "-CFW-")
                )
            ),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except Exception:
        siblings = []

    for sibling in siblings:
        runtime = sibling / "runtime"
        if runtime.exists() and runtime not in found:
            found.append(runtime)

    return found

def _best_legacy_stats(runtime_dirs, starter):
    best = None
    best_score = None

    for runtime in runtime_dirs:
        path = runtime / "stats" / f"{starter}.json"
        payload = _load_json(path)
        if not isinstance(payload, dict):
            continue
        try:
            score = (
                int(payload.get("lifetime_seen", 0)),
                int(payload.get("lifetime_shinies", 0)),
                path.stat().st_mtime,
            )
        except Exception:
            score = (0, 0, 0)

        if best is None or score > best_score:
            best = (path, payload)
            best_score = score

    return best

def _merge_history(runtime_dirs, filename, limit):
    merged = []
    seen = set()

    for runtime in runtime_dirs:
        payload = _load_json(runtime / filename, [])
        if not isinstance(payload, list):
            continue

        for item in payload:
            if not isinstance(item, dict):
                continue
            identity = (
                item.get("starter"),
                item.get("pid") or item.get("pokemon_pid"),
                item.get("time"),
                item.get("attempt"),
            )
            if identity in seen:
                continue
            seen.add(identity)
            merged.append(dict(item))

    merged.sort(
        key=lambda x: str(x.get("time") or ""),
        reverse=True,
    )
    return merged[:limit]

def _tail_jsonl_records(path, max_lines=64, max_bytes=262144):
    """Read only a bounded tail of a potentially large encounter JSONL."""
    path = Path(path)
    if not path.exists():
        return []
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            start = max(0, size - int(max_bytes))
            fh.seek(start)
            if start:
                fh.readline()  # discard possible partial first line
            raw_lines = fh.readlines()[-int(max_lines):]
    except Exception:
        return []

    records = []
    for raw in raw_lines:
        try:
            item = json.loads(raw.decode("utf-8"))
        except Exception:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def _safe_history_int(value, default=0):
    """Best-effort scalar integer coercion for persisted history records.

    Encounter history shares a directory with some aggregate ledgers (for
    example fossil batch summaries), so startup repair must never assume every
    JSONL record uses the single-Pokémon encounter schema.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, str)):
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return int(default)
    return int(default)


def _normalise_last_seen_record(item):
    """Normalise single-Pokémon encounter-history schemas for dashboard use.

    Non-encounter/aggregate records are ignored rather than allowed to break
    application startup.  In particular, fossil batch ledgers intentionally
    store ``species`` as a list of revived Pokémon names.
    """
    if not isinstance(item, dict):
        return None

    species = _safe_history_int(item.get("species", 0), 0)
    species_name = str(item.get("species_name") or "—")
    when = str(item.get("time") or "")
    if species <= 0 or not when:
        return None

    sv = item.get("shiny_xor")
    if sv is None:
        sv = item.get("sv", 0)

    shiny = item.get("is_shiny")
    if shiny is None:
        shiny = item.get("shiny", False)

    raw_ivs = item.get("ivs")
    ivs = dict(raw_ivs) if isinstance(raw_ivs, dict) else {}

    predicted_evolution = str(item.get("predicted_evolution") or "").strip()
    evolution_prediction = item.get("evolution_prediction")
    if not predicted_evolution:
        predicted = predict_split_evolution(species, item.get("ec"))
        if predicted:
            predicted_evolution = str(predicted.get("line") or "")
            evolution_prediction = predicted

    return {
        "attempt": _safe_history_int(item.get("attempt", 0), 0),
        "species": species,
        "species_name": species_name,
        "nature": str(item.get("nature") or "—"),
        "ability": item.get("ability"),
        "ability_id": item.get("ability_id"),
        "gender": str(item.get("gender") or "—"),
        "predicted_evolution": predicted_evolution or None,
        "evolution_prediction": evolution_prediction,
        "pokemon_pid": (
            item.get("pokemon_pid")
            or item.get("pid")
            or "—"
        ),
        "ec": item.get("ec", "—"),
        "tid": item.get("tid"),
        "sid": item.get("sid"),
        "shiny_xor": _safe_history_int(sv, 0),
        "is_shiny": bool(shiny),
        "ivs": ivs,
        "time": when,
        # Optional context is harmless to the existing dashboard and useful
        # for future UI/Discord work.
        "hunt_type": item.get("hunt_type"),
        "method": item.get("method") or item.get("target"),
        "location_name": item.get("location_name"),
    }


def repair_last_seen_history(profile, limit=7):
    """Repair the shared Last Seen ledger from persistent encounter history.

    Before v0p40f, Wild encounters were shown live but were never written into
    last_seen_oras.json. Wild encounter JSONL files *were* persisted, so use
    those files plus the existing starter ledger to reconstruct the newest
    entries on startup.

    This function is bounded: at most 64 tail records / 256 KiB per JSONL.
    """
    merged = []
    seen = set()

    existing = _load_json(profile.last_seen_path, [])
    sources = list(existing) if isinstance(existing, list) else []

    try:
        jsonl_paths = sorted(profile.encounters_dir.glob("*.jsonl"))
    except Exception:
        jsonl_paths = []

    for path in jsonl_paths:
        # Aggregate fossil batch ledgers are intentionally not single-Pokémon
        # encounter records and carry ``species`` as a list.  Do not feed them
        # into Last Seen reconstruction.
        if path.name.endswith("_batches.jsonl"):
            continue
        sources.extend(_tail_jsonl_records(path))

    for raw in sources:
        item = _normalise_last_seen_record(raw)
        if not item:
            continue
        identity = (
            item.get("time"),
            int(item.get("species", 0)),
            str(item.get("pokemon_pid") or ""),
            int(item.get("attempt", 0)),
        )
        if identity in seen:
            continue
        seen.add(identity)
        merged.append(item)

    merged.sort(
        key=lambda x: str(x.get("time") or ""),
        reverse=True,
    )
    merged = merged[:int(limit)]

    # Always atomically rewrite when a valid merged ledger exists. This
    # upgrades old starter-only last_seen files automatically.
    if merged:
        _atomic_write(profile.last_seen_path, merged)

    return merged


def load_species_shiny_totals(profile=None):
    profile = profile or get_profile_paths()
    data = _load_json(profile.species_shiny_totals_path, {})
    return data if isinstance(data, dict) else {}

def _initial_species_shiny_totals(profile):
    totals = {}

    def add_species(species_id, species_name, found_time=None):
        try:
            species_id = int(species_id)
        except Exception:
            return
        if species_id <= 0:
            return
        key = str(species_id)
        rec = totals.setdefault(key, {
            "species": species_id,
            "species_name": str(species_name or f"Species {species_id}"),
            "total": 0,
            "last_found": None,
            "source": "migrated_encounter_ledger",
        })
        rec["total"] = int(rec.get("total", 0)) + 1
        if found_time and (rec.get("last_found") is None or str(found_time) > str(rec.get("last_found"))):
            rec["last_found"] = found_time

    # Rebuild from the persistent encounter ledgers first. These are the most
    # specific historical source because each line identifies the species.
    if profile.encounters_dir.exists():
        for path in sorted(profile.encounters_dir.glob("*.jsonl")):
            try:
                with path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            item = json.loads(line)
                        except Exception:
                            continue
                        if not isinstance(item, dict):
                            continue
                        shiny = bool(item.get("is_shiny", item.get("shiny", False)))
                        if not shiny:
                            continue
                        species_id = item.get("species_id", item.get("species"))
                        if isinstance(species_id, str) and not species_id.isdigit():
                            # Some older ledgers used species for the display name.
                            species_id = item.get("target_species")
                        add_species(
                            species_id,
                            item.get("species_name") or item.get("species"),
                            item.get("time"),
                        )
            except Exception:
                continue

    # Starter lifetime stats are exact. If an older encounter ledger is
    # incomplete, promote each starter's species total to at least its stored
    # lifetime shiny count rather than under-counting it.
    for key, meta in STARTER_DEFAULTS.items():
        payload = _load_json(profile.stats_dir / f"{key}.json", {})
        try:
            count = int((payload or {}).get("lifetime_shinies", 0))
        except Exception:
            count = 0
        if count <= 0:
            continue
        skey = str(meta["species"])
        rec = totals.setdefault(skey, {
            "species": int(meta["species"]),
            "species_name": meta["starter"],
            "total": 0,
            "last_found": None,
            "source": "migrated_starter_lifetime",
        })
        if int(rec.get("total", 0)) < count:
            rec["total"] = count
            rec["source"] = "migrated_starter_lifetime"

    return totals

def ensure_species_shiny_totals(profile=None):
    profile = profile or get_profile_paths()
    if not profile.species_shiny_totals_path.exists():
        _atomic_write(
            profile.species_shiny_totals_path,
            _initial_species_shiny_totals(profile),
        )
    return load_species_shiny_totals(profile)

def increment_species_shiny_total(species, species_name, profile=None, *, found_time=None):
    profile = profile or get_profile_paths()
    totals = ensure_species_shiny_totals(profile)
    key = str(int(species))
    rec = totals.get(key)
    if not isinstance(rec, dict):
        rec = {
            "species": int(species),
            "species_name": str(species_name or f"Species {species}"),
            "total": 0,
            "last_found": None,
            "source": "live_species_ledger",
        }
    rec["species"] = int(species)
    rec["species_name"] = str(species_name or rec.get("species_name") or f"Species {species}")
    rec["total"] = int(rec.get("total", 0)) + 1
    rec["last_found"] = found_time
    rec["source"] = "live_species_ledger"
    totals[key] = rec
    _atomic_write(profile.species_shiny_totals_path, totals)
    return int(rec["total"])

def migrate_legacy_data(base_dir, profile=None):
    profile = profile or get_profile_paths()
    profile.root.mkdir(parents=True, exist_ok=True)
    profile.stats_dir.mkdir(parents=True, exist_ok=True)
    profile.history_dir.mkdir(parents=True, exist_ok=True)
    profile.encounters_dir.mkdir(parents=True, exist_ok=True)
    profile.session_stats_dir.mkdir(parents=True, exist_ok=True)

    runtime_dirs = _legacy_runtime_dirs(base_dir)
    migrated = {
        "legacy_runtime_dirs": [str(p) for p in runtime_dirs],
        "stats": {},
        "settings": None,
        "history": {},
    }

    for starter in STARTER_DEFAULTS:
        dest = profile.stats_dir / f"{starter}.json"
        if dest.exists():
            continue

        candidate = _best_legacy_stats(runtime_dirs, starter)
        if candidate:
            source, payload = candidate
            _atomic_write(dest, payload)
            migrated["stats"][starter] = str(source)
        else:
            _atomic_write(dest, _default_stats(starter))

    for method, payload in WILD_DEFAULTS.items():
        dest = profile.stats_dir / f"wild_{method}.json"
        if not dest.exists():
            _atomic_write(dest, dict(payload))

    if not profile.settings_path.exists():
        candidates = []
        for runtime in runtime_dirs:
            path = runtime / "settings.json"
            if path.exists():
                try:
                    candidates.append((path.stat().st_mtime, path))
                except Exception:
                    pass
        if candidates:
            candidates.sort(reverse=True)
            source = candidates[0][1]
            try:
                shutil.copy2(source, profile.settings_path)
                migrated["settings"] = str(source)
            except Exception:
                pass

    if not profile.last_seen_path.exists():
        history = _merge_history(
            runtime_dirs,
            "last_seen_oras.json",
            7,
        )
        _atomic_write(profile.last_seen_path, history)
        migrated["history"]["last_seen"] = len(history)

    if not profile.recent_shinies_path.exists():
        shinies = _merge_history(
            runtime_dirs,
            "recent_shinies.json",
            20,
        )
        _atomic_write(profile.recent_shinies_path, shinies)
        migrated["history"]["recent_shinies"] = len(shinies)

    _atomic_write(profile.migration_path, migrated)
    return profile

def ensure_profile(base_dir):
    profile = get_profile_paths()
    profile.root.mkdir(parents=True, exist_ok=True)
    profile.stats_dir.mkdir(parents=True, exist_ok=True)
    profile.history_dir.mkdir(parents=True, exist_ok=True)
    profile.encounters_dir.mkdir(parents=True, exist_ok=True)
    profile.session_stats_dir.mkdir(parents=True, exist_ok=True)

    # Legacy package-folder discovery can touch every sibling Pokebot build in
    # the extraction directory. It is migration work, not normal startup work.
    # Once migration_v1.json exists, keep startup local to AppData and only
    # create any newly introduced baseline files that are missing.
    if not profile.migration_path.exists():
        profile = migrate_legacy_data(base_dir, profile)
    else:
        for starter in STARTER_DEFAULTS:
            dest = profile.stats_dir / f"{starter}.json"
            if not dest.exists():
                _atomic_write(dest, _default_stats(starter))
        for method, payload in WILD_DEFAULTS.items():
            dest = profile.stats_dir / f"wild_{method}.json"
            if not dest.exists():
                _atomic_write(dest, dict(payload))
        if not profile.last_seen_path.exists():
            _atomic_write(profile.last_seen_path, [])
        if not profile.recent_shinies_path.exists():
            _atomic_write(profile.recent_shinies_path, [])

    ensure_species_shiny_totals(profile)
    repair_last_seen_history(profile)
    return profile

def reset_all_stats(profile=None):
    profile = profile or get_profile_paths()
    profile.stats_dir.mkdir(parents=True, exist_ok=True)
    profile.history_dir.mkdir(parents=True, exist_ok=True)
    profile.encounters_dir.mkdir(parents=True, exist_ok=True)
    profile.session_stats_dir.mkdir(parents=True, exist_ok=True)

    for starter in STARTER_DEFAULTS:
        _atomic_write(
            profile.stats_dir / f"{starter}.json",
            _default_stats(starter),
        )

    for method, payload in WILD_DEFAULTS.items():
        _atomic_write(
            profile.stats_dir / f"wild_{method}.json",
            dict(payload),
        )

    _atomic_write(profile.last_seen_path, [])
    _atomic_write(profile.recent_shinies_path, [])
    _atomic_write(profile.species_shiny_totals_path, {})

    return {
        "stats_reset": list(STARTER_DEFAULTS) + [
            f"wild_{method}" for method in WILD_DEFAULTS
        ],
        "last_seen_reset": True,
        "recent_shinies_reset": True,
        "species_shiny_totals_reset": True,
        "settings_preserved": True,
    }
