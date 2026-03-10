"""Dynamic CORS origin allow-list for amplifierd.

Builds a deduplicated allow-list from known-good sources (localhost,
Tailscale DNS, system hostname, explicit extras).  No wildcards —
enumerate known-good only.
"""

from __future__ import annotations

import socket

from amplifierd.security.tailscale import get_dns_name


def build_allowed_origins(extra: list[str] | None = None) -> list[str]:
    """Build a deduplicated origin allow-list.

    Always includes localhost and 127.0.0.1.  Adds the Tailscale DNS
    name (if available), the system hostname, and any explicit *extra*
    entries.  Returns a deduplicated list preserving insertion order.
    """
    origins: list[str] = ["localhost", "127.0.0.1"]

    # Tailscale DNS name (may be None)
    ts_name = get_dns_name()
    if ts_name:
        origins.append(ts_name)

    # System hostname
    hostname = socket.gethostname()
    if hostname:
        origins.append(hostname)

    # Explicit extras
    if extra:
        origins.extend(extra)

    # Deduplicate preserving insertion order
    return list(dict.fromkeys(origins))


def is_origin_allowed(origin: str | None, allowed: set[str]) -> bool:
    """Check if an Origin header value matches the allow-list.

    - ``None`` origin (no header) is always allowed.
    - Otherwise, returns ``True`` if any entry in *allowed* is a
      substring of *origin*.
    """
    if origin is None:
        return True
    # Substring match is intentional — entries are host fragments, not full URLs.
    return any(entry in origin for entry in allowed)