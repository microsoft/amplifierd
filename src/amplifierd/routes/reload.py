"""Reload routes for amplifierd."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from amplifierd.models.bundles import BundleUpdateCheck, ReloadBundlesResponse, ReloadStatusResponse
from amplifierd.models.errors import ErrorTypeURI, ProblemDetail

logger = logging.getLogger(__name__)

reload_router = APIRouter(prefix="/reload", tags=["reload"])


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


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------


@reload_router.post("/bundles", response_model=ReloadBundlesResponse)
async def reload_bundles(request: Request) -> ReloadBundlesResponse:
    """Reload all registered bundles daemon-wide."""
    registry = _get_registry_or_503(request)

    names: list[str] = registry.list_registered()
    reloaded: list[str] = []
    failed: list[str] = []

    for name in names:
        try:
            await registry.load(name)
            reloaded.append(name)
        except Exception:
            logger.warning("Failed to reload bundle '%s'", name, exc_info=True)
            failed.append(name)

    return ReloadBundlesResponse(
        reloaded=reloaded,
        failed=failed,
        total=len(reloaded),
    )


@reload_router.get("/status", response_model=ReloadStatusResponse)
async def reload_status(request: Request) -> ReloadStatusResponse:
    """Check which registered bundles have updates available."""
    registry = _get_registry_or_503(request)

    names: list[str] = registry.list_registered()
    bundle_checks: list[BundleUpdateCheck] = []

    for name in names:
        state = None
        try:
            state = registry.get_state(name)
        except Exception:
            logger.warning("Failed to get state for bundle '%s'", name, exc_info=True)

        current_version = getattr(state, "version", None) if state is not None else None

        try:
            update_info = await registry.check_update(name)
        except Exception:
            logger.warning("Failed to check updates for bundle '%s'", name, exc_info=True)
            bundle_checks.append(
                BundleUpdateCheck(
                    name=name,
                    current_version=current_version,
                    available_version=None,
                    has_update=False,
                )
            )
            continue

        if update_info is None:
            bundle_checks.append(
                BundleUpdateCheck(
                    name=name,
                    current_version=current_version,
                    available_version=None,
                    has_update=False,
                )
            )
        else:
            bundle_checks.append(
                BundleUpdateCheck(
                    name=name,
                    current_version=current_version,
                    available_version=getattr(update_info, "available_version", None),
                    has_update=True,
                )
            )

    return ReloadStatusResponse(bundles=bundle_checks)
