"""Web layer for HandsOff (再买剁手) — the buy-discipline display server.

Three small, self-contained pieces, mirroring the design proven in the upstream
Gold monitor but trimmed to the single self-discipline use case:

* :mod:`web_module.store`  — a robust SQLite store that permanently persists every
  parsed wallet BUY, a per-token metadata cache, and the soft/hard voice-alert play
  ledger (the atomic per-day cap gate).
* :mod:`web_module.events` — a tiny coalescing publish/subscribe hub that bridges a
  freshly-recorded buy to connected browsers over Server-Sent Events (SSE).
* :mod:`web_module.server` — an aiohttp web server (run inside the poller's own
  asyncio event loop) that serves the single-page UI and a small JSON API.

Everything degrades gracefully: a disabled / unopenable database or a failed
web-server bind logs a warning and is skipped — the wallet poller keeps running.
"""

from __future__ import annotations

__all__ = [
    "SignalStore",
    "SignalEventHub",
    "DisciplineWebServer",
    "build_links",
]

from web_module.store import SignalStore
from web_module.events import SignalEventHub
from web_module.server import DisciplineWebServer, build_links
