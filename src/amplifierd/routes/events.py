"""Global SSE events endpoint for real-time event streaming."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Request
from starlette.responses import StreamingResponse

from amplifierd.state.event_bus import EventBus

logger = logging.getLogger(__name__)

events_router = APIRouter(tags=["events"])


async def _event_generator(
    event_bus: EventBus,
    request: Request,
    session_id: str | None = None,
    filter_patterns: list[str] | None = None,
) -> AsyncGenerator[str]:
    """Yield SSE-formatted strings by subscribing to the EventBus.

    Each event is serialized via ``event.to_sse_dict()`` with
    ``json.dumps(ensure_ascii=False)``.  A keepalive comment is sent
    when the EventBus yields a ``None`` sentinel (no events for
    ``_KEEPALIVE_INTERVAL`` seconds) to prevent proxy timeouts and
    enable disconnect detection.
    """
    async for event in event_bus.subscribe(
        session_id=session_id,
        filter_patterns=filter_patterns,
    ):
        if await request.is_disconnected():
            logger.info("SSE client disconnected: session=%s", session_id)
            break

        # Keepalive sentinel from EventBus.subscribe()
        if event is None:
            yield ": keepalive\n\n"
            continue

        # Per-event error isolation — skip bad events, don't kill stream
        try:
            sse_dict = event.to_sse_dict()
            name = sse_dict.get("event", event.event_name)
            data = json.dumps(sse_dict, ensure_ascii=False)
            yield f"id: {event.sequence}\nevent: {name}\ndata: {data}\n\n"
        except Exception:
            logger.exception(
                "SSE serialization error for event=%s session=%s",
                event.event_name,
                session_id,
            )
            continue


@events_router.get("/events")
async def stream_events(
    request: Request,
    session: str | None = None,
    filter: str | None = None,  # noqa: A002
    preset: str | None = None,
) -> StreamingResponse:
    """Stream real-time SSE events from the global EventBus."""
    event_bus: EventBus = request.app.state.event_bus

    filter_patterns: list[str] | None = None
    if filter:
        filter_patterns = [p.strip() for p in filter.split(",") if p.strip()]

    return StreamingResponse(
        _event_generator(
            event_bus=event_bus,
            request=request,
            session_id=session,
            filter_patterns=filter_patterns,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
