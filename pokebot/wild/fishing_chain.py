from __future__ import annotations

"""Pure consecutive-fishing chain accounting for Gen 6 ORAS.

This module intentionally owns NO controller input and NO RAM addresses.  The
hardware-proven fishing controller in ``qt_ui.wild_worker`` feeds it resolved
outcomes (hooked encounter, no bite, missed hook, or anchor movement).

Generation 6 consecutive fishing shiny rolls are modeled as:

    1 + 2 * min(chain, 20)

with two additional rolls when the Shiny Charm is present.  ``chain`` here is
the number of consecutive successfully hooked encounters *before* the next
encounter is generated.  The tracker therefore records both the odds used for
the just-hooked encounter and the odds for the next cast.
"""

from dataclasses import dataclass, field
from datetime import datetime

BASE_SHINY_DENOMINATOR = 4096
MAX_CHAIN_BONUS = 20


def shiny_rolls_for_chain(chain: int, shiny_charm: bool = False) -> int:
    chain = max(0, int(chain))
    return 1 + (2 * min(chain, MAX_CHAIN_BONUS)) + (2 if shiny_charm else 0)


def shiny_probability_for_rolls(rolls: int) -> float:
    rolls = max(1, int(rolls))
    miss = (BASE_SHINY_DENOMINATOR - 1) / BASE_SHINY_DENOMINATOR
    return 1.0 - (miss ** rolls)


def shiny_odds_for_chain(chain: int, shiny_charm: bool = False) -> dict:
    rolls = shiny_rolls_for_chain(chain, shiny_charm)
    probability = shiny_probability_for_rolls(rolls)
    return {
        "chain": max(0, int(chain)),
        "rolls": rolls,
        "probability": probability,
        "percent": probability * 100.0,
        "one_in": (1.0 / probability) if probability > 0 else None,
        "capped": int(chain) >= MAX_CHAIN_BONUS,
        "shiny_charm": bool(shiny_charm),
    }


@dataclass
class ConsecutiveFishingTracker:
    shiny_charm: bool = False
    chain: int = 0
    peak_chain: int = 0
    casts: int = 0
    hooked: int = 0
    no_bites: int = 0
    missed_hooks: int = 0
    breaks: int = 0
    anchor_zone: int | None = None
    anchor_grid: tuple[int, int] | None = None
    last_break_reason: str | None = None
    last_event: str = "INIT"
    events: list[dict] = field(default_factory=list)

    def set_shiny_charm(self, detected) -> None:
        self.shiny_charm = detected is True

    def _stamp(self, event: str, **extra) -> dict:
        rec = {
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": str(event),
            "chain": int(self.chain),
            **extra,
        }
        self.last_event = str(event)
        self.events.append(rec)
        if len(self.events) > 100:
            self.events[:] = self.events[-100:]
        return rec

    def observe_anchor(self, zone_id, grid) -> dict | None:
        try:
            zone = int(zone_id)
            point = tuple(int(v) for v in (grid or ())[:2])
        except Exception:
            return None
        if len(point) != 2:
            return None
        if self.anchor_zone is None or self.anchor_grid is None:
            self.anchor_zone = zone
            self.anchor_grid = point
            return self._stamp("ANCHOR_SET", zone=zone, grid=list(point))
        if zone != self.anchor_zone or point != self.anchor_grid:
            old = {"zone": self.anchor_zone, "grid": list(self.anchor_grid)}
            self.break_chain("MOVED_FROM_FISHING_SPOT")
            self.anchor_zone = zone
            self.anchor_grid = point
            return self._stamp(
                "ANCHOR_CHANGED",
                old=old,
                new={"zone": zone, "grid": list(point)},
            )
        return None

    def record_cast(self) -> dict:
        self.casts += 1
        return self._stamp("CAST", cast=self.casts)

    def break_chain(self, reason: str) -> dict:
        previous = int(self.chain)
        if previous > 0:
            self.breaks += 1
        self.chain = 0
        self.last_break_reason = str(reason)
        return self._stamp(
            "CHAIN_BREAK",
            reason=str(reason),
            previous_chain=previous,
            break_count=int(self.breaks),
        )

    def record_no_bite(self) -> dict:
        self.no_bites += 1
        rec = self.break_chain("NO_BITE")
        rec["no_bites"] = int(self.no_bites)
        return rec

    def record_missed_hook(self, reason: str = "MISSED_HOOK") -> dict:
        self.missed_hooks += 1
        rec = self.break_chain(str(reason))
        rec["missed_hooks"] = int(self.missed_hooks)
        return rec

    def record_hooked_encounter(self) -> dict:
        # The encountered Pokémon was generated using the streak that existed
        # before this successful hook.  Increment only after capturing that fact.
        pre_chain = int(self.chain)
        encounter_odds = shiny_odds_for_chain(pre_chain, self.shiny_charm)
        self.hooked += 1
        self.chain += 1
        self.peak_chain = max(self.peak_chain, self.chain)
        next_odds = shiny_odds_for_chain(self.chain, self.shiny_charm)
        return self._stamp(
            "HOOKED_ENCOUNTER",
            pre_hook_chain=pre_chain,
            encounter_odds=encounter_odds,
            next_odds=next_odds,
            hooked=int(self.hooked),
            peak_chain=int(self.peak_chain),
        )

    def snapshot(self) -> dict:
        return {
            "chain": int(self.chain),
            "peak_chain": int(self.peak_chain),
            "casts": int(self.casts),
            "hooked": int(self.hooked),
            "no_bites": int(self.no_bites),
            "missed_hooks": int(self.missed_hooks),
            "breaks": int(self.breaks),
            "last_break_reason": self.last_break_reason,
            "last_event": self.last_event,
            "anchor_zone": self.anchor_zone,
            "anchor_grid": list(self.anchor_grid) if self.anchor_grid else None,
            "next_odds": shiny_odds_for_chain(self.chain, self.shiny_charm),
            "shiny_charm": bool(self.shiny_charm),
        }
