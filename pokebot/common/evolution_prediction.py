from __future__ import annotations

WURMPLE_SPECIES_ID = 265


def _u32(value) -> int | None:
    if isinstance(value, int):
        return value & 0xFFFFFFFF
    text = str(value or "").strip()
    if not text or text == "—":
        return None
    try:
        return int(text, 0) & 0xFFFFFFFF
    except Exception:
        return None


def predict_split_evolution(species: int, encryption_constant) -> dict | None:
    """Return deterministic Gen VI split-evolution metadata when PK6 data proves it.

    Wurmple is the relevant ORAS personality split. In Generation VI its
    evolution uses the Encryption Constant (EC), not PID: take the upper 16
    bits, modulo 10; 0-4 -> Silcoon -> Beautifly, 5-9 -> Cascoon -> Dustox.
    """
    try:
        species = int(species)
    except Exception:
        return None
    if species != WURMPLE_SPECIES_ID:
        return None

    ec = _u32(encryption_constant)
    if ec is None:
        return None
    upper16 = (ec >> 16) & 0xFFFF
    selector = upper16 % 10
    if selector <= 4:
        first, final = "Silcoon", "Beautifly"
    else:
        first, final = "Cascoon", "Dustox"
    return {
        "kind": "WURMPLE_GEN6_EC",
        "authority": "Gen VI Encryption Constant upper16 % 10",
        "ec": f"0x{ec:08X}",
        "upper16": upper16,
        "selector": selector,
        "first_evolution": first,
        "final_evolution": final,
        "line": f"{first} → {final}",
    }
