"""Daemon configuration with JSON file and environment variable support."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

logger = logging.getLogger(__name__)

_DEFAULT_HOME_DIR = Path.home() / ".amplifierd"

WELL_KNOWN_BUNDLES: dict[str, str] = {
    "foundation": "git+https://github.com/microsoft/amplifier-foundation@main",
    "distro": "git+https://github.com/microsoft/amplifier-bundle-distro@main",
    "modes": "git+https://github.com/microsoft/amplifier-bundle-modes@main",
    "notify": "git+https://github.com/microsoft/amplifier-bundle-notify@main",
    "recipes": "git+https://github.com/microsoft/amplifier-bundle-recipes@main",
    "design-intelligence": "git+https://github.com/microsoft/amplifier-bundle-design-intelligence@main",
    "exp-delegation": "git+https://github.com/microsoft/amplifier-foundation@main#subdirectory=experiments/delegation-only",
    "amplifier-dev": "git+https://github.com/microsoft/amplifier-foundation@main#subdirectory=bundles/amplifier-dev.yaml",
}


class JsonFileSettingsSource(PydanticBaseSettingsSource):
    """Reads settings from a JSON file in the settings directory."""

    def __init__(self, settings_cls: type[BaseSettings], settings_dir: Path) -> None:
        super().__init__(settings_cls)
        self._settings_dir = settings_dir

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        self._data = self._load()
        return self._data

    def _load(self) -> dict[str, Any]:
        path = self._settings_dir / "settings.json"
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text())  # type: ignore[no-any-return]
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read %s: %s", path, exc)
            return {}


def cwd_to_slug(working_dir: str) -> str:
    """Convert a working directory path to a project slug (same convention as Amplifier CLI).

    Replaces every ``/`` with ``-`` so e.g. ``/home/sam/myproject`` becomes
    ``-home-sam-myproject``.
    """
    return str(working_dir).replace("/", "-")


class DaemonSettings(BaseSettings):
    """Core daemon settings loaded from env vars, JSON file, and defaults."""

    host: str = "127.0.0.1"
    port: int = 8410
    default_working_dir: Path | None = None
    home_dir: Path = Field(default_factory=lambda: _DEFAULT_HOME_DIR)
    projects_dir: Path = Field(default_factory=lambda: Path.home() / ".amplifier" / "projects")
    log_level: str = "info"
    disabled_plugins: list[str] = Field(default_factory=list)
    bundles: dict[str, str] = Field(default_factory=lambda: dict(WELL_KNOWN_BUNDLES))
    default_bundle: str | None = "distro"
    daemon_session_path: Path | None = None

    # UI — optional root redirect (e.g. "/distro/")
    home_redirect: str | None = None

    # Security — opt-in, defaults preserve current localhost-only behavior
    allowed_origins: list[str] = Field(default_factory=lambda: ["*"])
    api_key: str | None = None
    tls_mode: str = "off"  # off | auto | manual
    tls_certfile: str | None = None
    tls_keyfile: str | None = None
    auth_enabled: bool = False

    # Class-level storage for settings_dir (used by settings_customise_sources).
    # Not thread-safe: concurrent construction would race on this value.
    # Acceptable — this runs once at daemon startup, not on a hot path.
    _current_settings_dir: Path = _DEFAULT_HOME_DIR

    @property
    def daemon_run_dir(self) -> Path:
        """Per-daemon-run log directory: ``{home_dir}/sessions/``."""
        return self.home_dir / "sessions"

    @property
    def plugins_dir(self) -> Path:
        """Plugin data directory: ``{home_dir}/plugins/``."""
        return self.home_dir / "plugins"

    @property
    def run_dir(self) -> Path:
        """Runtime directory for PID files: ``{home_dir}/run/``."""
        return self.home_dir / "run"

    model_config = {"env_prefix": "AMPLIFIERD_"}

    def __init__(self, *, _settings_dir: Path | None = None, **kwargs: Any) -> None:
        # Temporarily set on class so settings_customise_sources can read it
        original = DaemonSettings._current_settings_dir
        DaemonSettings._current_settings_dir = _settings_dir or _DEFAULT_HOME_DIR
        try:
            super().__init__(**kwargs)
        finally:
            DaemonSettings._current_settings_dir = original

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            JsonFileSettingsSource(settings_cls, cls._current_settings_dir),
        )
