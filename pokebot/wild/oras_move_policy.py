from __future__ import annotations

"""ORAS Horde attack selector (D25/BN-derived hardware-proven policy).

Move IDs and current PP come from the live PK6 read from the hardware-proven
runtime party chain.  Move targeting/category data is bundled from Alpha
Sapphire's own ``a/1/8/9`` move GARC, so a shiny encounter never depends on an
internet lookup.

Hard safety gate for Horde reduction:
- PP > 0
- physical or special damage class
- native ORAS target code 0 (one explicitly selected adjacent Pokemon)

Every other native target class is rejected.  That includes Surf/Earthquake
style spread moves, all-foe moves, field moves, and target code 9 used by
locked/random attacks such as Thrash/Petal Dance/Outrage.  Single-target
multi-hit attacks remain allowed because they still target only the one
explicitly selected opponent.
"""

import json
import os
from pathlib import Path

from pokebot.wild.best_ball import SELF_LOSS_RISK_MOVES

# Historical control-risk list. D25 no longer rejects these merely because they
# are two-turn/recharge/recoil/etc.; it ranks them below cleaner safe-target
# attacks. Targeting remains the hard shiny-protection authority.
CONTROL_RISK_MOVE_IDS = {
    19, 63, 76, 91, 99, 130, 143, 205, 264, 301, 307, 308,
    338, 340, 369, 416, 439, 459, 467, 507, 509, 521, 525, 553, 554, 566,
}

# ORAS MoveEditor6 target code 0 = Single Adjacent Ally/Foe. In a Horde, after
# the exact target-mask selector is opened, this is one explicitly selected
# opponent. Code 9 is intentionally NOT included: it is used by Thrash,
# Petal Dance, Rollout-style locked/random target behaviour and is unsafe.
SAFE_NATIVE_TARGET_CODES = {0}

# Compatibility fallback for a pre-existing veekun-style cache.
SAFE_VEEKUN_TARGET_IDS = {10}

FALLBACK_NAMES = {
    57: "surf", 61: "bubble-beam", 75: "razor-leaf", 80: "petal-dance",
    85: "thunderbolt", 89: "earthquake", 98: "quick-attack",
    120: "self-destruct", 129: "swift", 230: "sweet-scent", 405: "bug-buzz",
}


def _cache_path(base_dir: Path) -> Path:
    override = os.environ.get("POKEBOT_APPDATA_ROOT") or os.environ.get("POKEBOT_DATA_ROOT")
    root = Path(override).expanduser().resolve() if override else (Path(os.environ["APPDATA"]).expanduser().resolve() / "Pokebot-3DS") if os.environ.get("APPDATA") else (Path.home() / ".pokebot-3ds")
    return root / "cache" / "gen6_moves.json"


def _bundled_path(base_dir: Path) -> Path:
    return Path(base_dir) / "data" / "oras_gen6_move_metadata.json"


def _load_json(path: Path) -> dict[int, dict]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {int(k): dict(v) for k, v in raw.items()}
    except Exception:
        return {}


def ensure_move_metadata(base_dir: Path, *, timeout: float = 0.0) -> dict[int, dict]:
    """Return bundled ORAS Gen-6 metadata. No network access is performed."""
    db = _load_json(_bundled_path(base_dir))

    # A cache from an older build may contain better human-readable identifiers.
    # Never let it replace D25's native target/category authority.
    cached = _load_json(_cache_path(base_dir))
    for move_id, old in cached.items():
        if move_id in db:
            ident = str(old.get("identifier") or "").strip()
            if ident:
                db[move_id]["identifier"] = ident
        elif old:
            # Compatibility only; unknown/non-bundled data remains fail-closed
            # unless it contains a selected-Pokemon target ID and damage class.
            db[move_id] = dict(old)
    for move_id, name in FALLBACK_NAMES.items():
        if move_id in db and str(db[move_id].get("identifier") or "").startswith("move-"):
            db[move_id]["identifier"] = name
    return db


