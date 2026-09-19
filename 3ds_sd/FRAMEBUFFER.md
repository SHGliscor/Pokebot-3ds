# Pokebot-Luma unified framebuffer path

The single bundled `boot.firm` exposes read-only top-screen framebuffer commands
11-12 on the same UDP `4952` bridge as RAM and acknowledged input.

The framebuffer is presentation-only. It can never create a shiny decision,
authorize a reset, or write game RAM. RAM-authoritative PK6 validation remains
the decision boundary.

This package uses the recovered `v0p6-n3ds-fb1` firmware lineage as the unified
firmware candidate. The New 3DS latency changes affect only bridge scheduling:
worker priority `0x20`, non-blocking socket polling, and a 2 ms idle yield.

See `UNIFIED_FIRMWARE.txt` for exact provenance and SHA256.
