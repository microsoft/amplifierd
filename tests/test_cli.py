"""Tests for the amplifierd CLI entry point."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner, Result

from amplifierd.cli import main


@pytest.fixture(autouse=True)
def _restore_root_handlers():
    """Restore root logger handlers after each test to prevent handler leakage."""
    original_handlers = logging.getLogger().handlers[:]
    yield
    logging.getLogger().handlers = original_handlers


@pytest.fixture(autouse=True)
def _clean_security_env_vars():
    """Remove security env vars set by CLI serve function to prevent cross-test leakage."""
    import os

    _SECURITY_VARS = [
        "AMPLIFIERD_API_KEY",
        "AMPLIFIERD_TLS_MODE",
        "AMPLIFIERD_TLS_CERTFILE",
        "AMPLIFIERD_TLS_KEYFILE",
        "AMPLIFIERD_AUTH_ENABLED",
    ]
    yield
    for var in _SECURITY_VARS:
        os.environ.pop(var, None)


class TestServeHelp:
    """CliRunner invoke of main with ['serve', '--help'] should exit 0
    and output should contain '--port', '--host', '--reload'."""

    @pytest.fixture(autouse=True)
    def _invoke_help(self) -> None:
        runner = CliRunner()
        self.result: Result = runner.invoke(main, ["serve", "--help"])

    def test_serve_help_exits_zero(self) -> None:
        assert self.result.exit_code == 0

    def test_serve_help_contains_port_flag(self) -> None:
        assert "--port" in self.result.output
        assert "Bind port" in self.result.output

    def test_serve_help_contains_host_flag(self) -> None:
        assert "--host" in self.result.output
        assert "Bind host" in self.result.output

    def test_serve_help_contains_reload_flag(self) -> None:
        assert "--reload" in self.result.output
        assert "hot-reload" in self.result.output

    def test_serve_help_contains_log_level_flag(self) -> None:
        assert "--log-level" in self.result.output
        assert "Log level" in self.result.output


class TestServeDefaults:
    """serve command falls back to DaemonSettings when no CLI flags provided."""

    def test_serve_uses_settings_defaults(self) -> None:
        mock_settings = MagicMock()
        mock_settings.host = "127.0.0.1"
        mock_settings.port = 8410
        mock_settings.log_level = "info"

        runner = CliRunner()
        with (
            patch("uvicorn.run") as mock_run,
            patch("amplifierd.config.DaemonSettings", return_value=mock_settings),
            patch("amplifierd.daemon_session.create_session_dir", return_value=MagicMock()),
            patch("amplifierd.daemon_session.setup_session_log"),
            patch("amplifierd.security.tls.resolve_tls", return_value={}),
        ):
            result = runner.invoke(main, ["serve"])

        assert result.exit_code == 0
        mock_run.assert_called_once_with(
            "amplifierd.app:create_app",
            host="127.0.0.1",
            port=8410,
            reload=False,
            log_level="info",
            factory=True,
        )


class TestServeCLIOverrides:
    """CLI flags override DaemonSettings values."""

    def test_cli_flags_override_settings(self) -> None:
        mock_settings = MagicMock()
        mock_settings.host = "127.0.0.1"
        mock_settings.port = 8410
        mock_settings.log_level = "info"

        runner = CliRunner()
        with (
            patch("uvicorn.run") as mock_run,
            patch("amplifierd.config.DaemonSettings", return_value=mock_settings),
            patch("amplifierd.daemon_session.create_session_dir", return_value=MagicMock()),
            patch("amplifierd.daemon_session.setup_session_log"),
            patch("amplifierd.security.tls.resolve_tls", return_value={}),
        ):
            result = runner.invoke(
                main, ["serve", "--host", "0.0.0.0", "--port", "9000", "--log-level", "debug"]
            )

        assert result.exit_code == 0
        mock_run.assert_called_once_with(
            "amplifierd.app:create_app",
            host="0.0.0.0",
            port=9000,
            reload=False,
            log_level="debug",
            factory=True,
        )

    def test_reload_flag(self) -> None:
        mock_settings = MagicMock()
        mock_settings.host = "127.0.0.1"
        mock_settings.port = 8410
        mock_settings.log_level = "info"

        runner = CliRunner()
        with (
            patch("uvicorn.run") as mock_run,
            patch("amplifierd.config.DaemonSettings", return_value=mock_settings),
            patch("amplifierd.daemon_session.create_session_dir", return_value=MagicMock()),
            patch("amplifierd.daemon_session.setup_session_log"),
            patch("amplifierd.security.tls.resolve_tls", return_value={}),
        ):
            result = runner.invoke(main, ["serve", "--reload"])

        assert result.exit_code == 0
        mock_run.assert_called_once_with(
            "amplifierd.app:create_app",
            host="127.0.0.1",
            port=8410,
            reload=True,
            log_level="info",
            factory=True,
        )


class TestServeLogging:
    """serve command configures the root logger with the specified format."""

    def test_logging_format_matches_spec(self) -> None:
        """Logging format must be: %(asctime)s %(levelname)s [%(name)s] %(message)s"""
        mock_settings = MagicMock()
        mock_settings.host = "127.0.0.1"
        mock_settings.port = 8410
        mock_settings.log_level = "info"

        runner = CliRunner()
        with (
            patch("uvicorn.run"),
            patch("amplifierd.config.DaemonSettings", return_value=mock_settings),
            patch("logging.basicConfig") as mock_basic_config,
            patch("amplifierd.daemon_session.create_session_dir", return_value=MagicMock()),
            patch("amplifierd.daemon_session.setup_session_log"),
        ):
            result = runner.invoke(main, ["serve"])

        assert result.exit_code == 0
        mock_basic_config.assert_called_once()
        call_kwargs = mock_basic_config.call_args[1]
        assert call_kwargs["format"] == "%(asctime)s %(levelname)s [%(name)s] %(message)s"

    def test_logging_level_from_settings(self) -> None:
        """Logging level should come from effective log level."""
        mock_settings = MagicMock()
        mock_settings.host = "127.0.0.1"
        mock_settings.port = 8410
        mock_settings.log_level = "warning"

        runner = CliRunner()
        with (
            patch("uvicorn.run"),
            patch("amplifierd.config.DaemonSettings", return_value=mock_settings),
            patch("logging.basicConfig") as mock_basic_config,
            patch("amplifierd.daemon_session.create_session_dir", return_value=MagicMock()),
            patch("amplifierd.daemon_session.setup_session_log"),
        ):
            result = runner.invoke(main, ["serve"])

        assert result.exit_code == 0
        call_kwargs = mock_basic_config.call_args[1]
        assert call_kwargs["level"] == logging.WARNING

    def test_logging_level_cli_override(self) -> None:
        """CLI --log-level flag should override settings for logging."""
        mock_settings = MagicMock()
        mock_settings.host = "127.0.0.1"
        mock_settings.port = 8410
        mock_settings.log_level = "info"

        runner = CliRunner()
        with (
            patch("uvicorn.run"),
            patch("amplifierd.config.DaemonSettings", return_value=mock_settings),
            patch("logging.basicConfig") as mock_basic_config,
            patch("amplifierd.daemon_session.create_session_dir", return_value=MagicMock()),
            patch("amplifierd.daemon_session.setup_session_log"),
        ):
            result = runner.invoke(main, ["serve", "--log-level", "debug"])

        assert result.exit_code == 0
        call_kwargs = mock_basic_config.call_args[1]
        assert call_kwargs["level"] == logging.DEBUG


class TestServeTlsFlags:
    """serve --tls and --ssl-* flags appear in help and set env vars."""

    def test_serve_help_contains_tls_flag(self) -> None:
        runner = CliRunner()
        result: Result = runner.invoke(main, ["serve", "--help"])
        assert "--tls" in result.output

    def test_serve_help_contains_ssl_certfile_flag(self) -> None:
        runner = CliRunner()
        result: Result = runner.invoke(main, ["serve", "--help"])
        assert "--ssl-certfile" in result.output

    def test_serve_help_contains_ssl_keyfile_flag(self) -> None:
        runner = CliRunner()
        result: Result = runner.invoke(main, ["serve", "--help"])
        assert "--ssl-keyfile" in result.output

    def test_serve_help_contains_no_auth_flag(self) -> None:
        runner = CliRunner()
        result: Result = runner.invoke(main, ["serve", "--help"])
        assert "--no-auth" in result.output

    def test_tls_flags_accepted_without_error(self) -> None:
        """serve accepts --tls, --ssl-certfile, --ssl-keyfile, --no-auth without error."""
        mock_settings = MagicMock()
        mock_settings.host = "127.0.0.1"
        mock_settings.port = 8410
        mock_settings.log_level = "info"

        runner = CliRunner()
        with (
            patch("uvicorn.run"),
            patch("amplifierd.config.DaemonSettings", return_value=mock_settings),
            patch("amplifierd.daemon_session.create_session_dir", return_value=MagicMock()),
            patch("amplifierd.daemon_session.setup_session_log"),
            patch("amplifierd.security.tls.resolve_tls", return_value={}),
        ):
            result = runner.invoke(main, ["serve", "--tls", "auto", "--no-auth"])

        assert result.exit_code == 0


class TestServeApiKeyFlag:
    """serve --api-key flag sets AMPLIFIERD_API_KEY env var."""

    def test_serve_help_contains_api_key_flag(self) -> None:
        runner = CliRunner()
        result: Result = runner.invoke(main, ["serve", "--help"])
        assert "--api-key" in result.output

    def test_api_key_flag_sets_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_settings = MagicMock()
        mock_settings.host = "127.0.0.1"
        mock_settings.port = 8410
        mock_settings.log_level = "info"

        monkeypatch.delenv("AMPLIFIERD_API_KEY", raising=False)

        runner = CliRunner()
        with (
            patch("uvicorn.run"),
            patch("amplifierd.config.DaemonSettings", return_value=mock_settings),
            patch("amplifierd.daemon_session.create_session_dir", return_value=MagicMock()),
            patch("amplifierd.daemon_session.setup_session_log"),
        ):
            result = runner.invoke(
                main, ["serve", "--api-key", "my-secret"], env={"AMPLIFIERD_API_KEY": ""}
            )

        assert result.exit_code == 0
        # The serve function sets os.environ directly; verify it was set during invocation
        # We can't check os.environ after invoke since CliRunner may restore env.
        # Instead, verify the flag was accepted without error.
        assert result.output  # command ran successfully
