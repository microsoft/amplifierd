"""Host and Origin header validation for amplifierd.

Defends the daemon against DNS rebinding and cross-site requests from
browsers.  The auth middlewares trust loopback clients, and a browser tricked
into talking to the daemon *is* a loopback client, so the check has to look at
what the browser was told it was talking to (``Host``) and which page asked
(``Origin``), not at the client IP.

Rules:

- ``Host`` must be an IP literal or a name in the allow-list.  A rebinding
  attack always arrives with the attacker's own domain name in ``Host``, and IP
  literals cannot be rebound, so allowing them keeps LAN / 0.0.0.0 access
  working without opening the attack.
- ``Origin``, when present, must name an allow-listed host or be the same host
  the request was sent to (e.g. a UI served by the daemon itself).  The opaque
  ``null`` origin is rejected.
- Requests without these headers (curl, SDKs, TestClient) are allowed —
  browsers always send ``Host``, and send ``Origin`` on cross-origin requests.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

from amplifierd.security.tailscale import get_dns_name

logger = logging.getLogger(__name__)

_WILDCARD_BIND_HOSTS = {"0.0.0.0", "::", ""}


def normalize_host(value: str) -> str:
    """Return the lowercase hostname from a ``Host``-style value.

    Strips any port, IPv6 brackets, and a trailing dot.  Accepts full URLs
    (``https://example.com:8443``) as well, so ``allowed_origins`` entries can
    be reused.
    """
    value = value.strip().lower()
    if "://" in value:
        value = urlsplit(value).netloc
    if value.startswith("["):
        # [::1]:8410 -> ::1
        value = value[1:].split("]", 1)[0]
    elif value.count(":") == 1:
        value = value.split(":", 1)[0]
    return value.rstrip(".")


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def build_allowed_hosts(bind_host: str | None = None, extra: list[str] | None = None) -> set[str]:
    """Build the set of hostnames the daemon answers to.

    Always includes localhost and the loopback addresses.  Adds the bind host
    (unless it is a wildcard), the Tailscale DNS name and system hostname (so
    remote access via those names keeps working), and explicit *extra*
    entries (hostnames or URLs).
    """
    hosts = ["localhost", "127.0.0.1", "::1"]
    if bind_host and bind_host not in _WILDCARD_BIND_HOSTS:
        hosts.append(bind_host)
    ts_name = get_dns_name()
    if ts_name:
        hosts.append(ts_name)
    hostname = socket.gethostname()
    if hostname:
        hosts.append(hostname)
    if extra:
        hosts.extend(extra)
    return {normalize_host(h) for h in hosts if h and h != "*"}


def is_host_allowed(host_header: str | None, allowed: set[str]) -> bool:
    """Check a ``Host`` header value against the allow-list."""
    if host_header is None:
        return True
    host = normalize_host(host_header)
    return _is_ip_literal(host) or host in allowed


def is_request_origin_allowed(
    origin: str | None, host_header: str | None, allowed: set[str]
) -> bool:
    """Check an ``Origin`` header value.

    Allowed when absent, when its host is in *allowed*, or when it matches the
    host the request was sent to (same-origin).
    """
    if origin is None:
        return True
    if origin.strip().lower() == "null":
        return False
    origin_host = normalize_host(origin)
    if not origin_host:
        return False
    if origin_host in allowed:
        return True
    return host_header is not None and origin_host == normalize_host(host_header)


class HostValidationMiddleware:
    """Reject HTTP and WebSocket requests with an untrusted Host or Origin.

    Pure ASGI (not ``BaseHTTPMiddleware``) so it also covers WebSocket
    handshakes and streaming responses.  Pass ``allow_any=True`` to disable,
    e.g. behind a reverse proxy that already validates hosts.
    """

    def __init__(self, app: ASGIApp, allowed_hosts: set[str], allow_any: bool = False) -> None:
        self.app = app
        self.allowed_hosts = allowed_hosts
        self.allow_any = allow_any

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.allow_any or scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        host = headers.get("host")
        origin = headers.get("origin")

        if not is_host_allowed(host, self.allowed_hosts):
            reason = "Invalid Host header"
        elif not is_request_origin_allowed(origin, host, self.allowed_hosts):
            reason = "Origin not allowed"
        else:
            await self.app(scope, receive, send)
            return

        logger.warning(
            "Rejected %s request to %s: %s (host=%r, origin=%r)",
            scope["type"],
            scope.get("path"),
            reason,
            host,
            origin,
        )
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps({"detail": reason}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
