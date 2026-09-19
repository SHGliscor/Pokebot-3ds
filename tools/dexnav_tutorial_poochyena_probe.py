from __future__ import annotations

import json
import math
import struct
import sys
import time
import traceback
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pokebot.common.framebuffer import (
    SCREEN_BOTTOM,
    SCREEN_TOP_LEFT,
    capture_screen,
)
from pokebot.common.oras_profiles import (
    BATTLE_STATE,
    BATTLE_INACTIVE,
    PRIMARY_BASE,
    SECONDARY_BASE,
    ZONE_ADDR,
    profile_from_game_info,
)
from pokebot.common.acknowledged_input import AcknowledgedInput
from pokebot.wild.validated_loader import load_walk_v0p23
from qt_ui.appdata_store import get_profile_paths
from qt_ui.settings_store import load_settings

PROBE_NAME = "Pokebot3DS-CFW DexNav Tutorial Poochyena Mapper v0p1"
EXPECTED_SPECIES = 261
EXPECTED_LEVEL = 5
BATTLE_ACTIVE = 0x00040001
BATTLE_TRANSITION = 0x00000000

# Useful ORAS shared regions.  The probe does not assume these contain the
# tutorial text state; they are evidence to correlate with operator/screenshot
# checkpoints and battle activation.
SNAPSHOT_RANGES = (
    ("battle_globals", 0x081FB300, 0x200),
    ("field_position", 0x08C6E700, 0x200),
    ("field_zone_neighborhood", 0x08C6E800, 0x100),
)

