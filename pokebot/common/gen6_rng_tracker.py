from __future__ import annotations

"""Passive Generation 6 main-RNG telemetry.

Read-only implementation based on PokeReader's Gen-6 RAM layout/state tracking
and 3DSRNGTool's run-time MT19937 step.  Nothing in this module writes game
memory, sends input, delays a hunt, or exposes a control decision to hunt code.

``frames_away`` is the number of future *main MT outputs* to the next raw u32
whose Gen-6 PSV equals the active trainer TSV.  It is deliberately a raw shiny
PID-frame indicator: different encounter methods may consume the main RNG in
different ways before using a value as PID.
"""

from dataclasses import dataclass
import struct
import threading

# PokeReader reader_core/src/gen6/reader.rs.
GEN6_RNG_LAYOUTS = {
    "oras": {
        "initial_seed": 0x08C59E40,
        "mt_state_index": 0x08C59E44,
        "mt_start": 0x08C59E48,
        "tidsid": 0x08C81340,
    },
    "xy": {
        "initial_seed": 0x08C52844,
        "mt_state_index": 0x08C52848,
        "mt_start": 0x08C5284C,
        "tidsid": 0x08C79C3C,
    },
}

MT_N = 624
MT_M = 397
MT_MATRIX_A = 0x9908B0DF
MT_UPPER_MASK = 0x80000000
MT_LOWER_MASK = 0x7FFFFFFF


def gen6_tsv(tid: int, sid: int) -> int:
    return ((int(tid) ^ int(sid)) >> 4) & 0x0FFF


def gen6_psv(pid: int) -> int:
    pid = int(pid) & 0xFFFFFFFF
    return (((pid >> 16) ^ (pid & 0xFFFF)) >> 4) & 0x0FFF


def gen6_shiny_xor(tid: int, sid: int, pid: int) -> int:
    pid = int(pid) & 0xFFFFFFFF
    return (int(tid) ^ int(sid) ^ (pid & 0xFFFF) ^ ((pid >> 16) & 0xFFFF)) & 0xFFFF


def _temper(y: int) -> int:
    y &= 0xFFFFFFFF
    y ^= y >> 11
    y ^= (y << 7) & 0x9D2C5680
    y ^= (y << 15) & 0xEFC60000
    y ^= y >> 18
    return y & 0xFFFFFFFF


@dataclass
class LiveMT:
    """3DSRNGTool-compatible run-time MT state."""

    state: list[int]
    index: int

    def __post_init__(self) -> None:
        if len(self.state) != MT_N:
            raise ValueError(f"MT state must contain {MT_N} words")
        if not (0 <= int(self.index) < MT_N):
            raise ValueError(f"unsupported MT state index {self.index}")
        self.state = [int(v) & 0xFFFFFFFF for v in self.state]
        self.index = int(self.index)

    @classmethod
    def from_seed(cls, seed: int) -> "LiveMT":
        mt = [0] * MT_N
        mt[0] = int(seed) & 0xFFFFFFFF
        for i in range(1, MT_N):
            prev = mt[i - 1]
            mt[i] = (1812433253 * (prev ^ (prev >> 30)) + i) & 0xFFFFFFFF
        return cls(mt, 0)

    def clone(self) -> "LiveMT":
        return LiveMT(self.state.copy(), self.index)

    def current_raw_state(self) -> int:
        return self.state[self.index] & 0xFFFFFFFF

    def next_uint(self) -> int:
        i = self.index
        kk = i + 1 if i < MT_N - 1 else 0
        jj = i + MT_M if i < MT_N - MT_M else i + MT_M - MT_N
        y = (self.state[i] & MT_UPPER_MASK) | (self.state[kk] & MT_LOWER_MASK)
        self.state[i] = (
            self.state[jj]
            ^ (y >> 1)
            ^ (MT_MATRIX_A if (y & 1) else 0)
        ) & 0xFFFFFFFF
        result = _temper(self.state[i])
        self.index = kk
        return result


