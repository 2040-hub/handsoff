"""In-process publish/subscribe hub that bridges a freshly-recorded buy to the web
page over Server-Sent Events (SSE).

Why this exists
---------------
Without it the page would learn about a new buy ONLY on its next ``/api/signals``
poll (8s cadence). This hub closes that gap: the poller records a buy on the event
loop and calls :meth:`SignalEventHub.publish`; every connected SSE client is woken
instantly so the browser refreshes in milliseconds. The 8s poll is kept as a robust
fallback for clients whose SSE connection is unavailable.

Design notes
------------
* Everything runs on the single asyncio event loop, so no thread lock is needed.
  ``publish()`` MUST be called from the loop thread (it touches asyncio primitives).
* Coalescing: subscribers wait on a per-client :class:`asyncio.Event` and read a
  shared monotonically-increasing generation counter. Multiple ``publish()`` calls
  between a client's wake-ups collapse into a single refresh.
* Bounded: ``subscribe()`` refuses new subscribers past ``max_subscribers`` so a
  flood of stalled connections can never exhaust memory; the SSE endpoint then
  returns 503 and the browser falls back to polling.
* Fully fault-isolated: a failure to set one subscriber's event never stops the
  others and never propagates into the poller's hot path.
"""

from __future__ import annotations

import asyncio
import logging

__all__ = ["SignalEventHub"]

logger = logging.getLogger("HandsOffHub")


class SignalEventHub:
    """A tiny coalescing fan-out hub for "a new buy is available".

    The payload is intentionally just a generation counter — the client reacts by
    re-fetching ``/api/signals`` (reusing all of its render / chime / voice logic),
    so the hub never needs to carry card data or worry about its schema.
    """

    def __init__(self, max_subscribers: int = 512) -> None:
        self._subscribers: set[asyncio.Event] = set()
        self._generation: int = 0
        try:
            self._max_subscribers = max(1, int(max_subscribers))
        except (TypeError, ValueError):
            self._max_subscribers = 512

    @property
    def generation(self) -> int:
        """Current change generation (sent to clients so they can detect gaps)."""
        return self._generation

    @property
    def subscriber_count(self) -> int:
        """Number of currently-connected SSE clients."""
        return len(self._subscribers)

    def subscribe(self) -> asyncio.Event | None:
        """Register a new subscriber and return its wake-up event.

        Returns ``None`` when the hub is at capacity, so the SSE endpoint can reject
        the connection and let the client fall back to polling.
        """
        if len(self._subscribers) >= self._max_subscribers:
            logger.warning(
                "SignalEventHub at capacity (%d subscribers); rejecting new SSE "
                "client — it will fall back to polling",
                self._max_subscribers,
            )
            return None
        event = asyncio.Event()
        self._subscribers.add(event)
        return event

    def unsubscribe(self, event: asyncio.Event | None) -> None:
        """Remove a subscriber. Safe to call with ``None`` or an unknown event."""
        if event is None:
            return
        self._subscribers.discard(event)

    def publish(self) -> int:
        """Bump the generation and wake every subscriber. Returns the new gen.

        Must be called on the event loop thread. Never raises: a failure to set one
        subscriber's event is swallowed so a single bad client can never disturb the
        poller's hot path or the other subscribers.
        """
        self._generation += 1
        # Iterate a snapshot so a concurrent unsubscribe cannot mutate the set
        # mid-iteration.
        for event in list(self._subscribers):
            try:
                event.set()
            except Exception:  # pragma: no cover - defensive; Event.set is robust
                pass
        return self._generation
