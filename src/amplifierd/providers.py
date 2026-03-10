"""Provider configuration loading and injection.

Reads provider config from ~/.amplifier/settings.yaml (same source as amplifier-app-cli)
and injects it into PreparedBundle before session creation.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_ENV_PATTERN = re.compile(r"\$\{([^}:]+)(?::([^}]*))?}")


def load_provider_config(home: Path | None = None) -> list[dict[str, Any]]:
    """Load provider configuration from ~/.amplifier/settings.yaml.

    Args:
        home: Amplifier home directory. Falls back to AMPLIFIER_HOME env var,
              then ~/.amplifier.

    Returns:
        List of provider config dicts from config.providers, or empty list.
    """
    if home is None:
        home = Path(os.environ.get("AMPLIFIER_HOME", Path.home() / ".amplifier"))
    settings_path = home / "settings.yaml"
    if not settings_path.is_file():
        logger.debug("No settings file at %s", settings_path)
        return []
    try:
        data = yaml.safe_load(settings_path.read_text()) or {}
    except Exception:
        logger.warning("Failed to read %s", settings_path, exc_info=True)
        return []
    providers = data.get("config", {}).get("providers", [])
    if not isinstance(providers, list):
        return []
    logger.info(
        "Loaded %d provider(s) from %s: %s",
        len(providers),
        settings_path,
        [p.get("module", "?") for p in providers if isinstance(p, dict)],
    )
    return providers


def expand_env_vars(value: Any) -> Any:
    """Recursively expand ${VAR} and ${VAR:default} references in config values.

    After expansion, dict entries whose values are empty strings are removed.
    This prevents empty env vars (e.g. ANTHROPIC_BASE_URL='') from overriding
    provider defaults with blank values.
    """
    if isinstance(value, str):
        return _ENV_PATTERN.sub(
            lambda m: os.environ.get(m.group(1), m.group(2) if m.group(2) is not None else ""),
            value,
        )
    if isinstance(value, dict):
        expanded = {k: expand_env_vars(v) for k, v in value.items()}
        return {k: v for k, v in expanded.items() if v != ""}
    if isinstance(value, list):
        return [expand_env_vars(item) for item in value]
    return value


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *overlay* into *base*.

    - Nested dicts are merged recursively (overlay wins on leaf conflicts).
    - All other types: overlay value replaces base value.
    """
    result = base.copy()
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _merge_provider_item(
    bundle_item: dict[str, Any], settings_item: dict[str, Any]
) -> dict[str, Any]:
    """Merge a single settings provider entry on top of a bundle provider entry.

    The ``config`` sub-dict is deep-merged so that bundle-specific keys
    (e.g. ``debug``, ``default_model``) survive when settings only provides
    runtime keys (e.g. ``api_key``, ``base_url``).  All other top-level
    fields (``source``, ``module``, etc.) are taken from settings when present.
    """
    merged = bundle_item.copy()
    for key, value in settings_item.items():
        if key == "config" and key in merged:
            if isinstance(merged["config"], dict) and isinstance(value, dict):
                merged["config"] = _deep_merge(merged["config"], value)
            else:
                merged["config"] = value
        else:
            merged[key] = value
    return merged


def merge_settings_providers(
    existing: list[dict[str, Any]], settings_providers: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Merge settings providers into an existing provider list.

    Semantics:

    * **Bundle has providers** -- settings can only *update* providers the
      bundle already declared.  New providers from settings are silently
      dropped.  Matching providers are deep-merged (``config`` sub-dict is
      merged recursively; other top-level fields are replaced).
    * **Bundle has no providers** (provider-agnostic bundle) -- the full
      settings provider list is used as-is so the session has something to
      work with.

    Environment variable references (``${VAR}``) are **not** expanded here.
    Expansion happens later in ``inject_providers`` after the merge, so that
    bundle-owned values survive when an env var is unset in the daemon.

    Args:
        existing: Current provider list (from bundle).
        settings_providers: Provider config list from load_provider_config().

    Returns:
        Merged provider list (env vars still unexpanded).
    """
    if not settings_providers:
        return list(existing)

    # Provider-agnostic bundle: settings take over entirely.
    if not existing:
        return list(settings_providers)

    # Bundle declares providers: settings may only update matches.
    settings_by_module: dict[str, dict[str, Any]] = {
        p["module"]: p for p in settings_providers if isinstance(p, dict) and "module" in p
    }
    result: list[dict[str, Any]] = []
    for p in existing:
        module_id = p.get("module") if isinstance(p, dict) else None
        if module_id and module_id in settings_by_module:
            result.append(_merge_provider_item(p, settings_by_module[module_id]))
        else:
            result.append(p)
    return result


def inject_providers(bundle: Any, providers: list[dict[str, Any]]) -> None:
    """Inject provider config into a Bundle before prepare().

    Must be called BEFORE bundle.prepare() so that the activation step
    downloads and installs provider dependencies (e.g. the anthropic SDK).

    The merge preserves the bundle's provider list as a whitelist -- settings
    can configure existing providers but never inject new ones.  Environment
    variables are expanded *after* the merge so that bundle-owned config
    keys survive when a ${VAR} resolves to empty in the daemon.

    Args:
        bundle: An AmplifierBundle instance (has .providers list).
        providers: Provider config list from load_provider_config().
    """
    if not providers:
        return
    merged = merge_settings_providers(bundle.providers, providers)
    bundle.providers = expand_env_vars(merged)
