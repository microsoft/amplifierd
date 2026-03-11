"""Tests for POST /sessions — session creation endpoint."""

from __future__ import annotations

from collections.abc import Generator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from amplifierd.app import create_app


def _make_fake_session(session_id: str = "fake-session-1") -> SimpleNamespace:
    """Create a minimal fake AmplifierSession for testing."""
    fake_coordinator = SimpleNamespace(
        request_cancel=lambda immediate: None,
        hooks=MagicMock(),
    )
    return SimpleNamespace(
        session_id=session_id,
        parent_id=None,
        coordinator=fake_coordinator,
        cleanup=AsyncMock(),
        execute=AsyncMock(return_value="ok"),
    )


def _make_mock_registry(session_id: str = "fake-session-1") -> MagicMock:
    """Create a mock BundleRegistry that returns fake sessions."""
    fake_session = _make_fake_session(session_id)
    mock_prepared = MagicMock()
    mock_prepared.create_session = AsyncMock(return_value=fake_session)
    mock_bundle = MagicMock()
    mock_bundle.prepare = AsyncMock(return_value=mock_prepared)
    mock_registry = MagicMock()
    mock_registry.load = AsyncMock(return_value=mock_bundle)
    return mock_registry


@pytest.fixture()
def client() -> Generator[TestClient]:
    """Test client with mocked bundle registry for session creation.

    The mock is applied AFTER the lifespan runs (inside the context manager)
    so it overrides the real BundleRegistry that lifespan creates.
    """
    app = create_app()
    with TestClient(app) as c:
        # Override after lifespan so mock isn't replaced by the real registry
        mock_registry = _make_mock_registry()
        c.app.state.bundle_registry = mock_registry  # type: ignore[union-attr]
        c.app.state.session_manager._bundle_registry = mock_registry  # type: ignore[union-attr]  # noqa: SLF001
        # Mark bundles as ready so the 503 prewarm guard does not block tests
        c.app.state.bundles_ready.set()  # type: ignore[union-attr]
        yield c


@pytest.mark.unit
class TestCreateSessionEndpoint:
    """Tests for POST /sessions."""

    def test_create_session_returns_201(self, client: TestClient) -> None:
        """POST /sessions returns 201 with session_id and idle status."""
        resp = client.post("/sessions", json={"bundle_name": "test-bundle"})
        assert resp.status_code == 201
        data = resp.json()
        assert "session_id" in data
        assert data["status"] == "idle"

    def test_create_session_appears_in_list(self, client: TestClient) -> None:
        """Session created via POST /sessions appears in GET /sessions list."""
        create_resp = client.post("/sessions", json={"bundle_name": "test-bundle"})
        assert create_resp.status_code == 201
        sid = create_resp.json()["session_id"]
        list_resp = client.get("/sessions")
        assert list_resp.status_code == 200
        ids = [s["session_id"] for s in list_resp.json()["sessions"]]
        assert sid in ids

    def test_create_session_response_has_required_fields(self, client: TestClient) -> None:
        """POST /sessions response contains bundle_name, working_dir, created_at."""
        resp = client.post("/sessions", json={"bundle_name": "test-bundle"})
        assert resp.status_code == 201
        data = resp.json()
        assert "session_id" in data
        assert "status" in data
        assert "bundle_name" in data
        assert "working_dir" in data
        assert "created_at" in data

    def test_create_session_with_bundle_uri(self, client: TestClient) -> None:
        """POST /sessions with bundle_uri creates a session successfully."""
        resp = client.post("/sessions", json={"bundle_uri": "git+https://example.com/bundle"})
        assert resp.status_code == 201
        data = resp.json()
        assert "session_id" in data
        assert data["status"] == "idle"

    def test_create_session_no_registry_returns_503(self, client: TestClient) -> None:
        """POST /sessions returns 503 when bundle registry is unavailable."""
        # Set bundle_registry to None after lifespan so the route sees it missing
        client.app.state.bundle_registry = None  # type: ignore[union-attr]
        resp = client.post("/sessions", json={"bundle_name": "test-bundle"})
        assert resp.status_code == 503

    def test_create_session_no_bundle_uses_default(self, client: TestClient) -> None:
        """POST /sessions with no bundle uses the configured default_bundle."""
        resp = client.post("/sessions", json={})
        assert resp.status_code == 201
        data = resp.json()
        assert "session_id" in data
        assert data["bundle_name"] == "distro"

    def test_create_session_no_bundle_no_default_returns_400(self, client: TestClient) -> None:
        """POST /sessions returns 400 when no bundle specified and no default configured."""
        client.app.state.settings.default_bundle = None  # type: ignore[union-attr]
        resp = client.post("/sessions", json={})
        assert resp.status_code == 400

    def test_create_session_uses_prewarmed_bundle_skips_registry_load(
        self, client: TestClient
    ) -> None:
        """POST /sessions skips registry.load() when session_manager cache is populated.

        When session_manager has a cached PreparedBundle for the default bundle,
        manager.create() should skip the expensive registry.load() + bundle.prepare() pipeline.
        """
        # Build a fake prepared bundle that creates a new session
        fake_session = _make_fake_session("prewarmed-session-1")
        mock_prepared = MagicMock()
        mock_prepared.create_session = AsyncMock(return_value=fake_session)

        # Place it in the session_manager cache as if prewarm already ran
        session_manager = client.app.state.session_manager  # type: ignore[union-attr]
        session_manager.set_prepared_bundle("distro", mock_prepared)

        # Reset the registry's load call count
        mock_registry = client.app.state.bundle_registry  # type: ignore[union-attr]
        mock_registry.load.reset_mock()

        # POST with no bundle → should use default bundle → should use prewarmed bundle
        resp = client.post("/sessions", json={})
        assert resp.status_code == 201

        # registry.load() must NOT have been called — we took the fast path
        mock_registry.load.assert_not_called()
