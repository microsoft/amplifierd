"""Tests for TLS orchestration (resolve_tls)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from amplifierd.security.tls import resolve_tls


@pytest.mark.unit
class TestResolveTls:
    """Tests for resolve_tls() — the Tailscale/TLS mutual exclusion orchestrator."""

    def test_off_returns_empty(self):
        """tls_mode='off' returns empty dict (no TLS)."""
        settings = MagicMock()
        settings.tls_mode = "off"
        assert resolve_tls(settings, 8410) == {}

    def test_tailscale_serve_skips_native_tls(self):
        """When Tailscale serve is active, returns empty (Tailscale handles HTTPS)."""
        settings = MagicMock()
        settings.tls_mode = "auto"
        settings.home_dir = Path("/tmp/test")

        with (
            patch(
                "amplifierd.security.tls.tailscale.start_serve",
                return_value="https://myhost.ts.net",
            ),
            patch("amplifierd.security.tls.atexit"),
        ):
            result = resolve_tls(settings, 8410)

        assert result == {}

    def test_auto_falls_back_to_self_signed(self):
        """When Tailscale unavailable, auto mode generates self-signed cert."""
        settings = MagicMock()
        settings.tls_mode = "auto"
        settings.home_dir = Path("/tmp/test")

        cert = Path("/tmp/test/certs/self-signed.pem")
        key = Path("/tmp/test/certs/self-signed-key.pem")

        with (
            patch("amplifierd.security.tls.tailscale.start_serve", return_value=None),
            patch("amplifierd.security.tls.tailscale.provision_cert", return_value=None),
            patch(
                "amplifierd.security.tls.certs.generate_self_signed_cert", return_value=(cert, key)
            ),
        ):
            result = resolve_tls(settings, 8410)

        assert result == {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}

    def test_manual_returns_provided_paths(self, tmp_path: Path):
        """manual mode returns the provided certfile/keyfile."""
        cert = tmp_path / "cert.pem"
        key = tmp_path / "key.pem"
        cert.write_text("CERT")
        key.write_text("KEY")

        settings = MagicMock()
        settings.tls_mode = "manual"
        settings.tls_certfile = str(cert)
        settings.tls_keyfile = str(key)

        # Manual mode doesn't probe Tailscale serve — goes straight to cert validation
        with patch("amplifierd.security.tls.tailscale.start_serve", return_value=None):
            result = resolve_tls(settings, 8410)

        assert result == {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}

    def test_manual_missing_files_raises(self):
        """manual mode with missing cert/key files raises UsageError."""
        import click

        settings = MagicMock()
        settings.tls_mode = "manual"
        settings.tls_certfile = "/nonexistent/cert.pem"
        settings.tls_keyfile = "/nonexistent/key.pem"

        with (
            patch("amplifierd.security.tls.tailscale.start_serve", return_value=None),
            pytest.raises(click.UsageError, match="--ssl-certfile.*--ssl-keyfile"),
        ):
            resolve_tls(settings, 8410)
