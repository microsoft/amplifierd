"""Per-session state wrapper with serialized execution, stale flag, and children tracking."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from amplifierd.state.event_bus import EventBus

logger = logging.getLogger(__name__)


class SessionStatus(StrEnum):
    """Lifecycle status of a managed session."""

    IDLE = "idle"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"


class SessionHandle:
    """Wraps one live AmplifierSession with execution serialisation and state tracking."""

    def __init__(
        self,
        *,
        session: Any,
        prepared_bundle: Any | None,
        bundle_name: str,
        event_bus: EventBus,
        working_dir: str | None,
    ) -> None:
        self._session = session
        self._prepared_bundle = prepared_bundle
        self._bundle_name = bundle_name
        self._event_bus = event_bus
        self._working_dir = working_dir

        self._status: SessionStatus = SessionStatus.IDLE
        self._stale: bool = False
        self._children: dict[str, str] = {}
        self._turn_count: int = 0
        now = datetime.now(UTC)
        self._created_at: datetime = now
        self._last_activity: datetime = now
        self._correlation_id: str | None = None
        self._approval_cache: dict[str, Any] = {}
        self._execute_lock = asyncio.Lock()

        self._wire_events()
        self._wire_display()

    def __repr__(self) -> str:
        sid = self.session_id
        return f"<SessionHandle {sid} {self._status.value.upper()} turns={self._turn_count}>"

    # ------------------------------------------------------------------
    # Properties (safe concurrent reads)
    # ------------------------------------------------------------------

    @property
    def session(self) -> Any:
        return self._session

    @property
    def session_id(self) -> str:
        return self._session.session_id

    @property
    def parent_id(self) -> str | None:
        return self._session.parent_id

    @property
    def status(self) -> SessionStatus:
        return self._status

    @property
    def stale(self) -> bool:
        return self._stale

    @property
    def children(self) -> dict[str, str]:
        return dict(self._children)

    @property
    def bundle_name(self) -> str:
        return self._bundle_name

    @property
    def turn_count(self) -> int:
        return self._turn_count

    @property
    def created_at(self) -> datetime:
        return self._created_at

    @property
    def last_activity(self) -> datetime:
        return self._last_activity

    @property
    def working_dir(self) -> str | None:
        return self._working_dir

    @property
    def correlation_id(self) -> str | None:
        return self._correlation_id

    # ------------------------------------------------------------------
    # Event wiring
    # ------------------------------------------------------------------

    def _wire_events(self) -> None:
        """Forward kernel events to EventBus for SSE streaming.

        Registers async hook handlers on the session's coordinator for each
        known kernel event. Each handler publishes the event to EventBus so
        SSE subscribers receive it in real time.
        """
        try:
            from amplifier_core import HookResult
            from amplifier_core.events import ALL_EVENTS
        except ImportError:
            logger.debug("amplifier_core not available; skipping event wiring")
            return

        coordinator = self._session.coordinator
        hooks = getattr(coordinator, "hooks", None)
        if hooks is None:
            return

        # Delegate events are not included in ALL_EVENTS — add them explicitly
        _delegate_events = [
            "delegate:agent_spawned",
            "delegate:agent_resumed",
            "delegate:agent_completed",
            "delegate:error",
        ]
        all_events = list(ALL_EVENTS) + _delegate_events

        registered = 0
        for event_name in all_events:

            async def _on_event(
                name: str, data: dict[str, Any], _evt: str = event_name
            ) -> HookResult:
                self._event_bus.publish(
                    session_id=self.session_id,
                    event_name=_evt,
                    data=data,
                    correlation_id=self._correlation_id,
                )
                return HookResult(action="continue")

            try:
                hooks.register(event_name, _on_event, name=f"amplifierd_eventbus_{event_name}")
                registered += 1
            except Exception:
                logger.debug("Failed to register hook for event %s", event_name, exc_info=True)

        logger.debug("Wired %d event hooks for session %s", registered, self.session_id)

    def _wire_display(self) -> None:
        """Wire an EventBusDisplaySystem onto the coordinator.

        Enables hooks that call ``coordinator.get("display").show_message()``
        to have their messages published to EventBus as ``display_message``
        events visible to SSE subscribers.
        """
        try:
            from amplifierd.display import EventBusDisplaySystem
        except ImportError:
            logger.debug("Display system not available; skipping display wiring")
            return

        coordinator = self._session.coordinator
        if not hasattr(coordinator, "set"):
            return

        display = EventBusDisplaySystem(
            event_bus=self._event_bus,
            session_id=self.session_id,
        )
        coordinator.set("display", display)
        logger.debug("Display system wired for session %s", self.session_id)

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------

    def mark_stale(self) -> None:
        """Mark this session as stale."""
        self._stale = True

    def register_child(self, child_session_id: str, agent_name: str) -> None:
        """Register a child session spawned from this handle."""
        self._children[child_session_id] = agent_name
        self._event_bus.register_child(self.session_id, child_session_id)

    async def execute(self, prompt: str) -> Any:
        """Serialize execution: only one prompt at a time.

        Uses an asyncio.Lock to prevent TOCTOU races.  The status flag
        is kept for observability (other code reads it) but the lock
        provides the actual serialisation guarantee.
        """
        if self._execute_lock.locked():
            raise RuntimeError("Session is already executing")
        async with self._execute_lock:
            self._turn_count += 1
            self._correlation_id = f"prompt_{self.session_id}_{self._turn_count}"
            self._status = SessionStatus.EXECUTING
            try:
                result = await self._session.execute(prompt)
                self._status = SessionStatus.IDLE
                return result
            except Exception:
                self._status = SessionStatus.FAILED
                raise
            finally:
                self._last_activity = datetime.now(UTC)

    async def cancel(self, immediate: bool = False) -> None:
        """Request cancellation of the current execution."""
        await self._session.coordinator.request_cancel(immediate)

    async def cleanup(self) -> None:
        """Clean up the underlying session."""
        try:
            await self._session.cleanup()
        except Exception:
            logger.warning("Error during session cleanup for %s", self.session_id, exc_info=True)
        self._status = SessionStatus.COMPLETED
