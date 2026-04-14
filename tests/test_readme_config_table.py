"""Tests that README.md config table is expanded with security/proxy settings.

This test suite validates that the README configuration table has been updated
with the new security and proxy settings, serving as a living contract for
documentation completeness.
"""

from __future__ import annotations

from pathlib import Path

README_MD = Path(__file__).parent.parent / "README.md"


def _content() -> str:
    assert README_MD.exists(), f"README.md does not exist at {README_MD}"
    return README_MD.read_text()


# ---------------------------------------------------------------------------
# Core settings (original 5 rows)
# ---------------------------------------------------------------------------


def test_readme_exists() -> None:
    """README.md must be present."""
    assert README_MD.exists(), f"README.md not found at {README_MD}"


def test_original_host_setting_present() -> None:
    """Original 'host' setting must still be in the config table."""
    content = _content()
    assert "AMPLIFIERD_HOST" in content


def test_original_port_setting_present() -> None:
    """Original 'port' setting must still be in the config table."""
    content = _content()
    assert "AMPLIFIERD_PORT" in content


def test_original_log_level_setting_present() -> None:
    """Original 'log_level' setting must still be in the config table."""
    content = _content()
    assert "AMPLIFIERD_LOG_LEVEL" in content


def test_original_default_working_dir_present() -> None:
    """Original 'default_working_dir' setting must still be in the config table."""
    content = _content()
    assert "AMPLIFIERD_DEFAULT_WORKING_DIR" in content


def test_original_disabled_plugins_present() -> None:
    """Original 'disabled_plugins' setting must still be in the config table."""
    content = _content()
    assert "AMPLIFIERD_DISABLED_PLUGINS" in content


# ---------------------------------------------------------------------------
# New security/proxy settings (8 new rows)
# ---------------------------------------------------------------------------


def test_tls_mode_setting_present() -> None:
    """New 'tls_mode' setting must appear in the config table."""
    content = _content()
    assert "tls_mode" in content
    assert "AMPLIFIERD_TLS_MODE" in content


def test_auth_enabled_setting_present() -> None:
    """New 'auth_enabled' setting must appear in the config table."""
    content = _content()
    assert "auth_enabled" in content
    assert "AMPLIFIERD_AUTH_ENABLED" in content


def test_trust_proxy_auth_setting_present() -> None:
    """New 'trust_proxy_auth' setting must appear in the config table."""
    content = _content()
    assert "trust_proxy_auth" in content
    assert "AMPLIFIERD_TRUST_PROXY_AUTH" in content


def test_trusted_proxies_setting_present() -> None:
    """New 'trusted_proxies' setting must appear in the config table."""
    content = _content()
    assert "trusted_proxies" in content
    assert "AMPLIFIERD_TRUSTED_PROXIES" in content


def test_cookie_secure_setting_present() -> None:
    """New 'cookie_secure' setting must appear in the config table."""
    content = _content()
    assert "cookie_secure" in content
    assert "AMPLIFIERD_COOKIE_SECURE" in content


def test_cookie_samesite_setting_present() -> None:
    """New 'cookie_samesite' setting must appear in the config table."""
    content = _content()
    assert "cookie_samesite" in content
    assert "AMPLIFIERD_COOKIE_SAMESITE" in content


def test_api_key_setting_present() -> None:
    """New 'api_key' setting must appear in the config table."""
    content = _content()
    assert "api_key" in content
    assert "AMPLIFIERD_API_KEY" in content


def test_allowed_origins_setting_present() -> None:
    """New 'allowed_origins' setting must appear in the config table."""
    content = _content()
    assert "allowed_origins" in content
    assert "AMPLIFIERD_ALLOWED_ORIGINS" in content


# ---------------------------------------------------------------------------
# Table structure: 13 rows total
# ---------------------------------------------------------------------------


def test_config_table_has_15_rows() -> None:
    """Config table must have 15 data rows (5 original + 10 new settings).

    The 10 new settings are: tls_mode, tls_certfile, tls_keyfile, auth_enabled,
    trust_proxy_auth, trusted_proxies, cookie_secure, cookie_samesite, api_key,
    allowed_origins.

    We count rows by looking for lines starting with '|' inside the config table section.
    """
    content = _content()
    lines = content.splitlines()

    # Find the config table section
    in_table = False
    row_count = 0
    for line in lines:
        stripped = line.strip()
        # Detect table start (header row)
        if "| Setting" in stripped and "| Env var" in stripped:
            in_table = True
            continue
        if in_table:
            # Separator row
            if stripped.startswith("|---"):
                continue
            # Data row
            if stripped.startswith("|"):
                row_count += 1
            else:
                # Table ended
                break

    assert row_count == 15, (
        f"Expected 15 config table rows, found {row_count}. "
        "Did you add all 10 new security/proxy/TLS settings?"
    )


# ---------------------------------------------------------------------------
# Link to HOSTING.md
# ---------------------------------------------------------------------------


def test_hosting_md_link_present() -> None:
    """README must contain a link to docs/HOSTING.md after the config table."""
    content = _content()
    assert "docs/HOSTING.md" in content, (
        "README must link to docs/HOSTING.md for deployment and security details"
    )


def test_hosting_md_link_mentions_deployment() -> None:
    """The HOSTING.md link context must mention deployment modes or security."""
    content = _content()
    lower = content.lower()
    assert "deployment" in lower or "security" in lower, (
        "README must mention deployment modes or security configuration near HOSTING.md link"
    )
