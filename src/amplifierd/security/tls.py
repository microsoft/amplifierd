"""TLS orchestration for amplifierd.

Resolves TLS configuration from settings, enforcing the Tailscale/native-TLS
mutual exclusion invariant:

- If ``tailscale serve`` is active → no native TLS (Tailscale proxies HTTPS)
- If ``tailscale serve`` is not active → resolve certs per ``tls_mode``
"""

from __future__ import annotations

import atexit
import logging
from typing import TYPE_CHECKING, Any

import click

from amplifierd.security import certs, tailscale

if TYPE_CHECKING:
    from amplifierd.config import DaemonSettings

logger = logging.getLogger(__name__)


def resolve_tls(settings: DaemonSettings, port: int) -> dict[str, Any]:
    """Resolve TLS configuration.  Returns ``ssl_kwargs`` dict for ``uvicorn.run()``.

    Enforces the mutual exclusion invariant:

    - If Tailscale serve is active → return ``{}`` (no native TLS needed)
    - If Tailscale serve is not active → resolve certs per ``tls_mode``

    Modes:

    ``off``
        Returns ``{}``.
    ``auto``
        Probes Tailscale serve first.  If unavailable, tries
        ``tailscale cert`` then falls back to self-signed.
    ``manual``
        Validates the provided ``tls_certfile`` / ``tls_keyfile`` paths.
    """
    if settings.tls_mode == "off":
        return {}

    # Phase 1: Tailscale serve probe (must be first — determines TLS strategy)
    ts_url = tailscale.start_serve(port)
    if ts_url:
        atexit.register(tailscale.stop_serve)
        click.echo(click.style(f"  \u2713 HTTPS via Tailscale: {ts_url}", fg="green"))
        return {}  # Tailscale handles HTTPS — no native TLS

    # Phase 2: Cert resolution (only reached if no Tailscale serve)
    if settings.tls_mode == "manual":
        if (
            not settings.tls_certfile
            or not settings.tls_keyfile
            or not _path_exists(settings.tls_certfile)
            or not _path_exists(settings.tls_keyfile)
        ):
            raise click.UsageError(
                "--tls manual requires valid --ssl-certfile and --ssl-keyfile paths"
            )
        return {"ssl_certfile": settings.tls_certfile, "ssl_keyfile": settings.tls_keyfile}

    # mode == "auto" — try Tailscale cert, then self-signed
    cert_dir = settings.home_dir / "certs"
    ts_cert = tailscale.provision_cert(cert_dir)
    if ts_cert is not None:
        click.echo(click.style("  \u2713 Using Tailscale certificate for TLS", fg="green"))
        return {"ssl_certfile": str(ts_cert[0]), "ssl_keyfile": str(ts_cert[1])}

    # Fall back to self-signed
    click.echo(click.style("  \u26a0 Using self-signed certificate", fg="yellow", bold=True))
    cert_path, key_path = certs.generate_self_signed_cert(cert_dir)
    return {"ssl_certfile": str(cert_path), "ssl_keyfile": str(key_path)}


def _path_exists(path: str) -> bool:
    """Check if a path exists on the filesystem."""
    from pathlib import Path

    return Path(path).exists()