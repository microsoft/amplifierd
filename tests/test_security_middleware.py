"""Tests for API key authentication middleware."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from amplifierd.security.middleware import ApiKeyMiddleware, _resolve_client_ip, is_localhost


@pytest.mark.unit
class TestIsLocalhost:
    """Tests for localhost detection."""

    def test_ipv4_localhost(self):
        assert is_localhost("127.0.0.1") is True

    def test_ipv6_localhost(self):
        assert is_localhost("::1") is True

    def test_localhost_string(self):
        assert is_localhost("localhost") is True

    def test_none_is_localhost(self):
        assert is_localhost(None) is True

    def test_remote_ip_is_not_localhost(self):
        assert is_localhost("192.168.1.100") is False

    def test_zero_zero_is_not_localhost(self):
        assert is_localhost("0.0.0.0") is False


def _make_app(api_key: str) -> FastAPI:
    """Create a minimal FastAPI app with ApiKeyMiddleware for testing."""
    app = FastAPI()
    app.add_middleware(ApiKeyMiddleware, api_key=api_key)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/sessions")
    async def sessions():
        return {"sessions": []}

    return app


@pytest.mark.unit
class TestApiKeyMiddleware:
    """Tests for ApiKeyMiddleware."""

    def test_valid_api_key_passes(self):
        """Request with valid Bearer token passes through."""
        app = _make_app("test-secret")
        client = TestClient(app)
        with patch("amplifierd.security.middleware.is_localhost", return_value=False):
            resp = client.get("/sessions", headers={"Authorization": "Bearer test-secret"})
        assert resp.status_code == 200

    def test_missing_api_key_rejected(self):
        """Request without Authorization header is rejected."""
        app = _make_app("test-secret")
        client = TestClient(app)
        with patch("amplifierd.security.middleware.is_localhost", return_value=False):
            resp = client.get("/sessions")
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid or missing API key"

    def test_wrong_api_key_rejected(self):
        """Request with wrong API key is rejected."""
        app = _make_app("test-secret")
        client = TestClient(app)
        with patch("amplifierd.security.middleware.is_localhost", return_value=False):
            resp = client.get("/sessions", headers={"Authorization": "Bearer wrong-key"})
        assert resp.status_code == 401

    def test_public_path_bypasses_auth(self):
        """Public paths like /health bypass API key check."""
        app = _make_app("test-secret")
        client = TestClient(app)
        with patch("amplifierd.security.middleware.is_localhost", return_value=False):
            resp = client.get("/health")
        assert resp.status_code == 200

    def test_non_bearer_auth_rejected(self):
        """Non-Bearer auth scheme is rejected."""
        app = _make_app("test-secret")
        client = TestClient(app)
        with patch("amplifierd.security.middleware.is_localhost", return_value=False):
            resp = client.get("/sessions", headers={"Authorization": "Basic dXNlcjpwYXNz"})
        assert resp.status_code == 401

    def test_localhost_bypasses_auth(self):
        """Localhost requests bypass API key check entirely."""
        app = _make_app("test-secret")
        client = TestClient(app)
        with patch("amplifierd.security.middleware.is_localhost", return_value=True):
            resp = client.get("/sessions")
        assert resp.status_code == 200


@pytest.mark.unit
class TestResolveClientIp:
    """Tests for _resolve_client_ip helper function."""

    def test_no_forwarded_header_returns_direct_ip(self):
        assert _resolve_client_ip("192.168.1.100", None, {"127.0.0.1", "::1"}) == "192.168.1.100"

    def test_untrusted_proxy_ignores_forwarded_header(self):
        assert _resolve_client_ip("10.0.0.5", "203.0.113.50", {"127.0.0.1", "::1"}) == "10.0.0.5"

    def test_trusted_proxy_uses_forwarded_header(self):
        assert (
            _resolve_client_ip("127.0.0.1", "203.0.113.50", {"127.0.0.1", "::1"}) == "203.0.113.50"
        )

    def test_trusted_proxy_uses_leftmost_ip(self):
        assert (
            _resolve_client_ip("127.0.0.1", "203.0.113.50, 10.0.0.1", {"127.0.0.1", "::1"})
            == "203.0.113.50"
        )

    def test_none_direct_ip_returns_none(self):
        assert _resolve_client_ip(None, None, {"127.0.0.1", "::1"}) is None


def _make_session_auth_app() -> FastAPI:
    from amplifierd.security.middleware import SessionAuthMiddleware

    app = FastAPI()
    app.state.trusted_proxies = {"127.0.0.1", "::1"}
    app.state.auth_verify_session = lambda token: "testuser" if token.startswith("valid-") else None
    app.add_middleware(SessionAuthMiddleware)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/dashboard")
    async def dashboard():
        return {"page": "dashboard"}

    return app


@pytest.mark.unit
class TestSessionAuthMiddlewareProxyAware:
    def test_remote_client_via_proxy_requires_session(self):
        app = _make_session_auth_app()
        client = TestClient(app)
        resp = client.get("/dashboard", headers={"X-Forwarded-For": "203.0.113.50"})
        assert resp.status_code in (401, 302)

    def test_genuine_localhost_bypasses_session_auth(self):
        app = _make_session_auth_app()
        client = TestClient(app, client=("127.0.0.1", 50000))
        resp = client.get("/dashboard")
        assert resp.status_code == 200

    def test_public_paths_bypass_for_remote_via_proxy(self):
        app = _make_session_auth_app()
        client = TestClient(app)
        resp = client.get("/health", headers={"X-Forwarded-For": "203.0.113.50"})
        assert resp.status_code == 200


@pytest.mark.unit
class TestApiKeyMiddlewareProxyAware:
    """Integration tests for ApiKeyMiddleware proxy-aware behaviour.

    Note: TestClient presents its direct IP as ``"testclient"``, which is not
    in ``trusted_proxies = {"127.0.0.1", "::1"}``.  Therefore
    ``_resolve_client_ip`` returns ``"testclient"`` (the direct IP), not the
    X-Forwarded-For value.  These tests verify that a non-localhost client
    requires an API key; the unit tests in ``TestResolveClientIp`` thoroughly
    cover the trusted-proxy header-extraction path.
    """

    def test_remote_client_via_trusted_proxy_requires_api_key(self):
        app = _make_app("test-secret")
        app.state.trusted_proxies = {"127.0.0.1", "::1"}
        client = TestClient(app)
        resp = client.get("/sessions", headers={"X-Forwarded-For": "203.0.113.50"})
        assert resp.status_code == 401

    def test_remote_client_via_trusted_proxy_passes_with_api_key(self):
        app = _make_app("test-secret")
        app.state.trusted_proxies = {"127.0.0.1", "::1"}
        client = TestClient(app)
        resp = client.get(
            "/sessions",
            headers={"X-Forwarded-For": "203.0.113.50", "Authorization": "Bearer test-secret"},
        )
        assert resp.status_code == 200
