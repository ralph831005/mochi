"""Tests for model_factory key resolution and runtime model override."""

import pytest
from unittest.mock import patch, MagicMock


class TestKeyResolution:
    """model_factory.resolve_api_key fallback chain."""

    def test_named_key_from_model_config(self):
        """model_config.api_key → settings.api_keys[name]."""
        from mochi_agents.core.model_factory import resolve_api_key

        mock_settings = MagicMock()
        mock_settings.api_keys = {"budget": "key_budget", "default": "key_default"}
        mock_settings.gemini_api_key = "key_legacy"

        with patch("mochi_agents.core.model_factory.get_settings", return_value=mock_settings):
            assert resolve_api_key({"api_key": "budget"}) == "key_budget"

    def test_default_named_key(self):
        """No api_key in model_config → falls back to api_keys["default"]."""
        from mochi_agents.core.model_factory import resolve_api_key

        mock_settings = MagicMock()
        mock_settings.api_keys = {"default": "key_default", "pro": "key_pro"}
        mock_settings.gemini_api_key = "key_legacy"

        with patch("mochi_agents.core.model_factory.get_settings", return_value=mock_settings):
            assert resolve_api_key({}) == "key_default"

    def test_legacy_gemini_key(self):
        """No api_keys at all → falls back to gemini_api_key."""
        from mochi_agents.core.model_factory import resolve_api_key

        mock_settings = MagicMock()
        mock_settings.api_keys = {}
        mock_settings.gemini_api_key = "key_legacy"

        with patch("mochi_agents.core.model_factory.get_settings", return_value=mock_settings):
            assert resolve_api_key({}) == "key_legacy"

    def test_missing_named_key_falls_through(self):
        """Named key not found → falls through to default."""
        from mochi_agents.core.model_factory import resolve_api_key

        mock_settings = MagicMock()
        mock_settings.api_keys = {"default": "key_default"}
        mock_settings.gemini_api_key = "key_legacy"

        with patch("mochi_agents.core.model_factory.get_settings", return_value=mock_settings):
            assert resolve_api_key({"api_key": "nonexistent"}) == "key_default"

    def test_no_keys_at_all_raises(self):
        """No keys configured → raises ValueError."""
        from mochi_agents.core.model_factory import resolve_api_key

        mock_settings = MagicMock()
        mock_settings.api_keys = {}
        mock_settings.gemini_api_key = ""

        with patch("mochi_agents.core.model_factory.get_settings", return_value=mock_settings):
            with pytest.raises(ValueError, match="No API key configured"):
                resolve_api_key({})


class TestModelOverride:
    """AgentRuntime model override system."""

    def _make_runtime(self):
        from mochi_agents.core.agent_runtime import AgentRuntime

        runtime = AgentRuntime(tool_runner=MagicMock())
        # Pre-populate mission cache
        runtime._mission_cache["nutritionist"] = {
            "model_config": {"provider": "google", "model": "gemini-3.1-flash", "api_key": "budget"},
        }
        return runtime

    def test_effective_config_without_override(self):
        """Without overrides, returns base mission config."""
        runtime = self._make_runtime()
        cfg = runtime.get_effective_model_config("nutritionist")
        assert cfg["model"] == "gemini-3.1-flash"
        assert cfg["api_key"] == "budget"

    def test_override_model(self):
        """set_model_override changes the effective model."""
        runtime = self._make_runtime()
        runtime.set_model_override("nutritionist", model="gemini-3.1-pro")
        cfg = runtime.get_effective_model_config("nutritionist")
        assert cfg["model"] == "gemini-3.1-pro"
        # api_key unchanged
        assert cfg["api_key"] == "budget"

    def test_override_model_and_key(self):
        """Override both model and api_key."""
        runtime = self._make_runtime()
        runtime.set_model_override("nutritionist", model="gemini-3.1-pro", api_key="pro")
        cfg = runtime.get_effective_model_config("nutritionist")
        assert cfg["model"] == "gemini-3.1-pro"
        assert cfg["api_key"] == "pro"

    def test_clear_specific_agent(self):
        """clear_model_overrides(agent) removes only that agent's override."""
        runtime = self._make_runtime()
        runtime._mission_cache["admin"] = {"model_config": {"model": "gemini-2.5-flash"}}

        runtime.set_model_override("nutritionist", model="gemini-3.1-pro")
        runtime.set_model_override("admin", model="gemini-3.1-pro")

        runtime.clear_model_overrides("nutritionist")

        # nutritionist reverted
        assert runtime.get_effective_model_config("nutritionist")["model"] == "gemini-3.1-flash"
        # admin still overridden
        assert runtime.get_effective_model_config("admin")["model"] == "gemini-3.1-pro"

    def test_clear_all(self):
        """clear_model_overrides(None) clears all agents."""
        runtime = self._make_runtime()
        runtime._mission_cache["admin"] = {"model_config": {"model": "gemini-2.5-flash"}}

        runtime.set_model_override("nutritionist", model="gemini-3.1-pro")
        runtime.set_model_override("admin", model="gemini-3.1-pro")

        runtime.clear_model_overrides()

        assert runtime.get_effective_model_config("nutritionist")["model"] == "gemini-3.1-flash"
        assert runtime.get_effective_model_config("admin")["model"] == "gemini-2.5-flash"

    def test_override_does_not_mutate_mission(self):
        """Overrides don't modify the underlying mission cache."""
        runtime = self._make_runtime()
        runtime.set_model_override("nutritionist", model="gemini-3.1-pro")

        original = runtime.get_mission("nutritionist")["model_config"]
        assert original["model"] == "gemini-3.1-flash"  # unchanged