BUTTONS = {
    "A": ("A",),
    "B": ("B",),
    "X": ("X",),
    "Y": ("Y",),
    "U": ("UP",),
    "D": ("DOWN",),
    "L": ("LEFT",),
    "R": ("RIGHT",),
    "START": ("START",),
    "SELECT": ("SELECT",),
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def read_chunks(br, address: int, length: int) -> bytes:
    out = bytearray()
    cursor = int(address)
    remaining = int(length)
    while remaining > 0:
        take = min(0x200, remaining)
        out.extend(br.read(cursor, take))
        cursor += take
        remaining -= take
    return bytes(out)


def safe_read_chunks(br, address: int, length: int):
    try:
        return read_chunks(br, address, length)
    except Exception:
        return None


def safe_u32(br, address: int):
    try:
        return struct.unpack("<I", br.read(address, 4))[0]
    except Exception:
        return None


def field_sample(br) -> dict:
    battle = safe_u32(br, BATTLE_STATE)
    zone_word = safe_u32(br, ZONE_ADDR)
    p = safe_read_chunks(br, PRIMARY_BASE, 12)
    s = safe_read_chunks(br, SECONDARY_BASE, 12)

    def coords(raw):
        if raw is None or len(raw) < 12:
            return None
        x = struct.unpack_from("<f", raw, 0)[0]
        z = struct.unpack_from("<f", raw, 8)[0]
        if not (math.isfinite(x) and math.isfinite(z)):
            return None
        return [x, z]

    return {
        "battle": None if battle is None else f"0x{battle:08X}",
        "zone_word": None if zone_word is None else f"0x{zone_word:08X}",
        "zone": None if zone_word is None else int(zone_word & 0xFFFF),
        "world_primary": coords(p),
        "world_secondary": coords(s),
    }


def safe_capture(host: str, port: int, timeout: float, path: Path, selector: int):
    try:
        return capture_screen(
            host,
            path,
            selector=selector,
            port=port,
            timeout=timeout,
        )
    except Exception as exc:
        return {
            "error": f"{type(exc).__name__}: {exc}",
            "path": str(path),
        }


def simplify_pk6(pk6: dict) -> dict:
    keys = (
        "species", "level", "pid", "ec", "tid", "sid", "nature_id",
        "ability_id", "gender", "is_shiny", "shiny_xor", "ivs", "iv_sum",
        "moves", "move_ids", "hidden_power", "checksum_valid",
        "trainer_matches",
    )
    out = {k: pk6.get(k) for k in keys if k in pk6}
    # Preserve everything else that is already JSON-safe.
    for k, v in pk6.items():
        if k in out:
            continue
        if isinstance(v, (str, int, float, bool, type(None), list, dict)):
            out[k] = v
    return out


def decode_tutorial_extras(core, raw: bytes) -> dict:
    """Decode fields the frozen normal-Wild PK6 helper does not expose.

    Poochyena uses the Medium Fast growth curve, so its stored EXP can be
    converted directly to level.  The Route 101 tutorial specimen is also
    distinguished by one of the three elemental Fang moves.
    """
    if len(raw) != int(core.PK6_STORED_SIZE):
        return {"error": f"wrong PK6 length {len(raw)}"}

    ec = struct.unpack_from("<I", raw, 0x00)[0]
    encrypted_payload = bytearray(raw[8:232])
    core.crypt_pk6_payload(encrypted_payload, ec)
    sv = (ec >> 13) & 31
    canonical_payload = core.unshuffle_pk6_payload(encrypted_payload, sv)
    dec = bytearray(raw[:8]) + canonical_payload

    exp = struct.unpack_from("<I", dec, 0x10)[0]
    moves = [struct.unpack_from("<H", dec, off)[0] for off in (0x5A, 0x5C, 0x5E, 0x60)]
    level = 1
    for candidate in range(1, 101):
        if candidate ** 3 <= exp:
            level = candidate
        else:
            break

    fang_names = {422: "Thunder Fang", 423: "Ice Fang", 424: "Fire Fang"}
    fangs = [{"move_id": m, "name": fang_names[m]} for m in moves if m in fang_names]
    return {
        "experience": int(exp),
        "inferred_level_medium_fast": int(level),
        "move_ids": moves,
        "tutorial_fangs": fangs,
        "tutorial_fang_present": bool(fangs),
    }


class Probe:
    def __init__(self):
        profile = get_profile_paths()
        self.profile = profile
        self.settings = load_settings(profile.settings_path)
        self.host = str(self.settings["three_ds_ip"])
        self.port = int(self.settings["ram_bridge_port"])
        self.timeout = min(float(self.settings["bridge_timeout_s"]), 1.5)

        _runner, self.core, _terrain = load_walk_v0p23()
        self.br = self.core.Bridge(host=self.host, timeout=self.timeout)
        self.inputs = AcknowledgedInput(self.host, port=self.port, timeout=self.timeout)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.out_dir = profile.root / "dexnav_tutorial_poochyena_probe" / stamp
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.out_dir / "events.jsonl"
        self.report_path = self.out_dir / "report.json"
        self.support_path = profile.root / f"DexNav_Tutorial_Poochyena_Probe_{stamp}.zip"

        self.started = time.monotonic()
        self.sequence = 0
        self.poochyena_visible = False
        self.battle_captured = False
        self.game_profile = None
        self.save_tid = None
        self.save_sid = None
        self.report = {
            "probe": PROBE_NAME,
            "started": now_iso(),
            "host": self.host,
            "port": self.port,
            "expected_species": EXPECTED_SPECIES,
            "expected_level": EXPECTED_LEVEL,
            "purpose": (
                "Map the scripted Route 101 DexNav tutorial Poochyena sequence "
                "before UI/state-machine integration."
            ),
            "policy": {
                "ram_writes": False,
                "automatic_circle_pad": False,
                "circle_pad_reason": (
                    "Current acknowledged UDP/4952 controller exposes buttons/touch "
                    "but not Circle Pad. Sneak is intentionally manual for this mapper."
                ),
                "after_poochyena_visible": (
                    "D-pad movement is blocked by the probe to avoid scaring the hidden Pokemon."
                ),
            },
            "events": [],
            "result": "RUNNING",
        }

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

    def checkpoint(self, label: str, *, note: str | None = None, screenshots: bool = True):
        self.sequence += 1
        tag = f"{self.sequence:03d}_{''.join(c if c.isalnum() else '_' for c in label)[:50]}"
        ranges = {}
        for name, addr, length in SNAPSHOT_RANGES:
            raw = safe_read_chunks(self.br, addr, length)
            ranges[name] = {
                "address": f"0x{addr:08X}",
                "length": length,
                "ok": raw is not None,
                "hex": raw.hex() if raw is not None else None,
            }
        rec = {
            "label": label,
            "note": note,
            "field": field_sample(self.br),
            "ranges": ranges,
            "screenshots": {},
        }
        if screenshots:
            rec["screenshots"]["top"] = safe_capture(
                self.host, self.port, self.timeout,
                self.out_dir / f"{tag}_top.png",
                SCREEN_TOP_LEFT,
            )
            rec["screenshots"]["bottom"] = safe_capture(
                self.host, self.port, self.timeout,
                self.out_dir / f"{tag}_bottom.png",
                SCREEN_BOTTOM,
            )
        self.log("CHECKPOINT", **rec)
        print(
            f"CHECKPOINT {self.sequence:03d}: {label} | "
            f"battle={rec['field'].get('battle')} zone={rec['field'].get('zone')}"
        )
        return rec

    def send_button(self, token: str):
        token = token.upper()
        if token not in BUTTONS:
            raise ValueError(token)
        if self.poochyena_visible and token in {"U", "D", "L", "R"}:
            print(
                "\nBLOCKED: D-pad movement after Poochyena appears can scare it away.\n"
                "Use S and move with the PHYSICAL Circle Pad for the sneak.\n"
            )
            self.log("BLOCKED_DPAD_AFTER_POOCHYENA_VISIBLE", token=token)
            return

        buttons = BUTTONS[token]
        before = field_sample(self.br)
        rec = self.inputs.pulse(
            buttons,
            hold_ms=120,
            resume_settle_ms=0,
            packet_interval_ms=20,
            release_ms=260,
        )
        after = field_sample(self.br)
        self.log(
            "BUTTON",
            token=token,
            buttons=list(buttons),
            input_result=rec,
            before=before,
            after=after,
        )
        self.checkpoint(f"after_{token}", screenshots=True)
        if after.get("battle") == f"0x{BATTLE_ACTIVE:08X}" and not self.battle_captured:
            self.capture_battle("battle_seen_after_button")

    def capture_battle(self, reason: str):
        if self.battle_captured:
            return self.report.get("battle_result")

        print("\nBattle detected. No more controller input will be sent.")
        self.inputs.release_all()
        boundary = self.core.wait_for_state2_for_pk6(self.br, timeout=15.0)
        result = {
            "reason": reason,
            "boundary": boundary,
            "expected_species": EXPECTED_SPECIES,
            "expected_level": EXPECTED_LEVEL,
        }
        if not boundary.get("ready_for_pk6"):
            result.update({
                "valid_target": False,
                "error": f"PK6 state-2 boundary failed: {boundary.get('status')}",
            })
            self.log("BATTLE_CAPTURE_FAILED", **result)
            self.report["battle_result"] = result
            self.battle_captured = True
            return result

        raw = read_chunks(self.br, self.core.WILD_PK6_ADDR, self.core.PK6_STORED_SIZE)
        raw_path = self.out_dir / "poochyena_opponent.pk6.bin"
        raw_path.write_bytes(raw)
        pk6 = self.core.decode_stored_pk6(raw, self.save_tid, self.save_sid)
        extras = decode_tutorial_extras(self.core, raw)
        slim = simplify_pk6(pk6)
        species = int(pk6.get("species") or 0)
        level = int(extras.get("inferred_level_medium_fast") or 0)
        valid_pk6 = bool(pk6.get("valid"))
        fang_present = bool(extras.get("tutorial_fang_present"))
        result.update({
            "raw_path": str(raw_path),
            "pk6": slim,
            "tutorial_extras": extras,
            "pk6_valid": valid_pk6,
            "species_match": species == EXPECTED_SPECIES,
            "level_match": level == EXPECTED_LEVEL,
            "tutorial_fang_present": fang_present,
            "valid_target": (
                valid_pk6
                and species == EXPECTED_SPECIES
                and level == EXPECTED_LEVEL
                and fang_present
            ),
            "shiny": bool(pk6.get("is_shiny")),
        })
        self.log("BATTLE_PK6", **result)
        self.report["battle_result"] = result
        self.battle_captured = True
        self.checkpoint("battle_pk6_captured", screenshots=True)

        print("=" * 72)
        print(
            f"PK6: species={species} level={level} "
            f"fang={extras.get('tutorial_fangs')} "
            f"shiny={bool(pk6.get('is_shiny'))} "
            f"PID={pk6.get('pid')} XOR={pk6.get('shiny_xor')}"
        )
        print(
            "TARGET CHECK: "
            + ("PASS - tutorial Poochyena confirmed" if result["valid_target"]
               else "FAIL - species/level did not match tutorial Poochyena")
        )
        print("=" * 72)
        return result

    def watch_manual_sneak(self, timeout_s: float = 60.0):
        if not self.poochyena_visible:
            print(
                "\nMark P first when the hidden Poochyena is visibly on-screen. "
                "This prevents accidental D-pad movement during the sneak.\n"
            )
            return
        print("\n" + "=" * 72)
        print("MANUAL SNEAK WATCH ACTIVE")
        print("Use the PHYSICAL Circle Pad on the 3DS and creep toward Poochyena.")
        print("Do NOT use the D-pad. The probe is sending NO movement input.")
        print("It will stop watching automatically when battle RAM becomes active.")
        print("=" * 72)

        self.log("MANUAL_SNEAK_WATCH_START", timeout_s=float(timeout_s))
        deadline = time.monotonic() + float(timeout_s)
        samples = []
        last_state = None
        next_sample = 0.0
        while time.monotonic() < deadline:
            state = safe_u32(self.br, BATTLE_STATE)
            now = time.monotonic()
            if state != last_state or now >= next_sample:
                sample = field_sample(self.br)
                sample["elapsed_watch_s"] = round(float(timeout_s) - max(0.0, deadline - now), 3)
                samples.append(sample)
                last_state = state
                next_sample = now + 0.25
            if state == BATTLE_ACTIVE:
                self.log("MANUAL_SNEAK_BATTLE_DETECTED", samples=samples)
                self.capture_battle("manual_circle_pad_sneak")
                return
            time.sleep(0.06)

        self.log("MANUAL_SNEAK_WATCH_TIMEOUT", samples=samples)
        print("\nNo battle was detected during the watch window.")
        print("If Poochyena is still present, press S again. If it disappeared, finish and send the probe ZIP.")

    def mark_text(self):
        note = input("Type the text/description currently visible (short is fine): ").strip()
        self.log("TEXT_MARK", text=note)
        self.checkpoint("text_" + (note[:30] or "marked"), note=note, screenshots=True)

    def mark_poochyena(self):
        self.poochyena_visible = True
        self.log("POOCHYENA_VISIBLE_MARK")
        self.checkpoint("poochyena_visible_before_sneak", screenshots=True)
        print(
            "\nPoochyena marked visible. D-pad movement is now blocked by this probe.\n"
            "Press S when ready, then use the PHYSICAL Circle Pad to sneak into it.\n"
        )

    def finish(self, result="COMPLETE"):
        if self.report.get("result") == "RUNNING":
            self.report["result"] = result
        self.report["finished"] = now_iso()
        self.report["output_directory"] = str(self.out_dir)
        self.report_path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
        with zipfile.ZipFile(self.support_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(self.out_dir.rglob("*")):
                if p.is_file():
                    zf.write(p, p.relative_to(self.out_dir))
        return self.support_path

    def verify(self):
        gi = self.br.game_info()
        gp = profile_from_game_info(gi)
        if gp is None:
            raise RuntimeError(f"Omega Ruby / Alpha Sapphire is not the active bridge process: {gi}")
        self.game_profile = gp

        caps = self.inputs.input_ping()
        self.inputs.release_all()

        ids = self.br.read(self.core.TRAINER_IDS_ADDR, 4)
        self.save_tid, self.save_sid = struct.unpack("<HH", ids)

        self.report.update({
            "game_info": gi,
            "game_profile": gp,
            "input_caps": caps,
            "trainer_ids": {"tid": self.save_tid, "sid": self.save_sid},
        })
        self.log(
            "CONNECTED",
            game=gp.get("name"),
            title_id=gp.get("title_id"),
            process=gi.get("process_name"),
            tid=self.save_tid,
            sid=self.save_sid,
            input_caps=caps,
        )

    def run(self):
        self.verify()
        print("=" * 72)
        print(PROBE_NAME)
        print(f"{self.game_profile['name']} | {self.host}:{self.port}")
        print("=" * 72)
        print(
            "\nPREPARE THE 3DS:\n"
            "  1. Use a save BEFORE the Route 101 DexNav tutorial Poochyena is consumed.\n"
            "  2. Load the save and stand still immediately before the tutorial sequence.\n"
            "  3. Do NOT scare/defeat/run from the Poochyena on this calibration save.\n"
            "\nThis first mapper does NOT alter the Hunt UI and does NOT automate Circle Pad.\n"
        )
        input("When the save is loaded at the calibration start point, press ENTER...")
        self.checkpoint("baseline_before_tutorial", screenshots=True)

        help_text = (
            "\nCommands:\n"
            "  A/B/X/Y       send that button\n"
            "  U/D/L/R       D-pad (blocked after Poochyena is marked visible)\n"
            "  START/SELECT  send that button\n"
            "  T             mark/describe tutorial text currently visible\n"
            "  C             capture an extra RAM + top/bottom-screen checkpoint\n"
            "  P             mark hidden Poochyena VISIBLE (locks out D-pad)\n"
            "  S             watch RAM while YOU manually sneak with physical Circle Pad\n"
            "  N             add a note\n"
            "  H             show this help\n"
            "  Q             finish and create support ZIP\n"
        )
        print(help_text)

        while not self.battle_captured:
            token = input("Probe command> ").strip().upper()
            if not token:
                continue
            if token in BUTTONS:
                self.send_button(token)
            elif token == "T":
                self.mark_text()
            elif token == "C":
                label = input("Checkpoint label: ").strip() or "manual"
                self.checkpoint(label, screenshots=True)
            elif token == "P":
                self.mark_poochyena()
            elif token == "S":
                self.watch_manual_sneak()
            elif token == "N":
                note = input("Note: ").strip()
                self.log("NOTE", note=note)
            elif token == "H":
                print(help_text)
            elif token == "Q":
                break
            else:
                print("Unknown command. Press H for help.")

        if self.battle_captured:
            print("\nBattle evidence captured. Do not continue the encounter for this mapper run.")
        path = self.finish("BATTLE_CAPTURED" if self.battle_captured else "OPERATOR_FINISHED")
        print(f"\nSupport ZIP created:\n{path}")
        print("\nUpload that ZIP back to the Pokebot3DS-CFW chat.")
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
                path = probe.finish("INTERRUPTED")
                print(f"Partial support ZIP created:\n{path}")
            except Exception:
                pass
        return 130
    except Exception as exc:
        print("\nPROBE ERROR:", type(exc).__name__, exc)
        traceback.print_exc()
        if probe is not None:
            try:
                probe.log("ERROR", error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
                path = probe.finish("ERROR")
                print(f"\nError support ZIP created:\n{path}")
            except Exception:
                pass
        return 1
    finally:
        if probe is not None:
            try:
                probe.inputs.release_all()
            except Exception:
                pass
            try:
                probe.inputs.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
