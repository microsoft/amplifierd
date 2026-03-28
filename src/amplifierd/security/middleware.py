"""API key and session authentication middleware for amplifierd."""

from __future__ import annotations

import hmac
import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

logger = logging.getLogger(__name__)

_LOCALHOST_HOSTS = {"127.0.0.1", "localhost", "::1"}

_PUBLIC_PATHS = {"/health", "/info", "/docs", "/redoc", "/openapi.json"}

# Paths that must always be reachable even without a valid session.
# Includes the auth endpoints themselves, static assets, and favicon.
_AUTH_PATHS = {"/login", "/logout", "/auth/me", "/favicon.svg"}

_SESSION_COOKIE = "amplifier_session"


def is_localhost(host: str | None) -> bool:
    """Check if the request originates from localhost."""
    return host in _LOCALHOST_HOSTS or host is None


def _resolve_client_ip(
    direct_ip: str | None,
    forwarded_for: str | None,
    trusted_proxies: set[str],
) -> str | None:
    """Resolve the real client IP, honouring X-Forwarded-For only from trusted proxies.

    Returns the leftmost IP from the X-Forwarded-For header when the direct
    connection comes from a trusted proxy; otherwise returns the direct IP.
    An empty ``trusted_proxies`` set means no proxy forwarding is trusted.
    """
    if direct_ip is None:
        return None
    if forwarded_for and direct_ip in trusted_proxies:
        return forwarded_for.split(",")[0].strip()
    return direct_ip


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Require API key for non-localhost requests.

    Bypass order:
    1. Localhost requests -> always pass
    2. Public paths (/health, /info, /docs, /redoc, /openapi.json) -> always pass
    3. Valid Authorization: Bearer <api_key> -> pass
    4. Otherwise -> 401
    """

    def __init__(self, app, api_key: str) -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        self.api_key = api_key

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
        direct_ip = request.client.host if request.client else None
        trusted_proxies = getattr(request.app.state, "trusted_proxies", set())
        forwarded_for = request.headers.get("x-forwarded-for")
        client_ip = _resolve_client_ip(direct_ip, forwarded_for, trusted_proxies)
        if is_localhost(client_ip):
            return await call_next(request)
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            if hmac.compare_digest(token, self.api_key):
                return await call_next(request)
        logger.warning(
            "Rejected request from %s to %s: missing or invalid API key",
            client_ip,
            request.url.path,
        )
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid or missing API key"},
        )


class SessionAuthMiddleware(BaseHTTPMiddleware):
    """Enforce session cookie authentication for all non-public routes.

    The auth plugin registers a ``verify_session`` callable on
    ``app.state.auth_verify_session`` at startup.  This middleware reads that
    callable on every request so the secret is resolved after the plugin has
    fully initialised.

    Bypass order:
    1. Auth paths (/login, /logout, /auth/me, /favicon.svg) -> always pass
    2. Public paths (/health, /info, /docs, /redoc, /openapi.json) -> always pass
    3. Static assets (/static/*) -> always pass
    4. Valid ``amplifier_session`` cookie -> pass
    5. HTML-accepting clients -> redirect to /login
    6. Otherwise -> 401 JSON
    """

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
        path = request.url.path
        if path in _AUTH_PATHS or path in _PUBLIC_PATHS or path.startswith("/static/"):
            return await call_next(request)
        direct_ip = request.client.host if request.client else None
        trusted_proxies = getattr(request.app.state, "trusted_proxies", set())
        forwarded_for = request.headers.get("x-forwarded-for")
        client_ip = _resolve_client_ip(direct_ip, forwarded_for, trusted_proxies)
        if is_localhost(client_ip):
            return await call_next(request)
        verify = getattr(request.app.state, "auth_verify_session", None)
        if verify is None:
            logger.warning("SessionAuthMiddleware active but auth_verify_session not set")
            return await call_next(request)
        session_token = request.cookies.get(_SESSION_COOKIE)
        if session_token is not None and verify(session_token) is not None:
            return await call_next(request)
        logger.debug("Unauthenticated request to %s from %s", path, client_ip)
        if "text/html" in request.headers.get("accept", ""):
            from urllib.parse import quote

            return RedirectResponse(url=f"/login?next={quote(path, safe='/')}", status_code=302)
        return JSONResponse(status_code=401, content={"detail": "Authentication required"})
