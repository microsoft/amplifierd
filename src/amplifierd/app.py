"""FastAPI application factory for amplifierd."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from amplifierd.config import DaemonSettings
from amplifierd.errors import register_error_handlers
from amplifierd.plugins import discover_plugins
from amplifierd.routes import ALL_ROUTERS
from amplifierd.state.event_bus import EventBus
from amplifierd.state.session_manager import SessionManager

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Manage startup and shutdown of the daemon."""
    # --- Startup ---
    app.state.start_time = time.time()
    app.state.background_tasks = set()

    settings: DaemonSettings = getattr(app.state, "settings", DaemonSettings())
    app.state.settings = settings

    # Daemon session path (created by cli.py before uvicorn starts; read via pydantic-settings)
    app.state.daemon_session_path = settings.daemon_session_path

    # Wire up session log in worker process (needed for --reload where worker is a fresh process)
    if app.state.daemon_session_path and app.state.daemon_session_path.exists():
        import sys

        from amplifierd.daemon_session import _TeeWriter, setup_session_log

        if not isinstance(sys.stdout, _TeeWriter):
            setup_session_log(app.state.daemon_session_path)

    if app.state.daemon_session_path:
        from amplifierd.daemon_session import update_session_meta

        update_session_meta(app.state.daemon_session_path, {"status": "running"})

    app.state.event_bus = EventBus()

    # BundleRegistry — resilient: catches all exceptions, starts without registry
    try:
        from amplifier_foundation import BundleRegistry

        app.state.bundle_registry = BundleRegistry()

        # Register configured bundles (name → URI mappings, no downloads)
        if settings.bundles:
            app.state.bundle_registry.register(settings.bundles)
            logger.info(
                "Registered %d bundle(s): %s",
                len(settings.bundles),
                list(settings.bundles.keys()),
            )

        # Pre-load the default bundle so first session creation is fast
        if settings.default_bundle:
            try:
                await app.state.bundle_registry.load(settings.default_bundle)
                logger.info("Pre-loaded default bundle: %s", settings.default_bundle)
            except Exception:
                logger.warning(
                    "Failed to pre-load default bundle '%s'",
                    settings.default_bundle,
                    exc_info=True,
                )

    except Exception:
        logger.warning("Failed to create BundleRegistry; starting without it", exc_info=True)
        app.state.bundle_registry = None

    sessions_dir = settings.sessions_dir
    app.state.session_manager = SessionManager(
        event_bus=app.state.event_bus,
        settings=settings,
        bundle_registry=app.state.bundle_registry,
        sessions_dir=sessions_dir,
    )

    # Plugin discovery — resilient
    plugin_names: list[str] = []
    try:
        plugins = discover_plugins(
            disabled=settings.disabled_plugins,
            state=app.state,
        )
        for name, router in plugins:
            app.include_router(router)
            plugin_names.append(name)
            logger.info("Mounted plugin: %s", name)
    except Exception:
        logger.warning("Plugin discovery failed; starting without plugins")

    # Update daemon session meta.json with discovered plugins
    if app.state.daemon_session_path and plugin_names:
        from amplifierd.daemon_session import update_session_meta

        update_session_meta(app.state.daemon_session_path, {"plugins": plugin_names})

    yield

    # --- Shutdown ---
    if app.state.daemon_session_path:
        from datetime import UTC, datetime

        from amplifierd.daemon_session import update_session_meta

        update_session_meta(
            app.state.daemon_session_path,
            {
                "status": "stopped",
                "end_time": datetime.now(tz=UTC).isoformat(),
            },
        )

    await app.state.session_manager.shutdown()


def create_app(settings: DaemonSettings | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="amplifierd",
        description="HTTP/SSE daemon for amplifier-core and amplifier-foundation",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=_lifespan,
    )

    if settings is not None:
        app.state.settings = settings

    # CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_error_handlers(app)

    for r in ALL_ROUTERS:
        app.include_router(r)

    return app
