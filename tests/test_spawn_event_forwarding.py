"""Tests for child session event forwarding via spawn.py.

Verifies that when the delegate/task tool spawns a sub-agent, the child
session's streaming events appear on the parent session's SSE stream
through the EventBus tree.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from amplifierd.state.event_bus import EventBus
from amplifierd.state.session_handle import SessionHandle
from amplifierd.state.session_manager import SessionManager
from amplifierd.state.transport_event import TransportEvent

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _make_mock_session(
    session_id: str = "parent-1",
    parent_id: str | None = None,
) -> MagicMock:
    """Create a mock AmplifierSession with coordinator and hooks."""
    session = MagicMock()
    session.session_id = session_id
    session.parent_id = parent_id
    session.execute = AsyncMock(return_value="result-ok")
    session.cleanup = AsyncMock()
    session.initialize = AsyncMock()
    session.coordinator = MagicMock()
    session.coordinator.request_cancel = MagicMock()
    session.coordinator.get_capability = MagicMock(return_value=None)
    session.coordinator.mount = AsyncMock()
    return session


def _make_child_session(
    session_id: str = "child-1",
    parent_id: str = "parent-1",
    execute_return: str = "child-response",
) -> MagicMock:
    """Create a mock AmplifierSession that simulates a child session.

    The mock emits events through its hooks during execute() to simulate
    what a real orchestrator does during execution.  The hooks dict is
    shared with ``SessionHandle._wire_events()`` via a custom
    ``hooks.register`` side-effect so that EventBus forwarding hooks
    fire during ``execute()``.
    """
    session = MagicMock()
    session.session_id = session_id
    session.parent_id = parent_id
    session.cleanup = AsyncMock()
    session.initialize = AsyncMock()
    session.coordinator = MagicMock()
    session.coordinator.request_cancel = MagicMock()
    session.coordinator.get_capability = MagicMock(return_value=None)
    # get() returns None so the system-prompt / context-injection paths are skipped
    session.coordinator.get = MagicMock(return_value=None)
    session.coordinator.mount = AsyncMock()
    session.coordinator.register_capability = MagicMock()

    # Hooks: track registered handlers so we can fire them in execute()
    _hooks: dict[str, list] = {}

    def _register_hook(
        event_name: str,
        handler: Any,
        priority: int = 0,
        name: str | None = None,
    ) -> Any:
        _hooks.setdefault(event_name, []).append(handler)
        return lambda: _hooks.get(event_name, []).remove(handler)

    hooks_mock = MagicMock()
    hooks_mock.register = MagicMock(side_effect=_register_hook)
    session.coordinator.hooks = hooks_mock

    # Also make getattr(coordinator, "hooks", None) work in _wire_events
    session.coordinator.__dict__["hooks"] = hooks_mock

    async def _execute(prompt: str) -> str:
        """Simulate orchestrator execution: emit events through hooks.

        Event names match amplifier_core.events.ALL_EVENTS:
        content_block:start, content_block:delta, content_block:end,
        orchestrator:complete.
        """
        for h in list(_hooks.get("content_block:start", [])):
            await h("content_block:start", {"type": "text"})

        for h in list(_hooks.get("content_block:delta", [])):
            await h("content_block:delta", {"text": execute_return})

        for h in list(_hooks.get("content_block:end", [])):
            await h("content_block:end", {})

        for h in list(_hooks.get("orchestrator:complete", [])):
            await h(
                "orchestrator:complete",
                {"status": "success", "turn_count": 1, "metadata": {}},
            )

        return execute_return

    session.execute = AsyncMock(side_effect=_execute)
    return session


def _make_mock_bundle(agents: dict[str, dict] | None = None) -> MagicMock:
    """Create a mock Bundle with compose() and to_mount_plan()."""
    bundle = MagicMock()
    bundle.agents = agents or {}
    bundle.base_path = None
    bundle.instruction = None
    bundle.context = None

    composed = MagicMock()
    composed.to_mount_plan = MagicMock(return_value={"tools": [], "hooks": []})
    composed.instruction = None
    composed.context = None
    bundle.compose = MagicMock(return_value=composed)

    return bundle


def _make_mock_prepared(bundle: MagicMock | None = None) -> MagicMock:
    """Create a mock PreparedBundle with bundle, resolver, and spawn()."""
    prepared = MagicMock()
    prepared.bundle = bundle or _make_mock_bundle()
    prepared.resolver = MagicMock()
    prepared.spawn = AsyncMock(
        return_value={
            "output": "fallback-response",
            "session_id": "fallback-child",
            "status": "success",
            "turn_count": 1,
            "metadata": {},
        }
    )
    return prepared


def _make_parent_handle(
    session_id: str = "parent-1",
    event_bus: EventBus | None = None,
) -> SessionHandle:
    """Create a real SessionHandle for a parent session."""
    bus = event_bus or EventBus()
    mock_session = _make_mock_session(session_id=session_id)
    return SessionHandle(
        session=mock_session,
        prepared_bundle=None,
        bundle_name="test-parent",
        event_bus=bus,
        working_dir="/tmp/test",
    )


def _make_manager(event_bus: EventBus | None = None) -> SessionManager:
    """Create a real SessionManager with a real EventBus."""
    from amplifierd.config import DaemonSettings

    bus = event_bus or EventBus()
    settings = DaemonSettings()
    return SessionManager(event_bus=bus, settings=settings)


# ------------------------------------------------------------------
# Tests: register_spawn_capability signature
# ------------------------------------------------------------------


@pytest.mark.unit
class TestRegisterSpawnCapability:
    """Verify register_spawn_capability wires the spawn closure correctly."""

    def test_registers_capability_on_coordinator(self):
        """session.spawn capability is registered on the coordinator."""
        session = _make_mock_session()
        prepared = _make_mock_prepared()

        from amplifierd.spawn import register_spawn_capability

        register_spawn_capability(session, prepared, "parent-1")

        session.coordinator.register_capability.assert_called_once()
        args = session.coordinator.register_capability.call_args
        assert args[0][0] == "session.spawn"
        assert callable(args[0][1])

    def test_accepts_session_manager_and_parent_handle(self):
        """New keyword-only params don't break registration."""
        bus = EventBus()
        session = _make_mock_session()
        prepared = _make_mock_prepared()
        handle = _make_parent_handle(event_bus=bus)
        manager = _make_manager(event_bus=bus)

        from amplifierd.spawn import register_spawn_capability

        register_spawn_capability(
            session,
            prepared,
            "parent-1",
            session_manager=manager,
            parent_handle=handle,
        )

        session.coordinator.register_capability.assert_called_once()


