"""Global async event fanout with session-tree propagation and backpressure."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from amplifierd.state.transport_event import TransportEvent

logger = logging.getLogger(__name__)

_KEEPALIVE_INTERVAL: float = 15.0


class _Subscriber:
    """Internal subscriber tracking a session filter and an asyncio queue."""

    __slots__ = ("session_id", "filter_patterns", "queue")

    def __init__(
        self,
        session_id: str | None,
        filter_patterns: list[str] | None,
        queue: asyncio.Queue[TransportEvent],
    ) -> None:
        self.session_id = session_id
        self.filter_patterns = filter_patterns
        self.queue = queue

    def matches(self, event_session_id: str, bus: EventBus) -> bool:
        """Return True if this subscriber should receive an event from *event_session_id*.

        Note: ``filter_patterns`` is stored for future pattern-based filtering
        but not yet consulted during matching (intentional scaffolding).
        """
        if self.session_id is None:
            return True
        if event_session_id == self.session_id:
            return True
        if event_session_id in bus.get_descendants(self.session_id):
            return True
        return False


class EventBus:
    """Global async event fanout with session-tree propagation and backpressure."""

    _MAX_QUEUE_SIZE: int = 10_000

    def __init__(self) -> None:
        self._subscribers: list[_Subscriber] = []
        self._lock = asyncio.Lock()  # Reserved for future concurrent-publish support
        self._children: dict[str, set[str]] = {}

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def subscriber_count(self) -> int:
        """Return the number of active subscribers."""
        return len(self._subscribers)

    # ------------------------------------------------------------------
    # Session tree management
    # ------------------------------------------------------------------

    def register_child(self, parent_id: str, child_id: str) -> None:
        """Register *child_id* as a child of *parent_id*."""
        self._children.setdefault(parent_id, set()).add(child_id)

    def unregister_child(self, parent_id: str, child_id: str) -> None:
        """Remove *child_id* from the children of *parent_id*."""
        children = self._children.get(parent_id)
        if children is not None:
            children.discard(child_id)
            if not children:
                del self._children[parent_id]

    def get_descendants(self, session_id: str) -> set[str]:
        """Return all transitive descendants of *session_id* via BFS."""
        visited: set[str] = set()
        queue: deque[str] = deque()
        # Seed with direct children
        for child in self._children.get(session_id, ()):
            queue.append(child)
        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            for child in self._children.get(current, ()):
                if child not in visited:
                    queue.append(child)
        return visited

    # ------------------------------------------------------------------
    # Publish (SYNCHRONOUS – non-blocking)
    # ------------------------------------------------------------------

    def publish(
        self,
        session_id: str,
        event_name: str,
        data: dict[str, Any],
        correlation_id: str | None = None,
    ) -> None:
        """Publish an event to all matching subscribers (non-blocking)."""
        event = TransportEvent(
            event_name=event_name,
            data=data,
            session_id=session_id,
            timestamp=datetime.now(UTC).isoformat(),
            correlation_id=correlation_id,
        )
        for sub in self._subscribers:
            if sub.matches(session_id, self):
                try:
                    sub.queue.put_nowait(event)
                except asyncio.QueueFull:
                    # Backpressure: drop oldest event then retry
                    try:
                        sub.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    sub.queue.put_nowait(event)
                    logger.warning(
                        "EventBus queue full for subscriber session=%s; "
                        "dropped oldest event (current_event=%s)",
                        sub.session_id,
                        event_name,
                    )

    # ------------------------------------------------------------------
    # Subscribe (async generator)
    # ------------------------------------------------------------------

    async def subscribe(
        self,
        session_id: str | None = None,
        filter_patterns: list[str] | None = None,
    ) -> AsyncIterator[TransportEvent | None]:
        """Async generator yielding TransportEvents for this subscriber."""
        queue: asyncio.Queue[TransportEvent] = asyncio.Queue(maxsize=self._MAX_QUEUE_SIZE)
        sub = _Subscriber(session_id=session_id, filter_patterns=filter_patterns, queue=queue)
        self._subscribers.append(sub)
        logger.info(
            "SSE subscriber connected: session=%s (total=%d)",
            session_id,
            len(self._subscribers),
        )
        sequence = 0
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_INTERVAL)
                except TimeoutError:
                    yield None  # Keepalive sentinel for SSE generator
                    continue
                sequence += 1
                event = TransportEvent(
                    event_name=raw.event_name,
                    data=raw.data,
                    session_id=raw.session_id,
                    timestamp=raw.timestamp,
                    correlation_id=raw.correlation_id,
                    sequence=sequence,
                )
                yield event
        finally:
            self._subscribers.remove(sub)
            logger.info(
                "SSE subscriber disconnected: session=%s (total=%d)",
                session_id,
                len(self._subscribers),
            )
