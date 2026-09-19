from __future__ import annotations

import json
import math
import struct
import sys
import time
import traceback
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pokebot.common.framebuffer import SCREEN_BOTTOM, SCREEN_TOP_LEFT, capture_screen
from pokebot.common.oras_profiles import (
    BATTLE_STATE,
    BATTLE_INACTIVE,
    PRIMARY_BASE,
    SECONDARY_BASE,
    ZONE_ADDR,
    profile_from_game_info,
)
from pokebot.wild.validated_loader import load_walk_v0p23
from qt_ui.appdata_store import get_profile_paths
from qt_ui.settings_store import load_settings

PROBE_NAME = "Pokebot3DS-CFW DexNav Overworld Target Probe v0p1"
EXPECTED_SPECIES = 261
BATTLE_ACTIVE = 0x00040001

# The entire ORAS field-object/DexNav arena that already contains the proven
# player object, duplicate player coordinates, DexNav chain/step globals and
# field root.  One snapshot is intentionally broad: target-object discovery is
# the purpose of this probe and guessing a tiny address window defeats it.
SCAN_RANGES = (
    ("field_arena", 0x08D00000, 0x00100000),   # 1 MiB
    ("field_globals", 0x08C60000, 0x00030000), # 192 KiB
)
CHUNK = 0x200
BLOCK = 0x40
MAX_WATCH_CANDIDATE_BLOCKS = 32
WATCH_PERIOD_S = 0.35
WATCH_TIMEOUT_S = 45.0


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def u32(raw: bytes, off: int = 0) -> int:
    return struct.unpack_from("<I", raw, off)[0]


def f32(raw: bytes, off: int = 0) -> float:
    return struct.unpack_from("<f", raw, off)[0]


def safe_u32(br, address: int):
    try:
        return u32(br.read(address, 4))
    except Exception:
        return None


def read_sparse_chunks(br, address: int, length: int, progress=None):
    """Read a broad RAM arena without dying on an isolated unmapped page.

    Failed chunks are filled with 0xCC and recorded explicitly.  Using the
    same fixed-width image at every stage keeps addresses directly comparable.
    """
    out = bytearray()
    failures = []
    total = (length + CHUNK - 1) // CHUNK
    for idx, off in enumerate(range(0, length, CHUNK), 1):
        take = min(CHUNK, length - off)
        try:
            out.extend(br.read(address + off, take))
        except Exception as exc:
            out.extend(b"\xCC" * take)
            failures.append({
                "address": f"0x{address + off:08X}",
                "length": take,
                "error": f"{type(exc).__name__}: {exc}",
            })
        if progress and (idx == 1 or idx == total or idx % 256 == 0):
            progress(idx, total)
    return bytes(out), failures


def read_field(br) -> dict:
    battle = safe_u32(br, BATTLE_STATE)
    zone_word = safe_u32(br, ZONE_ADDR)

    def pos(base):
        try:
            raw = br.read(base, 12)
            x, z = f32(raw, 0), f32(raw, 8)
            if not (math.isfinite(x) and math.isfinite(z)):
                return None
            return [round(float(x), 5), round(float(z), 5)]
        except Exception:
            return None

    return {
        "battle": None if battle is None else f"0x{battle:08X}",
        "battle_raw": battle,
        "zone": None if zone_word is None else int(zone_word & 0xFFFF),
        "world_primary": pos(PRIMARY_BASE),
        "world_secondary": pos(SECONDARY_BASE),
    }


def safe_capture(host: str, port: int, timeout: float, path: Path, selector: int):
    try:
        return capture_screen(host, path, selector=selector, port=port, timeout=timeout)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "path": str(path)}


def plausible_world(v: float) -> bool:
    return math.isfinite(v) and 500.0 <= abs(v) <= 10000.0