# ------------------------------------------------------------------
# Tests: fallback path (no session_manager)
# ------------------------------------------------------------------


@pytest.mark.unit
class TestSpawnFallback:
    """When session_manager is None, spawn_fn delegates to prepared.spawn()."""

    async def test_falls_back_to_prepared_spawn(self):
        """Without session_manager, spawn_fn calls prepared.spawn() directly."""
        session = _make_mock_session()
        prepared = _make_mock_prepared()

        from amplifierd.spawn import register_spawn_capability

        register_spawn_capability(session, prepared, "parent-1")

        spawn_fn = session.coordinator.register_capability.call_args[0][1]

        # agent_name="self" bypasses agent resolution; Bundle is constructed
        # but prepared.spawn() is mocked, so the child_bundle value doesn't
        # matter.
        result = await spawn_fn(
            agent_name="self",
            instruction="do something",
            parent_session=session,
        )

        assert result == prepared.spawn.return_value
        prepared.spawn.assert_awaited_once()


# ------------------------------------------------------------------
# Tests: _spawn_with_event_forwarding
# ------------------------------------------------------------------


@pytest.mark.unit
class TestSpawnWithEventForwarding:
    """Verify _spawn_with_event_forwarding creates SessionHandle, wires
    EventBus tree, and propagates child events to parent subscribers."""

    async def test_child_registered_in_session_manager(self):
        """Child session is registered in SessionManager during spawn,
        then destroyed (cleaned up) after execution completes."""
        bus = EventBus()
        manager = _make_manager(event_bus=bus)
        parent_handle = _make_parent_handle(session_id="parent-ef-1", event_bus=bus)
        manager._sessions["parent-ef-1"] = parent_handle  # noqa: SLF001

        child_session = _make_child_session(session_id="child-ef-1", parent_id="parent-ef-1")
        prepared = _make_mock_prepared()

        from amplifierd.spawn import _spawn_with_event_forwarding

        with patch("amplifier_core.AmplifierSession", return_value=child_session):
            result = await _spawn_with_event_forwarding(
                prepared=prepared,
                child_bundle=MagicMock(),
                agent_name="test-agent",
                instruction="test instruction",
                parent_session=_make_mock_session(session_id="parent-ef-1"),
                sub_session_id="child-ef-1",
                orchestrator_config=None,
                parent_messages=None,
                provider_preferences=None,
                self_delegation_depth=0,
                session_manager=manager,
                parent_handle=parent_handle,
            )

        assert result["output"] == "child-response"
        assert result["session_id"] == "child-ef-1"
        assert result["status"] == "success"
        # Child should be destroyed after execution
        assert manager.get("child-ef-1") is None

    async def test_child_wired_in_eventbus_tree(self):
        """parent_handle.register_child() is called, wiring EventBus tree
        BEFORE execution starts."""
        bus = EventBus()
        manager = _make_manager(event_bus=bus)
        parent_handle = _make_parent_handle(session_id="parent-ef-2", event_bus=bus)
        manager._sessions["parent-ef-2"] = parent_handle  # noqa: SLF001

        child_session = _make_child_session(session_id="child-ef-2", parent_id="parent-ef-2")
        prepared = _make_mock_prepared()

        # Check the tree is wired DURING execution (before execute returns)
        tree_wired_during_execute = False
        original_execute = child_session.execute.side_effect

        async def _checking_execute(prompt: str) -> str:
            nonlocal tree_wired_during_execute
            tree_wired_during_execute = "child-ef-2" in bus.get_descendants("parent-ef-2")
            return await original_execute(prompt)

        child_session.execute = AsyncMock(side_effect=_checking_execute)

        from amplifierd.spawn import _spawn_with_event_forwarding

        with patch("amplifier_core.AmplifierSession", return_value=child_session):
            await _spawn_with_event_forwarding(
                prepared=prepared,
                child_bundle=MagicMock(),
                agent_name="test-agent",
                instruction="test",
                parent_session=_make_mock_session(session_id="parent-ef-2"),
                sub_session_id="child-ef-2",
                orchestrator_config=None,
                parent_messages=None,
                provider_preferences=None,
                self_delegation_depth=0,
                session_manager=manager,
                parent_handle=parent_handle,
            )

        assert tree_wired_during_execute, (
            "EventBus tree should be wired before child execution starts"
        )

    async def test_child_events_propagate_to_parent_subscriber(self):
        """SSE subscriber on parent session receives child session events.

        Events are published synchronously during execute(), so we start
        the subscriber, run the spawn, yield control to let the subscriber
        drain the queue, then verify.
        """
        bus = EventBus()
        manager = _make_manager(event_bus=bus)
        parent_handle = _make_parent_handle(session_id="parent-ef-3", event_bus=bus)
        manager._sessions["parent-ef-3"] = parent_handle  # noqa: SLF001

        child_session = _make_child_session(
            session_id="child-ef-3",
            parent_id="parent-ef-3",
            execute_return="streamed-text",
        )
        prepared = _make_mock_prepared()

        # Collect events from parent's SSE stream
        received: list[TransportEvent] = []

        async def _consume() -> None:
            async for event in bus.subscribe(session_id="parent-ef-3"):
                received.append(event)
                # content_block:start, delta, stop, orchestrator:complete
                if len(received) >= 4:
                    break

        consumer_task = asyncio.create_task(_consume())
        await asyncio.sleep(0.05)  # let subscriber register

        from amplifierd.spawn import _spawn_with_event_forwarding

        with patch("amplifier_core.AmplifierSession", return_value=child_session):
            result = await _spawn_with_event_forwarding(
                prepared=prepared,
                child_bundle=MagicMock(),
                agent_name="test-agent",
                instruction="stream me",
                parent_session=_make_mock_session(session_id="parent-ef-3"),
                sub_session_id="child-ef-3",
                orchestrator_config=None,
                parent_messages=None,
                provider_preferences=None,
                self_delegation_depth=0,
                session_manager=manager,
                parent_handle=parent_handle,
            )

        # Events were published synchronously during execute(); yield so
        # the consumer task can drain the queue entries.
        await asyncio.sleep(0.1)

        # If the consumer hasn't finished yet, give it a bounded wait.
        # The events are already queued, so this should be near-instant.
        try:
            await asyncio.wait_for(consumer_task, timeout=2.0)
        except TimeoutError:
            # If we time out, the consumer didn't get enough events.
            # Cancel and proceed — assertions below will report what we got.
            consumer_task.cancel()
            try:
                await consumer_task
            except asyncio.CancelledError:
                pass

        assert result["output"] == "streamed-text"

        # Verify at least the core streaming events arrived
        event_names = [e.event_name for e in received]
        assert "content_block:start" in event_names, (
            f"Expected content_block:start in {event_names}"
        )
        assert "content_block:delta" in event_names, (
            f"Expected content_block:delta in {event_names}"
        )
        assert "content_block:end" in event_names, f"Expected content_block:end in {event_names}"

        # All events should carry the child's session_id
        for evt in received:
            assert evt.session_id == "child-ef-3"

    async def test_child_cleanup_after_execution(self):
        """Child SessionHandle is destroyed after execution completes."""
        bus = EventBus()
        manager = _make_manager(event_bus=bus)
        parent_handle = _make_parent_handle(session_id="parent-ef-4", event_bus=bus)
        manager._sessions["parent-ef-4"] = parent_handle  # noqa: SLF001

        child_session = _make_child_session(session_id="child-ef-4", parent_id="parent-ef-4")
        prepared = _make_mock_prepared()

        from amplifierd.spawn import _spawn_with_event_forwarding

        with patch("amplifier_core.AmplifierSession", return_value=child_session):
            await _spawn_with_event_forwarding(
                prepared=prepared,
                child_bundle=MagicMock(),
                agent_name="test-agent",
                instruction="cleanup test",
                parent_session=_make_mock_session(session_id="parent-ef-4"),
                sub_session_id="child-ef-4",
                orchestrator_config=None,
                parent_messages=None,
                provider_preferences=None,
                self_delegation_depth=0,
                session_manager=manager,
                parent_handle=parent_handle,
            )

        assert manager.get("child-ef-4") is None
        child_session.cleanup.assert_awaited_once()

    async def test_child_cleanup_on_execution_failure(self):
        """Child is cleaned up even when execution raises an exception."""
        bus = EventBus()
        manager = _make_manager(event_bus=bus)
        parent_handle = _make_parent_handle(session_id="parent-ef-5", event_bus=bus)
        manager._sessions["parent-ef-5"] = parent_handle  # noqa: SLF001

        child_session = _make_child_session(session_id="child-ef-5", parent_id="parent-ef-5")
        child_session.execute = AsyncMock(side_effect=RuntimeError("boom"))
        prepared = _make_mock_prepared()

        from amplifierd.spawn import _spawn_with_event_forwarding

        with (
            patch("amplifier_core.AmplifierSession", return_value=child_session),
            pytest.raises(RuntimeError, match="boom"),
        ):
            await _spawn_with_event_forwarding(
                prepared=prepared,
                child_bundle=MagicMock(),
                agent_name="test-agent",
                instruction="fail test",
                parent_session=_make_mock_session(session_id="parent-ef-5"),
                sub_session_id="child-ef-5",
                orchestrator_config=None,
                parent_messages=None,
                provider_preferences=None,
                self_delegation_depth=0,
                session_manager=manager,
                parent_handle=parent_handle,
            )

        assert manager.get("child-ef-5") is None
        child_session.cleanup.assert_awaited_once()

    async def test_completion_data_captured_in_result(self):
        """orchestrator:complete hook data is included in the return dict."""
        bus = EventBus()
        manager = _make_manager(event_bus=bus)
        parent_handle = _make_parent_handle(session_id="parent-ef-6", event_bus=bus)
        manager._sessions["parent-ef-6"] = parent_handle  # noqa: SLF001

        child_session = _make_child_session(session_id="child-ef-6", parent_id="parent-ef-6")
        prepared = _make_mock_prepared()

        from amplifierd.spawn import _spawn_with_event_forwarding

        with patch("amplifier_core.AmplifierSession", return_value=child_session):
            result = await _spawn_with_event_forwarding(
                prepared=prepared,
                child_bundle=MagicMock(),
                agent_name="test-agent",
                instruction="completion test",
                parent_session=_make_mock_session(session_id="parent-ef-6"),
                sub_session_id="child-ef-6",
                orchestrator_config=None,
                parent_messages=None,
                provider_preferences=None,
                self_delegation_depth=0,
                session_manager=manager,
                parent_handle=parent_handle,
            )

        assert result["status"] == "success"
        assert result["turn_count"] == 1
        assert result["metadata"] == {}

    async def test_recursive_spawn_capability_on_child(self):
        """Child session gets its own session.spawn capability."""
        bus = EventBus()
        manager = _make_manager(event_bus=bus)
        parent_handle = _make_parent_handle(session_id="parent-ef-7", event_bus=bus)
        manager._sessions["parent-ef-7"] = parent_handle  # noqa: SLF001

        child_session = _make_child_session(session_id="child-ef-7", parent_id="parent-ef-7")
        prepared = _make_mock_prepared()

        from amplifierd.spawn import _spawn_with_event_forwarding

        with patch("amplifier_core.AmplifierSession", return_value=child_session):
            await _spawn_with_event_forwarding(
                prepared=prepared,
                child_bundle=MagicMock(),
                agent_name="test-agent",
                instruction="recursive test",
                parent_session=_make_mock_session(session_id="parent-ef-7"),
                sub_session_id="child-ef-7",
                orchestrator_config=None,
                parent_messages=None,
                provider_preferences=None,
                self_delegation_depth=0,
                session_manager=manager,
                parent_handle=parent_handle,
            )

        spawn_calls = [
            c
            for c in child_session.coordinator.register_capability.call_args_list
            if c[0][0] == "session.spawn"
        ]
        assert len(spawn_calls) == 1, "Child should have session.spawn capability"

    async def test_child_events_have_correlation_id(self):
        """Events published from child session include a correlation_id."""
        bus = EventBus()
        manager = _make_manager(event_bus=bus)
        parent_handle = _make_parent_handle(session_id="parent-ef-8", event_bus=bus)
        manager._sessions["parent-ef-8"] = parent_handle  # noqa: SLF001

        child_session = _make_child_session(session_id="child-ef-8", parent_id="parent-ef-8")
        prepared = _make_mock_prepared()

        received: list[TransportEvent] = []

        async def _consume() -> None:
            async for event in bus.subscribe(session_id="parent-ef-8"):
                received.append(event)
                if len(received) >= 3:
                    break

        consumer_task = asyncio.create_task(_consume())
        await asyncio.sleep(0.05)

        from amplifierd.spawn import _spawn_with_event_forwarding

        with patch("amplifier_core.AmplifierSession", return_value=child_session):
            await _spawn_with_event_forwarding(
                prepared=prepared,
                child_bundle=MagicMock(),
                agent_name="test-agent",
                instruction="correlation test",
                parent_session=_make_mock_session(session_id="parent-ef-8"),
                sub_session_id="child-ef-8",
                orchestrator_config=None,
                parent_messages=None,
                provider_preferences=None,
                self_delegation_depth=0,
                session_manager=manager,
                parent_handle=parent_handle,
            )

        await asyncio.wait_for(consumer_task, timeout=2.0)

        # SessionHandle.execute sets correlation_id before calling session.execute
        for evt in received:
            assert evt.correlation_id is not None
            assert evt.correlation_id.startswith("prompt_child-ef-8_")


