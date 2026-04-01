"""Tests that docs/HOSTING.md exists and contains all required sections.

This test suite validates the HOSTING.md documentation coverage,
serving as a living contract for what the file must contain.
"""

from __future__ import annotations

from pathlib import Path

HOSTING_MD = Path(__file__).parent.parent / "docs" / "HOSTING.md"


def _content() -> str:
    assert HOSTING_MD.exists(), f"docs/HOSTING.md does not exist at {HOSTING_MD}"
    return HOSTING_MD.read_text()


# ---------------------------------------------------------------------------
# File existence
# ---------------------------------------------------------------------------


def test_hosting_md_exists() -> None:
    """docs/HOSTING.md must be present."""
    assert HOSTING_MD.exists(), f"docs/HOSTING.md not found at {HOSTING_MD}"


# ---------------------------------------------------------------------------
# Section 1 – Deployment modes
# ---------------------------------------------------------------------------


def test_deployment_modes_section_present() -> None:
    """Deployment modes section must exist."""
    content = _content()
    assert "Deployment Mode" in content or "deployment mode" in content.lower()


def test_localhost_mode_documented() -> None:
    """Localhost (default) mode: 127.0.0.1:8410, TLS off, auth off."""
    content = _content()
    assert "127.0.0.1" in content
    assert "8410" in content


def test_network_exposed_mode_documented() -> None:
    """Network-Exposed mode: 0.0.0.0 binding documented."""
    content = _content()
    assert "0.0.0.0" in content


def test_reverse_proxy_mode_documented() -> None:
    """Behind-a-Reverse-Proxy mode: AMPLIFIERD_TRUST_PROXY_AUTH referenced."""
    content = _content()
    assert "AMPLIFIERD_TRUST_PROXY_AUTH" in content


def test_network_mode_cookie_attributes_documented() -> None:
    """Network-Exposed mode must mention secure and samesite cookie settings."""
    content = _content()
    assert "secure" in content.lower()
    assert "samesite" in content.lower() or "SameSite" in content


# ---------------------------------------------------------------------------
# Section 2 – TLS Modes table
# ---------------------------------------------------------------------------


def test_tls_modes_table_present() -> None:
    """TLS Modes section must cover off, auto, and manual."""
    content = _content()
    assert "TLS" in content
    assert "off" in content
    assert "auto" in content
    assert "manual" in content


def test_tls_auto_tailscale_mentioned() -> None:
    """auto TLS mode must mention Tailscale."""
    content = _content()
    assert "Tailscale" in content or "tailscale" in content.lower()


def test_tls_manual_certs_mentioned() -> None:
    """manual TLS mode must mention user-supplied certs."""
    content = _content()
    # Check for certfile/keyfile references
    assert "certfile" in content.lower() or "certificate" in content.lower()


# ---------------------------------------------------------------------------
# Section 3 – Proxy Deployment
# ---------------------------------------------------------------------------


def test_trusted_proxies_env_var_documented() -> None:
    """AMPLIFIERD_TRUSTED_PROXIES must appear in proxy section."""
    content = _content()
    assert "AMPLIFIERD_TRUSTED_PROXIES" in content


def test_proxy_auth_trust_documented() -> None:
    """AMPLIFIERD_TRUST_PROXY_AUTH=true and X-Authenticated-User must be documented."""
    content = _content()
    assert "AMPLIFIERD_TRUST_PROXY_AUTH" in content
    assert "X-Authenticated-User" in content


def test_proxy_x_forwarded_for_documented() -> None:
    """X-Forwarded-For trust behavior must be documented."""
    content = _content()
    assert "X-Forwarded-For" in content


def test_proxy_security_warning_documented() -> None:
    """Security warning when trust_proxy_auth used without explicit trusted_proxies."""
    content = _content()
    # Must mention either warning or security concern about this combination
    lower = content.lower()
    assert "warn" in lower or "security" in lower or "caution" in lower or "danger" in lower


# ---------------------------------------------------------------------------
# Section 4 – Cookie Behavior
# ---------------------------------------------------------------------------


def test_cookie_secure_env_var_documented() -> None:
    """AMPLIFIERD_COOKIE_SECURE must appear in cookie section."""
    content = _content()
    assert "AMPLIFIERD_COOKIE_SECURE" in content


def test_cookie_samesite_env_var_documented() -> None:
    """AMPLIFIERD_COOKIE_SAMESITE must appear in cookie section."""
    content = _content()
    assert "AMPLIFIERD_COOKIE_SAMESITE" in content


def test_cookie_auto_default_documented() -> None:
    """AMPLIFIERD_COOKIE_SECURE default of 'auto' must be documented."""
    content = _content()
    assert "auto" in content


def test_cookie_lax_samesite_default_documented() -> None:
    """AMPLIFIERD_COOKIE_SAMESITE default of 'lax' must be documented."""
    content = _content()
    assert "lax" in content


# ---------------------------------------------------------------------------
# Section 5 – Port Auto-Increment
# ---------------------------------------------------------------------------


def test_port_auto_increment_documented() -> None:
    """Port auto-increment behaviour must be explained."""
    content = _content()
    lower = content.lower()
    assert "auto-increment" in lower or "auto increment" in lower or "increment" in lower


# ---------------------------------------------------------------------------
# Section 6 – Full Configuration Reference
# ---------------------------------------------------------------------------


def test_config_reference_section_present() -> None:
    """Configuration Reference section must exist."""
    content = _content()
    lower = content.lower()
    assert "configuration reference" in lower or "config reference" in lower


def test_config_reference_host_setting() -> None:
    """AMPLIFIERD_HOST must appear in the configuration reference."""
    content = _content()
    assert "AMPLIFIERD_HOST" in content


def test_config_reference_port_setting() -> None:
    """AMPLIFIERD_PORT must appear in the configuration reference."""
    content = _content()
    assert "AMPLIFIERD_PORT" in content


def test_config_reference_tls_mode_setting() -> None:
    """AMPLIFIERD_TLS_MODE must appear in the configuration reference."""
    content = _content()
    assert "AMPLIFIERD_TLS_MODE" in content


def test_config_reference_auth_enabled() -> None:
    """AMPLIFIERD_AUTH_ENABLED must appear in the configuration reference."""
    content = _content()
    assert "AMPLIFIERD_AUTH_ENABLED" in content


def test_config_reference_api_key() -> None:
    """AMPLIFIERD_API_KEY must appear in the configuration reference."""
    content = _content()
    assert "AMPLIFIERD_API_KEY" in content


def test_config_reference_trusted_proxies() -> None:
    """AMPLIFIERD_TRUSTED_PROXIES must appear in the configuration reference."""
    content = _content()
    assert "AMPLIFIERD_TRUSTED_PROXIES" in content


def test_config_reference_cookie_secure() -> None:
    """AMPLIFIERD_COOKIE_SECURE must appear in the configuration reference."""
    content = _content()
    assert "AMPLIFIERD_COOKIE_SECURE" in content


def test_config_reference_cookie_samesite() -> None:
    """AMPLIFIERD_COOKIE_SAMESITE must appear in the configuration reference."""
    content = _content()
    assert "AMPLIFIERD_COOKIE_SAMESITE" in content
