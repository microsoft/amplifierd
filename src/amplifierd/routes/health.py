"""Health and info endpoints for amplifierd."""

from __future__ import annotations

import time

import amplifier_core
from fastapi import APIRouter, Request
from pydantic import BaseModel

import amplifierd

health_router = APIRouter()

CAPABILITIES: list[str] = [
    "streaming",
    "approval",
    "cancellation",
    "hot_mount",
    "fork",
    "spawn",
]

MODULE_TYPES: list[str] = [
    "orchestrator",
    "provider",
    "tool",
    "hook",
    "context",
    "resolver",
]


class HealthResponse(BaseModel):
    """Response model for GET /health."""

    status: str
    version: str
    uptime_seconds: float
    active_sessions: int
    rust_engine: bool


class InfoResponse(BaseModel):
    """Response model for GET /info."""

    version: str
    amplifier_core_version: str
    rust_available: bool
    capabilities: list[str]
    module_types: list[str]


def _rust_available() -> bool:
    """Check if the Rust engine is available."""
    try:
        return bool(getattr(amplifier_core, "rust_available", False))
    except Exception:
        # Any failure means Rust engine isn't usable
        return False


@health_router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    """Return health status of the daemon."""
    start_time: float = getattr(request.app.state, "start_time", time.time())
    uptime_seconds = round(time.time() - start_time, 2)

    session_manager = getattr(request.app.state, "session_manager", None)
    active_sessions = len(session_manager.list_sessions()) if session_manager else 0

    return HealthResponse(
        status="healthy",
        version=amplifierd.__version__,
        uptime_seconds=uptime_seconds,
        active_sessions=active_sessions,
        rust_engine=_rust_available(),
    )


@health_router.get("/info", response_model=InfoResponse)
async def info() -> InfoResponse:
    """Return daemon info: version, capabilities, module types."""
    return InfoResponse(
        version=amplifierd.__version__,
        amplifier_core_version=amplifier_core.__version__,
        rust_available=_rust_available(),
        capabilities=CAPABILITIES,
        module_types=MODULE_TYPES,
    )


@health_router.get("/ready")
async def ready(request: Request) -> dict:
    """Return bundle readiness status for loading screen polling."""
    bundles_ready = getattr(request.app.state, "bundles_ready", None)
    prewarm_error = getattr(request.app.state, "prewarm_error", None)
    is_ready = bundles_ready.is_set() if bundles_ready else True
    result: dict = {"ready": is_ready}
    if prewarm_error:
        result["error"] = prewarm_error
    return result


@health_router.post("/ready/retry")
async def ready_retry(request: Request) -> dict:
    """Retry bundle prewarm after a failure."""
    import asyncio

    app = request.app

    # Clear error state
    app.state.prewarm_error = None

    # Cancel existing task if any
    old_task = getattr(app.state, "prewarm_task", None)
    if old_task and not old_task.done():
        old_task.cancel()
        try:
            await old_task
        except (asyncio.CancelledError, Exception):
            pass

    # Clear ready event
    bundles_ready = getattr(app.state, "bundles_ready", None)
    if bundles_ready:
        bundles_ready.clear()

    # Start new prewarm task
    from amplifierd.app import _prewarm

    new_task = asyncio.create_task(_prewarm(app))
    app.state.prewarm_task = new_task
    app.state.background_tasks.add(new_task)
    new_task.add_done_callback(app.state.background_tasks.discard)

    return {"status": "retrying"}
