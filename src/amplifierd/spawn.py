"""Session spawning capability for amplifierd.

Registers the ``session.spawn`` capability on a coordinator so the
``delegate`` and ``recipes`` tools can spawn sub-sessions.

When *session_manager* and *parent_handle* are provided, child sessions
are wrapped in a :class:`SessionHandle` and wired into the EventBus tree
so their streaming events (``content_block:*``, ``thinking:*``, ``tool:*``)
appear on the parent session's SSE stream.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from amplifierd.state.session_handle import SessionHandle
    from amplifierd.state.session_manager import SessionManager

logger = logging.getLogger(__name__)

# Synthetic version for dynamically-constructed child bundles.
_CHILD_BUNDLE_VERSION = "1.0.0"


def register_spawn_capability(
    session: Any,
    prepared: Any,
    session_id: str,
    *,
    session_manager: SessionManager | None = None,
    parent_handle: SessionHandle | None = None,
) -> None:
    """Register ``session.spawn`` capability on *session*'s coordinator.

    Args:
        session:    AmplifierSession whose coordinator receives the capability.
        prepared:   PreparedBundle used to create *session*.  Its ``spawn()``
                    method, ``bundle``, and ``resolver`` are used for
                    sub-session creation.
        session_id: ID of *session* (for logging only).
        session_manager:
                    When provided together with *parent_handle*, child sessions
                    are registered in the SessionManager and wired into the
                    EventBus tree so their streaming events are visible to SSE
                    subscribers.
        parent_handle:
                    SessionHandle of *session*.  Used to call
                    ``register_child()`` for EventBus tree propagation.
    """
    from amplifier_foundation import Bundle  # type: ignore[import]

    coordinator = session.coordinator

    async def spawn_fn(
        agent_name: str,
        instruction: str,
        parent_session: Any,
        agent_configs: dict[str, dict[str, Any]] | None = None,
        sub_session_id: str | None = None,
        orchestrator_config: dict[str, Any] | None = None,
        parent_messages: list[dict[str, Any]] | None = None,
        tool_inheritance: dict[str, list[str]] | None = None,
        hook_inheritance: dict[str, list[str]] | None = None,
        provider_preferences: list[Any] | None = None,
        self_delegation_depth: int = 0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Spawn a sub-session for *agent_name* and execute *instruction*.

        Returns:
            dict with at minimum ``{"response": str, "session_id": str}``.

        Raises:
            ValueError: If *agent_name* is not "self" and cannot be resolved.
        """
        configs = agent_configs or {}

        # --- Resolve agent name -> Bundle config ---
        if agent_name == "self":
            config: dict[str, Any] = {}
        elif agent_name in configs:
            config = configs[agent_name]
        elif (
            hasattr(prepared, "bundle")
            and hasattr(prepared.bundle, "agents")
            and agent_name in prepared.bundle.agents
        ):
            config = prepared.bundle.agents[agent_name]
        else:
            available = sorted(
                list(configs.keys())
                + (
                    list(prepared.bundle.agents.keys())
                    if hasattr(prepared, "bundle") and hasattr(prepared.bundle, "agents")
                    else []
                )
            )
            raise ValueError(f"Agent '{agent_name}' not found. Available: {available}")

        # --- Build child Bundle from config ---
        child_bundle = Bundle(
            name=agent_name,
            version=_CHILD_BUNDLE_VERSION,
            session=config.get("session", {}),
            providers=config.get("providers", []),
            tools=config.get("tools", []),
            hooks=list(config.get("hooks", [])),
            instruction=(config.get("instruction") or config.get("system", {}).get("instruction")),
        )

        logger.debug(
            "Spawning sub-session: agent=%s session_id=%s parent=%s",
            agent_name,
            sub_session_id,
            session_id,
        )

        # --- Spawn with or without EventBus integration ---
        if session_manager is not None and parent_handle is not None:
            return await _spawn_with_event_forwarding(
                prepared=prepared,
                child_bundle=child_bundle,
                agent_name=agent_name,
                instruction=instruction,
                parent_session=parent_session,
                sub_session_id=sub_session_id,
                orchestrator_config=orchestrator_config,
                parent_messages=parent_messages,
                provider_preferences=provider_preferences,
                self_delegation_depth=self_delegation_depth,
                session_manager=session_manager,
                parent_handle=parent_handle,
            )

        # Fallback: delegate to PreparedBundle.spawn() without event forwarding.
        return await prepared.spawn(
            child_bundle=child_bundle,
            instruction=instruction,
            session_id=sub_session_id,
            parent_session=parent_session,
            orchestrator_config=orchestrator_config,
            parent_messages=parent_messages,
            provider_preferences=provider_preferences,
            self_delegation_depth=self_delegation_depth,
        )

    coordinator.register_capability("session.spawn", spawn_fn)
    logger.info("session.spawn capability registered for session %s", session_id)


