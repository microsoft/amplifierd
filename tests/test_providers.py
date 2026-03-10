"""Tests for provider configuration loading and injection."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.mark.unit
class TestLoadProviderConfig:
    """Tests for load_provider_config()."""

    def test_missing_file(self, tmp_path: Path) -> None:
        from amplifierd.providers import load_provider_config

        result = load_provider_config(home=tmp_path)
        assert result == []

    def test_no_providers_key(self, tmp_path: Path) -> None:
        from amplifierd.providers import load_provider_config

        (tmp_path / "settings.yaml").write_text("config:\n  other: true\n")
        result = load_provider_config(home=tmp_path)
        assert result == []

    def test_empty_config(self, tmp_path: Path) -> None:
        from amplifierd.providers import load_provider_config

        (tmp_path / "settings.yaml").write_text("")
        result = load_provider_config(home=tmp_path)
        assert result == []

    def test_reads_providers(self, tmp_path: Path) -> None:
        from amplifierd.providers import load_provider_config

        (tmp_path / "settings.yaml").write_text(
            "config:\n"
            "  providers:\n"
            "  - module: provider-anthropic\n"
            "    source: git+https://example.com/provider\n"
            "    config:\n"
            "      api_key: sk-test\n"
            "      default_model: claude-opus-4-6\n"
        )
        result = load_provider_config(home=tmp_path)
        assert len(result) == 1
        assert result[0]["module"] == "provider-anthropic"
        assert result[0]["config"]["api_key"] == "sk-test"

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        from amplifierd.providers import load_provider_config

        (tmp_path / "settings.yaml").write_text("{{invalid yaml!!")
        result = load_provider_config(home=tmp_path)
        assert result == []

    def test_providers_not_a_list(self, tmp_path: Path) -> None:
        from amplifierd.providers import load_provider_config

        (tmp_path / "settings.yaml").write_text("config:\n  providers: not-a-list\n")
        result = load_provider_config(home=tmp_path)
        assert result == []


@pytest.mark.unit
class TestExpandEnvVars:
    """Tests for expand_env_vars()."""

    def test_string_expansion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from amplifierd.providers import expand_env_vars

        monkeypatch.setenv("TEST_KEY", "my-secret")
        assert expand_env_vars("${TEST_KEY}") == "my-secret"

    def test_default_value(self) -> None:
        from amplifierd.providers import expand_env_vars

        result = expand_env_vars("${DEFINITELY_NOT_SET:fallback}")
        assert result == "fallback"

    def test_missing_no_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from amplifierd.providers import expand_env_vars

        monkeypatch.delenv("DEFINITELY_NOT_SET", raising=False)
        assert expand_env_vars("${DEFINITELY_NOT_SET}") == ""

    def test_nested_dict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from amplifierd.providers import expand_env_vars

        monkeypatch.setenv("MY_KEY", "secret123")
        result = expand_env_vars({"config": {"api_key": "${MY_KEY}", "model": "gpt-4"}})
        assert result == {"config": {"api_key": "secret123", "model": "gpt-4"}}

    def test_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from amplifierd.providers import expand_env_vars

        monkeypatch.setenv("A", "1")
        monkeypatch.setenv("B", "2")
        assert expand_env_vars(["${A}", "${B}", "literal"]) == ["1", "2", "literal"]

    def test_empty_env_var_stripped_from_dict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from amplifierd.providers import expand_env_vars

        monkeypatch.setenv("GOOD_KEY", "value")
        monkeypatch.setenv("EMPTY_KEY", "")
        monkeypatch.delenv("MISSING_KEY", raising=False)
        result = expand_env_vars({
            "good": "${GOOD_KEY}",
            "empty": "${EMPTY_KEY}",
            "missing": "${MISSING_KEY}",
            "literal": "keep",
        })
        assert result == {"good": "value", "literal": "keep"}

    def test_non_string_passthrough(self) -> None:
        from amplifierd.providers import expand_env_vars

        assert expand_env_vars(42) == 42
        assert expand_env_vars(True) is True
        assert expand_env_vars(None) is None


def _make_bundle(
    providers: list[dict[str, Any]] | None = None,
) -> SimpleNamespace:
    """Create a fake Bundle for testing."""
    return SimpleNamespace(providers=list(providers or []))


@pytest.mark.unit
class TestDeepMerge:
    """Tests for _deep_merge()."""

    def test_flat_overlay(self) -> None:
        from amplifierd.providers import _deep_merge

        base = {"a": 1, "b": 2}
        overlay = {"b": 3, "c": 4}
        assert _deep_merge(base, overlay) == {"a": 1, "b": 3, "c": 4}

    def test_nested_merge(self) -> None:
        from amplifierd.providers import _deep_merge

        base = {"config": {"model": "gpt-4", "debug": True}}
        overlay = {"config": {"api_key": "sk-123"}}
        result = _deep_merge(base, overlay)
        assert result == {"config": {"model": "gpt-4", "debug": True, "api_key": "sk-123"}}

    def test_overlay_wins_on_conflict(self) -> None:
        from amplifierd.providers import _deep_merge

        base = {"config": {"model": "old"}}
        overlay = {"config": {"model": "new"}}
        assert _deep_merge(base, overlay) == {"config": {"model": "new"}}

    def test_does_not_mutate_base(self) -> None:
        from amplifierd.providers import _deep_merge

        base = {"config": {"model": "gpt-4"}}
        _deep_merge(base, {"config": {"api_key": "sk-123"}})
        assert base == {"config": {"model": "gpt-4"}}


@pytest.mark.unit
class TestMergeProviderItem:
    """Tests for _merge_provider_item()."""

    def test_deep_merges_config(self) -> None:
        from amplifierd.providers import _merge_provider_item

        bundle = {"module": "provider-anthropic", "config": {"default_model": "claude-sonnet-4-6", "debug": True}}
        settings = {"module": "provider-anthropic", "config": {"api_key": "${ANTHROPIC_API_KEY}"}}
        result = _merge_provider_item(bundle, settings)
        assert result["config"] == {
            "default_model": "claude-sonnet-4-6",
            "debug": True,
            "api_key": "${ANTHROPIC_API_KEY}",
        }

    def test_settings_replaces_top_level_fields(self) -> None:
        from amplifierd.providers import _merge_provider_item

        bundle = {"module": "provider-anthropic", "source": "old-source"}
        settings = {"module": "provider-anthropic", "source": "new-source"}
        result = _merge_provider_item(bundle, settings)
        assert result["source"] == "new-source"

    def test_bundle_keys_preserved_when_settings_has_none(self) -> None:
        from amplifierd.providers import _merge_provider_item

        bundle = {"module": "provider-anthropic", "config": {"debug": True, "raw_debug": True}}
        settings = {"module": "provider-anthropic"}
        result = _merge_provider_item(bundle, settings)
        assert result["config"] == {"debug": True, "raw_debug": True}


@pytest.mark.unit
class TestInjectProviders:
    """Tests for inject_providers()."""

    def test_empty_bundle_gets_settings_wholesale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Provider-agnostic bundle gets all settings providers."""
        from amplifierd.providers import inject_providers

        monkeypatch.setenv("KEY", "val")
        bundle = _make_bundle()
        providers = [{"module": "provider-test", "config": {"api_key": "${KEY}"}}]
        inject_providers(bundle, providers)
        assert len(bundle.providers) == 1
        assert bundle.providers[0]["config"]["api_key"] == "val"

    def test_updates_matching_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Settings update a provider the bundle already declares."""
        from amplifierd.providers import inject_providers

        monkeypatch.setenv("KEY", "val")
        bundle = _make_bundle(providers=[{"module": "provider-test", "config": {"api_key": "old"}}])
        providers = [{"module": "provider-test", "config": {"api_key": "${KEY}"}}]
        inject_providers(bundle, providers)
        assert len(bundle.providers) == 1
        assert bundle.providers[0]["config"]["api_key"] == "val"

    def test_empty_noop(self) -> None:
        from amplifierd.providers import inject_providers

        bundle = _make_bundle()
        inject_providers(bundle, [])
        assert bundle.providers == []

    def test_does_not_inject_new_providers(self) -> None:
        """Settings providers not in the bundle are silently dropped."""
        from amplifierd.providers import inject_providers

        bundle = _make_bundle(
            providers=[
                {"module": "existing-provider", "config": {"key": "old"}},
            ]
        )
        providers = [
            {"module": "existing-provider", "config": {"key": "new"}},
            {"module": "new-provider", "config": {"key": "added"}},
        ]
        inject_providers(bundle, providers)
        assert len(bundle.providers) == 1
        assert bundle.providers[0]["module"] == "existing-provider"
        assert bundle.providers[0]["config"]["key"] == "new"

    def test_deep_merges_config_preserving_bundle_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Bundle config keys survive when settings adds runtime keys."""
        from amplifierd.providers import inject_providers

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        bundle = _make_bundle(providers=[{
            "module": "provider-anthropic",
            "config": {"default_model": "claude-sonnet-4-6", "debug": True},
        }])
        providers = [{
            "module": "provider-anthropic",
            "config": {"api_key": "${ANTHROPIC_API_KEY}"},
        }]
        inject_providers(bundle, providers)
        assert len(bundle.providers) == 1
        cfg = bundle.providers[0]["config"]
        assert cfg["default_model"] == "claude-sonnet-4-6"
        assert cfg["debug"] is True
        assert cfg["api_key"] == "sk-test"

    def test_env_expansion_after_merge_preserves_bundle_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When env var is unset, bundle config survives (not clobbered by empty expansion)."""
        from amplifierd.providers import inject_providers

        monkeypatch.delenv("UNSET_KEY", raising=False)
        bundle = _make_bundle(providers=[{
            "module": "provider-anthropic",
            "config": {"default_model": "claude-sonnet-4-6", "debug": True},
        }])
        providers = [{
            "module": "provider-anthropic",
            "config": {"api_key": "${UNSET_KEY}"},
        }]
        inject_providers(bundle, providers)
        cfg = bundle.providers[0]["config"]
        # api_key expands to "" and gets stripped, but bundle keys survive
        assert cfg["default_model"] == "claude-sonnet-4-6"
        assert cfg["debug"] is True
        assert "api_key" not in cfg


@pytest.mark.unit
class TestMergeSettingsProviders:
    """Tests for merge_settings_providers()."""

    def test_empty_settings(self) -> None:
        from amplifierd.providers import merge_settings_providers

        existing = [{"module": "a", "config": {}}]
        assert merge_settings_providers(existing, []) == existing

    def test_settings_override(self) -> None:
        from amplifierd.providers import merge_settings_providers

        existing = [{"module": "a", "config": {"key": "old"}}]
        settings = [{"module": "a", "config": {"key": "new"}}]
        result = merge_settings_providers(existing, settings)
        assert len(result) == 1
        assert result[0]["config"]["key"] == "new"

    def test_does_not_add_new_provider(self) -> None:
        """Settings providers not declared in the bundle are silently dropped."""
        from amplifierd.providers import merge_settings_providers

        existing = [{"module": "a", "config": {}}]
        settings = [{"module": "b", "config": {}}]
        result = merge_settings_providers(existing, settings)
        assert len(result) == 1
        assert result[0]["module"] == "a"

    def test_empty_bundle_gets_settings(self) -> None:
        """Provider-agnostic bundle (no providers) gets settings wholesale."""
        from amplifierd.providers import merge_settings_providers

        settings = [
            {"module": "provider-anthropic", "config": {"api_key": "sk-123"}},
            {"module": "provider-openai", "config": {"api_key": "sk-456"}},
        ]
        result = merge_settings_providers([], settings)
        assert len(result) == 2

    def test_deep_merges_matching_config(self) -> None:
        """Config sub-dict is deep-merged, not replaced."""
        from amplifierd.providers import merge_settings_providers

        existing = [{"module": "a", "config": {"model": "gpt-4", "debug": True}}]
        settings = [{"module": "a", "config": {"api_key": "sk-123"}}]
        result = merge_settings_providers(existing, settings)
        assert result[0]["config"] == {"model": "gpt-4", "debug": True, "api_key": "sk-123"}

    def test_no_env_expansion_in_merge(self) -> None:
        """Env var references are preserved through merge (expanded later)."""
        from amplifierd.providers import merge_settings_providers

        existing = [{"module": "a", "config": {"debug": True}}]
        settings = [{"module": "a", "config": {"api_key": "${MY_KEY}"}}]
        result = merge_settings_providers(existing, settings)
        assert result[0]["config"]["api_key"] == "${MY_KEY}"

    def test_preserves_bundle_order(self) -> None:
        """Result order matches bundle's provider order."""
        from amplifierd.providers import merge_settings_providers

        existing = [
            {"module": "b", "config": {}},
            {"module": "a", "config": {}},
        ]
        settings = [
            {"module": "a", "config": {"key": "from-settings"}},
            {"module": "b", "config": {"key": "from-settings"}},
        ]
        result = merge_settings_providers(existing, settings)
        assert result[0]["module"] == "b"
        assert result[1]["module"] == "a"
