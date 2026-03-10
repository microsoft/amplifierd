"""Tailscale remote-access integration for amplifierd.

Auto-detects Tailscale and sets up HTTPS reverse proxy via ``tailscale serve``.
No configuration needed -- if Tailscale is connected, it just works.
"""

from __future__ import annotations

import contextlib
import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def get_dns_name() -> str | None:
    """Get the MagicDNS name if Tailscale is connected.

    Returns e.g. ``"win-dlpodl2cijb.tail79ce67.ts.net"`` or ``None``.
    """
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None

        data = json.loads(result.stdout)
        if data.get("BackendState") != "Running":
            return None

        dns = data.get("Self", {}).get("DNSName", "").rstrip(".")
        return dns or None

    except (
        FileNotFoundError,
        PermissionError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ):
        return None


def start_serve(port: int) -> str | None:
    """Start ``tailscale serve`` to proxy HTTPS -> localhost:port.

    Returns the HTTPS URL on success, or ``None`` on any failure.
    Failures are logged but never raise -- the server starts regardless.
    """
    dns_name = get_dns_name()
    if dns_name is None:
        return None

    try:
        result = subprocess.run(
            ["tailscale", "serve", "--bg", str(port)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "not enabled" in stderr.lower() or "enable" in stderr.lower():
                logger.warning(
                    "Tailscale Serve not enabled on tailnet. "
                    "Enable HTTPS in Tailscale admin: "
                    "https://login.tailscale.com/admin/dns"
                )
            else:
                logger.warning("tailscale serve failed: %s", stderr)
            return None

        url = f"https://{dns_name}"
        logger.info("Tailscale HTTPS active: %s -> localhost:%d", url, port)
        return url

    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.debug("tailscale serve unavailable: %s", exc)
        return None


def provision_cert(cert_dir: Path) -> tuple[Path, Path] | None:
    """Provision a TLS certificate via ``tailscale cert``.

    Returns ``(cert_path, key_path)`` on success, or ``None`` on any failure.
    Creates *cert_dir* if it does not exist.  Failures are logged but never
    raise.
    """
    dns_name = get_dns_name()
    if dns_name is None:
        return None

    cert_dir.mkdir(parents=True, exist_ok=True)

    cert_file = cert_dir / f"{dns_name}.crt"
    key_file = cert_dir / f"{dns_name}.key"

    try:
        result = subprocess.run(
            [
                "tailscale",
                "cert",
                "--cert-file",
                str(cert_file),
                "--key-file",
                str(key_file),
                dns_name,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            logger.warning("tailscale cert failed: %s", stderr)
            import click

            click.echo("")
            click.echo(
                click.style(
                    "  \u26a0 Tailscale certificate provisioning failed",
                    fg="yellow",
                    bold=True,
                )
            )
            if "access denied" in stderr.lower():
                click.echo("  Your user doesn't have permission to request certs.")
                click.echo("  Fix: Run once then restart the server:")
                click.echo(
                    click.style("    sudo tailscale set --operator=$USER", bold=True)
                )
            elif "does not support" in stderr.lower():
                click.echo(
                    "  HTTPS certificates are not enabled for your Tailscale account."
                )
                click.echo("  Fix: Enable HTTPS in your Tailscale admin console:")
                click.echo(
                    click.style("    https://login.tailscale.com/admin/dns", bold=True)
                )
                click.echo("  Check 'Enable HTTPS' then restart the server.")
            else:
                click.echo(f"  {stderr}")
            click.echo("")
            return None

        return (cert_file, key_file)

    except subprocess.TimeoutExpired:
        logger.debug("tailscale cert timed out")
        return None


def stop_serve() -> None:
    """Tear down ``tailscale serve``. Idempotent -- safe if not serving."""
    with contextlib.suppress(FileNotFoundError, subprocess.TimeoutExpired):
        subprocess.run(
            ["tailscale", "serve", "off"],
            capture_output=True,
            text=True,
            timeout=10,
        )