# ------------------------------------------------------------------
# Tests: end-to-end via spawn_fn
# ------------------------------------------------------------------


@pytest.mark.unit
class TestSpawnFnEndToEnd:
    """Test the full spawn_fn path through register_spawn_capability."""

    async def test_spawn_fn_uses_event_forwarding_when_manager_available(self):
        """spawn_fn takes the event-forwarding path when session_manager is set."""
        bus = EventBus()
        manager = _make_manager(event_bus=bus)

        parent_session = _make_mock_session(session_id="parent-e2e-1")
        parent_handle = _make_parent_handle(session_id="parent-e2e-1", event_bus=bus)
        manager._sessions["parent-e2e-1"] = parent_handle  # noqa: SLF001

        child_session = _make_child_session(
            session_id="child-e2e-1",
            parent_id="parent-e2e-1",
            execute_return="e2e-result",
        )
        prepared = _make_mock_prepared()

        from amplifierd.spawn import register_spawn_capability

        register_spawn_capability(
            parent_session,
            prepared,
            "parent-e2e-1",
            session_manager=manager,
            parent_handle=parent_handle,
        )

        spawn_fn = parent_session.coordinator.register_capability.call_args[0][1]

        # Subscribe to parent to verify events flow
        received: list[TransportEvent] = []

        async def _consume() -> None:
            async for event in bus.subscribe(session_id="parent-e2e-1"):
                received.append(event)
                if len(received) >= 3:
                    break

        consumer_task = asyncio.create_task(_consume())
        await asyncio.sleep(0.05)

        with patch("amplifier_core.AmplifierSession", return_value=child_session):
            result = await spawn_fn(
                agent_name="self",
                instruction="e2e test",
                parent_session=parent_session,
            )

        await asyncio.wait_for(consumer_task, timeout=2.0)

        assert result["output"] == "e2e-result"
        assert result["session_id"] == "child-e2e-1"

        # prepared.spawn() should NOT have been called (event-forwarding path used)
        prepared.spawn.assert_not_awaited()

        # Events propagated through EventBus
        event_names = [e.event_name for e in received]
        assert "content_block:start" in event_names
        assert "content_block:delta" in event_names

    async def test_spawn_fn_falls_back_without_manager(self):
        """spawn_fn uses prepared.spawn() when session_manager is None."""
        parent_session = _make_mock_session(session_id="parent-e2e-2")
        prepared = _make_mock_prepared()

        from amplifierd.spawn import register_spawn_capability

        register_spawn_capability(parent_session, prepared, "parent-e2e-2")

        spawn_fn = parent_session.coordinator.register_capability.call_args[0][1]

        result = await spawn_fn(
            agent_name="self",
            instruction="fallback test",
            parent_session=parent_session,
        )

        prepared.spawn.assert_awaited_once()
        assert result == prepared.spawn.return_value


