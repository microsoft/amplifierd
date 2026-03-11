"""Tests for app.py lifespan behaviour."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport

from amplifierd.app import create_app
from amplifierd.config import DaemonSettings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_bundle() -> SimpleNamespace:
    """Minimal bundle stub that satisfies _prewarm."""
    return SimpleNamespace(
        name="test-bundle",
        providers=[],
        prepare=AsyncMock(return_value=None),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_server_accepts_connections_before_bundle_load() -> None:
    """Server should respond to /health while bundle is still loading.

    With the old lifespan the startup blocked at ``await registry.load()``
    before yielding, so the HTTP server never became reachable until the
    load finished.  After the restructure the lifespan yields immediately
    and load runs in a background task, so /health is immediately reachable.
    """
    load_started: asyncio.Event = asyncio.Event()
    load_gate: asyncio.Event = asyncio.Event()

    async def blocking_load(source: str) -> SimpleNamespace:
        load_started.set()
        await load_gate.wait()
        return _make_fake_bundle()

    mock_registry = MagicMock()
    mock_registry.load = blocking_load
    mock_registry.register = MagicMock()

    settings = DaemonSettings(default_bundle="test-bundle")
    app = create_app(settings=settings)

    with patch("amplifier_foundation.BundleRegistry", return_value=mock_registry):
        # The whole interaction must complete within 5 s.
        # With the OLD lifespan (blocking load before yield) the lifespan
        # context manager would block indefinitely and we'd never enter the
        # body — causing the outer timeout to fire.
        async with asyncio.timeout(5.0):
            async with app.router.lifespan_context(app):
                # Prewarm must have started before we check health — proves
                # load is running in the background, not in the startup path.
                await asyncio.wait_for(load_started.wait(), timeout=2.0)

                async with httpx.AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    resp = await client.get("/health")
                    assert resp.status_code == 200

                # Let _prewarm finish so the app shuts down cleanly.
                load_gate.set()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bundles_ready_event_set_after_successful_prewarm() -> None:
    """app.state.bundles_ready is set once _prewarm completes successfully."""

    async def instant_load(source: str) -> SimpleNamespace:
        return _make_fake_bundle()

    mock_registry = MagicMock()
    mock_registry.load = instant_load
    mock_registry.register = MagicMock()

    settings = DaemonSettings(default_bundle="test-bundle")
    app = create_app(settings=settings)

    with patch("amplifier_foundation.BundleRegistry", return_value=mock_registry):
        async with asyncio.timeout(5.0):
            async with app.router.lifespan_context(app):
                await asyncio.wait_for(app.state.bundles_ready.wait(), timeout=3.0)

                async with httpx.AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    resp = await client.get("/health")
                    assert resp.status_code == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_prewarm_error_stored_in_app_state() -> None:
    """When _prewarm fails the error message is stored in app.state.prewarm_error."""

    async def failing_load(source: str) -> None:
        raise RuntimeError("registry exploded")

    mock_registry = MagicMock()
    mock_registry.load = failing_load
    mock_registry.register = MagicMock()

    settings = DaemonSettings(default_bundle="test-bundle")
    app = create_app(settings=settings)

    with patch("amplifier_foundation.BundleRegistry", return_value=mock_registry):
        async with asyncio.timeout(5.0):
            async with app.router.lifespan_context(app):
                # Give the background task a moment to fail.
                await asyncio.sleep(0.1)

                async with httpx.AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    # Server still healthy even when prewarm fails.
                    resp = await client.get("/health")
                    assert resp.status_code == 200

                # Error captured on app.state.
                assert app.state.prewarm_error is not None
                assert "registry exploded" in app.state.prewarm_error


@pytest.mark.unit
@pytest.mark.asyncio
async def test_prewarm_stores_prepared_bundle_on_session_manager() -> None:
    """_prewarm stores the PreparedBundle result on session_manager._prepared_bundles.

    After prewarm completes, session_manager._prepared_bundles[default_bundle] should
    hold the PreparedBundle returned by bundle.prepare() so subsequent session
    creation can reuse it instead of calling prepare() again.
    """
    fake_prepared = MagicMock(name="fake_prepared_bundle")
    fake_prepared.create_session = AsyncMock()

    async def instant_load(source: str) -> SimpleNamespace:
        bundle = _make_fake_bundle()
        bundle.prepare = AsyncMock(return_value=fake_prepared)
        return bundle

    mock_registry = MagicMock()
    mock_registry.load = instant_load
    mock_registry.register = MagicMock()

    settings = DaemonSettings(default_bundle="test-bundle")
    app = create_app(settings=settings)

    with patch("amplifier_foundation.BundleRegistry", return_value=mock_registry):
        async with asyncio.timeout(5.0):
            async with app.router.lifespan_context(app):
                await asyncio.wait_for(app.state.bundles_ready.wait(), timeout=3.0)

                cached = app.state.session_manager._prepared_bundles.get("test-bundle")  # noqa: SLF001
                assert cached is fake_prepared, (
                    "session_manager._prepared_bundles['test-bundle'] should be the "
                    "PreparedBundle returned by prepare()"
                )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_prepared_bundle_cache_empty_before_prewarm() -> None:
    """session_manager._prepared_bundles is empty at lifespan startup.

    Before prewarm has a chance to populate it, the cache should be empty.
    """
    load_gate: asyncio.Event = asyncio.Event()

    async def blocking_load(source: str) -> SimpleNamespace:
        await load_gate.wait()
        return _make_fake_bundle()

    mock_registry = MagicMock()
    mock_registry.load = blocking_load
    mock_registry.register = MagicMock()

    settings = DaemonSettings(default_bundle="test-bundle")
    app = create_app(settings=settings)

    with patch("amplifier_foundation.BundleRegistry", return_value=mock_registry):
        async with asyncio.timeout(5.0):
            async with app.router.lifespan_context(app):
                # Before prewarm completes, cache should be empty
                assert app.state.session_manager._prepared_bundles == {}  # noqa: SLF001

                load_gate.set()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_prewarm_skipped_when_no_default_bundle() -> None:
    """When no default_bundle is configured bundles_ready is set immediately."""
    mock_registry = MagicMock()
    mock_registry.register = MagicMock()

    settings = DaemonSettings(default_bundle=None)  # explicitly no default bundle
    app = create_app(settings=settings)

    with patch("amplifier_foundation.BundleRegistry", return_value=mock_registry):
        async with asyncio.timeout(5.0):
            async with app.router.lifespan_context(app):
                await asyncio.wait_for(app.state.bundles_ready.wait(), timeout=2.0)

                async with httpx.AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    resp = await client.get("/health")
                    assert resp.status_code == 200
