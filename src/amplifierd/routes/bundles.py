"""Bundle management routes for amplifierd."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from amplifierd.models.bundles import (
    BundleDetail,
    BundleListResponse,
    BundleSummary,
    BundleUpdateCheck,
    ComposeBundlesRequest,
    LoadBundleRequest,
    PrepareBundleRequest,
    RegisterBundleRequest,
)
from amplifierd.models.errors import ErrorTypeURI, ProblemDetail

logger = logging.getLogger(__name__)

bundles_router = APIRouter(prefix="/bundles", tags=["bundles"])


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _get_registry_or_503(request: Request) -> Any:
    """Return bundle_registry or raise HTTPException 503 if unavailable."""
    registry = getattr(request.app.state, "bundle_registry", None)
    if registry is None:
        detail = ProblemDetail(
            type=ErrorTypeURI.BUNDLE_ERROR,
            title="Bundle Registry Unavailable",
            status=503,
            detail=("Bundle registry is not available (amplifier_lib failed to load at startup)"),
            instance=str(request.url.path),
        )
        raise HTTPException(
            status_code=503,
            detail=detail.model_dump(exclude_none=True),
        )
    return registry


def _bundle_to_detail(bundle: Any) -> BundleDetail:
    """Convert a Bundle (or duck-typed object) to a BundleDetail response model."""
    description = getattr(bundle, "description", None) or None
    includes = getattr(bundle, "includes", None) or None
    providers = getattr(bundle, "providers", None) or None
    tools = getattr(bundle, "tools", None) or None
    hooks = getattr(bundle, "hooks", None) or None
    agents = getattr(bundle, "agents", None) or None
    context = getattr(bundle, "context", None)
    context_files = list(context.keys()) if context else None

    return BundleDetail(
        name=getattr(bundle, "name", ""),
        version=getattr(bundle, "version", None),
        description=description,
        includes=includes,
        providers=providers,
        tools=tools,
        hooks=hooks,
        agents=agents,
        context_files=context_files or None,
    )


def _bundle_not_found_error(name: str, path: str) -> HTTPException:
    """Return a 404 HTTPException with RFC 7807 body for a missing bundle."""
    detail = ProblemDetail(
        type=ErrorTypeURI.BUNDLE_NOT_FOUND,
        title="Bundle Not Found",
        status=404,
        detail=f"Bundle '{name}' not found in registry",
        instance=path,
    )
    return HTTPException(
        status_code=404,
        detail=detail.model_dump(exclude_none=True),
    )


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------


@bundles_router.get("", response_model=BundleListResponse)
async def list_bundles(request: Request) -> BundleListResponse:
    """List all registered bundles."""
    registry = _get_registry_or_503(request)
    names: list[str] = registry.list_registered()

    summaries: list[BundleSummary] = []
    for name in names:
        try:
            state = registry.get_state(name)
            if state is not None:
                loaded_at_raw = getattr(state, "loaded_at", None)
                loaded_at_str = loaded_at_raw.isoformat() if loaded_at_raw else None
                summaries.append(
                    BundleSummary(
                        name=name,
                        uri=getattr(state, "uri", None),
                        version=getattr(state, "version", None),
                        loaded_at=loaded_at_str,
                    )
                )
        except Exception:
            logger.warning("Failed to get state for bundle '%s'", name, exc_info=True)

    return BundleListResponse(bundles=summaries)


@bundles_router.post("/register", status_code=201, response_model=BundleSummary)
async def register_bundle(request: Request, body: RegisterBundleRequest) -> BundleSummary:
    """Register a bundle name → URI mapping."""
    registry = _get_registry_or_503(request)
    try:
        registry.register({body.name: body.uri})
    except Exception:
        logger.exception("Failed to register bundle '%s'", body.name)
        detail = ProblemDetail(
            type=ErrorTypeURI.BUNDLE_ERROR,
            title="Bundle Registration Failed",
            status=500,
            detail=f"Failed to register bundle '{body.name}'",
            instance=str(request.url.path),
        )
        raise HTTPException(
            status_code=500,
            detail=detail.model_dump(exclude_none=True),
        )
    return BundleSummary(name=body.name, uri=body.uri)


@bundles_router.delete("/{name}", status_code=204)
async def unregister_bundle(request: Request, name: str) -> None:
    """Unregister a bundle by name."""
    registry = _get_registry_or_503(request)
    removed: bool = registry.unregister(name)
    if not removed:
        raise _bundle_not_found_error(name, str(request.url.path))


@bundles_router.post("/load", response_model=BundleDetail)
async def load_bundle(request: Request, body: LoadBundleRequest) -> BundleDetail:
    """Load a bundle from a URI or registered name and return its details."""
    registry = _get_registry_or_503(request)
    try:
        bundle = await registry.load(body.source)
    except Exception as exc:
        logger.exception("Failed to load bundle from '%s'", body.source)
        detail = ProblemDetail(
            type=ErrorTypeURI.BUNDLE_LOAD_ERROR,
            title="Bundle Load Error",
            status=502,
            detail=f"Failed to load bundle: {exc}",
            instance=str(request.url.path),
        )
        raise HTTPException(
            status_code=502,
            detail=detail.model_dump(exclude_none=True),
        )
    return _bundle_to_detail(bundle)


@bundles_router.post("/prepare", response_model=BundleDetail)
async def prepare_bundle(request: Request, body: PrepareBundleRequest) -> BundleDetail:
    """Load and prepare a bundle for session creation (activates modules)."""
    registry = _get_registry_or_503(request)
    try:
        bundle = await registry.load(body.source)
    except Exception as exc:
        logger.exception("Failed to load bundle from '%s'", body.source)
        detail = ProblemDetail(
            type=ErrorTypeURI.BUNDLE_LOAD_ERROR,
            title="Bundle Load Error",
            status=502,
            detail=f"Failed to load bundle: {exc}",
            instance=str(request.url.path),
        )
        raise HTTPException(
            status_code=502,
            detail=detail.model_dump(exclude_none=True),
        )

    # Activate modules — failures are non-fatal; we still return bundle detail
    install_deps = body.install_deps if body.install_deps is not None else True
    try:
        await bundle.prepare(install_deps=install_deps)
    except Exception:
        logger.warning(
            "Bundle prepare step failed for '%s'; returning bundle detail anyway",
            body.source,
            exc_info=True,
        )

    return _bundle_to_detail(bundle)


@bundles_router.post("/compose", response_model=BundleDetail)
async def compose_bundles(request: Request, body: ComposeBundlesRequest) -> BundleDetail:
    """Load and compose multiple bundles into a single merged bundle."""
    registry = _get_registry_or_503(request)

    if not body.bundles:
        detail = ProblemDetail(
            type=ErrorTypeURI.INVALID_REQUEST,
            title="Invalid Request",
            status=400,
            detail="At least one bundle must be specified",
            instance=str(request.url.path),
        )
        raise HTTPException(
            status_code=400,
            detail=detail.model_dump(exclude_none=True),
        )

    loaded: list[Any] = []
    for source in body.bundles:
        try:
            bundle = await registry.load(source)
            loaded.append(bundle)
        except Exception as exc:
            logger.exception("Failed to load bundle '%s' during compose", source)
            detail = ProblemDetail(
                type=ErrorTypeURI.BUNDLE_LOAD_ERROR,
                title="Bundle Load Error",
                status=502,
                detail=f"Failed to load bundle '{source}': {exc}",
                instance=str(request.url.path),
            )
            raise HTTPException(
                status_code=502,
                detail=detail.model_dump(exclude_none=True),
            )

    # Compose: start with first bundle, compose with each subsequent one
    result = loaded[0]
    for other in loaded[1:]:
        try:
            result = result.compose(other)
        except Exception as exc:
            logger.exception("Failed to compose bundles")
            detail = ProblemDetail(
                type=ErrorTypeURI.BUNDLE_ERROR,
                title="Bundle Compose Error",
                status=500,
                detail=f"Failed to compose bundles: {exc}",
                instance=str(request.url.path),
            )
            raise HTTPException(
                status_code=500,
                detail=detail.model_dump(exclude_none=True),
            )

    return _bundle_to_detail(result)


@bundles_router.post("/{name}/check-updates", response_model=BundleUpdateCheck)
async def check_updates(request: Request, name: str) -> BundleUpdateCheck:
    """Check whether an update is available for a registered bundle."""
    registry = _get_registry_or_503(request)

    # Verify bundle is registered
    state = None
    try:
        state = registry.get_state(name)
    except Exception:
        pass
    if state is None:
        raise _bundle_not_found_error(name, str(request.url.path))

    current_version = getattr(state, "version", None)

    try:
        update_info = await registry.check_update(name)
    except Exception as exc:
        logger.exception("Failed to check updates for bundle '%s'", name)
        detail = ProblemDetail(
            type=ErrorTypeURI.BUNDLE_ERROR,
            title="Update Check Failed",
            status=502,
            detail=f"Failed to check updates for bundle '{name}': {exc}",
            instance=str(request.url.path),
        )
        raise HTTPException(
            status_code=502,
            detail=detail.model_dump(exclude_none=True),
        )

    if update_info is None:
        return BundleUpdateCheck(
            name=name,
            current_version=current_version,
            available_version=None,
            has_update=False,
        )

    return BundleUpdateCheck(
        name=name,
        current_version=current_version,
        available_version=getattr(update_info, "available_version", None),
        has_update=True,
    )


@bundles_router.post("/{name}/update", response_model=BundleDetail)
async def update_bundle(request: Request, name: str) -> BundleDetail:
    """Update a registered bundle to the latest version."""
    registry = _get_registry_or_503(request)

    # Verify bundle is registered
    state = None
    try:
        state = registry.get_state(name)
    except Exception:
        pass
    if state is None:
        raise _bundle_not_found_error(name, str(request.url.path))

    try:
        bundle = await registry.update(name)
    except Exception as exc:
        logger.exception("Failed to update bundle '%s'", name)
        detail = ProblemDetail(
            type=ErrorTypeURI.BUNDLE_ERROR,
            title="Bundle Update Failed",
            status=502,
            detail=f"Failed to update bundle '{name}': {exc}",
            instance=str(request.url.path),
        )
        raise HTTPException(
            status_code=502,
            detail=detail.model_dump(exclude_none=True),
        )

    return _bundle_to_detail(bundle)
