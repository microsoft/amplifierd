"""LLM/Bundle error mapping to RFC 7807 Problem Details with FastAPI exception handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

try:
    from amplifier_core.llm_errors import (
        AbortError,
        AccessDeniedError,
        AuthenticationError,
        ConfigurationError,
        ContentFilterError,
        ContextLengthError,
        InvalidRequestError,
        InvalidToolCallError,
        LLMError,
        LLMTimeoutError,
        NetworkError,
        NotFoundError,
        ProviderUnavailableError,
        QuotaExceededError,
        RateLimitError,
        StreamError,
    )

    _HAS_AMPLIFIER_CORE = True
except ImportError:
    AbortError = None  # type: ignore[assignment,misc]
    AccessDeniedError = None  # type: ignore[assignment,misc]
    AuthenticationError = None  # type: ignore[assignment,misc]
    ConfigurationError = None  # type: ignore[assignment,misc]
    ContentFilterError = None  # type: ignore[assignment,misc]
    ContextLengthError = None  # type: ignore[assignment,misc]
    InvalidRequestError = None  # type: ignore[assignment,misc]
    InvalidToolCallError = None  # type: ignore[assignment,misc]
    LLMError = None  # type: ignore[assignment,misc]
    LLMTimeoutError = None  # type: ignore[assignment,misc]
    NetworkError = None  # type: ignore[assignment,misc]
    NotFoundError = None  # type: ignore[assignment,misc]
    ProviderUnavailableError = None  # type: ignore[assignment,misc]
    QuotaExceededError = None  # type: ignore[assignment,misc]
    RateLimitError = None  # type: ignore[assignment,misc]
    StreamError = None  # type: ignore[assignment,misc]
    _HAS_AMPLIFIER_CORE = False

try:
    from amplifier_foundation.exceptions import (
        BundleDependencyError,
        BundleError,
        BundleLoadError,
        BundleNotFoundError,
        BundleValidationError,
    )

    _HAS_AMPLIFIER_FOUNDATION = True
except ImportError:
    BundleDependencyError = None  # type: ignore[assignment,misc]
    BundleError = None  # type: ignore[assignment,misc]
    BundleLoadError = None  # type: ignore[assignment,misc]
    BundleNotFoundError = None  # type: ignore[assignment,misc]
    BundleValidationError = None  # type: ignore[assignment,misc]
    _HAS_AMPLIFIER_FOUNDATION = False

from fastapi.responses import JSONResponse

from amplifierd.models.errors import ProblemDetail

if TYPE_CHECKING:
    from fastapi import FastAPI, Request

# Ordered list: subclasses before parents so isinstance matches the most specific type first.
# Only populated when amplifier_core/amplifier_foundation are available.
LLM_ERROR_MAP: list[tuple[type, int, str]] = (
    [
        (QuotaExceededError, 429, "quota-exceeded"),
        (RateLimitError, 429, "rate-limit"),
        (AccessDeniedError, 502, "provider-access-denied"),
        (AuthenticationError, 502, "provider-auth"),
        (ContextLengthError, 413, "context-too-large"),
        (ContentFilterError, 422, "content-filtered"),
        (InvalidRequestError, 400, "invalid-request"),
        (NetworkError, 503, "network-error"),
        (ProviderUnavailableError, 503, "provider-unavailable"),
        (LLMTimeoutError, 504, "provider-timeout"),
        (NotFoundError, 502, "provider-not-found"),
        (StreamError, 502, "stream-error"),
        (AbortError, 499, "aborted"),
        (InvalidToolCallError, 502, "invalid-tool-call"),
        (ConfigurationError, 500, "configuration-error"),
        (LLMError, 502, "llm-error"),
    ]
    if _HAS_AMPLIFIER_CORE
    else []
)

BUNDLE_ERROR_MAP: list[tuple[type, int, str]] = (
    [
        (BundleNotFoundError, 404, "bundle-not-found"),
        (BundleLoadError, 422, "bundle-load-error"),
        (BundleValidationError, 422, "bundle-validation-error"),
        (BundleDependencyError, 422, "bundle-dependency-error"),
        (BundleError, 500, "bundle-error"),
    ]
    if _HAS_AMPLIFIER_FOUNDATION
    else []
)

_BASE_URI = "https://amplifier.dev/errors"

_TITLE_MAP: dict[str, str] = {
    "quota-exceeded": "Quota Exceeded",
    "rate-limit": "Rate Limit Exceeded",
    "provider-access-denied": "Provider Access Denied",
    "provider-auth": "Provider Authentication Failed",
    "context-too-large": "Context Too Large",
    "content-filtered": "Content Filtered",
    "invalid-request": "Invalid Request",
    "network-error": "Network Error",
    "provider-unavailable": "Provider Unavailable",
    "provider-timeout": "Provider Timeout",
    "provider-not-found": "Provider Not Found",
    "stream-error": "Stream Error",
    "aborted": "Request Aborted",
    "invalid-tool-call": "Invalid Tool Call",
    "configuration-error": "Configuration Error",
    "llm-error": "LLM Error",
    "bundle-not-found": "Bundle Not Found",
    "bundle-load-error": "Bundle Load Error",
    "bundle-validation-error": "Bundle Validation Error",
    "bundle-dependency-error": "Bundle Dependency Error",
    "bundle-error": "Bundle Error",
}


def map_llm_error(exc: LLMError) -> tuple[int, str]:
    """Map an LLMError to (HTTP status code, error suffix)."""
    for error_type, status, suffix in LLM_ERROR_MAP:
        if isinstance(exc, error_type):
            return status, suffix
    # Fallback (should not be reached since LLMError is last in the map)
    return 502, "llm-error"


def map_bundle_error(exc: BundleError) -> tuple[int, str]:
    """Map a BundleError to (HTTP status code, error suffix)."""
    for error_type, status, suffix in BUNDLE_ERROR_MAP:
        if isinstance(exc, error_type):
            return status, suffix
    # Fallback (should not be reached since BundleError is last in the map)
    return 500, "bundle-error"


def build_problem_detail(exc: LLMError | BundleError, instance: str) -> ProblemDetail:
    """Build an RFC 7807 ProblemDetail from an LLM or Bundle error."""
    if isinstance(exc, LLMError):
        status, suffix = map_llm_error(exc)
    else:
        status, suffix = map_bundle_error(exc)

    type_uri = f"{_BASE_URI}/{suffix}"
    title = _TITLE_MAP.get(suffix, suffix.replace("-", " ").title())

    # Build optional extension fields from LLMError attributes
    retryable: bool | None = None
    retry_after_seconds: float | None = None
    provider: str | None = None
    model: str | None = None
    upstream_status: int | None = None
    error_class: str | None = None
    tool_name: str | None = None
    raw_arguments: str | None = None

    if isinstance(exc, LLMError):
        retryable = exc.retryable
        retry_after_seconds = exc.retry_after
        provider = exc.provider
        model = exc.model
        upstream_status = exc.status_code
        error_class = type(exc).__name__

    if isinstance(exc, InvalidToolCallError):
        tool_name = exc.tool_name
        raw_arguments = exc.raw_arguments

    return ProblemDetail(
        type=type_uri,
        title=title,
        status=status,
        detail=str(exc),
        instance=instance,
        retryable=retryable,
        retry_after_seconds=retry_after_seconds,
        provider=provider,
        model=model,
        upstream_status=upstream_status,
        error_class=error_class,
        tool_name=tool_name,
        raw_arguments=raw_arguments,
    )


def register_error_handlers(app: FastAPI) -> None:
    """Register FastAPI exception handlers for LLMError and BundleError."""

    if _HAS_AMPLIFIER_CORE:

        @app.exception_handler(LLMError)
        async def llm_error_handler(request: Request, exc: LLMError) -> JSONResponse:
            pd = build_problem_detail(exc, instance=str(request.url.path))
            headers: dict[str, str] = {}
            if isinstance(exc, RateLimitError) and exc.retry_after is not None:
                headers["Retry-After"] = str(int(exc.retry_after))
            return JSONResponse(
                status_code=pd.status,
                content=pd.model_dump(exclude_none=True),
                headers=headers or None,
            )

    if _HAS_AMPLIFIER_FOUNDATION:

        @app.exception_handler(BundleError)
        async def bundle_error_handler(request: Request, exc: BundleError) -> JSONResponse:
            pd = build_problem_detail(exc, instance=str(request.url.path))
            return JSONResponse(
                status_code=pd.status,
                content=pd.model_dump(exclude_none=True),
            )
