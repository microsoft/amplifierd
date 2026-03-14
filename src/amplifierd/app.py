"""FastAPI application factory for amplifierd."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from amplifierd.config import DaemonSettings
from amplifierd.errors import register_error_handlers
from amplifierd.plugins import discover_plugins
from amplifierd.routes import ALL_ROUTERS
from amplifierd.state.event_bus import EventBus
from amplifierd.state.session_manager import SessionManager

logger = logging.getLogger(__name__)


async def prewarm(app: FastAPI) -> None:
    """Background task: load default bundle, inject providers, prepare.

    Public API — imported by amplifierd.routes.health and distro_plugin.reload.
    """
    try:
        registry = app.state.bundle_registry
        if not registry:
            app.state.bundles_ready.set()
            return
        settings = app.state.settings
        if not settings.default_bundle:
            app.state.bundles_ready.set()
            return

        logger.info("Starting bundle prewarm for '%s'...", settings.default_bundle)

        bundle = await registry.load(settings.default_bundle)

        from amplifierd.providers import inject_providers, load_provider_config

        providers = load_provider_config()
        inject_providers(bundle, providers)

        # bundle.prepare() calls ModuleActivator._install_dependencies which uses
        # synchronous subprocess.run() (uv pip install -e). Awaiting it directly
        # on the main event loop freezes the entire server — no other coroutines
        # (including GET /ready) can execute while uv is running.
        #
        # asyncio.to_thread() offloads to a worker thread. Because prepare() is
        # itself async (uses asyncio.gather internally), we give it a dedicated
        # event loop inside that thread via asyncio.run(). The main uvicorn loop
        # stays free and can respond to /ready, /health, etc. throughout.
        #
        # NOTE: asyncio.wait_for() + asyncio.to_thread() — the timeout cancels
        # the awaiter but the worker thread keeps running. The thread running
        # subprocess.run() (uv pip install) will complete on its own. A brief
        # period of overlapping work is possible on retry, but uv uses file locks
        # so this is safe. The user gets a clear timeout error immediately.
        try:
            prepared = await asyncio.wait_for(
                asyncio.to_thread(lambda: asyncio.run(bundle.prepare())),
                timeout=300,
            )
        except TimeoutError:
            raise TimeoutError(
                "Bundle preparation timed out after 300 seconds. "
                "Check network connectivity and retry."
            )

        # Warm Python's sys.modules cache by creating (and immediately cleaning
        # up) a throwaway session.  create_session() -> session.initialize()
        # imports every module package via importlib.  The first call is
        # expensive (~12s); subsequent calls hit sys.modules and are fast
        # (~1-2s).  We pay the cost here in the background so the user's first
        # real session is near-instant.
        async def _warmup_imports() -> None:
            """Create and immediately clean up a session to warm sys.modules."""
            session = await prepared.create_session()
            if hasattr(session, "cleanup"):
                await session.cleanup()

        try:
            await asyncio.to_thread(lambda: asyncio.run(_warmup_imports()))
            logger.info("Module import cache warmed via throwaway session")
        except Exception:
            logger.warning("Throwaway session failed (non-fatal)", exc_info=True)

        session_manager = getattr(app.state, "session_manager", None)
        if session_manager:
            session_manager.set_prepared_bundle(settings.default_bundle, prepared)

        app.state.bundles_ready.set()
        logger.info("Bundle pre-warmed and ready: %s", settings.default_bundle)
    except asyncio.CancelledError:
        logger.info("Bundle prewarm cancelled")
        raise
    except Exception as exc:
        # Set bundles_ready even on failure so the 503 guard releases.
        # Users can still attempt session creation (which will fail with 502
        # if the bundle is actually broken), access the wizard to fix config,
        # or retry via POST /ready/retry.
        app.state.prewarm_error = str(exc)
        app.state.bundles_ready.set()
        logger.warning("Bundle prewarm failed: %s", exc, exc_info=True)


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

    # Load API keys from ~/.amplifier/keys.env into os.environ so that
    # ${ANTHROPIC_API_KEY} etc. in settings.yaml resolve during env-var
    # expansion.  The CLI and distro both do this at startup; without it
    # the daemon has no API keys and providers mount without credentials.
    try:
        import os as _os
        from pathlib import Path as _Path

        _home = _Path(_os.environ.get("AMPLIFIER_HOME", _Path.home() / ".amplifier"))
        _keys_path = _home / "keys.env"
        if _keys_path.is_file():
            _loaded = []
            for _line in _keys_path.read_text().splitlines():
                _line = _line.strip()
                if not _line or _line.startswith("#") or "=" not in _line:
                    continue
                _k, _v = _line.split("=", 1)
                _k = _k.strip()
                _v = _v.strip().strip('"').strip("'")
                if _k and _k not in _os.environ:
                    _os.environ[_k] = _v
                    _loaded.append(_k)
            if _loaded:
                logger.info("Loaded %d key(s) from %s: %s", len(_loaded), _keys_path, _loaded)
            else:
                logger.debug("keys.env found but all keys already in environment")
        else:
            logger.debug("No keys.env at %s", _keys_path)
    except Exception:
        logger.debug("Could not load keys.env", exc_info=True)

    # Invalidate module install-state cache so that bundle.prepare() re-checks
    # whether provider SDKs are actually installed in this venv.  The cache at
    # ~/.amplifier/cache/install-state.json may be stale from another venv
    # (e.g. the CLI's), causing ModuleActivator to skip installation even when
    # packages like `anthropic` or `openai` are missing.  Invalidating is cheap
    # — uv pip install -e is a fast no-op when packages are already present.
    try:
        from amplifier_lib.modules.install_state import InstallStateManager
        from amplifier_lib.paths import get_amplifier_home

        state = InstallStateManager(get_amplifier_home() / "cache")
        state.invalidate()
        state.save()
        logger.debug("Invalidated module install-state cache")
    except Exception:
        logger.debug("Could not invalidate install-state cache", exc_info=True)

    # BundleRegistry — resilient: catches all exceptions, starts without registry
    try:
        from amplifier_lib import BundleRegistry

        app.state.bundle_registry = BundleRegistry()

        # Register configured bundles (name → URI mappings, no downloads)
        if settings.bundles:
            app.state.bundle_registry.register(settings.bundles)
            logger.info(
                "Registered %d bundle(s): %s",
                len(settings.bundles),
                list(settings.bundles.keys()),
            )

    except Exception:
        logger.warning("Failed to create BundleRegistry; starting without it", exc_info=True)
        app.state.bundle_registry = None

    app.state.session_manager = SessionManager(
        event_bus=app.state.event_bus,
        settings=settings,
        bundle_registry=app.state.bundle_registry,
        projects_dir=settings.projects_dir,
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

    # Background state tracking for prewarm
    app.state.bundles_ready = asyncio.Event()
    app.state.prewarm_task = None
    app.state.prewarm_error = None

    # Only launch background prewarm when there is real work to do (registry +
    # default bundle configured).  Otherwise mark bundles as ready immediately
    # so that the 503 route guard does not block session creation.
    if app.state.bundle_registry and settings.default_bundle:
        prewarm_task = asyncio.create_task(prewarm(app))
        app.state.prewarm_task = prewarm_task
        app.state.background_tasks.add(prewarm_task)
        prewarm_task.add_done_callback(app.state.background_tasks.discard)
    else:
        app.state.bundles_ready.set()

    yield

    # --- Shutdown ---
    if app.state.prewarm_task and not app.state.prewarm_task.done():
        app.state.prewarm_task.cancel()
        try:
            await app.state.prewarm_task
        except (asyncio.CancelledError, Exception):
            pass

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
    resolved_settings = settings or DaemonSettings()

    app = FastAPI(
        title="amplifierd",
        description="HTTP/SSE daemon for amplifier-core and amplifier-foundation",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=_lifespan,
    )

    app.state.settings = resolved_settings

    # CORS middleware — configurable via settings.allowed_origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Session auth middleware — opt-in when auth_enabled is True.
    # Must be added before ApiKeyMiddleware so it sits *inside* it in the
    # Starlette middleware stack (each add_middleware call wraps the current
    # app, making the last-added middleware the outermost layer).
    if resolved_settings.auth_enabled:
        from amplifierd.security.middleware import SessionAuthMiddleware

        app.add_middleware(SessionAuthMiddleware)

    # API key middleware — opt-in when api_key is configured
    if resolved_settings.api_key:
        from amplifierd.security.middleware import ApiKeyMiddleware

        app.add_middleware(ApiKeyMiddleware, api_key=resolved_settings.api_key)

    register_error_handlers(app)

    for r in ALL_ROUTERS:
        app.include_router(r)

    # Optional root redirect — configured via AMPLIFIERD_HOME_REDIRECT
    if resolved_settings.home_redirect:
        target = resolved_settings.home_redirect

        @app.get("/", include_in_schema=False)
        async def root_redirect() -> RedirectResponse:
            return RedirectResponse(url=target)

    return app
