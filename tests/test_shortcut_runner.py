"""Tests for ShortcutRunner — session lifecycle, parsing, CRUD, aliases."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from mochi_agents.core.shortcut_runner import (
    AliasRegistry,
    ShortcutRunner,
    ShortcutSession,
)
from mochi_agents.core.tool_runner import ToolResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SMOOTHIE_SHORTCUT = {
    "command": "smoothie",
    "description": "Log morning smoothie",
    "fields": [
        {"name": "banana_g", "label": "banana (g)", "type": "number", "default": 100},
        {"name": "protein_g", "label": "protein (g)", "type": "number", "default": 30},
        {"name": "milk_ml", "label": "milk (ml)", "type": "number", "default": 200},
    ],
    "on_complete": {
        "tool": "log_meal",
        "args": {
            "meal_type": "breakfast",
            "description": "Smoothie: ${banana_g}g banana, ${protein_g}g protein, ${milk_ml}ml milk",
        },
    },
}


def make_runner(shortcuts: dict[str, dict[str, dict]] | None = None) -> ShortcutRunner:
    """Create a ShortcutRunner with pre-loaded shortcuts (no disk I/O)."""
    mock_tool_runner = MagicMock()
    mock_tool_runner.execute = AsyncMock(return_value=ToolResult(success=True, data={"logged": True}))
    mock_registry = MagicMock()

    runner = ShortcutRunner(tool_runner=mock_tool_runner, registry=mock_registry)
    if shortcuts:
        runner._shortcuts = shortcuts
    return runner


# ---------------------------------------------------------------------------
# Shortcut session lifecycle
# ---------------------------------------------------------------------------


class TestShortcutSession:
    """Test the shortcut flow from start to completion."""

    @pytest.mark.asyncio
    async def test_start_returns_prompt(self):
        runner = make_runner({"nutritionist": {"smoothie": SMOOTHIE_SHORTCUT}})

        prompt = await runner.start_shortcut("user1", "nutritionist", "smoothie")

        assert prompt is not None
        assert "Log morning smoothie" in prompt
        assert "banana (g)" in prompt
        assert "cancel" in prompt.lower()

    @pytest.mark.asyncio
    async def test_start_nonexistent_returns_none(self):
        runner = make_runner({"nutritionist": {"smoothie": SMOOTHIE_SHORTCUT}})

        result = await runner.start_shortcut("user1", "nutritionist", "nonexistent")

        assert result is None

    @pytest.mark.asyncio
    async def test_session_created_after_start(self):
        runner = make_runner({"nutritionist": {"smoothie": SMOOTHIE_SHORTCUT}})

        await runner.start_shortcut("user1", "nutritionist", "smoothie")

        assert runner.has_active_session("user1")

    @pytest.mark.asyncio
    async def test_no_session_for_unknown_user(self):
        runner = make_runner()

        assert not runner.has_active_session("unknown")


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------


class TestInputParsing:
    """Test handling of user input during shortcut sessions."""

    @pytest.mark.asyncio
    async def test_correct_values_calls_tool(self):
        runner = make_runner({"nutritionist": {"smoothie": SMOOTHIE_SHORTCUT}})
        await runner.start_shortcut("user1", "nutritionist", "smoothie")

        result = await runner.handle_input("user1", "100 30 200")

        assert "✅" in result
        assert not runner.has_active_session("user1")  # session cleaned up

    @pytest.mark.asyncio
    async def test_wrong_value_count_asks_retry(self):
        runner = make_runner({"nutritionist": {"smoothie": SMOOTHIE_SHORTCUT}})
        await runner.start_shortcut("user1", "nutritionist", "smoothie")

        result = await runner.handle_input("user1", "100 30")

        assert "Expected 3 values" in result
        assert runner.has_active_session("user1")  # session still active

    @pytest.mark.asyncio
    async def test_cancel_clears_session(self):
        runner = make_runner({"nutritionist": {"smoothie": SMOOTHIE_SHORTCUT}})
        await runner.start_shortcut("user1", "nutritionist", "smoothie")

        result = await runner.handle_input("user1", "cancel")

        assert "cancelled" in result.lower()
        assert not runner.has_active_session("user1")

    @pytest.mark.asyncio
    async def test_slash_command_auto_cancels(self):
        runner = make_runner({"nutritionist": {"smoothie": SMOOTHIE_SHORTCUT}})
        await runner.start_shortcut("user1", "nutritionist", "smoothie")

        result = await runner.handle_input("user1", "/admin")

        assert result is None  # fall through to router
        assert not runner.has_active_session("user1")

    @pytest.mark.asyncio
    async def test_invalid_number_returns_error(self):
        runner = make_runner({"nutritionist": {"smoothie": SMOOTHIE_SHORTCUT}})
        await runner.start_shortcut("user1", "nutritionist", "smoothie")

        result = await runner.handle_input("user1", "abc 30 200")

        assert "Invalid value" in result
        assert not runner.has_active_session("user1")

    @pytest.mark.asyncio
    async def test_variable_substitution(self):
        runner = make_runner({"nutritionist": {"smoothie": SMOOTHIE_SHORTCUT}})
        await runner.start_shortcut("user1", "nutritionist", "smoothie")
        await runner.handle_input("user1", "100 30 200")

        # Check the tool was called with substituted args
        call_args = runner._tool_runner.execute.call_args
        tool_args = call_args[0][2]  # third positional arg
        assert "100g banana" in tool_args["description"]
        assert "30g protein" in tool_args["description"]
        assert "200ml milk" in tool_args["description"]

    @pytest.mark.asyncio
    async def test_no_session_returns_none(self):
        runner = make_runner()

        result = await runner.handle_input("user1", "100 30 200")

        assert result is None


# ---------------------------------------------------------------------------
# Shortcut CRUD (data layer)
# ---------------------------------------------------------------------------


class TestShortcutCRUD:
    """Test save/delete shortcuts with file persistence."""

    @pytest.mark.asyncio
    async def test_save_creates_file(self, tmp_path, monkeypatch):
        """save_shortcut should write to data/shortcuts/<agent>.yaml."""
        mock_settings = MagicMock()
        mock_settings.resolve_path = lambda p: tmp_path / p if p != "." else tmp_path
        mock_settings.data_dir = "data"
        mock_settings.agents_dir = "agents"
        monkeypatch.setattr("mochi_agents.core.shortcut_runner.get_settings", lambda: mock_settings)

        runner = make_runner()
        result = runner.save_shortcut("nutritionist", "smoothie", SMOOTHIE_SHORTCUT)

        assert result["status"] == "ok"
        assert (tmp_path / "data" / "shortcuts" / "nutritionist.yaml").exists()
        assert runner.get_shortcut("nutritionist", "smoothie") is not None

    @pytest.mark.asyncio
    async def test_delete_removes_shortcut(self, tmp_path, monkeypatch):
        """delete_shortcut should remove from file and memory."""
        mock_settings = MagicMock()
        mock_settings.resolve_path = lambda p: tmp_path / p if p != "." else tmp_path
        mock_settings.data_dir = "data"
        mock_settings.agents_dir = "agents"
        monkeypatch.setattr("mochi_agents.core.shortcut_runner.get_settings", lambda: mock_settings)

        runner = make_runner()
        runner.save_shortcut("nutritionist", "smoothie", SMOOTHIE_SHORTCUT)
        result = runner.delete_shortcut("nutritionist", "smoothie")

        assert result["status"] == "ok"
        assert runner.get_shortcut("nutritionist", "smoothie") is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_error(self):
        runner = make_runner()
        result = runner.delete_shortcut("nutritionist", "nonexistent")
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_list_shortcuts(self):
        runner = make_runner({"nutritionist": {"smoothie": SMOOTHIE_SHORTCUT}})
        shortcuts = runner.list_shortcuts("nutritionist")

        assert len(shortcuts) == 1
        assert shortcuts[0]["command"] == "smoothie"

    @pytest.mark.asyncio
    async def test_list_empty_agent(self):
        runner = make_runner()
        assert runner.list_shortcuts("nutritionist") == []


# ---------------------------------------------------------------------------
# Alias registry
# ---------------------------------------------------------------------------


class TestAliasRegistry:
    """Test global command alias management."""

    def test_add_and_get(self, tmp_path):
        reg = AliasRegistry(tmp_path)
        reg.add("smoothie", "nutritionist", "smoothie")

        result = reg.get("smoothie")

        assert result is not None
        assert result["agent"] == "nutritionist"
        assert result["shortcut"] == "smoothie"

    def test_remove(self, tmp_path):
        reg = AliasRegistry(tmp_path)
        reg.add("smoothie", "nutritionist", "smoothie")
        result = reg.remove("smoothie")

        assert result["status"] == "ok"
        assert reg.get("smoothie") is None

    def test_remove_nonexistent(self, tmp_path):
        reg = AliasRegistry(tmp_path)
        result = reg.remove("nonexistent")
        assert result["status"] == "error"

    def test_persistence(self, tmp_path):
        """Aliases should survive a new AliasRegistry instance."""
        reg1 = AliasRegistry(tmp_path)
        reg1.add("smoothie", "nutritionist", "smoothie")

        # Create a new instance (simulating restart)
        reg2 = AliasRegistry(tmp_path)
        assert reg2.get("smoothie") is not None

    def test_case_insensitive(self, tmp_path):
        reg = AliasRegistry(tmp_path)
        reg.add("Smoothie", "nutritionist", "smoothie")

        assert reg.get("smoothie") is not None
        assert reg.get("SMOOTHIE") is not None

    def test_list_all(self, tmp_path):
        reg = AliasRegistry(tmp_path)
        reg.add("smoothie", "nutritionist", "smoothie")
        reg.add("deploy", "admin", "deploy")

        all_aliases = reg.list_all()

        assert len(all_aliases) == 2
        assert "smoothie" in all_aliases
        assert "deploy" in all_aliases
