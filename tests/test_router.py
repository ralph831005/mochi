"""Tests for Router — sticky sessions, shortcut routing, alias routing."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mochi_agents.core.router import Router


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_router(
    agents: list[dict] | None = None,
    shortcut_runner: MagicMock | None = None,
) -> Router:
    """Create a Router with mocked dependencies."""
    mock_registry = MagicMock()
    mock_runtime = MagicMock()
    mock_runtime.execute = AsyncMock(return_value="agent response")
    mock_tool_runner = MagicMock()
    mock_workflow_engine = MagicMock()

    # Set up agent lookup
    agent_map = {}
    all_agents = []
    if agents:
        for a in agents:
            mock_agent = MagicMock()
            mock_agent.name = a["name"]
            mock_agent.display_name = a.get("display_name", a["name"])
            mock_agent.aliases = a.get("aliases", [])
            mock_agent.routing_keys = a.get("routing_keys", [])
            agent_map[a["name"]] = mock_agent
            for alias in a.get("aliases", []):
                agent_map[alias] = mock_agent
            all_agents.append(mock_agent)

    mock_registry.get_agent = lambda name: agent_map.get(name)
    mock_registry.list_agents = lambda: all_agents
    mock_registry.get_all_agents = lambda: all_agents

    router = Router(
        registry=mock_registry,
        runtime=mock_runtime,
        tool_runner=mock_tool_runner,
        workflow_engine=mock_workflow_engine,
        shortcut_runner=shortcut_runner,
    )
    return router


# ---------------------------------------------------------------------------
# Sticky sessions
# ---------------------------------------------------------------------------


class TestStickySessions:
    """Test sticky session routing behavior."""

    @pytest.mark.asyncio
    async def test_slash_command_sets_session(self):
        router = make_router(agents=[{"name": "nutritionist", "aliases": ["noa"]}])

        await router.route("/noa hello", user_id="user1")

        assert router._get_active_agent("user1") == "nutritionist"

    @pytest.mark.asyncio
    async def test_subsequent_message_sticks_to_agent(self):
        router = make_router(agents=[
            {"name": "nutritionist", "aliases": ["noa"]},
            {"name": "admin", "aliases": ["anna"]},
        ])

        # First message sets session
        await router.route("/noa hello", user_id="user1")
        # Second message (no slash) should stick to nutritionist
        await router.route("what did I eat today?", user_id="user1")

        # Verify nutritionist was called both times
        calls = router.runtime.execute.call_args_list
        assert len(calls) == 2
        assert calls[0][0][0] == "nutritionist"
        assert calls[1][0][0] == "nutritionist"

    @pytest.mark.asyncio
    async def test_new_slash_command_switches_session(self):
        router = make_router(agents=[
            {"name": "nutritionist", "aliases": ["noa"]},
            {"name": "admin", "aliases": ["anna"]},
        ])

        await router.route("/noa hello", user_id="user1")
        await router.route("/anna status", user_id="user1")

        assert router._get_active_agent("user1") == "admin"

    @pytest.mark.asyncio
    async def test_session_timeout(self):
        router = make_router(agents=[{"name": "nutritionist", "aliases": ["noa"]}])
        router._session_timeout = 0.01  # 10ms timeout

        await router.route("/noa hello", user_id="user1")

        # Wait for timeout
        import asyncio
        await asyncio.sleep(0.02)

        assert router._get_active_agent("user1") is None

    @pytest.mark.asyncio
    async def test_clear_session(self):
        router = make_router(agents=[{"name": "nutritionist", "aliases": ["noa"]}])

        await router.route("/noa hello", user_id="user1")
        router._clear_active_agent("user1")

        assert router._get_active_agent("user1") is None

    @pytest.mark.asyncio
    async def test_independent_user_sessions(self):
        router = make_router(agents=[
            {"name": "nutritionist", "aliases": ["noa"]},
            {"name": "admin", "aliases": ["anna"]},
        ])

        await router.route("/noa hello", user_id="user1")
        await router.route("/anna hello", user_id="user2")

        assert router._get_active_agent("user1") == "nutritionist"
        assert router._get_active_agent("user2") == "admin"


# ---------------------------------------------------------------------------
# Shortcut routing
# ---------------------------------------------------------------------------


class TestShortcutRouting:
    """Test shortcut and alias routing in the Router."""

    @pytest.mark.asyncio
    async def test_agent_subcommand_triggers_shortcut(self):
        mock_sr = MagicMock()
        mock_sr.has_active_session = MagicMock(return_value=False)
        mock_sr.start_shortcut = AsyncMock(return_value="⚡ Enter values...")
        mock_sr.list_shortcuts = MagicMock(return_value=[])

        router = make_router(
            agents=[{"name": "nutritionist", "aliases": ["noa"]}],
            shortcut_runner=mock_sr,
        )

        result = await router.route("/noa smoothie", user_id="user1")

        assert "Enter values" in result
        mock_sr.start_shortcut.assert_called_once_with("user1", "nutritionist", "smoothie")

    @pytest.mark.asyncio
    async def test_agent_subcommand_falls_through_if_no_shortcut(self):
        mock_sr = MagicMock()
        mock_sr.has_active_session = MagicMock(return_value=False)
        mock_sr.start_shortcut = AsyncMock(return_value=None)  # no shortcut found
        mock_sr.list_shortcuts = MagicMock(return_value=[])

        router = make_router(
            agents=[{"name": "nutritionist", "aliases": ["noa"]}],
            shortcut_runner=mock_sr,
        )

        result = await router.route("/noa random message", user_id="user1")

        # Should fall through to normal LLM routing
        assert result == "agent response"

    @pytest.mark.asyncio
    async def test_bare_command_lists_shortcuts(self):
        from mochi_agents.core.router import RouteResponse

        mock_sr = MagicMock()
        mock_sr.has_active_session = MagicMock(return_value=False)
        mock_sr.list_shortcuts = MagicMock(return_value=[
            {"command": "smoothie", "description": "Log smoothie"},
            {"command": "salad", "description": "Log salad"},
        ])

        router = make_router(
            agents=[{"name": "nutritionist", "aliases": ["noa"], "display_name": "Noa"}],
            shortcut_runner=mock_sr,
        )

        result = await router.route("/noa", user_id="user1")

        assert isinstance(result, RouteResponse)
        assert "smoothie" in result.text
        assert "salad" in result.text
        assert "Noa" in result.text
        assert result.keyboard is not None
        assert len(result.keyboard) == 3  # 2 shortcuts + 1 "Talk to" button

    @pytest.mark.asyncio
    async def test_active_shortcut_session_intercepts_message(self):
        mock_sr = MagicMock()
        mock_sr.has_active_session = MagicMock(return_value=True)
        mock_sr.handle_input = AsyncMock(return_value="✅ Logged!")

        router = make_router(shortcut_runner=mock_sr)

        result = await router.route("100 30 200", user_id="user1")

        assert "Logged" in result
        mock_sr.handle_input.assert_called_once_with("user1", "100 30 200")

    @pytest.mark.asyncio
    async def test_global_alias_triggers_shortcut(self):
        mock_sr = MagicMock()
        mock_sr.has_active_session = MagicMock(return_value=False)
        mock_sr.start_shortcut = AsyncMock(return_value="⚡ Enter values...")

        router = make_router(shortcut_runner=mock_sr)

        with patch("mochi_agents.core.shortcut_runner.get_alias_registry") as mock_get_reg:
            mock_reg = MagicMock()
            mock_reg.get = MagicMock(return_value={"agent": "nutritionist", "shortcut": "smoothie"})
            mock_get_reg.return_value = mock_reg

            result = await router.route("/smoothie", user_id="user1")

        assert "Enter values" in result
        mock_sr.start_shortcut.assert_called_once_with("user1", "nutritionist", "smoothie")
