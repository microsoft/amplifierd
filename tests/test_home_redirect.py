"""Tests for the configurable home redirect (GET /)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from amplifierd.app import create_app
from amplifierd.config import DaemonSettings


@pytest.mark.unit
class TestHomeRedirectConfig:
    """Tests for the home_redirect setting in DaemonSettings."""

    def test_home_redirect_defaults_to_none(self, tmp_path: Path) -> None:
        """home_redirect defaults to None (no root route)."""
        settings = DaemonSettings(_settings_dir=tmp_path)
        assert settings.home_redirect is None

    def test_home_redirect_from_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AMPLIFIERD_HOME_REDIRECT env var sets the redirect target."""
        monkeypatch.setenv("AMPLIFIERD_HOME_REDIRECT", "/distro/")
        settings = DaemonSettings(_settings_dir=tmp_path)
        assert settings.home_redirect == "/distro/"

    def test_home_redirect_from_json(self, tmp_path: Path) -> None:
        """home_redirect can be set from settings.json."""
        import json

        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps({"home_redirect": "/my-app/"}))
        settings = DaemonSettings(_settings_dir=tmp_path)
        assert settings.home_redirect == "/my-app/"


@pytest.mark.unit
class TestHomeRedirectRoute:
    """Tests for GET / behavior based on home_redirect setting."""

    def test_root_returns_404_when_no_redirect(self) -> None:
        """GET / returns 404 when home_redirect is not configured."""
        app = create_app(DaemonSettings(home_redirect=None))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 404

    def test_root_redirects_when_configured(self) -> None:
        """GET / returns 307 redirect to the configured target."""
        app = create_app(DaemonSettings(home_redirect="/distro/"))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == "/distro/"

    def test_root_redirects_to_custom_target(self) -> None:
        """GET / redirects to whatever path is configured."""
        app = create_app(DaemonSettings(home_redirect="/my-dashboard/"))
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == "/my-dashboard/"
