from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pokebot.common.bridge import Bridge
from pokebot.wild.post_capture_ram import locate_pokedex_module
from qt_ui.appdata_store import get_profile_paths
from qt_ui.settings_store import load_settings

AS_TITLE = "0x000400000011C500"


def main():
    ap = argparse.ArgumentParser(
        description="Read-only Alpha Sapphire 1.4 DllSangoZukan RAM probe"
    )
    ap.add_argument("--samples", type=int, default=6)
    ap.add_argument("--interval", type=float, default=0.75)
    args = ap.parse_args()

    profile = get_profile_paths()
    settings = load_settings(profile.settings_path)
    br = Bridge(
        settings["three_ds_ip"],
        int(settings["ram_bridge_port"]),
        float(settings["bridge_timeout_s"]),
    )

    gi = br.game_info()
    if gi.get("status") != 0 or gi.get("title_id") != AS_TITLE:
        raise SystemExit(
            "Alpha Sapphire is not the active RAM-bridge process: " + str(gi)
        )

    records = []
    count = max(1, min(30, int(args.samples)))
    interval = max(0.1, min(5.0, float(args.interval)))
    print("Pokebot3DS-CFW Pokédex RAM probe")
    print("Target: DllSangoZukan (read-only QUERY/CRO identity)")
    print(f"Samples: {count}  interval: {interval:.2f}s")

    for index in range(1, count + 1):
        scan = locate_pokedex_module(br)
        target = scan.get("target") or {}
        row = {
            "sample": index,
            "time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "present": bool(scan.get("present")),
            "base": target.get("base"),
            "query_count": scan.get("query_count"),
            "cro_count": scan.get("cro_count"),
            "elapsed_seconds": scan.get("elapsed_seconds"),
            "bounded": scan.get("bounded"),
            "errors": scan.get("errors") or [],
        }
        records.append(row)
        state = "PRESENT" if row["present"] else "absent"
        print(
            f"[{index}/{count}] {state:7} base={row['base'] or '-'} "
            f"queries={row['query_count']} cros={row['cro_count']} "
            f"scan={row['elapsed_seconds']}s"
        )
        if index < count:
            time.sleep(interval)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = profile.root / "support"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"pokedex_ram_probe_{stamp}.json"
    payload = {
        "tool": "Pokebot3DS-CFW Pokédex RAM Probe v0p42ZO",
        "game_info": gi,
        "authority": "DllSangoZukan CRO residency; read-only QUERY + READ",
        "ram_writes": False,
        "records": records,
    }
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Saved: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
