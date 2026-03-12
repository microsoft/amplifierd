"""Validation routes for amplifierd."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from amplifierd.models.errors import ErrorTypeURI, ProblemDetail
from amplifierd.models.modules import (
    ValidateBundleRequest,
    ValidateModuleRequest,
    ValidateMountPlanRequest,
    ValidationResponse,
)

logger = logging.getLogger(__name__)

validation_router = APIRouter(prefix="/validate", tags=["validation"])


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


def _result_to_response(result: Any) -> ValidationResponse:
    """Convert a validation result object (duck-typed) to ValidationResponse."""
    valid = bool(getattr(result, "valid", True))
    errors_raw = getattr(result, "errors", None)
    warnings_raw = getattr(result, "warnings", None)
    checks_raw = getattr(result, "checks", None)
    errors = list(errors_raw) if errors_raw else None
    warnings = list(warnings_raw) if warnings_raw else None
    checks = list(checks_raw) if checks_raw else None
    return ValidationResponse(valid=valid, errors=errors, warnings=warnings, checks=checks)


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------


@validation_router.post("/mount-plan", response_model=ValidationResponse)
async def validate_mount_plan(
    request: Request, body: ValidateMountPlanRequest
) -> ValidationResponse:
    """Validate a mount plan configuration."""
    registry = _get_registry_or_503(request)

    validate_fn = getattr(registry, "validate_mount_plan", None)
    if not callable(validate_fn):
        logger.debug("Registry has no validate_mount_plan method; skipping deep validation")
        return ValidationResponse(valid=True)

    try:
        result = validate_fn(body.mount_plan)
        return _result_to_response(result)
    except Exception as exc:
        logger.exception("Mount plan validation raised an unexpected error")
        detail = ProblemDetail(
            type=ErrorTypeURI.VALIDATION_ERROR,
            title="Validation Error",
            status=500,
            detail=f"Mount plan validation failed unexpectedly: {exc}",
            instance=str(request.url.path),
        )
        raise HTTPException(
            status_code=500,
            detail=detail.model_dump(exclude_none=True),
        )


@validation_router.post("/module", response_model=ValidationResponse)
async def validate_module(request: Request, body: ValidateModuleRequest) -> ValidationResponse:
    """Validate a module for protocol compliance."""
    registry = _get_registry_or_503(request)

    validate_fn = getattr(registry, "validate_module", None)
    if not callable(validate_fn):
        logger.debug("Registry has no validate_module method; skipping deep validation")
        return ValidationResponse(valid=True)

    try:
        result = validate_fn(
            body.module_id,
            type=body.type,
            source=body.source,
            config=body.config,
        )
        return _result_to_response(result)
    except Exception as exc:
        logger.exception("Module validation raised an unexpected error")
        detail = ProblemDetail(
            type=ErrorTypeURI.VALIDATION_ERROR,
            title="Validation Error",
            status=500,
            detail=f"Module validation failed unexpectedly: {exc}",
            instance=str(request.url.path),
        )
        raise HTTPException(
            status_code=500,
            detail=detail.model_dump(exclude_none=True),
        )


@validation_router.post("/bundle", response_model=ValidationResponse)
async def validate_bundle(request: Request, body: ValidateBundleRequest) -> ValidationResponse:
    """Validate a bundle for correctness."""
    registry = _get_registry_or_503(request)

    validate_fn = getattr(registry, "validate_bundle", None)
    if not callable(validate_fn):
        logger.debug("Registry has no validate_bundle method; skipping deep validation")
        return ValidationResponse(valid=True)

    try:
        result = validate_fn(body.source)
        return _result_to_response(result)
    except Exception as exc:
        logger.exception("Bundle validation raised an unexpected error")
        detail = ProblemDetail(
            type=ErrorTypeURI.VALIDATION_ERROR,
            title="Validation Error",
            status=500,
            detail=f"Bundle validation failed unexpectedly: {exc}",
            instance=str(request.url.path),
        )
        raise HTTPException(
            status_code=500,
            detail=detail.model_dump(exclude_none=True),
        )
