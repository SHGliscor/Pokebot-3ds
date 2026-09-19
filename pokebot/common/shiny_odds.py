from __future__ import annotations

from dataclasses import dataclass
import math
import struct

BASE_SHINY_DENOMINATOR = 4096

# ORAS 1.4 live Key Items pocket. Alpha Sapphire was hardware-proven first;
# Omega Ruby shares the same live save-block/bag layout used by the unified
# ORAS RAM profile.
ORAS_14_KEY_ITEMS_ADDR = 0x08C6F2B0
ORAS_AS14_KEY_ITEMS_ADDR = ORAS_14_KEY_ITEMS_ADDR
ORAS_KEY_ITEMS_SIZE = 0x180
ORAS_BAG_ENTRY_SIZE = 4
SHINY_CHARM_ITEM_ID = 632


def probability_for_rolls(rolls: int, denominator: int = BASE_SHINY_DENOMINATOR) -> float:
    """Exact probability that at least one of ``rolls`` independent PID rolls is shiny."""
    rolls = max(1, int(rolls))
    denominator = max(2, int(denominator))
    miss = (denominator - 1.0) / denominator
    return 1.0 - (miss ** rolls)


def phase_log_miss_for_constant(seen: int, encounter_probability: float) -> float:
    seen = max(0, int(seen))
    p = min(max(float(encounter_probability), 0.0), 1.0)
    if seen <= 0 or p <= 0.0:
        return 0.0
    if p >= 1.0:
        return float('-inf')
    return float(seen) * math.log1p(-p)


def advance_phase_log_miss(current_log_miss, encounter_probability: float) -> float:
    """Multiply the phase's no-shiny probability by one encounter's miss chance.

    Log space keeps long phases stable and also supports variable odds such as
    consecutive fishing, where every encounter can have a different roll count.
    """
    try:
        current = float(current_log_miss)
    except (TypeError, ValueError):
        current = 0.0
    p = min(max(float(encounter_probability), 0.0), 1.0)
    if p <= 0.0:
        return current
    if p >= 1.0:
        return float('-inf')
    return current + math.log1p(-p)


def cumulative_probability_from_log_miss(log_miss) -> float:
    try:
        value = float(log_miss)
    except (TypeError, ValueError):
        return 0.0
    if math.isinf(value) and value < 0:
        # Only a truly certain per-encounter event reaches mathematical 1.0.
        return 1.0
    value = min(0.0, value)
    # -expm1(x) is accurate when x is very close to zero.  For a finite hunt,
    # floating point can still round an extremely tiny miss chance to exactly
    # 1.0. Keep finite phases infinitesimally below certainty.
    out = min(1.0, max(0.0, -math.expm1(value)))
    if out >= 1.0:
        return math.nextafter(1.0, 0.0)
    return out


def cumulative_probability(seen: int, encounter_probability: float) -> float:
    return cumulative_probability_from_log_miss(
        phase_log_miss_for_constant(seen, encounter_probability)
    )


def format_cumulative_percent(probability: float) -> str:
    """Never round a finite phase to a misleading displayed 100.00%."""
    p = min(1.0, max(0.0, float(probability or 0.0)))
    pct = p * 100.0
    if pct >= 99.995:
        return '99.99+%'
    return f'{pct:.2f}%'


def phase_progress_bar_value(probability: float) -> int:
    """0..9999 for finite probabilities so the bar never visually implies certainty."""
    p = min(1.0, max(0.0, float(probability or 0.0)))
    # A shiny hunt phase is never logically guaranteed by a finite number of
    # independent rolls, so telemetry never paints a completely full bar.
    return min(9999, max(0, int(round(p * 10000.0))))


def additional_encounters_to_probability(
    current_log_miss, encounter_probability: float, target_probability: float = 1.0 - math.e**-1
) -> int | None:
    """Additional constant-odds encounters needed to reach a cumulative target.

    The default target is 1-e^-1 (~63.21%), the conventional one-expected-
    interval cumulative probability. For changing methods such as fishing,
    callers should label this as an estimate using the *next* encounter odds.
    """
    p = min(max(float(encounter_probability), 0.0), 1.0)
    target = min(max(float(target_probability), 0.0), 1.0)
    if p <= 0.0 or target <= 0.0:
        return None
    if p >= 1.0:
        return 0
    current_prob = cumulative_probability_from_log_miss(current_log_miss)
    if current_prob >= target:
        return 0
    try:
        current = float(current_log_miss)
    except (TypeError, ValueError):
        current = 0.0
    target_log_miss = math.log1p(-target)
    per_miss = math.log1p(-p)
    remaining = (target_log_miss - current) / per_miss
    return max(0, int(math.ceil(remaining - 1e-12)))


