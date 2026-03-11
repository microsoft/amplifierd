"""Tests for health and info endpoints."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from amplifierd.app import create_app


@pytest.fixture()
def client() -> TestClient:
    """Create a test client from the app factory."""
    app = create_app()
    return TestClient(app)


def _make_fake_session(session_id: str = "fake-session-1") -> SimpleNamespace:
    """Create a minimal fake AmplifierSession for session creation tests."""
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
def session_client() -> Generator[TestClient]:
    """Test client with lifespan + mocked bundle registry for session creation."""
    app = create_app()
    with TestClient(app) as c:
        mock_registry = _make_mock_registry()
        c.app.state.bundle_registry = mock_registry  # type: ignore[union-attr]
        c.app.state.session_manager._bundle_registry = mock_registry  # type: ignore[union-attr]  # noqa: SLF001
        yield c


@pytest.mark.unit
class TestHealthEndpoint:
    """Tests for GET /health."""

    def test_health_returns_200(self, client: TestClient) -> None:
        """GET /health returns 200 with status=healthy, version, uptime_seconds, active_sessions."""
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert "version" in data
        assert "uptime_seconds" in data
        assert isinstance(data["uptime_seconds"], (int, float))
        assert data["uptime_seconds"] >= 0
        assert "active_sessions" in data
        assert isinstance(data["active_sessions"], int)
        assert data["active_sessions"] >= 0

    def test_health_without_session_manager(self, client: TestClient) -> None:
        """GET /health returns active_sessions=0 when session_manager is None."""
        client.app.state.session_manager = None  # type: ignore[union-attr]
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active_sessions"] == 0

    def test_health_has_rust_engine_field(self, client: TestClient) -> None:
        """GET /health includes rust_engine boolean field."""
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "rust_engine" in data
        assert isinstance(data["rust_engine"], bool)


@pytest.mark.unit
class TestInfoEndpoint:
    """Tests for GET /info."""

    def test_info_returns_200(self, client: TestClient) -> None:
        """GET /info returns 200 with version and amplifier_core_version."""
        resp = client.get("/info")
        assert resp.status_code == 200
        data = resp.json()
        assert "version" in data
        assert "amplifier_core_version" in data

    def test_info_has_capabilities(self, client: TestClient) -> None:
        """GET /info includes capabilities list."""
        resp = client.get("/info")
        data = resp.json()
        assert "capabilities" in data
        caps = data["capabilities"]
        assert isinstance(caps, list)
        for expected in (
            "streaming",
            "approval",
            "cancellation",
            "hot_mount",
            "fork",
            "spawn",
        ):
            assert expected in caps

    def test_info_has_module_types(self, client: TestClient) -> None:
        """GET /info includes module_types list."""
        resp = client.get("/info")
        data = resp.json()
        assert "module_types" in data
        mtypes = data["module_types"]
        assert isinstance(mtypes, list)
        for expected in ("orchestrator", "provider", "tool", "hook", "context", "resolver"):
            assert expected in mtypes


@pytest.mark.unit
class TestOpenAPIEndpoint:
    """Tests for OpenAPI schema availability."""

    def test_openapi_json_returns_200(self, client: TestClient) -> None:
        """GET /openapi.json returns 200 with a valid OpenAPI schema."""
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        schema = resp.json()
        assert "openapi" in schema
        assert schema["openapi"].startswith("3.")
        assert schema["info"]["title"] == "amplifierd"
        assert schema["info"]["version"] == "0.1.0"
        assert "paths" in schema
        assert len(schema["paths"]) > 0

    def test_openapi_includes_all_route_groups(self, client: TestClient) -> None:
        """The OpenAPI schema includes paths for all major route groups."""
        resp = client.get("/openapi.json")
        schema = resp.json()
        paths = list(schema["paths"].keys())
        # Spot-check key endpoints from each route module
        assert "/health" in paths
        assert "/sessions" in paths
        assert "/events" in paths
        assert "/bundles" in paths
        assert "/modules" in paths

    def test_docs_endpoint_returns_200(self, client: TestClient) -> None:
        """GET /docs (Swagger UI) returns 200."""
        resp = client.get("/docs")
        assert resp.status_code == 200

    def test_redoc_endpoint_returns_200(self, client: TestClient) -> None:
        """GET /redoc returns 200."""
        resp = client.get("/redoc")
        assert resp.status_code == 200


@pytest.mark.unit
class TestReadyEndpoint:
    """Tests for GET /ready."""

    def test_ready_returns_false_during_prewarm(self, client: TestClient) -> None:
        """GET /ready returns {ready: false} when bundles_ready event is unset."""
        client.app.state.bundles_ready = asyncio.Event()  # unset = still loading
        resp = client.get("/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ready"] is False

    def test_ready_returns_true_after_prewarm(self, client: TestClient) -> None:
        """GET /ready returns {ready: true} when bundles_ready event is set."""
        event = asyncio.Event()
        event.set()
        client.app.state.bundles_ready = event
        resp = client.get("/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ready"] is True

    def test_ready_returns_error_on_failure(self, client: TestClient) -> None:
        """GET /ready includes error field when prewarm_error is set."""
        client.app.state.bundles_ready = asyncio.Event()  # unset = failed, not ready
        client.app.state.prewarm_error = "Bundle load failed: connection timeout"
        resp = client.get("/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ready"] is False
        assert data["error"] == "Bundle load failed: connection timeout"


@pytest.mark.unit
class TestPrewarmGuard503:
    """Tests for 503 guard on session creation/resume during prewarm."""

    def test_session_creation_returns_503_during_prewarm(self, session_client: TestClient) -> None:
        """POST /sessions returns 503 with Retry-After when bundles are still loading."""
        session_client.app.state.bundles_ready = asyncio.Event()  # unset = loading
        resp = session_client.post("/sessions", json={"bundle_name": "test-bundle"})
        assert resp.status_code == 503
        assert "Retry-After" in resp.headers

    def test_session_creation_works_after_prewarm(self, session_client: TestClient) -> None:
        """POST /sessions proceeds normally when bundles_ready event is set."""
        event = asyncio.Event()
        event.set()
        session_client.app.state.bundles_ready = event
        resp = session_client.post("/sessions", json={"bundle_name": "test-bundle"})
        assert resp.status_code == 201


@pytest.mark.unit
class TestPrewarmFunction:
    """Tests for the prewarm() background task (public API)."""

    async def test_prewarm_failure_releases_bundles_ready(self) -> None:
        """prewarm() sets bundles_ready even when bundle loading fails.

        Without this, the 503 guard permanently blocks ALL session creation
        after a prewarm failure. Users should still be able to reach the
        wizard or retry via POST /ready/retry.
        """
        from fastapi import FastAPI

        from amplifierd.app import prewarm  # public name after rename

        app = FastAPI()
        app.state.bundles_ready = asyncio.Event()
        app.state.prewarm_error = None

        mock_registry = MagicMock()
        mock_registry.load = AsyncMock(side_effect=RuntimeError("Network failure"))
        app.state.bundle_registry = mock_registry

        app.state.settings = SimpleNamespace(default_bundle="my-bundle")

        await prewarm(app)

        # The 503 guard MUST release even after failure
        assert app.state.bundles_ready.is_set(), (
            "bundles_ready must be set after prewarm failure "
            "to prevent the 503 guard permanently blocking session creation"
        )
        # Error must be captured so GET /ready can surface it
        assert app.state.prewarm_error == "Network failure"
