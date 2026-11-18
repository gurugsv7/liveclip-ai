"""Thread-safe WebSocket event hub."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque

log = logging.getLogger("liveclip.events")


class EventHub:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: set[asyncio.Queue] = set()
        self.recent: deque[dict] = deque(maxlen=200)   # notable events for late joiners
        self.warnings: dict[str, dict] = {}

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.clients.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.clients.discard(q)

    def _deliver(self, msg: str) -> None:
        for q in list(self.clients):
            if q.full():
                try:
                    q.get_nowait()  # drop oldest for slow clients
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(msg)

    def publish(self, type_: str, data: dict, notable: bool = False) -> None:
        payload = {"type": type_, "ts": time.time(), "data": data}
        if notable:
            self.recent.append(payload)
        msg = json.dumps(payload, default=str)
        if self.loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            self._deliver(msg)
        else:
            try:
                self.loop.call_soon_threadsafe(self._deliver, msg)
            except RuntimeError:
                pass  # loop closed during shutdown

    def warn(self, key: str, level: str, message: str) -> None:
        cur = self.warnings.get(key)
        if cur and cur["message"] == message:
            return
        self.warnings[key] = {"key": key, "level": level, "message": message, "ts": time.time()}
        self.publish("warning", self.warnings[key], notable=True)
        log.warning("[%s] %s", key, message)

    def clear_warning(self, key: str) -> None:
        if self.warnings.pop(key, None):
            self.publish("warning_cleared", {"key": key})
