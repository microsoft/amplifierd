"""Tests for Host/Origin validation (DNS rebinding and cross-site defense)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from amplifierd.security.hosts import (
    HostValidationMiddleware,
    build_allowed_hosts,
    is_host_allowed,
    is_request_origin_allowed,
    normalize_host,
)
from amplifierd.security.middleware import ApiKeyMiddleware

ALLOWED = {"localhost", "127.0.0.1", "::1", "myhost.tail1234.ts.net"}


@pytest.mark.unit
class TestNormalizeHost:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("localhost", "localhost"),
            ("LOCALHOST:8410", "localhost"),
            ("127.0.0.1:8410", "127.0.0.1"),
            ("[::1]:8410", "::1"),
            ("::1", "::1"),
            ("example.com.", "example.com"),
            ("https://app.example.com:8443", "app.example.com"),
        ],
    )
    def test_normalize(self, value, expected):
        assert normalize_host(value) == expected


@pytest.mark.unit
class TestIsHostAllowed:
    def test_localhost_with_port(self):
        assert is_host_allowed("localhost:8410", ALLOWED) is True

    def test_ip_literals_always_allowed(self):
        # IP literals cannot be rebound, so LAN / 0.0.0.0 access keeps working.
        assert is_host_allowed("192.168.1.20:8410", ALLOWED) is True
        assert is_host_allowed("[fe80::1]:8410", ALLOWED) is True

    def test_rebinding_hostname_rejected(self):
        assert is_host_allowed("attacker.example:8410", ALLOWED) is False

    def test_lookalike_hostname_rejected(self):
        # Exact match only — no substring matching.
        assert is_host_allowed("localhost.attacker.example", ALLOWED) is False

    def test_missing_host_allowed(self):
        assert is_host_allowed(None, ALLOWED) is True


@pytest.mark.unit
class TestIsRequestOriginAllowed:
    def test_no_origin(self):
        assert is_request_origin_allowed(None, "localhost:8410", ALLOWED) is True

    def test_allowed_origin(self):
        assert is_request_origin_allowed("http://localhost:5173", "127.0.0.1:8410", ALLOWED)

    def test_same_origin_on_lan_ip(self):
        assert is_request_origin_allowed("http://192.168.1.20:8410", "192.168.1.20:8410", ALLOWED)

    def test_cross_site_origin_rejected(self):
        assert not is_request_origin_allowed("https://attacker.example", "127.0.0.1:8410", ALLOWED)

    def test_null_origin_rejected(self):
        assert not is_request_origin_allowed("null", "127.0.0.1:8410", ALLOWED)


@pytest.mark.unit
class TestBuildAllowedHosts:
    def test_defaults_and_extras(self):
        with (
            patch("amplifierd.security.hosts.get_dns_name", return_value="box.tail1.ts.net"),
            patch("amplifierd.security.hosts.socket.gethostname", return_value="BOX"),
        ):
            hosts = build_allowed_hosts(
                bind_host="0.0.0.0", extra=["https://app.example.com", "proxy.local", "*"]
            )
        assert {"localhost", "127.0.0.1", "::1", "box.tail1.ts.net", "box"} <= hosts
        assert {"app.example.com", "proxy.local"} <= hosts
        assert "0.0.0.0" not in hosts
        assert "*" not in hosts

    def test_specific_bind_host_included(self):
        with (
            patch("amplifierd.security.hosts.get_dns_name", return_value=None),
            patch("amplifierd.security.hosts.socket.gethostname", return_value=""),
        ):
            assert "daemon.internal" in build_allowed_hosts(bind_host="daemon.internal")


def _make_app(*, api_key: str | None = None, allow_any: bool = False) -> FastAPI:
    app = FastAPI()

    @app.get("/sessions")
    async def sessions():
        return {"sessions": []}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("hi")
        await websocket.close()

    if api_key:
        app.add_middleware(ApiKeyMiddleware, api_key=api_key)
    app.add_middleware(HostValidationMiddleware, allowed_hosts=ALLOWED, allow_any=allow_any)
    return app


@pytest.mark.unit
class TestHostValidationMiddleware:
    def test_localhost_request_passes(self):
        client = TestClient(_make_app(), base_url="http://localhost:8410")
        assert client.get("/sessions").status_code == 200

    def test_dns_rebinding_blocked_even_with_api_key(self):
        """A rebound browser is a loopback client, so ApiKeyMiddleware lets it
        through; Host validation must stop it first."""
        client = TestClient(_make_app(api_key="secret"), base_url="http://attacker.example:8410")
        resp = client.get("/sessions")
        assert resp.status_code == 403
        assert resp.json() == {"detail": "Invalid Host header"}

    def test_cross_site_origin_blocked(self):
        client = TestClient(_make_app(), base_url="http://127.0.0.1:8410")
        resp = client.get("/sessions", headers={"Origin": "https://attacker.example"})
        assert resp.status_code == 403
        assert resp.json() == {"detail": "Origin not allowed"}

    def test_allowed_origin_passes(self):
        client = TestClient(_make_app(), base_url="http://127.0.0.1:8410")
        resp = client.get("/sessions", headers={"Origin": "http://localhost:5173"})
        assert resp.status_code == 200

    def test_websocket_rebinding_blocked(self):
        # websocket_connect ignores base_url, so set Host explicitly.
        client = TestClient(_make_app())
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/ws", headers={"host": "attacker.example:8410"}),
        ):
            pass

    def test_websocket_localhost_passes(self):
        client = TestClient(_make_app())
        with client.websocket_connect("/ws", headers={"host": "localhost:8410"}) as ws:
            assert ws.receive_text() == "hi"

    def test_allow_any_disables_check(self):
        client = TestClient(_make_app(allow_any=True), base_url="http://attacker.example")
        assert client.get("/sessions").status_code == 200


@pytest.mark.unit
def test_create_app_rejects_rebound_host(monkeypatch):
    """End to end: the real app rejects an unknown Host by default."""
    from amplifierd.app import create_app
    from amplifierd.config import DaemonSettings

    monkeypatch.delenv("AMPLIFIERD_ALLOWED_HOSTS", raising=False)
    app = create_app(DaemonSettings())
    client = TestClient(app, base_url="http://attacker.example:8410")
    assert client.get("/health").status_code == 403
    ok = TestClient(app, base_url="http://localhost:8410")
    assert ok.get("/health").status_code == 200
