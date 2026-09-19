from pathlib import Path

root = Path(__file__).resolve().parents[2] / "Luma3DS"
source_dir = root / "sysmodules" / "rosalina" / "source"
bridge_c = source_dir / "pokebot_ram_bridge.c"
menus_c = source_dir / "menus.c"

text = bridge_c.read_text(encoding="utf-8")

old_thread = """                                 0x24, CORE_SYSTEM)))"""
new_thread = """                                 0x20, CORE_SYSTEM)))"""
if old_thread not in text:
    raise SystemExit("Pokebot bridge thread-priority marker not found")
text = text.replace(old_thread, new_thread, 1)

old_poll = """        int pollres = socPoll(&pfd, 1, 20);
        if (pollres > 0 && (pfd.revents & POLLIN))
        {
            PokebotRequest req;
            struct sockaddr_in remote;
            socklen_t remoteLen = sizeof(remote);
            ssize_t n = socRecvfrom(sock, &req, sizeof(req), 0,
                                    (struct sockaddr *)&remote, &remoteLen);
            if (n < 0)
                break;
            if ((u32)n != sizeof(req))
                continue;

            pokebotRamBridgePackets++;
            Pokebot_HandleRequest(sock, &remote, remoteLen, &req);
        }
        else if (pollres < -10000)
            break;
"""
new_poll = """        int pollres = socPoll(&pfd, 1, 0);
        if (pollres > 0 && (pfd.revents & POLLIN))
        {
            PokebotRequest req;
            struct sockaddr_in remote;
            socklen_t remoteLen = sizeof(remote);
            ssize_t n = socRecvfrom(sock, &req, sizeof(req), 0,
                                    (struct sockaddr *)&remote, &remoteLen);
            if (n < 0)
                break;
            if ((u32)n != sizeof(req))
                continue;

            pokebotRamBridgePackets++;
            Pokebot_HandleRequest(sock, &remote, remoteLen, &req);
        }
        else if (pollres < -10000)
            break;
        else
            svcSleepThread(2 * 1000 * 1000LL);
"""
if old_poll not in text:
    raise SystemExit("Pokebot bridge poll-loop marker not found")
text = text.replace(old_poll, new_poll, 1)

if '"Pokebot3DS-Luma-v0p5"' not in text:
    raise SystemExit("v0p5 bridge identity marker not found")
text = text.replace(
    '"Pokebot3DS-Luma-v0p5"',
    '"Pokebot3DS-Luma-v0p7-n3ds-fb1-cpad"',
    1,
)
bridge_c.write_text(text, encoding="utf-8")

menus = menus_c.read_text(encoding="utf-8")
if "Pokebot-Luma v0p5-fb1" not in menus:
    raise SystemExit("v0p5-fb1 menu label not found")
menus = menus.replace(
    "Pokebot-Luma v0p5-fb1",
    "Pokebot-Luma v0p7-n3ds-fb1-cpad",
    1,
)
menus_c.write_text(menus, encoding="utf-8")

print("Pokebot-Luma New 3DS latency + acknowledged CPAD build applied on top of framebuffer build.")
