from __future__ import annotations

"""Compatibility wrapper for the shared live-party authority.

The implementation moved to pokebot.common.live_party in v0p43BO so dashboard,
hunt logic, telemetry and support tools all use one RAM authority.
"""

from pokebot.common.live_party import *  # noqa: F401,F403
