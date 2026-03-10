"""Tests for Tailscale integration."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from amplifierd.security.tailscale import get_dns_name, provision_cert, start_serve


@pytest.mark.unit
class TestGetDnsName:
    """Tests for get_dns_name()."""

    def test_returns_dns_name_when_connected(self):
        status = {"BackendState": "Running", "Self": {"DNSName": "myhost.tail1234.ts.net."}}
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(status))
        with patch("amplifierd.security.tailscale.subprocess.run", return_value=result):
            assert get_dns_name() == "myhost.tail1234.ts.net"

    def test_returns_none_when_not_running(self):
        status = {"BackendState": "Stopped", "Self": {"DNSName": "myhost.tail1234.ts.net."}}
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(status))
        with patch("amplifierd.security.tailscale.subprocess.run", return_value=result):
            assert get_dns_name() is None

    def test_returns_none_when_tailscale_not_installed(self):
        with patch("amplifierd.security.tailscale.subprocess.run", side_effect=FileNotFoundError):
            assert get_dns_name() is None

    def test_returns_none_on_nonzero_exit(self):
        result = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="error")
        with patch("amplifierd.security.tailscale.subprocess.run", return_value=result):
            assert get_dns_name() is None

    def test_returns_none_on_timeout(self):
        with patch(
            "amplifierd.security.tailscale.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="tailscale", timeout=5),
        ):
            assert get_dns_name() is None


@pytest.mark.unit
class TestStartServe:
    """Tests for start_serve()."""

    def test_returns_url_on_success(self):
        with (
            patch(
                "amplifierd.security.tailscale.get_dns_name",
                return_value="myhost.tail1234.ts.net",
            ),
            patch(
                "amplifierd.security.tailscale.subprocess.run",
                return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout=""),
            ),
        ):
            assert start_serve(8410) == "https://myhost.tail1234.ts.net"

    def test_returns_none_when_tailscale_not_available(self):
        with patch("amplifierd.security.tailscale.get_dns_name", return_value=None):
            assert start_serve(8410) is None

    def test_returns_none_on_serve_failure(self):
        with (
            patch(
                "amplifierd.security.tailscale.get_dns_name",
                return_value="myhost.tail1234.ts.net",
            ),
            patch(
                "amplifierd.security.tailscale.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=[], returncode=1, stdout="", stderr="some error"
                ),
            ),
        ):
            assert start_serve(8410) is None


@pytest.mark.unit
class TestProvisionCert:
    """Tests for provision_cert()."""

    def test_returns_none_when_tailscale_not_available(self, tmp_path: Path):
        with patch("amplifierd.security.tailscale.get_dns_name", return_value=None):
            assert provision_cert(tmp_path / "certs") is None

    def test_returns_paths_on_success(self, tmp_path: Path):
        cert_dir = tmp_path / "certs"
        with (
            patch(
                "amplifierd.security.tailscale.get_dns_name",
                return_value="myhost.tail1234.ts.net",
            ),
            patch(
                "amplifierd.security.tailscale.subprocess.run",
                return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout=""),
            ),
        ):
            result = provision_cert(cert_dir)
        assert result is not None
        assert result[0] == cert_dir / "myhost.tail1234.ts.net.crt"
        assert result[1] == cert_dir / "myhost.tail1234.ts.net.key"
