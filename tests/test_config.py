"""Tests for DaemonSettings configuration."""

import json
from pathlib import Path

import pytest


@pytest.mark.unit
class TestDaemonSettings:
    """Tests for DaemonSettings with defaults, env vars, and JSON file support."""

    def test_defaults(self, tmp_path: Path):
        """DaemonSettings provides correct defaults when no overrides given."""
        from amplifierd.config import DaemonSettings

        settings = DaemonSettings(_settings_dir=tmp_path)
        assert settings.host == "127.0.0.1"
        assert settings.port == 8410
        assert settings.default_working_dir is None
        assert settings.log_level == "info"
        assert settings.disabled_plugins == []

    def test_env_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Environment variables with AMPLIFIERD_ prefix override defaults."""
        from amplifierd.config import DaemonSettings

        monkeypatch.setenv("AMPLIFIERD_PORT", "9999")
        monkeypatch.setenv("AMPLIFIERD_HOST", "0.0.0.0")
        monkeypatch.setenv("AMPLIFIERD_LOG_LEVEL", "debug")
        settings = DaemonSettings(_settings_dir=tmp_path)
        assert settings.port == 9999
        assert settings.host == "0.0.0.0"
        assert settings.log_level == "debug"

    def test_json_settings_file(self, tmp_path: Path):
        """Settings are loaded from a JSON file in the settings directory."""
        from amplifierd.config import DaemonSettings

        settings_file = tmp_path / "settings.json"
        settings_file.write_text(
            json.dumps(
                {
                    "port": 7777,
                    "default_working_dir": "/tmp/work",
                    "disabled_plugins": ["pluginA", "pluginB"],
                }
            )
        )
        settings = DaemonSettings(_settings_dir=tmp_path)
        assert settings.port == 7777
        assert settings.default_working_dir == Path("/tmp/work")
        assert settings.disabled_plugins == ["pluginA", "pluginB"]

    def test_env_overrides_json_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Env vars take priority over JSON file values."""
        from amplifierd.config import DaemonSettings

        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps({"port": 7777, "host": "192.168.1.1"}))
        monkeypatch.setenv("AMPLIFIERD_PORT", "5555")
        settings = DaemonSettings(_settings_dir=tmp_path)
        assert settings.port == 5555
        assert settings.host == "192.168.1.1"

    def test_invalid_json_settings_file(self, tmp_path: Path, caplog: pytest.LogCaptureFixture):
        """Malformed JSON in settings.json logs a warning and falls back to defaults."""
        from amplifierd.config import DaemonSettings

        settings_file = tmp_path / "settings.json"
        settings_file.write_text("{invalid json!!!")
        with caplog.at_level("WARNING", logger="amplifierd.config"):
            settings = DaemonSettings(_settings_dir=tmp_path)
        assert settings.host == "127.0.0.1"
        assert settings.port == 8410
        assert settings.disabled_plugins == []
        assert any("Failed to read" in msg for msg in caplog.messages)

    def test_missing_settings_file_uses_defaults(self, tmp_path: Path):
        """When settings.json doesn't exist, defaults are used without error."""
        from amplifierd.config import DaemonSettings

        settings = DaemonSettings(_settings_dir=tmp_path / "nonexistent")
        assert settings.host == "127.0.0.1"
        assert settings.port == 8410

    def test_bundles_defaults_to_well_known(self, tmp_path: Path):
        """Bundles default to the WELL_KNOWN_BUNDLES dict."""
        from amplifierd.config import WELL_KNOWN_BUNDLES, DaemonSettings

        settings = DaemonSettings(_settings_dir=tmp_path)
        assert settings.bundles == WELL_KNOWN_BUNDLES
        assert "foundation" in settings.bundles
        assert "distro" in settings.bundles
        assert "modes" in settings.bundles

    def test_default_bundle_defaults_to_distro(self, tmp_path: Path):
        """Default bundle defaults to 'distro'."""
        from amplifierd.config import DaemonSettings

        settings = DaemonSettings(_settings_dir=tmp_path)
        assert settings.default_bundle == "distro"

    def test_bundles_override_from_json(self, tmp_path: Path):
        """Bundles in settings.json replace the default well-known bundles."""
        from amplifierd.config import DaemonSettings

        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps({"bundles": {"custom": "file:///tmp/mybundle"}}))
        settings = DaemonSettings(_settings_dir=tmp_path)
        assert settings.bundles == {"custom": "file:///tmp/mybundle"}

    def test_bundles_from_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """AMPLIFIERD_BUNDLES env var overrides default bundles."""
        from amplifierd.config import DaemonSettings

        monkeypatch.setenv(
            "AMPLIFIERD_BUNDLES",
            json.dumps({"my-bundle": "git+https://example.com/bundle"}),
        )
        settings = DaemonSettings(_settings_dir=tmp_path)
        assert settings.bundles == {"my-bundle": "git+https://example.com/bundle"}

    def test_default_bundle_from_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """AMPLIFIERD_DEFAULT_BUNDLE env var overrides default."""
        from amplifierd.config import DaemonSettings

        monkeypatch.setenv("AMPLIFIERD_DEFAULT_BUNDLE", "foundation")
        settings = DaemonSettings(_settings_dir=tmp_path)
        assert settings.default_bundle == "foundation"

    def test_allowed_origins_defaults_to_wildcard(self, tmp_path: Path):
        """allowed_origins defaults to ['*'] (permit all)."""
        from amplifierd.config import DaemonSettings

        settings = DaemonSettings(_settings_dir=tmp_path)
        assert settings.allowed_origins == ["*"]

    def test_api_key_defaults_to_none(self, tmp_path: Path):
        """api_key defaults to None (no auth)."""
        from amplifierd.config import DaemonSettings

        settings = DaemonSettings(_settings_dir=tmp_path)
        assert settings.api_key is None

    def test_allowed_origins_from_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """AMPLIFIERD_ALLOWED_ORIGINS env var overrides default."""
        from amplifierd.config import DaemonSettings

        monkeypatch.setenv(
            "AMPLIFIERD_ALLOWED_ORIGINS",
            '["http://localhost:3000","https://my.host"]',
        )
        settings = DaemonSettings(_settings_dir=tmp_path)
        assert settings.allowed_origins == ["http://localhost:3000", "https://my.host"]

    def test_api_key_from_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """AMPLIFIERD_API_KEY env var sets the api_key."""
        from amplifierd.config import DaemonSettings

        monkeypatch.setenv("AMPLIFIERD_API_KEY", "my-secret-key")
        settings = DaemonSettings(_settings_dir=tmp_path)
        assert settings.api_key == "my-secret-key"

    def test_allowed_origins_from_json(self, tmp_path: Path):
        """allowed_origins can be set from settings.json."""
        from amplifierd.config import DaemonSettings

        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps({"allowed_origins": ["https://example.com"]}))
        settings = DaemonSettings(_settings_dir=tmp_path)
        assert settings.allowed_origins == ["https://example.com"]

    def test_api_key_from_json(self, tmp_path: Path):
        """api_key can be set from settings.json."""
        from amplifierd.config import DaemonSettings

        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps({"api_key": "json-secret"}))
        settings = DaemonSettings(_settings_dir=tmp_path)
        assert settings.api_key == "json-secret"

    def test_tls_mode_defaults_to_off(self, tmp_path: Path):
        """tls_mode defaults to 'off'."""
        from amplifierd.config import DaemonSettings

        settings = DaemonSettings(_settings_dir=tmp_path)
        assert settings.tls_mode == "off"

    def test_tls_certfile_defaults_to_none(self, tmp_path: Path):
        """tls_certfile defaults to None."""
        from amplifierd.config import DaemonSettings

        settings = DaemonSettings(_settings_dir=tmp_path)
        assert settings.tls_certfile is None

    def test_auth_enabled_defaults_to_false(self, tmp_path: Path):
        """auth_enabled defaults to False."""
        from amplifierd.config import DaemonSettings

        settings = DaemonSettings(_settings_dir=tmp_path)
        assert settings.auth_enabled is False

    def test_tls_mode_from_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """AMPLIFIERD_TLS_MODE env var overrides default."""
        from amplifierd.config import DaemonSettings

        monkeypatch.setenv("AMPLIFIERD_TLS_MODE", "auto")
        settings = DaemonSettings(_settings_dir=tmp_path)
        assert settings.tls_mode == "auto"

    def test_auth_enabled_from_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """AMPLIFIERD_AUTH_ENABLED env var overrides default."""
        from amplifierd.config import DaemonSettings

        monkeypatch.setenv("AMPLIFIERD_AUTH_ENABLED", "true")
        settings = DaemonSettings(_settings_dir=tmp_path)
        assert settings.auth_enabled is True
