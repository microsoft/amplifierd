"""Self-signed TLS certificate generation for amplifierd.

Primary path uses the ``openssl`` CLI; falls back to the ``cryptography``
Python library when ``openssl`` is not available on the system PATH.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_CERT_NAME = "self-signed.pem"
_KEY_NAME = "self-signed-key.pem"


def generate_self_signed_cert(cert_dir: Path) -> tuple[Path, Path]:
    """Generate a self-signed TLS certificate and key in *cert_dir*.

    Returns ``(cert_path, key_path)``.  If both files already exist they are
    reused without regeneration.  Creates *cert_dir* if it does not exist.

    Raises :class:`RuntimeError` if neither ``openssl`` CLI nor the
    ``cryptography`` Python package is available.
    """
    cert_dir.mkdir(parents=True, exist_ok=True)

    cert_path = cert_dir / _CERT_NAME
    key_path = cert_dir / _KEY_NAME

    # Reuse existing cert if both files are present.
    if cert_path.exists() and key_path.exists():
        logger.debug("Reusing existing self-signed cert in %s", cert_dir)
        return cert_path, key_path

    # Try openssl CLI first.
    if _generate_via_openssl(cert_path, key_path):
        key_path.chmod(0o600)
        return cert_path, key_path

    # Fallback to cryptography library.
    if _generate_via_cryptography(cert_path, key_path):
        key_path.chmod(0o600)
        return cert_path, key_path

    msg = (
        "Cannot generate self-signed certificate: "
        "neither openssl CLI nor the cryptography Python package is available"
    )
    raise RuntimeError(msg)


def _generate_via_openssl(cert_path: Path, key_path: Path) -> bool:
    """Generate cert/key using the ``openssl`` CLI.  Returns True on success."""
    try:
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-keyout",
                str(key_path),
                "-out",
                str(cert_path),
                "-days",
                "3650",
                "-nodes",
                "-subj",
                "/CN=localhost",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except FileNotFoundError:
        logger.debug("openssl CLI not found, trying fallback")
        return False
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.warning("openssl failed: %s", exc)
        return False
    else:
        logger.debug("Generated self-signed cert via openssl CLI")
        return True


def _generate_via_cryptography(cert_path: Path, key_path: Path) -> bool:
    """Generate cert/key via ``cryptography`` library.  Returns True on success."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        logger.debug("cryptography library not available")
        return False

    import datetime

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    logger.debug("Generated self-signed cert via cryptography library")
    return True