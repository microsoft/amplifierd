"""Tests for API key authentication middleware."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from amplifierd.security.middleware import ApiKeyMiddleware, is_localhost


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
