"""Tests for self-signed certificate generation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from amplifierd.security.certs import generate_self_signed_cert


@pytest.mark.unit
class TestGenerateSelfSignedCert:
    """Tests for generate_self_signed_cert()."""

    def test_reuses_existing_cert(self, tmp_path: Path):
        """If cert and key already exist, returns them without regenerating."""
        cert_dir = tmp_path / "certs"
        cert_dir.mkdir()
        cert = cert_dir / "self-signed.pem"
        key = cert_dir / "self-signed-key.pem"
        cert.write_text("CERT")
        key.write_text("KEY")

        result = generate_self_signed_cert(cert_dir)
        assert result == (cert, key)

    def test_generates_via_openssl(self, tmp_path: Path):
        """Generates cert via openssl CLI when available."""
        cert_dir = tmp_path / "certs"

        # Simulate openssl writing files
        def fake_openssl(*args, **kwargs):
            cert_dir.mkdir(parents=True, exist_ok=True)
            (cert_dir / "self-signed.pem").write_text("CERT")
            (cert_dir / "self-signed-key.pem").write_text("KEY")
            from subprocess import CompletedProcess

            return CompletedProcess(args=[], returncode=0)

        with patch("amplifierd.security.certs.subprocess.run", side_effect=fake_openssl):
            cert_path, key_path = generate_self_signed_cert(cert_dir)

        assert cert_path.exists()
        assert key_path.exists()

    def test_raises_when_no_backend_available(self, tmp_path: Path):
        """Raises RuntimeError when neither openssl nor cryptography is available."""
        cert_dir = tmp_path / "certs"

        with (
            patch(
                "amplifierd.security.certs.subprocess.run", side_effect=FileNotFoundError
            ),
            patch("amplifierd.security.certs._generate_via_cryptography", return_value=False),
        ):
            with pytest.raises(RuntimeError, match="Cannot generate self-signed certificate"):
                generate_self_signed_cert(cert_dir)