# ------------------------------------------------------------------
# Tests: session_manager.create() passes new params
# ------------------------------------------------------------------


@pytest.mark.unit
class TestSessionManagerSpawnWiring:
    """Verify SessionManager.create() threads self and handle into
    register_spawn_capability."""

    async def test_create_passes_session_manager_and_handle(self):
        """SessionManager.create() passes session_manager=self and
        parent_handle=<created handle> to register_spawn_capability."""
        bus = EventBus()
        manager = _make_manager(event_bus=bus)

        mock_bundle = MagicMock()
        mock_bundle.prepare = AsyncMock()
        mock_prepared = MagicMock()
        mock_bundle.prepare.return_value = mock_prepared

        mock_session = _make_mock_session(session_id="create-test-1")
        mock_prepared.create_session = AsyncMock(return_value=mock_session)

        manager._bundle_registry = MagicMock()  # noqa: SLF001
        manager._bundle_registry.load = AsyncMock(return_value=mock_bundle)  # noqa: SLF001

        # These are lazy-imported inside create(), so patch at the source module.
        with (
            patch("amplifierd.providers.inject_providers"),
            patch("amplifierd.providers.load_provider_config", return_value={}),
            patch("amplifierd.spawn.register_spawn_capability") as mock_register,
        ):
            handle = await manager.create(bundle_name="test-bundle")

        mock_register.assert_called_once()
        call_args = mock_register.call_args
        assert call_args[0][0] is mock_session
        assert call_args[0][1] is mock_prepared
        assert call_args[0][2] == "create-test-1"
        assert call_args[1]["session_manager"] is manager
        assert call_args[1]["parent_handle"] is handle
