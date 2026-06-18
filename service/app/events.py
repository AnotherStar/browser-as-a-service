"""In-memory event bus + service stats powering the admin panel.

The bus keeps a small ring buffer of recent log events (so a freshly opened
panel sees backlog) and fans new events out to any connected SSE subscribers.
Everything lives in the single asyncio event loop, so no locking is needed.

Kept dependency-free on purpose: only stdlib here. `browser.py` and the
endpoints import `bus` from this module — never the other way round — so there
is no import cycle.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Deque, List, Set

# Log levels (drive colour in the UI): info | success | warn | error
# Sources (drive the tag in the UI): http | scrape | system


@dataclass
class LogEvent:
    ts: float       # epoch seconds
    level: str
    source: str
    who: str        # client ip / "system"
    action: str     # short label, e.g. "POST /run" or "ozon price"
    detail: str = ""

    def sse(self) -> str:
        """Serialise as a Server-Sent-Events `data:` frame."""
        return f"data: {json.dumps(asdict(self), ensure_ascii=False)}\n\n"


class EventBus:
    def __init__(self, history: int = 500) -> None:
        self._buffer: Deque[LogEvent] = deque(maxlen=history)
        self._subscribers: Set["asyncio.Queue[LogEvent]"] = set()
        self.started_at = time.time()
        self.total = 0      # requests seen
        self.errors = 0     # requests that failed / 4xx-5xx
        self.active = 0     # requests in flight

    # -- producing --------------------------------------------------------- #

    def emit(
        self,
        level: str,
        source: str,
        who: str,
        action: str,
        detail: str = "",
    ) -> LogEvent:
        ev = LogEvent(time.time(), level, source, who, action, detail)
        self._buffer.append(ev)
        for q in list(self._subscribers):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                # Slow consumer: drop rather than block the producer.
                pass
        return ev

    def recent(self) -> List[LogEvent]:
        return list(self._buffer)

    # -- consuming (SSE) --------------------------------------------------- #

    def register(self) -> "asyncio.Queue[LogEvent]":
        q: "asyncio.Queue[LogEvent]" = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unregister(self, q: "asyncio.Queue[LogEvent]") -> None:
        self._subscribers.discard(q)

    # -- status ------------------------------------------------------------ #

    def stats(self) -> dict:
        return {
            "uptime_s": int(time.time() - self.started_at),
            "total_requests": self.total,
            "errors": self.errors,
            "active_requests": self.active,
            "subscribers": len(self._subscribers),
        }


bus = EventBus()
