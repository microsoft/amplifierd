"""Agent spawn/resume routes with session tree integration."""

from __future__ import annotations

import asyncio
import logging
import uuid
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from amplifierd.models.agents import (
    AgentInfo,
    AgentListResponse,
    SpawnRequest,
    SpawnResponse,
    SpawnResumeRequest,
)
from amplifierd.models.errors import ErrorTypeURI, ProblemDetail
from amplifierd.state.session_handle import SessionHandle

logger = logging.getLogger(__name__)

agents_router = APIRouter(prefix="/sessions", tags=["agents"])


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _get_handle_or_404(request: Request, session_id: str) -> SessionHandle:
    """Return SessionHandle or raise HTTPException 404 with RFC 7807 ProblemDetail body."""
    manager = request.app.state.session_manager
    handle = manager.get(session_id)
    if handle is None:
        detail = ProblemDetail(
            type=ErrorTypeURI.SESSION_NOT_FOUND,
            title="Session Not Found",
            status=404,
            detail=f"Session '{session_id}' not found",
            instance=str(request.url.path),
        )
        raise HTTPException(
            status_code=404,
            detail=detail.model_dump(exclude_none=True),
        )
    return handle


async def _noop_execute(prompt: str) -> str:
    """No-op execute stub for placeholder child sessions."""
    return ""


async def _noop_cleanup() -> None:
    """No-op cleanup stub for placeholder child sessions."""


def _create_placeholder_child(
    child_session_id: str, parent_session_id: str, agent_name: str
) -> Any:
    """Create a minimal placeholder session for when amplifier_foundation is unavailable."""
    return SimpleNamespace(
        session_id=child_session_id,
        parent_id=parent_session_id,
        coordinator=SimpleNamespace(request_cancel=lambda immediate: None),
        execute=_noop_execute,
        cleanup=_noop_cleanup,
    )


async def _create_child_handle(
    request: Request,
    parent_handle: SessionHandle,
    agent_name: str,
) -> tuple[str, SessionHandle]:
    """Create and register a child session handle.

    Attempts to use amplifier_foundation to create a real child session.
    Falls back to a placeholder if unavailable.

    Returns ``(child_session_id, child_handle)``.
    """
    manager = request.app.state.session_manager

    # Try the real foundation path first
    try:
        from amplifier_foundation import create_child_session  # type: ignore[import-not-found]

        child_session = await create_child_session(parent_handle.session, agent_name)
        child_handle = manager.register(
            session=child_session,
            prepared_bundle=None,
            bundle_name=agent_name,
        )
        return child_session.session_id, child_handle
    except (ImportError, AttributeError, Exception):
        pass

    # Fallback: placeholder child session
    child_session_id = str(uuid.uuid4())
    child_session = _create_placeholder_child(
        child_session_id, parent_handle.session_id, agent_name
    )
    child_handle = manager.register(
        session=child_session,
        prepared_bundle=None,
        bundle_name=agent_name,
    )
    return child_session_id, child_handle


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------


@agents_router.post("/{session_id}/spawn", response_model=SpawnResponse)
async def spawn_agent(request: Request, session_id: str, body: SpawnRequest) -> SpawnResponse:
    """Spawn a child agent session synchronously (blocks until instruction completes)."""
    handle = _get_handle_or_404(request, session_id)

    child_session_id, child_handle = await _create_child_handle(request, handle, body.agent)

    # Register child in parent's tracking (also propagates to EventBus)
    handle.register_child(child_session_id, body.agent)

    output: str | None = None
    try:
        result = await child_handle.execute(body.instruction)
        output = str(result) if result is not None else None
    except Exception:
        logger.exception(
            "Spawn execution failed for session %s child %s", session_id, child_session_id
        )

    return SpawnResponse(
        session_id=child_session_id,
        output=output,
        status=child_handle.status.value,
        turn_count=child_handle.turn_count,
    )


@agents_router.post(
    "/{session_id}/spawn/stream",
    status_code=202,
    response_model=SpawnResponse,
)
async def spawn_agent_stream(
    request: Request, session_id: str, body: SpawnRequest
) -> SpawnResponse:
    """Spawn a child agent session asynchronously (fire-and-forget, returns 202)."""
    handle = _get_handle_or_404(request, session_id)

    # Create and register child eagerly so the session_id is available immediately
    child_session_id, child_handle = await _create_child_handle(request, handle, body.agent)
    handle.register_child(child_session_id, body.agent)

    # Fire instruction in background
    async def _run() -> None:
        try:
            await child_handle.execute(body.instruction)
        except Exception:
            logger.exception("Background spawn execution failed for child %s", child_session_id)
        finally:
            background_tasks.discard(task)

    background_tasks: set[asyncio.Task[None]] = request.app.state.background_tasks
    task = asyncio.create_task(_run())
    background_tasks.add(task)

    return SpawnResponse(
        session_id=child_session_id,
        status=child_handle.status.value,
        turn_count=child_handle.turn_count,
    )


@agents_router.post(
    "/{session_id}/spawn/{child_id}/resume",
    response_model=SpawnResponse,
)
async def resume_child_agent(
    request: Request,
    session_id: str,
    child_id: str,
    body: SpawnResumeRequest,
) -> SpawnResponse:
    """Resume a previously spawned child agent session."""
    _get_handle_or_404(request, session_id)  # Verify parent exists
    child_handle = _get_handle_or_404(request, child_id)  # Verify child exists

    output: str | None = None
    try:
        result = await child_handle.execute(body.instruction)
        output = str(result) if result is not None else None
    except Exception:
        logger.exception("Resume execution failed for child %s", child_id)

    return SpawnResponse(
        session_id=child_id,
        output=output,
        status=child_handle.status.value,
        turn_count=child_handle.turn_count,
    )


@agents_router.get("/{session_id}/agents", response_model=AgentListResponse)
async def list_agents(request: Request, session_id: str) -> AgentListResponse:
    """List available agents from the bundle registry for the given session."""
    _get_handle_or_404(request, session_id)

    agents: dict[str, AgentInfo] = {}

    bundle_registry = getattr(request.app.state, "bundle_registry", None)
    if bundle_registry is not None:
        try:
            raw_agents: dict[str, Any] = bundle_registry.list_agents()
            for name, info in raw_agents.items():
                if isinstance(info, dict):
                    description = info.get("description")
                    model_role = info.get("model_role")
                else:
                    description = getattr(info, "description", None)
                    model_role = getattr(info, "model_role", None)
                agents[name] = AgentInfo(description=description, model_role=model_role)
        except Exception:
            logger.warning("Failed to read agents from bundle registry", exc_info=True)

    return AgentListResponse(agents=agents)
