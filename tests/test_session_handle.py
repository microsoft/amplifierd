"""Tests for SessionHandle – per-session state wrapper."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from amplifierd.state.event_bus import EventBus
from amplifierd.state.session_handle import SessionHandle, SessionStatus


def _make_mock_session(session_id: str = "sess-1", parent_id: str | None = None) -> MagicMock:
    """Create a mock AmplifierSession with the attributes SessionHandle expects.

    The mock's ``execute`` returns ``"result-ok"`` by default; tests that need
    a different return value or side-effect should override it after creation.
    """
    session = MagicMock()
    session.session_id = session_id
    session.parent_id = parent_id
    session.execute = AsyncMock(return_value="result-ok")
    session.cleanup = AsyncMock()
    session.coordinator = MagicMock()
    session.coordinator.request_cancel = AsyncMock()
    return session


@pytest.mark.unit
class TestSessionHandle:
    """Verify SessionHandle state management, execution serialisation, and children tracking."""

    def test_initial_status(self):
        """SessionHandle starts IDLE with correct session_id and not stale."""
        bus = EventBus()
        mock_session = _make_mock_session(session_id="sess-abc")

        handle = SessionHandle(
            session=mock_session,
            prepared_bundle=None,
            bundle_name="test-bundle",
            event_bus=bus,
            working_dir="/tmp/test",
        )

        assert handle.status == SessionStatus.IDLE
        assert handle.session_id == "sess-abc"
        assert handle.stale is False
        assert handle.turn_count == 0
        assert handle.bundle_name == "test-bundle"
        assert handle.working_dir == "/tmp/test"
        assert handle.children == {}
        assert handle.session is mock_session

    async def test_execute_sets_status(self):
        """execute() returns the session result and leaves status back to IDLE."""
        bus = EventBus()
        mock_session = _make_mock_session(session_id="sess-exec")
        mock_session.execute = AsyncMock(return_value="hello-world")

        handle = SessionHandle(
            session=mock_session,
            prepared_bundle=None,
            bundle_name="exec-bundle",
            event_bus=bus,
            working_dir=None,
        )

        result = await handle.execute("test prompt")

        assert result == "hello-world"
        assert handle.status == SessionStatus.IDLE
        mock_session.execute.assert_awaited_once_with("test prompt")

    def test_mark_stale(self):
        """mark_stale() sets the stale flag to True."""
        bus = EventBus()
        mock_session = _make_mock_session()

        handle = SessionHandle(
            session=mock_session,
            prepared_bundle=None,
            bundle_name="stale-bundle",
            event_bus=bus,
            working_dir=None,
        )

        assert handle.stale is False
        handle.mark_stale()
        assert handle.stale is True

    def test_children_tracking(self):
        """register_child() updates children dict and calls event_bus.register_child()."""
        bus = EventBus()
        mock_session = _make_mock_session(session_id="parent-1")

        handle = SessionHandle(
            session=mock_session,
            prepared_bundle=None,
            bundle_name="parent-bundle",
            event_bus=bus,
            working_dir=None,
        )

        handle.register_child("child-sess-1", "code-reviewer")
        handle.register_child("child-sess-2", "explorer")

        children = handle.children
        assert children == {
            "child-sess-1": "code-reviewer",
            "child-sess-2": "explorer",
        }
        # Verify it's a copy, not a reference
        children["child-sess-3"] = "hacker"
        assert "child-sess-3" not in handle.children

        # Verify event_bus.register_child was called
        assert "child-sess-1" in bus.get_descendants("parent-1")
        assert "child-sess-2" in bus.get_descendants("parent-1")

    async def test_turn_counter(self):
        """Turn counter increments on each execute() call."""
        bus = EventBus()
        mock_session = _make_mock_session(session_id="sess-turns")
        mock_session.execute = AsyncMock(return_value="ok")

        handle = SessionHandle(
            session=mock_session,
            prepared_bundle=None,
            bundle_name="counter-bundle",
            event_bus=bus,
            working_dir=None,
        )

        assert handle.turn_count == 0

        await handle.execute("prompt-1")
        assert handle.turn_count == 1

        await handle.execute("prompt-2")
        assert handle.turn_count == 2

        await handle.execute("prompt-3")
        assert handle.turn_count == 3

        # Verify correlation_id format after 3 executions
        assert handle.correlation_id == "prompt_sess-turns_3"

    async def test_cancel_delegates_to_coordinator(self):
        """cancel() forwards the immediate flag to session.coordinator.request_cancel()."""
        bus = EventBus()
        mock_session = _make_mock_session(session_id="sess-cancel")

        handle = SessionHandle(
            session=mock_session,
            prepared_bundle=None,
            bundle_name="cancel-bundle",
            event_bus=bus,
            working_dir=None,
        )

        await handle.cancel(immediate=False)
        mock_session.coordinator.request_cancel.assert_called_once_with(False)

        mock_session.coordinator.request_cancel.reset_mock()
        await handle.cancel(immediate=True)
        mock_session.coordinator.request_cancel.assert_called_once_with(True)

    async def test_cleanup_sets_completed_status(self):
        """cleanup() calls session.cleanup() and transitions to COMPLETED."""
        bus = EventBus()
        mock_session = _make_mock_session(session_id="sess-clean")

        handle = SessionHandle(
            session=mock_session,
            prepared_bundle=None,
            bundle_name="clean-bundle",
            event_bus=bus,
            working_dir=None,
        )

        await handle.cleanup()

        assert handle.status == SessionStatus.COMPLETED
        mock_session.cleanup.assert_awaited_once()

    async def test_cleanup_sets_completed_on_cancelled_error(self):
        """cleanup() sets COMPLETED even when CancelledError bypasses except Exception.

        Regression test: asyncio.CancelledError is BaseException and bypasses
        ``except Exception:``. The status assignment must be in a ``finally:``
        block to guarantee COMPLETED regardless of exception type.
        """
        import asyncio

        bus = EventBus()
        mock_session = _make_mock_session(session_id="sess-clean-cancel")

        async def _raise_cancelled() -> None:
            raise asyncio.CancelledError()

        mock_session.cleanup = AsyncMock(side_effect=_raise_cancelled)

        handle = SessionHandle(
            session=mock_session,
            prepared_bundle=None,
            bundle_name="clean-cancel-bundle",
            event_bus=bus,
            working_dir=None,
        )

        with pytest.raises(asyncio.CancelledError):
            await handle.cleanup()

        assert handle.status == SessionStatus.COMPLETED, (
            "CancelledError during cleanup must not prevent COMPLETED status"
        )

    async def test_cleanup_logs_warning_on_error(self, caplog):
        """cleanup() catches exceptions, logs a warning, and still sets COMPLETED."""
        bus = EventBus()
        mock_session = _make_mock_session(session_id="sess-clean-err")
        mock_session.cleanup = AsyncMock(side_effect=RuntimeError("cleanup failed"))

        handle = SessionHandle(
            session=mock_session,
            prepared_bundle=None,
            bundle_name="clean-err-bundle",
            event_bus=bus,
            working_dir=None,
        )

        with caplog.at_level(logging.WARNING, logger="amplifierd.state.session_handle"):
            await handle.cleanup()

        assert handle.status == SessionStatus.COMPLETED
        mock_session.cleanup.assert_awaited_once()
        assert any(
            "sess-clean-err" in record.message and record.levelno == logging.WARNING
            for record in caplog.records
        )

    def test_repr_includes_key_state(self):
        """__repr__ includes session_id, status, and turn_count for debugging."""
        bus = EventBus()
        mock_session = _make_mock_session(session_id="sess-abc")

        handle = SessionHandle(
            session=mock_session,
            prepared_bundle=None,
            bundle_name="test-bundle",
            event_bus=bus,
            working_dir="/tmp/test",
        )

        r = repr(handle)
        assert "sess-abc" in r
        assert "IDLE" in r or "idle" in r
        assert "turns=0" in r

    async def test_cancelled_error_sets_failed_status(self):
        """CancelledError (BaseException) must transition status to FAILED, not leave EXECUTING.

        Regression test: asyncio.CancelledError is a BaseException in Python 3.9+,
        so it bypasses ``except Exception:``. Without the finally-block catch-all,
        _status gets stuck at EXECUTING and the session permanently rejects new prompts.

        See: fix/cancelled-error-status-recovery
        """
        import asyncio

        bus = EventBus()
        mock_session = _make_mock_session(session_id="sess-cancel-err")

        async def _raise_cancelled(prompt: str) -> str:
            raise asyncio.CancelledError()

        mock_session.execute = AsyncMock(side_effect=_raise_cancelled)

        handle = SessionHandle(
            session=mock_session,
            prepared_bundle=None,
            bundle_name="cancel-err-bundle",
            event_bus=bus,
            working_dir=None,
        )

        activity_before = handle.last_activity

        with pytest.raises(asyncio.CancelledError):
            await handle.execute("doomed prompt")

        assert handle.status == SessionStatus.FAILED, (
            "CancelledError must not leave status stuck at EXECUTING"
        )
        assert handle.turn_count == 1
        assert handle.last_activity >= activity_before

        # Crucially: the lock must be released so the session can accept new prompts
        assert not handle._execute_lock.locked(), (
            "Lock must be released after CancelledError"
        )

    async def test_execute_failure_sets_failed_status(self):
        """execute() sets FAILED status on exception and still updates last_activity."""
        bus = EventBus()
        mock_session = _make_mock_session(session_id="sess-fail")
        mock_session.execute = AsyncMock(side_effect=RuntimeError("boom"))

        handle = SessionHandle(
            session=mock_session,
            prepared_bundle=None,
            bundle_name="fail-bundle",
            event_bus=bus,
            working_dir=None,
        )

        activity_before = handle.last_activity

        with pytest.raises(RuntimeError, match="boom"):
            await handle.execute("bad prompt")

        assert handle.status == SessionStatus.FAILED
        assert handle.turn_count == 1
        assert handle.last_activity >= activity_before

    async def test_execute_uses_asyncio_lock(self):
        """execute() uses an asyncio.Lock (not just status flag) for serialisation.

        _execute_lock must exist as an asyncio.Lock, be acquired while executing,
        and cause a RuntimeError when a second concurrent call is attempted while
        the first holds the lock.
        """
        import asyncio

        bus = EventBus()
        mock_session = _make_mock_session(session_id="sess-lock")

        gate = asyncio.Event()

        async def gated_execute(prompt: str) -> str:
            await gate.wait()
            return "done"

        mock_session.execute = AsyncMock(side_effect=gated_execute)

        handle = SessionHandle(
            session=mock_session,
            prepared_bundle=None,
            bundle_name="lock-bundle",
            event_bus=bus,
            working_dir=None,
        )

        # _execute_lock must exist and be an asyncio.Lock
        assert hasattr(handle, "_execute_lock"), "_execute_lock attribute missing"
        assert isinstance(handle._execute_lock, asyncio.Lock), "_execute_lock must be asyncio.Lock"

        # Start first execute in background; it will block at gate.wait()
        task1 = asyncio.create_task(handle.execute("prompt-1"))
        await asyncio.sleep(0)  # yield so task1 acquires the lock

        # Lock must be held while first execute is in-flight
        assert handle._execute_lock.locked(), "Lock should be held during execution"

        # Second concurrent call must raise immediately (lock is held)
        with pytest.raises(RuntimeError, match="already executing"):
            await handle.execute("prompt-2")

        # Release the gate and let task1 finish
        gate.set()
        result = await task1
        assert result == "done"

        # Lock must be released after completion
        assert not handle._execute_lock.locked(), "Lock should be released after execute()"