def _read_mt_state_words(bridge, mt_start: int) -> list[int]:
    """Read the 624-word Gen 6 MT table using bridge-safe <=0x200 chunks."""
    total = MT_N * 4
    data = bytearray()
    offset = 0
    while offset < total:
        size = min(0x200, total - offset)
        chunk = bridge.read(int(mt_start) + offset, size)
        if len(chunk) != size:
            raise RuntimeError(
                f"short MT read at +0x{offset:X}: expected {size}, got {len(chunk)}"
            )
        data.extend(chunk)
        offset += size
    return list(struct.unpack("<624I", bytes(data)))


def _normalize_mt_index(raw_index: int) -> int:
    """Normalize the game's special 624 boundary to the runtime slot 0."""
    raw_index = int(raw_index)
    if raw_index == MT_N:
        return 0
    if not (0 <= raw_index < MT_N):
        raise ValueError(f"invalid MT index {raw_index}")
    return raw_index


def _forward_mt_distance(start: int, end: int) -> int:
    """Forward distance on the 624-slot run-time MT ring."""
    return (_normalize_mt_index(end) - _normalize_mt_index(start)) % MT_N


def _read_word_arc(bridge, mt_start: int, start_word: int, count: int) -> list[int]:
    """Read ``count`` consecutive MT words on the circular 624-word table.

    The repair arc is normally tiny (the number of frames elapsed while the
    five large table reads crossed UDP), but this helper is fully bounded and
    handles wraparound.
    """
    start_word = int(start_word) % MT_N
    count = int(count)
    if count <= 0:
        return []
    if count > MT_N:
        raise ValueError("MT repair arc exceeds one full period")

    out: list[int] = []
    cursor = start_word
    remaining = count
    while remaining:
        contiguous = min(remaining, MT_N - cursor)
        # Keep every firmware READ under the existing read-only 0x200 contract.
        while contiguous:
            words_now = min(contiguous, 0x200 // 4)
            raw = bridge.read(int(mt_start) + cursor * 4, words_now * 4)
            if len(raw) != words_now * 4:
                raise RuntimeError(
                    f"short MT repair read at word {cursor}: "
                    f"expected {words_now * 4}, got {len(raw)}"
                )
            out.extend(struct.unpack(f"<{words_now}I", raw))
            cursor = (cursor + words_now) % MT_N
            remaining -= words_now
            contiguous -= words_now
    return out


def read_live_main_rng_snapshot(bridge, family: str = "oras", retries: int = 4) -> dict:
    """Reconstruct an exact live MT snapshot without requiring the RNG to stop.

    HF80 tried to demand that the 16-bit MT index remain unchanged while all
    2,496 bytes of MT state crossed UDP.  On real hardware the main RNG keeps
    moving, so that condition can fail forever (for example index 49 -> 58).

    HF81 uses a rolling-repair snapshot instead:

    1. Read the starting MT index.
    2. Read all 624 MT words in the normal bounded 0x200 chunks.
    3. Read the MT index again and designate *that* instant as snapshot time.
    4. Every slot the game advanced through between the two index samples is
       re-read.  Those slots have already been updated and will not change
       again until a complete 624-frame lap, so the repaired table is exactly
       the table that existed at the designated snapshot index.

    This preserves the firmware's existing read-only RAM contract and needs no
    game pause, RAM write, PokeReader seed hook, or new boot.firm command.
    """
    family = str(family or "oras").lower()
    if family not in GEN6_RNG_LAYOUTS:
        raise ValueError(f"unsupported Gen 6 family {family!r}")
    layout = GEN6_RNG_LAYOUTS[family]

    last_reason = "live MT snapshot unavailable"
    for _attempt in range(max(1, int(retries))):
        idx_raw = bridge.read(layout["mt_state_index"], 2)
        if len(idx_raw) != 2:
            raise RuntimeError("short MT index read")
        index_before_raw = struct.unpack("<H", idx_raw)[0]
        try:
            index_before = _normalize_mt_index(index_before_raw)
        except ValueError:
            last_reason = f"invalid MT index {index_before_raw}"
            continue

        state = _read_mt_state_words(bridge, layout["mt_start"])

        idx_raw = bridge.read(layout["mt_state_index"], 2)
        if len(idx_raw) != 2:
            raise RuntimeError("short MT index confirmation read")
        index_snapshot_raw = struct.unpack("<H", idx_raw)[0]
        try:
            index_snapshot = _normalize_mt_index(index_snapshot_raw)
        except ValueError:
            last_reason = f"invalid MT index {index_snapshot_raw}"
            continue

        advanced = _forward_mt_distance(index_before_raw, index_snapshot_raw)

        # Five UDP state reads should normally cost only a handful of MT frames.
        # If we somehow crossed a very large part of the ring, retry instead of
        # accepting the modulo distance as proof that only one lap occurred.
        if advanced > 192:
            last_reason = (
                "MT advanced too far during snapshot; retrying "
                f"({index_before_raw} -> {index_snapshot_raw}, +{advanced})"
            )
            continue

        if advanced:
            repaired = _read_word_arc(
                bridge,
                layout["mt_start"],
                index_before,
                advanced,
            )
            for offset, value in enumerate(repaired):
                state[(index_before + offset) % MT_N] = int(value) & 0xFFFFFFFF

        ids = bridge.read(layout["tidsid"], 4)
        if len(ids) != 4:
            raise RuntimeError("short TID/SID read")
        tid, sid = struct.unpack("<HH", ids)

        return {
            "available": True,
            "family": family,
            "index": int(index_snapshot_raw),
            "normalized_index": int(index_snapshot),
            "state": state,
            "tid": int(tid),
            "sid": int(sid),
            "tsv": gen6_tsv(tid, sid),
            "snapshot_advanced": int(advanced),
            "snapshot_authority": "rolling 624-word MT RAM snapshot + repaired advanced arc",
        }

    return {
        "available": False,
        "family": family,
        "reason": last_reason,
    }

def find_next_raw_shiny_frame(snapshot: dict, max_advances: int = 250_000) -> dict:
    if not snapshot.get("available"):
        return dict(snapshot)

    state = snapshot.get("state")
    if not isinstance(state, (list, tuple)) or len(state) != MT_N:
        out = dict(snapshot)
        out["available"] = False
        out["reason"] = "live MT table unavailable"
        return out

    rng = LiveMT(list(state), int(snapshot.get("normalized_index", 0)))
    tid = int(snapshot["tid"])
    sid = int(snapshot["sid"])
    tsv = int(snapshot["tsv"])

    for distance in range(1, int(max_advances) + 1):
        pid = rng.next_uint()
        if gen6_psv(pid) == tsv:
            return {
                "available": True,
                "family": snapshot.get("family", "oras"),
                "current_index": int(snapshot.get("index", 0)),
                "next_index": int(rng.index),
                "tid": tid,
                "sid": sid,
                "tsv": tsv,
                "frames_away": distance,
                "pid": pid,
                "pid_hex": f"0x{pid:08X}",
                "shiny_xor": gen6_shiny_xor(tid, sid, pid),
                "search_limit": int(max_advances),
                "authority": "live MT RAM snapshot + 3DSRNGTool MT/Gen6 PSV",
                "mode": "passive_live_mt_pid_frame",
            }

    return {
        "available": False,
        "reason": f"no shiny PID frame within +{int(max_advances):,} MT outputs",
        "family": snapshot.get("family", "oras"),
        "current_index": int(snapshot.get("index", 0)),
        "tid": tid,
        "sid": sid,
        "tsv": tsv,
    }


def read_nearest_shiny_frame(
    bridge,
    family: str = "oras",
    max_advances: int = 250_000,
    scan_budget: int = 1_000_000,  # retained for worker API compatibility
) -> dict:
    del scan_budget
    snapshot = read_live_main_rng_snapshot(bridge, family=family)
    return find_next_raw_shiny_frame(snapshot, max_advances=max_advances)



def find_locked_raw_pid_frame(snapshot: dict, target_pid: int, max_advances: int) -> dict | None:
    """Return the live distance to one already-locked raw MT PID output.

    This is the key to a real countdown.  Rather than asking "what is the
    nearest shiny from this new snapshot?" on every UI refresh, HF82 keeps the
    exact raw PID that was previously selected and searches only for that value
    ahead of the new live snapshot.  If it is still ahead, its distance can
    only decrease.  If it is no longer ahead, the target frame was passed (or
    the game reset to a different stream) and the caller can acquire a new
    future shiny target.
    """
    if not snapshot.get("available"):
        return None
    state = snapshot.get("state")
    if not isinstance(state, (list, tuple)) or len(state) != MT_N:
        return None

    target_pid = int(target_pid) & 0xFFFFFFFF
    rng = LiveMT(list(state), int(snapshot.get("normalized_index", 0)))
    for distance in range(1, max(1, int(max_advances)) + 1):
        pid = rng.next_uint()
        if pid == target_pid:
            tid = int(snapshot["tid"])
            sid = int(snapshot["sid"])
            return {
                "available": True,
                "family": snapshot.get("family", "oras"),
                "current_index": int(snapshot.get("index", 0)),
                "next_index": int(rng.index),
                "tid": tid,
                "sid": sid,
                "tsv": int(snapshot["tsv"]),
                "frames_away": int(distance),
                "pid": target_pid,
                "pid_hex": f"0x{target_pid:08X}",
                "shiny_xor": gen6_shiny_xor(tid, sid, target_pid),
                "target_status": "tracking",
                "target_locked": True,
                "authority": "locked raw shiny PID + rolling live MT snapshot",
                "mode": "passive_locked_live_mt_pid_frame",
            }
    return None


def read_locked_shiny_countdown(
    bridge,
    family: str = "oras",
    locked_pid: int | None = None,
    previous_distance: int | None = None,
    max_advances: int = 250_000,
) -> dict:
    """Read one live countdown sample while preserving a selected shiny frame.

    A previously selected PID remains the target as long as that exact MT
    output is still ahead of the current snapshot.  Once it has passed, or a
    reset moves the game onto a different stream, a new nearest future shiny
    PID is acquired.  No controller input or game-memory write is performed.
    """
    snapshot = read_live_main_rng_snapshot(bridge, family=family)
    if not snapshot.get("available"):
        return dict(snapshot)

    if locked_pid is not None and previous_distance is not None:
        # A genuine future target can never get farther away as time advances.
        # Restricting the search to the previous distance also makes a reset
        # reject the stale target immediately instead of scanning 250k outputs.
        search_limit = max(1, min(int(max_advances), int(previous_distance)))
        tracked = find_locked_raw_pid_frame(snapshot, int(locked_pid), search_limit)
        if tracked is not None:
            tracked["previous_distance"] = int(previous_distance)
            tracked["countdown_delta"] = int(previous_distance) - int(tracked["frames_away"])
            return tracked

        # The target is no longer in the future: it was consumed/skipped, or
        # the title was reset and the MT stream changed.  Acquire the next one.
        fresh = find_next_raw_shiny_frame(snapshot, max_advances=max_advances)
        if fresh.get("available"):
            fresh["target_status"] = "passed_reacquired"
            fresh["target_locked"] = True
            fresh["previous_target_pid"] = int(locked_pid) & 0xFFFFFFFF
            fresh["previous_target_pid_hex"] = f"0x{int(locked_pid) & 0xFFFFFFFF:08X}"
        return fresh

    fresh = find_next_raw_shiny_frame(snapshot, max_advances=max_advances)
    if fresh.get("available"):
        fresh["target_status"] = "acquired"
        fresh["target_locked"] = True
    return fresh



def read_live_mt_cursor(bridge, family: str = "oras") -> dict:
    """Read the live Gen-6 MT cursor from one contiguous RAM sample.

    3DSRNGTool reads eight bytes beginning at the MT index address: the
    16-bit cursor plus the first raw MT word four bytes later.  That first
    word is a period anchor: it stays constant while the cursor walks around
    most of the 624-word ring, and changes when slot 0 is generated.

    Reading both values in one bridge READ avoids HF83's impossible
    requirement that two separate index reads agree while ORAS is actively
    advancing the RNG.  The local MT clone can be advanced until both the
    cursor and period anchor match this atomic sample.
    """
    family = str(family or "oras").lower()
    if family not in GEN6_RNG_LAYOUTS:
        raise ValueError(f"unsupported Gen 6 family {family!r}")
    layout = GEN6_RNG_LAYOUTS[family]

    raw = bridge.read(layout["mt_state_index"], 8)
    if len(raw) != 8:
        raise RuntimeError("short MT cursor/anchor read")
    idx_raw = struct.unpack_from("<H", raw, 0)[0]
    try:
        idx = _normalize_mt_index(idx_raw)
    except ValueError:
        return {
            "available": False,
            "family": family,
            "reason": f"invalid MT cursor index {idx_raw}",
        }

    anchor = struct.unpack_from("<I", raw, 4)[0]

    ids = bridge.read(layout["tidsid"], 4)
    if len(ids) != 4:
        raise RuntimeError("short TID/SID cursor read")
    tid, sid = struct.unpack("<HH", ids)
    return {
        "available": True,
        "family": family,
        "index": int(idx_raw),
        "normalized_index": int(idx),
        "anchor_state": int(anchor) & 0xFFFFFFFF,
        "tid": int(tid),
        "sid": int(sid),
        "tsv": gen6_tsv(tid, sid),
        "cursor_authority": "single 8-byte MT index + period-anchor RAM sample",
    }

def _tracking_state_from_snapshot(snapshot: dict, result: dict) -> dict:
    return {
        "family": str(snapshot.get("family") or "oras"),
        "state": list(snapshot.get("state") or []),
        "index": int(snapshot.get("normalized_index", 0)),
        "remaining": max(1, int(result.get("frames_away", 1))),
        "pid": int(result.get("pid", 0)) & 0xFFFFFFFF,
        "tid": int(snapshot.get("tid", 0)),
        "sid": int(snapshot.get("sid", 0)),
        "tsv": int(snapshot.get("tsv", 0)),
    }


def acquire_locked_shiny_target(bridge, family: str = "oras", max_advances: int = 250_000) -> dict:
    """Acquire one future raw shiny PID and the exact live MT state it was based on."""
    snapshot = read_live_main_rng_snapshot(bridge, family=family)
    result = find_next_raw_shiny_frame(snapshot, max_advances=max_advances)
    if result.get("available"):
        result["target_status"] = "acquired"
        result["target_locked"] = True
        result["tracking_state"] = _tracking_state_from_snapshot(snapshot, result)
        result["countdown_delta"] = 0
        result["cursor_mode"] = "full_snapshot_acquire"
    return result


def read_live_locked_shiny_countdown(
    bridge,
    family: str = "oras",
    tracking_state: dict | None = None,
    max_advances: int = 250_000,
    max_catchup: int = 4096,
) -> dict:
    """HF83 low-cost live shiny-frame countdown.

    A full 624-word MT snapshot is needed only when acquiring/reacquiring a
    target.  Normal refreshes read a coherent 10-byte-ish cursor sample and
    advance the saved local MT clone until it matches the live index+raw word.
    This makes NEXT SHINY visibly decay while avoiding five large UDP reads on
    every UI tick.

    If the local stream cannot match the live cursor (soft reset/reseed, long
    pause, or target passed), the old target is discarded and a fresh target
    is acquired from a new full snapshot.  This remains read-only/passive.
    """
    family = str(family or "oras").lower()
    ts = dict(tracking_state or {})
    state = ts.get("state")
    usable = (
        ts.get("family") == family
        and isinstance(state, (list, tuple))
        and len(state) == MT_N
        and 0 <= int(ts.get("index", -1)) < MT_N
        and int(ts.get("remaining", 0)) > 0
        and int(ts.get("pid", 0)) != 0
    )
    if not usable:
        return acquire_locked_shiny_target(bridge, family=family, max_advances=max_advances)

    cursor = read_live_mt_cursor(bridge, family=family)
    if not cursor.get("available"):
        return dict(cursor)

    # Trainer identity changing means this is not the same shiny target.
    if int(cursor.get("tid", -1)) != int(ts.get("tid", -2)) or int(cursor.get("sid", -1)) != int(ts.get("sid", -2)):
        fresh = acquire_locked_shiny_target(bridge, family=family, max_advances=max_advances)
        if fresh.get("available"):
            fresh["target_status"] = "reset_reacquired"
        return fresh

    rng = LiveMT(list(state), int(ts["index"]))
    live_idx = int(cursor["normalized_index"])
    live_anchor = int(cursor["anchor_state"]) & 0xFFFFFFFF

    advanced = None
    # Check zero first: when the RNG genuinely has not advanced, the displayed
    # distance should remain unchanged rather than forcing a reacquire.
    for steps in range(0, max(1, int(max_catchup)) + 1):
        if rng.index == live_idx and (rng.state[0] & 0xFFFFFFFF) == live_anchor:
            advanced = steps
            break
        rng.next_uint()

    if advanced is None:
        # Usually a soft reset/reseed. A fresh full snapshot is the only safe
        # way to establish the new stream.
        fresh = acquire_locked_shiny_target(bridge, family=family, max_advances=max_advances)
        if fresh.get("available"):
            fresh["target_status"] = "reset_reacquired"
            fresh["previous_target_pid"] = int(ts.get("pid", 0)) & 0xFFFFFFFF
        return fresh

    previous_remaining = int(ts["remaining"])
    remaining = previous_remaining - int(advanced)

    if remaining <= 0:
        # The locked shiny output was consumed between samples. Whether the
        # actual encounter used it or not, it is no longer a future frame.
        fresh = acquire_locked_shiny_target(bridge, family=family, max_advances=max_advances)
        if fresh.get("available"):
            fresh["target_status"] = "passed_reacquired"
            fresh["previous_target_pid"] = int(ts.get("pid", 0)) & 0xFFFFFFFF
            fresh["previous_target_pid_hex"] = f"0x{int(ts.get('pid', 0)) & 0xFFFFFFFF:08X}"
            fresh["countdown_delta"] = int(advanced)
        return fresh

    pid = int(ts["pid"]) & 0xFFFFFFFF
    tid = int(cursor["tid"])
    sid = int(cursor["sid"])
    new_tracking = {
        "family": family,
        "state": rng.state.copy(),
        "index": int(rng.index),
        "remaining": int(remaining),
        "pid": pid,
        "tid": tid,
        "sid": sid,
        "tsv": int(cursor["tsv"]),
    }
    return {
        "available": True,
        "family": family,
        "current_index": int(cursor["index"]),
        "tid": tid,
        "sid": sid,
        "tsv": int(cursor["tsv"]),
        "frames_away": int(remaining),
        "pid": pid,
        "pid_hex": f"0x{pid:08X}",
        "shiny_xor": gen6_shiny_xor(tid, sid, pid),
        "target_status": "tracking",
        "target_locked": True,
        "previous_distance": int(previous_remaining),
        "countdown_delta": int(advanced),
        "tracking_state": new_tracking,
        "authority": "locked raw shiny PID + atomic MT cursor/period anchor",
        "mode": "passive_locked_live_mt_cursor",
        "cursor_mode": "atomic_cursor_anchor",
    }
