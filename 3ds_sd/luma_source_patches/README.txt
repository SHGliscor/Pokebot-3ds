Source patch lineage for the single bundled Pokebot-Luma firmware.

Base bridge stages retained from the v0p5-fb1 line:
1. apply_ram_bridge.py
2. apply_ram_bridge_compile_fix.py
3. apply_additive_input_redirection.py
4. apply_pokebot_menu.py
5. apply_ack_controller_v0p5.py
6. apply_framebuffer_bridge.py

Unified New 3DS service-latency stage:
7. apply_n3ds_latency_fix_fb.py

HF77 controller extension:
- apply_ack_controller_v0p5.py now adds acknowledged command 13 CPAD_PULSE.
- Capability flags are 0x000001CF.
- Remote Circle Pad state is reset to 0x007FF7FF neutral on completion/release.
- Commands 11-12 remain reserved for framebuffer INFO/READ.

The final latency stage retains bridge worker priority 0x20, non-blocking socket
polling and a 2 ms idle yield. RAM/input/framebuffer/CPAD remain on shared UDP 4952.

Included boot.firm SHA256:
1b0cb91d9816ca805955dff41debdddd28fa47c39b43a94f5b0407a5311564f8

GitHub Actions build:
run 34659684931
source head 19019e5093a0ff31bc0086a1e7d6827b0bfc5cee

This build includes Pokemon X/Y GAME_INFO support in the RAM bridge.