@dataclass(frozen=True)
class ShinyOdds:
    numerator: int
    denominator: int
    shiny_charm_present: bool | None
    shiny_charm_applies: bool
    method: str

    @property
    def rolls(self) -> int:
        return max(1, int(self.numerator))

    @property
    def probability(self) -> float:
        return probability_for_rolls(self.rolls, self.denominator)

    @property
    def one_in(self) -> float:
        return 1.0 / self.probability

    @property
    def display(self) -> str:
        if self.rolls == 1:
            return f'1/{self.denominator:,}'
        return f'~1/{self.one_in:,.2f}'

    def phase_progress_percent(self, seen: int) -> float:
        # Phase progress means the cumulative probability of at least one shiny
        # having occurred by now, NOT percent of one expected interval.
        return cumulative_probability(seen, self.probability) * 100.0

    def phase_probability(self, seen: int) -> float:
        return cumulative_probability(seen, self.probability)


def resolve_shiny_odds(
    game: str,
    hunt_type: str,
    shiny_charm_present: bool | None = None,
    shiny_charm_applies: bool | None = None,
) -> ShinyOdds:
    """Resolve ORAS encounter odds for shared telemetry.

    Starters, in-game gifts and fossil revivals are full odds in Gen VI and
    ignore the Shiny Charm. Standard wild and static encounters can use the
    Charm's two extra rerolls. Consecutive fishing supplies its own dynamic
    roll count through ``pokebot.wild.fishing_chain``.
    """
    hunt_key = str(hunt_type or '').strip().casefold()
    no_charm = {
        'starter', 'starters', 'starter hunt',
        'gift', 'gifts', 'fossil', 'fossils', 'fossil batch',
    }
    if hunt_key in no_charm:
        return ShinyOdds(
            numerator=1, denominator=BASE_SHINY_DENOMINATOR,
            shiny_charm_present=shiny_charm_present,
            shiny_charm_applies=False, method=hunt_type or 'Encounter',
        )

    applies = bool(shiny_charm_applies)
    rolls = 3 if applies and shiny_charm_present is True else 1
    return ShinyOdds(
        numerator=rolls, denominator=BASE_SHINY_DENOMINATOR,
        shiny_charm_present=shiny_charm_present,
        shiny_charm_applies=applies, method=hunt_type or 'Encounter',
    )


def detect_oras_shiny_charm(bridge, game_key: str | None = None) -> dict:
    """Read the shared ORAS 1.4 Key Items pocket once.

    Returns a structured, read-only authority result:
      detected=True  -> Shiny Charm item 632 present with count > 0
      detected=False -> pocket read succeeded and Charm was not present
      detected=None  -> RAM read/parse failed; callers must not guess odds

    The function performs exactly one 0x180-byte RAM read. It is read-only and
    returns the resolved game key for support evidence.
    """
    try:
        raw = bridge.read(ORAS_14_KEY_ITEMS_ADDR, ORAS_KEY_ITEMS_SIZE)
        if len(raw) != ORAS_KEY_ITEMS_SIZE:
            return {
                "detected": None,
                "status": "READ_SIZE_MISMATCH",
                "address": f"0x{ORAS_14_KEY_ITEMS_ADDR:08X}",
                "read_size": len(raw),
                "expected_size": ORAS_KEY_ITEMS_SIZE,
                "game_key": str(game_key or "oras"),
            }

        nonzero = []
        hits = []
        for off in range(0, ORAS_KEY_ITEMS_SIZE, ORAS_BAG_ENTRY_SIZE):
            item_id, count = struct.unpack_from("<HH", raw, off)
            if item_id or count:
                entry = {
                    "offset": off,
                    "address": f"0x{ORAS_14_KEY_ITEMS_ADDR + off:08X}",
                    "item_id": item_id,
                    "item_id_hex": f"0x{item_id:04X}",
                    "count": count,
                }
                nonzero.append(entry)
                if item_id == SHINY_CHARM_ITEM_ID and count > 0:
                    hits.append(entry)

        present = bool(hits)
        return {
            "detected": present,
            "status": (
                "SHINY_CHARM_PRESENT"
                if present
                else "SHINY_CHARM_NOT_PRESENT"
            ),
            "address": f"0x{ORAS_14_KEY_ITEMS_ADDR:08X}",
            "read_size": ORAS_KEY_ITEMS_SIZE,
            "game_key": str(game_key or "oras"),
            "layout": "shared_oras_1.4_key_items",
            "item_id": SHINY_CHARM_ITEM_ID,
            "item_id_hex": f"0x{SHINY_CHARM_ITEM_ID:04X}",
            "hits": hits,
            "nonzero_entry_count": len(nonzero),
            "ram_write": False,
        }
    except Exception as exc:
        return {
            "detected": None,
            "status": "READ_FAILED",
            "address": f"0x{ORAS_14_KEY_ITEMS_ADDR:08X}",
            "read_size": ORAS_KEY_ITEMS_SIZE,
            "game_key": str(game_key or "oras"),
            "layout": "shared_oras_1.4_key_items",
            "item_id": SHINY_CHARM_ITEM_ID,
            "item_id_hex": f"0x{SHINY_CHARM_ITEM_ID:04X}",
            "error": f"{type(exc).__name__}: {exc}",
            "ram_write": False,
        }