def move_name(meta: dict | None, move_id: int) -> str:
    if not meta:
        return f"Move {move_id}"
    ident = str(meta.get("identifier") or "").strip()
    if not ident or ident == f"move-{move_id}":
        return f"Move {move_id}"
    return ident.replace("-", " ").title()


def _target_is_safe(meta: dict) -> tuple[bool, str]:
    if meta.get("target_code") is not None:
        code = int(meta.get("target_code"))
        label = str(meta.get("target_label") or f"native target {code}")
        if code in SAFE_NATIVE_TARGET_CODES:
            return True, f"single selected opponent ({label})"
        return False, f"unsafe ORAS target class {code} ({label})"
    # Backward compatibility for a veekun-style cache.
    target_id = int(meta.get("target_id") or 0)
    if target_id in SAFE_VEEKUN_TARGET_IDS:
        return True, "single selected opponent"
    return False, f"unsafe target class {target_id}"


def classify_move(move_id: int, pp: int, db: dict[int, dict]) -> dict:
    move_id = int(move_id or 0)
    pp = int(pp or 0)
    meta = db.get(move_id)
    rec = {
        "move_id": move_id,
        "pp": pp,
        "name": move_name(meta, move_id),
        "safe": False,
        "reason": None,
        "meta": meta,
        "control_risk": [],
    }
    if move_id <= 0:
        rec["reason"] = "empty move slot"
        return rec
    if pp <= 0:
        rec["reason"] = "no PP"
        return rec
    if meta is None:
        rec["reason"] = "move metadata unavailable"
        return rec
    if int(meta.get("damage_class_id") or 0) not in (2, 3):
        rec["reason"] = "status/non-damaging move"
        return rec
    target_ok, target_reason = _target_is_safe(meta)
    if not target_ok:
        rec["reason"] = target_reason
        return rec

    risks = []
    if move_id in SELF_LOSS_RISK_MOVES:
        risks.append("recoil/crash/self-loss")
    if move_id in CONTROL_RISK_MOVE_IDS:
        risks.append("two-turn/recharge/switch/control")
    if int(meta.get("recoil_native") or 0) != 0:
        risks.append("recoil")
    rec["control_risk"] = sorted(set(risks))
    rec["safe"] = True
    rec["reason"] = target_reason + ("; control-risk fallback" if rec["control_risk"] else "")
    return rec


def choose_safe_attack(parsed_pk6: dict, db: dict[int, dict], available_move_slots: set[int]) -> dict:
    moves = list(parsed_pk6.get("moves") or [])[:4]
    pp = list(parsed_pk6.get("move_pp") or [])[:4]
    while len(moves) < 4:
        moves.append(0)
    while len(pp) < 4:
        pp.append(0)

    candidates, all_rows = [], []
    available = set(int(x) for x in available_move_slots)
    for index in range(4):
        row = classify_move(moves[index], pp[index], db)
        row["move_slot"] = index + 1
        row["touch_profile_available"] = (index + 1) in available
        all_rows.append(row)
        if row["safe"] and row["touch_profile_available"]:
            candidates.append(row)
    if not candidates:
        return {"selected": None, "moves": all_rows}

    # Prefer clean/reliable attacks. A risky single-target attack is still
    # authorized when it is the only safe-target damaging option, matching the
    # user's "any attacking move except moves that can hit the shiny" policy.
    def score(row):
        meta = row.get("meta") or {}
        risk_penalty = len(row.get("control_risk") or [])
        acc = meta.get("accuracy")
        accuracy = 101 if acc is None else int(acc)
        power = int(meta.get("power") or 1)
        return (-risk_penalty, accuracy, int(row.get("pp") or 0), power, -int(row["move_slot"]))

    selected = max(candidates, key=score)
    return {"selected": dict(selected), "moves": all_rows}
