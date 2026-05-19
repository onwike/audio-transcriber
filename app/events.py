from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from typing import AsyncIterator


@dataclass
class ProgressEvent:
    phase: str
    percent: int
    message: str
    status: str = "running"  # running | done | error
    error: str | None = None

    def to_sse(self) -> str:
        payload = {
            "phase": self.phase,
            "percent": self.percent,
            "message": self.message,
            "status": self.status,
            "error": self.error,
        }
        return f"data: {json.dumps(payload)}\n\n"


class EventBus:
    """In-memory pub/sub for SSE progress. Single-process, single-user."""

    def __init__(self) -> None:
        self._history: dict[str, list[ProgressEvent]] = defaultdict(list)
        self._queues: dict[str, list[asyncio.Queue[ProgressEvent | None]]] = defaultdict(list)
        self._closed: set[str] = set()

    def publish(self, job_id: str, event: ProgressEvent) -> None:
        self._history[job_id].append(event)
        for q in list(self._queues.get(job_id, [])):
            q.put_nowait(event)
        if event.status in {"done", "error", "cancelled"}:
            self._closed.add(job_id)
            for q in list(self._queues.get(job_id, [])):
                q.put_nowait(None)

    def reset(self, job_id: str) -> None:
        """Clear history and disconnect subscribers — used when re-running a phase."""
        self._history.pop(job_id, None)
        self._closed.discard(job_id)
        for q in self._queues.get(job_id, []):
            q.put_nowait(None)
        self._queues.pop(job_id, None)

    async def subscribe(self, job_id: str) -> AsyncIterator[ProgressEvent]:
        # Snapshot history BEFORE registering queue so we never duplicate or miss events.
        history = list(self._history.get(job_id, []))
        closed = job_id in self._closed
        q: asyncio.Queue[ProgressEvent | None] = asyncio.Queue()
        if not closed:
            self._queues[job_id].append(q)

        try:
            for past in history:
                yield past
            if closed:
                return
            while True:
                event = await q.get()
                if event is None:
                    break
                yield event
        finally:
            if not closed and q in self._queues.get(job_id, []):
                self._queues[job_id].remove(q)


@lru_cache
def get_bus() -> EventBus:
    return EventBus()
