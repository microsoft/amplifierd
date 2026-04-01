"""Tests that docs/building-apps.md contains hosting considerations for app authors.

This test suite validates the required documentation coverage for the
'Hosting considerations for app authors' section.
"""

from __future__ import annotations

from pathlib import Path

BUILDING_APPS_MD = Path(__file__).parent.parent / "docs" / "building-apps.md"


def _content() -> str:
    assert BUILDING_APPS_MD.exists(), f"docs/building-apps.md does not exist at {BUILDING_APPS_MD}"
    return BUILDING_APPS_MD.read_text()


# ---------------------------------------------------------------------------
# Section: Hosting considerations for app authors
# ---------------------------------------------------------------------------


def test_hosting_considerations_section_present() -> None:
    """Hosting considerations section heading must exist."""
    content = _content()
    assert "## Hosting considerations for app authors" in content


def test_dont_implement_own_auth_present() -> None:
    """'Don't implement your own auth' subsection must exist."""
    content = _content()
    assert (
        "Don't implement your own auth" in content or "implement your own auth" in content.lower()
    )


def test_x_authenticated_user_header_documented() -> None:
    """X-Authenticated-User header must be referenced in the auth section."""
    content = _content()
    assert "X-Authenticated-User" in content


def test_request_state_authenticated_user_documented() -> None:
    """request.state.authenticated_user pattern must be documented."""
    content = _content()
    assert "request.state.authenticated_user" in content


def test_smart_defaults_host_section_present() -> None:
    """Smart defaults from --host 0.0.0.0 must be explained."""
    content = _content()
    assert "--host 0.0.0.0" in content or "0.0.0.0" in content


def test_port_auto_increment_references_find_available_port() -> None:
    """Port auto-increment section must reference find_available_port."""
    content = _content()
    assert "find_available_port" in content


def test_hosting_section_before_checklist() -> None:
    """Hosting considerations section must appear before ## Checklist."""
    content = _content()
    hosting_idx = content.find("## Hosting considerations for app authors")
    checklist_idx = content.find("## Checklist")
    assert hosting_idx != -1, "Hosting considerations section not found"
    assert checklist_idx != -1, "Checklist section not found"
    assert hosting_idx < checklist_idx, "Hosting section must appear before Checklist"


# ---------------------------------------------------------------------------
# Checklist items
# ---------------------------------------------------------------------------


def test_checklist_has_authenticated_user_item() -> None:
    """Checklist must include item about reading request.state.authenticated_user."""
    content = _content()
    assert "request.state.authenticated_user" in content
    # Verify it's in a checklist context (has a checkbox marker)
    lines = content.splitlines()
    found = any(
        "request.state.authenticated_user" in line
        and ("- [ ]" in line or "- [x]" in line or "- [X]" in line)
        for line in lines
    )
    assert found, "Checklist item for request.state.authenticated_user not found"


def test_checklist_has_localhost_proxy_modes_item() -> None:
    """Checklist must include item about working in localhost and behind-proxy modes."""
    content = _content()
    lines = content.splitlines()
    # Look for a checklist item mentioning both localhost and proxy modes
    found = any(
        ("localhost" in line or "proxy" in line)
        and ("- [ ]" in line or "- [x]" in line or "- [X]" in line)
        and ("auth" in line.lower() or "mode" in line.lower())
        for line in lines
    )
    assert found, "Checklist item for localhost/proxy mode compatibility not found"