# ------------------------------------------------------------------
# Internal helper
# ------------------------------------------------------------------


async def _spawn_with_event_forwarding(
    *,
    prepared: Any,
    child_bundle: Any,
    agent_name: str,
    instruction: str,
    parent_session: Any,
    sub_session_id: str | None,
    orchestrator_config: dict[str, Any] | None,
    parent_messages: list[dict[str, Any]] | None,
    provider_preferences: list[Any] | None,
    self_delegation_depth: int,
    session_manager: SessionManager,
    parent_handle: SessionHandle,
) -> dict[str, Any]:
    """Spawn a child session with EventBus integration for SSE streaming.

    Replicates the essential logic of ``PreparedBundle.spawn()`` but wraps
    the child session in a ``SessionHandle`` so its streaming events are
    forwarded to the EventBus.  This allows SSE subscribers on the parent
    session (``GET /events?session=<parent_id>``) to receive child session
    events in real time.

    Reference implementations:
        - ``amplifier_foundation/bundle.py`` — ``PreparedBundle.spawn()``
        - ``amplifier-app-cli/session_spawner.py`` — ``spawn_sub_session()``
    """
    from amplifier_core import AmplifierSession, HookResult  # type: ignore[import]

    # 1. Compose child bundle with parent bundle
    effective_bundle = prepared.bundle.compose(child_bundle)

    # 2. Create mount plan from composed bundle
    child_mount_plan = effective_bundle.to_mount_plan()

    # 3. Merge orchestrator config override
    if orchestrator_config:
        orch = child_mount_plan.setdefault("orchestrator", {})
        orch.setdefault("config", {}).update(orchestrator_config)

    # 4. Apply provider preferences (fallback chain)
    if provider_preferences:
        from amplifier_foundation import (  # type: ignore[import]
            apply_provider_preferences_with_resolution,
        )

        child_mount_plan = await apply_provider_preferences_with_resolution(
            child_mount_plan,
            provider_preferences,
            parent_session.coordinator if parent_session else None,
        )

    # 5. Create child AmplifierSession
    child_session = AmplifierSession(
        child_mount_plan,
        session_id=sub_session_id,
        parent_id=parent_session.session_id if parent_session else None,
        approval_system=(
            getattr(
                getattr(parent_session, "coordinator", None),
                "approval_system",
                None,
            )
            if parent_session
            else None
        ),
        display_system=(
            getattr(
                getattr(parent_session, "coordinator", None),
                "display_system",
                None,
            )
            if parent_session
            else None
        ),
    )

    # 6. Mount module resolver from parent PreparedBundle
    await child_session.coordinator.mount("module-source-resolver", prepared.resolver)

    # 7. Inherit working directory from parent session
    if parent_session:
        parent_wd = parent_session.coordinator.get_capability("session.working_dir")
        effective_cwd = (
            Path(parent_wd)
            if parent_wd
            else (getattr(prepared.bundle, "base_path", None) or Path.cwd())
        )
    else:
        effective_cwd = getattr(prepared.bundle, "base_path", None) or Path.cwd()
    child_session.coordinator.register_capability(
        "session.working_dir",
        str(Path(effective_cwd).resolve()),
    )

    # 8. Initialize child session (mounts modules from mount plan)
    await child_session.initialize()

    # 9. Register self-delegation depth for recursion limiting
    if self_delegation_depth > 0:
        child_session.coordinator.register_capability(
            "self_delegation_depth",
            self_delegation_depth,
        )

    # 10. Inject parent messages for context inheritance (new sessions only)
    if parent_messages and not sub_session_id:
        child_context = child_session.coordinator.get("context")
        if child_context and hasattr(child_context, "set_messages"):
            await child_context.set_messages(parent_messages)

    # 11. Set up system prompt from composed bundle
    try:
        if effective_bundle.instruction or getattr(effective_bundle, "context", None):
            factory = prepared._create_system_prompt_factory(
                effective_bundle,
                child_session,
            )
            context = child_session.coordinator.get("context")
            if context and hasattr(context, "set_system_prompt_factory"):
                await context.set_system_prompt_factory(factory)
            elif context:
                resolved_prompt = await factory()
                await context.add_message(
                    {"role": "system", "content": resolved_prompt},
                )
    except (AttributeError, TypeError):
        logger.debug(
            "Could not set system prompt via _create_system_prompt_factory",
            exc_info=True,
        )

    # ------------------------------------------------------------------
    # Persistence — mirror what SessionManager.create() does for parents
    # ------------------------------------------------------------------

    # 12a. Inherit working_dir and project_id from the parent session so the
    #      child gets its own on-disk session directory and persistence hooks.
    #      Without this, GET /sessions/{child_id}/transcript returns 404
    #      because resolve_session_dir() only looks on disk.
    child_working_dir = parent_handle.working_dir or str(effective_cwd)
    child_project_id = ""

    if session_manager.projects_dir:
        from amplifierd.config import cwd_to_slug
        from amplifierd.persistence import register_persistence_hooks

        child_project_id = cwd_to_slug(child_working_dir)
        child_session_dir = (
            session_manager.projects_dir / child_project_id / "sessions" / child_session.session_id
        )
        child_session_dir.mkdir(parents=True, exist_ok=True)
        register_persistence_hooks(
            child_session,
            child_session_dir,
            initial_metadata={
                "session_id": child_session.session_id,
                "created": datetime.now(tz=UTC).isoformat(),
                "bundle": agent_name,
                "working_dir": child_working_dir,
                "parent_session_id": (parent_session.session_id if parent_session else None),
            },
        )

    # ------------------------------------------------------------------
    # EventBus integration
    # ------------------------------------------------------------------

    # 12b. Register child in SessionManager — creates a SessionHandle whose
    #      __init__ calls _wire_events(), hooking all kernel events to EventBus.
    child_handle = session_manager.register(
        session=child_session,
        prepared_bundle=None,
        bundle_name=agent_name,
        working_dir=child_working_dir,
        project_id=child_project_id,
    )

    # 13. Wire parent -> child in EventBus so SSE subscribers on the parent
    #     session automatically receive child events via get_descendants().
    parent_handle.register_child(child_session.session_id, agent_name)

    # 14. Register spawn capability on child (enables recursive delegation)
    register_spawn_capability(
        child_session,
        prepared,
        child_session.session_id,
        session_manager=session_manager,
        parent_handle=child_handle,
    )

    # 15. Register temporary hook to capture orchestrator:complete metadata
    completion_data: dict[str, Any] = {}
    unregister_capture = None
    hooks = getattr(child_session.coordinator, "hooks", None)
    if hooks:

        async def _capture_completion(
            event: str,
            data: dict[str, Any],
        ) -> HookResult:
            completion_data.update(data)
            return HookResult(action="continue")

        unregister_capture = hooks.register(
            "orchestrator:complete",
            _capture_completion,
            priority=999,
            name="_amplifierd_spawn_capture",
        )

    # 16. Execute via SessionHandle (sets correlation_id for event attribution)
    try:
        response = await child_handle.execute(instruction)
    finally:
        if unregister_capture:
            unregister_capture()
        # 17. Cleanup: remove child from SessionManager, teardown session
        await session_manager.destroy(child_session.session_id)

    return {
        "output": response,
        "session_id": child_session.session_id,
        "status": completion_data.get("status", "success"),
        "turn_count": completion_data.get("turn_count", 1),
        "metadata": completion_data.get("metadata", {}),
    }
