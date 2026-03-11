"""Tests for SessionManager — the central session registry."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from amplifierd.config import DaemonSettings
from amplifierd.state.event_bus import EventBus
from amplifierd.state.session_manager import SessionManager


class TestSessionManager:
    @pytest.fixture
    def bus(self) -> EventBus:
        return EventBus()

    @pytest.fixture
    def settings(self) -> DaemonSettings:
        return DaemonSettings()

    @pytest.fixture
    def manager(self, bus: EventBus, settings: DaemonSettings) -> SessionManager:
        return SessionManager(event_bus=bus, settings=settings)

    def test_initially_empty(self, manager: SessionManager) -> None:
        assert manager.list_sessions() == []

    def test_get_nonexistent(self, manager: SessionManager) -> None:
        assert manager.get("nonexistent") is None

    async def test_register_and_get(self, manager: SessionManager) -> None:
        """Register a pre-built SessionHandle and retrieve it."""
        mock_session = MagicMock()
        mock_session.session_id = "test-123"
        mock_session.parent_id = None

        handle = manager.register(
            session=mock_session,
            prepared_bundle=None,
            bundle_name="test-bundle",
        )
        assert handle.session_id == "test-123"
        assert manager.get("test-123") is handle

    async def test_destroy(self, manager: SessionManager) -> None:
        mock_session = MagicMock()
        mock_session.session_id = "to-destroy"
        mock_session.parent_id = None
        mock_session.cleanup = AsyncMock()

        manager.register(
            session=mock_session,
            prepared_bundle=None,
            bundle_name="test-bundle",
        )
        assert manager.get("to-destroy") is not None
        await manager.destroy("to-destroy")
        assert manager.get("to-destroy") is None

    async def test_list_sessions(self, manager: SessionManager) -> None:
        for i in range(3):
            mock = MagicMock()
            mock.session_id = f"session-{i}"
            mock.parent_id = None
            manager.register(session=mock, prepared_bundle=None, bundle_name="b")

        sessions = manager.list_sessions()
        assert len(sessions) == 3
        ids = {s["session_id"] for s in sessions}
        assert ids == {"session-0", "session-1", "session-2"}


def _make_fake_session(session_id: str = "cwd-test-1") -> MagicMock:
    """Minimal fake session for create/resume tests."""
    session = MagicMock()
    session.session_id = session_id
    session.parent_id = None
    session.cleanup = AsyncMock()
    session.coordinator = MagicMock()
    session.coordinator.hooks = MagicMock()
    return session


def _make_mock_registry(session: MagicMock | None = None) -> tuple[MagicMock, MagicMock]:
    """Return (registry, mock_prepared) so tests can inspect create_session calls."""
    if session is None:
        session = _make_fake_session()
    mock_prepared = MagicMock()
    mock_prepared.create_session = AsyncMock(return_value=session)
    mock_bundle = MagicMock()
    mock_bundle.prepare = AsyncMock(return_value=mock_prepared)
    mock_bundle.raw = {}
    mock_registry = MagicMock()
    mock_registry.load = AsyncMock(return_value=mock_bundle)
    return mock_registry, mock_prepared


class TestCreatePassesSessionCwd:
    """SessionManager.create() must forward working_dir as session_cwd."""

    async def test_create_passes_session_cwd(self, tmp_path: Path) -> None:
        registry, mock_prepared = _make_mock_registry()
        bus = EventBus()
        settings = DaemonSettings()
        manager = SessionManager(event_bus=bus, settings=settings)
        manager._bundle_registry = registry  # noqa: SLF001

        working_dir = str(tmp_path / "my-project")

        with (
            patch("amplifierd.providers.load_provider_config", return_value=[]),
            patch("amplifierd.providers.inject_providers"),
        ):
            await manager.create(bundle_name="test", working_dir=working_dir)

        mock_prepared.create_session.assert_awaited_once()
        call_kwargs = mock_prepared.create_session.call_args.kwargs
        assert "session_cwd" in call_kwargs, "session_cwd not passed to create_session"
        assert call_kwargs["session_cwd"] == Path(working_dir)


class TestResumePassesSessionCwd:
    """SessionManager.resume() must forward working_dir as session_cwd."""

    async def test_resume_passes_session_cwd(self, tmp_path: Path) -> None:
        session_id = "resume-cwd-test"
        fake_session = _make_fake_session(session_id)

        # Set up a context mock that resume() will try to inject transcript into
        mock_context = AsyncMock()
        mock_context.get_messages = AsyncMock(return_value=[])
        mock_context.set_messages = AsyncMock()
        fake_session.coordinator.get = MagicMock(return_value=mock_context)

        registry, mock_prepared = _make_mock_registry(fake_session)
        bus = EventBus()
        settings = DaemonSettings()

        # Create the session dir structure that resume() expects
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "my-project"
        sessions_dir = project_dir / "sessions"
        session_dir = sessions_dir / session_id
        session_dir.mkdir(parents=True)

        # Write transcript and metadata files
        (session_dir / "transcript.jsonl").write_text("")
        working_dir = "/Users/test/my-project"
        (session_dir / "metadata.json").write_text(
            json.dumps({"bundle": "test-bundle", "working_dir": working_dir})
        )

        manager = SessionManager(event_bus=bus, settings=settings, projects_dir=projects_dir)
        manager._bundle_registry = registry  # noqa: SLF001

        with (
            patch("amplifierd.providers.load_provider_config", return_value=[]),
            patch("amplifierd.providers.inject_providers"),
            patch("amplifierd.persistence.register_persistence_hooks"),
        ):
            await manager.resume(session_id)

        mock_prepared.create_session.assert_awaited_once()
        call_kwargs = mock_prepared.create_session.call_args.kwargs
        assert "session_cwd" in call_kwargs, "session_cwd not passed to create_session"
        assert call_kwargs["session_cwd"] == Path(working_dir)
        assert call_kwargs["session_id"] == session_id
        assert call_kwargs["is_resumed"] is True


class TestSessionManagerCreateWithPreparedBundle:
    """Tests for the fast-path prepared_bundle kwarg in SessionManager.create()."""

    @pytest.fixture
    def manager_with_registry(self) -> SessionManager:
        mock_registry = MagicMock()
        mock_registry.load = AsyncMock()
        return SessionManager(
            event_bus=EventBus(),
            settings=DaemonSettings(),
            bundle_registry=mock_registry,
        )

    async def test_create_with_prepared_bundle_skips_registry_load(
        self, manager_with_registry: SessionManager
    ) -> None:
        """create() with prepared_bundle skips registry.load() entirely."""
        fake_session = MagicMock()
        fake_session.session_id = "fast-session-1"
        fake_session.parent_id = None

        mock_prepared = MagicMock()
        mock_prepared.create_session = AsyncMock(return_value=fake_session)

        handle = await manager_with_registry.create(prepared_bundle=mock_prepared)

        # registry.load() must NOT be called — we skipped the slow path
        manager_with_registry._bundle_registry.load.assert_not_called()  # noqa: SLF001
        assert handle.session_id == "fast-session-1"

    async def test_create_with_prepared_bundle_calls_create_session(
        self, manager_with_registry: SessionManager
    ) -> None:
        """create() with prepared_bundle calls create_session() on the PreparedBundle."""
        fake_session = MagicMock()
        fake_session.session_id = "fast-session-2"
        fake_session.parent_id = None

        mock_prepared = MagicMock()
        mock_prepared.create_session = AsyncMock(return_value=fake_session)

        await manager_with_registry.create(prepared_bundle=mock_prepared)

        mock_prepared.create_session.assert_awaited_once()

    async def test_create_without_bundle_info_raises_mentioning_prepared_bundle(
        self, manager_with_registry: SessionManager
    ) -> None:
        """create() error message mentions prepared_bundle as a third option."""
        with pytest.raises(ValueError, match="prepared_bundle"):
            await manager_with_registry.create()