def route101_targetish(x: float, z: float) -> bool:
    # This is a ranking hint only, never an authority check.  The manually
    # proven collision area is around x~1711/z~2334 and the target entity can
    # be somewhat beyond the collision point.
    return 1600.0 <= x <= 1850.0 and 2200.0 <= z <= 2475.0


def iter_float_pairs(raw: bytes):
    # ORAS player position objects use X and Z eight bytes apart; also inspect
    # adjacent pairs because the hidden target object may use another struct.
    for stride in (4, 8, 12, 16):
        for off in range(0, len(raw) - stride - 3, 4):
            try:
                a = f32(raw, off)
                b = f32(raw, off + stride)
            except struct.error:
                continue
            if plausible_world(a) and plausible_world(b):
                yield off, stride, a, b


class Probe:
    def __init__(self):
        profile = get_profile_paths()
        self.profile = profile
        settings = load_settings(profile.settings_path)
        self.host = str(settings["three_ds_ip"])
        self.port = int(settings["ram_bridge_port"])
        self.timeout = min(float(settings["bridge_timeout_s"]), 1.5)
        _runner, self.core, _terrain = load_walk_v0p23()
        self.br = self.core.Bridge(host=self.host, timeout=self.timeout)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.out_dir = profile.root / "dexnav_overworld_target_probe" / stamp
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.support_path = profile.root / f"DexNav_Overworld_Target_Probe_{stamp}.zip"
        self.report_path = self.out_dir / "report.json"
        self.analysis_path = self.out_dir / "candidate_analysis.json"
        self.events_path = self.out_dir / "events.jsonl"
        self.started = time.monotonic()
        self.report = {
            "probe": PROBE_NAME,
            "started": now_iso(),
            "host": self.host,
            "port": self.port,
            "expected_species": EXPECTED_SPECIES,
            "read_only": True,
            "ram_writes": False,
            "controller_input_sent": False,
            "scan_ranges": [
                {"name": n, "address": f"0x{a:08X}", "length": l}
                for n, a, l in SCAN_RANGES
            ],
            "result": "RUNNING",
            "stages": {},
            "events": [],
        }
        self.snapshots: dict[str, dict[str, bytes]] = {}
        self.candidates = []

    def log(self, event: str, **fields):
        rec = {
            "time": now_iso(),
            "elapsed_s": round(time.monotonic() - self.started, 3),
            "event": event,
            **fields,
        }
        self.report["events"].append(rec)
        with self.events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
        return rec

    def verify(self):
        gi = self.br.game_info()
        gp = profile_from_game_info(gi)
        if gp is None:
            raise RuntimeError(f"ORAS is not the active bridge process: {gi}")
        self.report["game_info"] = gi
        self.report["game_profile"] = gp
        self.log("CONNECTED", game=gp.get("name"), game_info=gi)
        return gp

    def _progress(self, label):
        def cb(done, total):
            pct = int((done * 100) / max(1, total))
            print(f"\r  {label}: {done}/{total} chunks ({pct:3d}%)", end="", flush=True)
            if done == total:
                print()
        return cb

    def full_snapshot(self, label: str, *, screenshots: bool = True):
        print(f"\nCapturing {label} RAM snapshot...")
        stage_dir = self.out_dir / label
        stage_dir.mkdir(parents=True, exist_ok=True)
        before = read_field(self.br)
        ranges = {}
        blobs = {}
        for name, addr, length in SCAN_RANGES:
            started = time.monotonic()
            raw, failures = read_sparse_chunks(self.br, addr, length, self._progress(name))
            elapsed = time.monotonic() - started
            (stage_dir / f"{name}_0x{addr:08X}_{length:08X}.bin").write_bytes(raw)
            blobs[name] = raw
            ranges[name] = {
                "address": f"0x{addr:08X}",
                "length": length,
                "elapsed_s": round(elapsed, 3),
                "failed_chunks": failures,
            }
        after = read_field(self.br)
        shots = {}
        if screenshots:
            shots["top"] = safe_capture(
                self.host, self.port, self.timeout,
                stage_dir / "top.png", SCREEN_TOP_LEFT,
            )
            shots["bottom"] = safe_capture(
                self.host, self.port, self.timeout,
                stage_dir / "bottom.png", SCREEN_BOTTOM,
            )
        rec = {
            "before": before,
            "after": after,
            "ranges": ranges,
            "screenshots": shots,
        }
        self.report["stages"][label] = rec
        self.snapshots[label] = blobs
        self.log("FULL_SNAPSHOT", label=label, field_before=before, field_after=after)
        return rec

    def analyze_baseline_visible(self):
        base = self.snapshots.get("baseline_before_target")
        vis = self.snapshots.get("target_visible_stationary")
        if not base or not vis:
            return []

        ranked = []
        for range_name, base_raw in base.items():
            vis_raw = vis[range_name]
            base_addr = next(a for n, a, _l in SCAN_RANGES if n == range_name)
            for off in range(0, min(len(base_raw), len(vis_raw)), BLOCK):
                b = base_raw[off:off + BLOCK]
                v = vis_raw[off:off + BLOCK]
                if b == v:
                    continue
                changed = sum(1 for x, y in zip(b, v) if x != y)
                score = min(changed, 32)
                reasons = [f"{changed}_bytes_changed"]
                species_offsets = []
                for o in range(0, len(v) - 1, 2):
                    if struct.unpack_from("<H", v, o)[0] == EXPECTED_SPECIES:
                        species_offsets.append(o)
                if species_offsets:
                    score += 70
                    reasons.append("contains_species_261")

                float_pairs = []
                for poff, stride, x, z in iter_float_pairs(v):
                    pscore = 8
                    if route101_targetish(x, z):
                        pscore += 90
                    score += pscore
                    float_pairs.append({
                        "offset": poff,
                        "stride": stride,
                        "x": round(float(x), 5),
                        "z": round(float(z), 5),
                        "route101_hint": route101_targetish(x, z),
                    })
                if float_pairs:
                    reasons.append("plausible_coordinate_pair")

                # Pointer-rich changed blocks are likely field/entity structs.
                ptrs = []
                for o in range(0, len(v) - 3, 4):
                    p = u32(v, o)
                    if 0x08000000 <= p < 0x09000000:
                        ptrs.append({"offset": o, "value": f"0x{p:08X}"})
                if ptrs:
                    score += min(20, len(ptrs) * 2)
                    reasons.append("contains_ram_pointers")

                ranked.append({
                    "range": range_name,
                    "address": base_addr + off,
                    "address_hex": f"0x{base_addr + off:08X}",
                    "block_size": len(v),
                    "score": score,
                    "changed_bytes": changed,
                    "reasons": reasons,
                    "species_261_offsets": species_offsets,
                    "float_pairs": float_pairs[:12],
                    "pointers": ptrs[:12],
                    "baseline_hex": b.hex(),
                    "visible_hex": v.hex(),
                })

        ranked.sort(key=lambda r: (r["score"], r["changed_bytes"]), reverse=True)
        self.candidates = ranked[:MAX_WATCH_CANDIDATE_BLOCKS]
        analysis = {
            "method": (
                "Blocks changed from pre-target to target-visible are ranked by species 261, "
                "plausible world-coordinate pairs, Route 101 coordinate proximity and RAM pointers."
            ),
            "watch_candidate_count": len(self.candidates),
            "top_candidates": ranked[:200],
        }
        self.analysis_path.write_text(json.dumps(analysis, indent=2), encoding="utf-8")
        self.log(
            "BASELINE_VISIBLE_ANALYZED",
            changed_blocks=len(ranked),
            watch_candidates=[c["address_hex"] for c in self.candidates],
        )
        print(f"Found {len(ranked)} changed 0x{BLOCK:X}-byte blocks; watching top {len(self.candidates)}.")
        for c in self.candidates[:10]:
            print(f"  {c['address_hex']} score={c['score']} {'; '.join(c['reasons'])}")
        return self.candidates

    def _read_candidate_blocks(self):
        out = {}
        for c in self.candidates:
            addr = int(c["address"])
            try:
                out[f"0x{addr:08X}"] = self.br.read(addr, BLOCK).hex()
            except Exception as exc:
                out[f"0x{addr:08X}"] = {"error": f"{type(exc).__name__}: {exc}"}
        return out

    def watch_manual_sneak(self, timeout_s: float = WATCH_TIMEOUT_S):
        print("\n" + "=" * 76)
        print("LIVE SNEAK WATCH")
        print("The probe sends NO controller input.")
        print("Use the PHYSICAL Circle Pad and sneak toward the visible Poochyena now.")
        print("Battle activation is detected automatically. Do not use the D-pad.")
        print("=" * 76)
        self.log("SNEAK_WATCH_START", timeout_s=timeout_s)

        path = self.out_dir / "sneak_watch.jsonl"
        deadline = time.monotonic() + timeout_s
        start_pos = None
        max_move = 0.0
        samples = 0
        next_at = 0.0
        battle_seen = False
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now < next_at:
                time.sleep(min(0.03, next_at - now))
                continue
            next_at = now + WATCH_PERIOD_S
            field = read_field(self.br)
            pos = field.get("world_primary")
            if start_pos is None and pos:
                start_pos = list(pos)
            moved = None
            if start_pos and pos:
                moved = math.hypot(pos[0] - start_pos[0], pos[1] - start_pos[1])
                max_move = max(max_move, moved)
            rec = {
                "time": now_iso(),
                "elapsed_s": round(now - self.started, 3),
                "field": field,
                "distance_from_watch_start": None if moved is None else round(moved, 5),
                "candidate_blocks": self._read_candidate_blocks(),
            }
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, sort_keys=True) + "\n")
            samples += 1
            if field.get("battle_raw") == BATTLE_ACTIVE:
                battle_seen = True
                self.log(
                    "BATTLE_DETECTED_DURING_SNEAK",
                    samples=samples,
                    max_player_move=round(max_move, 5),
                    field=field,
                )
                print("\nBattle detected from RAM.")
                break

        if not battle_seen:
            self.log(
                "SNEAK_WATCH_TIMEOUT",
                samples=samples,
                max_player_move=round(max_move, 5),
            )
            print("\nSneak watch timed out before battle. A final snapshot will still be captured.")
        return battle_seen

    def dynamic_analysis(self):
        watch_path = self.out_dir / "sneak_watch.jsonl"
        if not watch_path.exists() or not self.candidates:
            return
        rows = []
        for line in watch_path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
        if len(rows) < 2:
            return

        dynamic = []
        for c in self.candidates:
            key = c["address_hex"]
            block_values = []
            for row in rows:
                v = row.get("candidate_blocks", {}).get(key)
                if isinstance(v, str):
                    try:
                        block_values.append(bytes.fromhex(v))
                    except Exception:
                        pass
            if not block_values:
                continue
            first = block_values[0]
            byte_changes_over_watch = 0
            for blob in block_values[1:]:
                byte_changes_over_watch += sum(a != b for a, b in zip(first, blob))

            stable_pairs = []
            for poff, stride, x0, z0 in iter_float_pairs(first):
                xs, zs = [], []
                ok = True
                for blob in block_values:
                    try:
                        x = f32(blob, poff)
                        z = f32(blob, poff + stride)
                    except Exception:
                        ok = False
                        break
                    if not (math.isfinite(x) and math.isfinite(z)):
                        ok = False
                        break
                    xs.append(x); zs.append(z)
                if not ok:
                    continue
                span = max(max(xs) - min(xs), max(zs) - min(zs))
                if span <= 0.25:
                    stable_pairs.append({
                        "offset": poff,
                        "stride": stride,
                        "x": round(x0, 5),
                        "z": round(z0, 5),
                        "span": round(span, 6),
                        "route101_hint": route101_targetish(x0, z0),
                    })

            dyn_score = c["score"]
            if stable_pairs:
                dyn_score += 30
                if any(p["route101_hint"] for p in stable_pairs):
                    dyn_score += 120
            # A target entity can animate internally, but a completely chaotic
            # block is less useful than a mostly stable entity transform.
            if byte_changes_over_watch == 0:
                dyn_score += 8
            dynamic.append({
                "address": key,
                "baseline_visible_score": c["score"],
                "dynamic_score": dyn_score,
                "byte_changes_over_watch": byte_changes_over_watch,
                "stable_coordinate_pairs": stable_pairs[:20],
                "species_261_offsets": c.get("species_261_offsets", []),
                "reasons": c.get("reasons", []),
            })

        dynamic.sort(key=lambda x: x["dynamic_score"], reverse=True)
        existing = {}
        if self.analysis_path.exists():
            try:
                existing = json.loads(self.analysis_path.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
        existing["dynamic_watch_analysis"] = dynamic
        self.analysis_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        self.log("DYNAMIC_ANALYSIS_COMPLETE", top=dynamic[:10])

    def finish(self, result: str):
        self.report["result"] = result
        self.report["finished"] = now_iso()
        self.report["output_directory"] = str(self.out_dir)
        self.report["support_zip"] = str(self.support_path)
        self.report_path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
        with zipfile.ZipFile(self.support_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for p in sorted(self.out_dir.rglob("*")):
                if p.is_file():
                    zf.write(p, p.relative_to(self.out_dir))
        return self.support_path

    def run(self):
        gp = self.verify()
        print("=" * 76)
        print(PROBE_NAME)
        print(f"{gp['name']} | {self.host}:{self.port}")
        print("=" * 76)
        print("\nThis mapper is READ-ONLY. It never sends controller input or writes RAM.")
        print("It is looking for the actual overworld DexNav Pokemon entity/coordinates.\n")
        print("STEP 1")
        print("Reset/load the calibration save BEFORE the tutorial Poochyena is visible.")
        print("Stand still, then press ENTER here.")
        input("> ")
        self.full_snapshot("baseline_before_target")

        print("\nSTEP 2")
        print("Use the 3DS normally to advance the tutorial until the hidden Poochyena")
        print("is visibly rustling/available on Route 101. DO NOT move toward it yet.")
        print("Leave the player stationary and press ENTER here.")
        input("> ")
        self.full_snapshot("target_visible_stationary")
        self.analyze_baseline_visible()

        print("\nSTEP 3")
        print("Press ENTER, then pick up the 3DS and use the PHYSICAL Circle Pad to")
        print("sneak all the way into the Poochyena. The probe watches automatically.")
        input("> ")
        battle_seen = self.watch_manual_sneak()

        label = "battle_after_target_contact" if battle_seen else "end_of_sneak_watch"
        self.full_snapshot(label)
        self.dynamic_analysis()
        result = "BATTLE_CAPTURED" if battle_seen else "PARTIAL_NO_BATTLE"
        path = self.finish(result)
        print("\n" + "=" * 76)
        print("PROBE COMPLETE")
        print(f"Support ZIP created:\n{path}")
        print("Upload that ZIP back to the Pokebot3DS-CFW chat.")
        print("=" * 76)
        return 0


def main():
    probe = None
    try:
        probe = Probe()
        return probe.run()
    except KeyboardInterrupt:
        print("\nInterrupted by operator.")
        if probe is not None:
            try:
                print(f"Partial ZIP:\n{probe.finish('INTERRUPTED')}")
            except Exception:
                pass
        return 130
    except Exception as exc:
        print(f"\nPROBE ERROR: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        if probe is not None:
            try:
                probe.log("ERROR", error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
                print(f"Error ZIP:\n{probe.finish('ERROR')}")
            except Exception:
                pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
