"""Tests for the global SSE events endpoint."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock

from amplifierd.app import create_app
from amplifierd.routes.events import _event_generator
from amplifierd.state.event_bus import EventBus
from amplifierd.state.transport_event import TransportEvent


@pytest.mark.unit
class TestEventGenerator:
    """Tests for the _event_generator async generator."""

    async def test_published_event_received_via_generator(self) -> None:
        """An event published after 50ms delay is received by _event_generator."""
        event_bus = EventBus()

        async def publish_after_delay() -> None:
            await asyncio.sleep(0.05)
            event_bus.publish(
                session_id="s1",
                event_name="test:event",
                data={"hello": "world"},
            )

        # Mock request with is_disconnected() returning False
        mock_request = MagicMock()
        mock_request.is_disconnected = AsyncMock(return_value=False)

        task = asyncio.create_task(publish_after_delay())
        gen = _event_generator(event_bus, request=mock_request)
        sse_chunk = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
        await gen.aclose()
        await task

        # Verify SSE format: 'id: {id}\nevent: {name}\ndata: {json}\n\n'
        assert sse_chunk.startswith("id: 1\nevent: test:event\n")
        assert "data: " in sse_chunk
        assert sse_chunk.endswith("\n\n")

        # Verify the data line is valid JSON containing our payload
        lines = sse_chunk.strip().split("\n")
        data_line = next(line for line in lines if line.startswith("data: "))
        payload = json.loads(data_line[len("data: ") :])
        assert payload["data"]["hello"] == "world"
        assert payload["event"] == "test:event"
        assert payload["session_id"] == "s1"


@pytest.mark.unit
class TestEventsEndpoint:
    """Tests for GET /events SSE streaming endpoint."""

    def test_events_stream_returns_200_with_correct_headers(self) -> None:
        """GET /events returns 200 streaming response with SSE headers.

        The monkeypatch must happen INSIDE the TestClient context (after
        lifespan startup), because ``_lifespan`` unconditionally overwrites
        ``app.state.event_bus`` with a fresh ``EventBus()`` during startup.
        Patching before the context means the patch lands on a stale instance
        that the handler never touches, so the real ``subscribe()`` runs — an
        infinite ``while True: await queue.get()`` — causing the test to hang.

        The streaming API (``client.stream``) is used instead of
        ``client.get`` so the test can assert headers immediately and drain
        body chunks via ``iter_text()`` without waiting for the TCP connection
        to close.  The stream terminates naturally once ``bounded_subscribe``
        exhausts and ``StreamingResponse`` sends ``more_body=False``.
        """
        app = create_app()

        # Create a test event to be yielded by the bounded subscribe
        test_event = TransportEvent(
            event_name="test:event",
            data={"hello": "world"},
            session_id="s1",
            timestamp="2024-01-01T00:00:00+00:00",
            sequence=1,
        )

        async def bounded_subscribe(
            session_id: str | None = None,
            filter_patterns: list[str] | None = None,
        ):  # type: ignore[override]
            yield test_event

        with TestClient(app) as client:
            # Lifespan has now run: app.state.event_bus is the live EventBus
            # created by _lifespan.  Patch THAT instance so the handler picks
            # up bounded_subscribe when it calls event_bus.subscribe(...).
            live_event_bus: EventBus = app.state.event_bus
            original_subscribe = live_event_bus.subscribe
            live_event_bus.subscribe = bounded_subscribe  # type: ignore[assignment]

            try:
                with client.stream("GET", "/events") as response:
                    assert response.status_code == 200
                    assert "text/event-stream" in response.headers["content-type"]
                    assert response.headers["cache-control"] == "no-cache"
                    assert response.headers["x-accel-buffering"] == "no"

                    # Drain the stream — terminates when bounded_subscribe exhausts
                    body = "".join(response.iter_text())

                assert "event: test:event" in body
                assert "data: " in body
                assert '"hello": "world"' in body or '"hello":"world"' in body
            finally:
                live_event_bus.subscribe = original_subscribe  # type: ignore[assignment]

    def test_events_router_registered_in_app(self) -> None:
        """The events router is registered and /events route exists."""
        app = create_app()
        route_paths = [route.path for route in app.routes]  # type: ignore[union-attr]
        assert "/events" in route_paths
