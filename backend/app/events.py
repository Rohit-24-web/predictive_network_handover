"""Session event hub — the piece that was missing for a live dashboard.

THE PROBLEM THIS SOLVES
-----------------------
Before this module the M7 WebSocket was strictly request/response: a client pushed
a telemetry frame and got frames back on the same socket. Nothing was ever pushed
on its own. So when the Vivo posts to HTTP `/telemetry`, no socket learns about it,
and a dashboard connected to `/ws/<id>` sits silent no matter which session id it
uses. Copying the phone's session id onto the dashboard would not have helped —
the transport had no path from "HTTP ingest" to "open socket".

This hub adds exactly that path and nothing more: publishers (the HTTP ingest
endpoint) hand frames to per-session subscriber queues, and read-only observer
sockets drain them.

DESIGN NOTES
------------
* Queues are bounded and drop the OLDEST frame when full. A dashboard that stalls
  must never grow memory without limit or block the phone's ingest path.
* Publishing is a no-op when nobody is listening, so a deployment with no observers
  behaves byte-identically to M5/M7.
* State is in-process, like SessionStore and TelemetryStore. Multiple workers would
  need sticky routing or a shared bus.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional, Set

logger = logging.getLogger("handover.events")

DEFAULT_QUEUE_SIZE = 32


class SessionHub:
    """Per-session fan-out to read-only observers."""

    def __init__(self, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        self.queue_size = queue_size
        self._subscribers: Dict[str, Set[asyncio.Queue]] = {}

    # -- subscription ------------------------------------------------------
    def subscribe(self, session_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.queue_size)
        self._subscribers.setdefault(session_id, set()).add(q)
        return q

    def unsubscribe(self, session_id: str, q: asyncio.Queue) -> None:
        subs = self._subscribers.get(session_id)
        if subs is None:
            return
        subs.discard(q)
        if not subs:
            self._subscribers.pop(session_id, None)

    def observer_count(self, session_id: Optional[str] = None) -> int:
        if session_id is None:
            return sum(len(v) for v in self._subscribers.values())
        return len(self._subscribers.get(session_id, ()))

    def sessions_with_observers(self) -> Set[str]:
        return set(self._subscribers)

    # -- publishing --------------------------------------------------------
    def publish(self, session_id: str, frame: Dict[str, Any]) -> int:
        """Fan a frame out to this session's observers. Returns how many received it.

        Synchronous and non-blocking on purpose: the ingest path must not wait on a
        slow dashboard. A full queue drops its oldest frame rather than the newest,
        so an observer that falls behind still sees the most recent state.
        """
        subs = self._subscribers.get(session_id)
        if not subs:
            return 0
        delivered = 0
        for q in list(subs):
            if q.full():
                try:
                    q.get_nowait()          # drop oldest
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(frame)
                delivered += 1
            except asyncio.QueueFull:       # pragma: no cover - racing consumer
                logger.debug("observer queue full for session %s", session_id)
        return delivered
