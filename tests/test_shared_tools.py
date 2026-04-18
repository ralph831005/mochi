"""Tests for shared_tools — ContextVar isolation, delegation guards."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from mochi_agents.core.shared_tools import (
    _current_agent,
    _current_depth,
    _current_runtime,
    set_current_agent,
    set_current_depth,
    set_current_runtime,
    delegate_to_agent,
    MAX_DELEGATION_DEPTH,
)


# ---------------------------------------------------------------------------
# ContextVar isolation
# ---------------------------------------------------------------------------


class TestContextVarIsolation:
    """Verify that ContextVars are isolated between concurrent tasks."""

    @pytest.mark.asyncio
    async def test_current_agent_isolated_between_tasks(self):
        """Two concurrent tasks should not see each other's agent context."""
        results = {}

        async def set_and_read(name: str, delay: float):
            set_current_agent(name)
            await asyncio.sleep(delay)
            results[name] = _current_agent.get()

        # Task A sets "nutritionist" then sleeps; task B sets "admin" immediately.
        # Without isolation, task A would wake up and see "admin".
        await asyncio.gather(
            asyncio.create_task(set_and_read("nutritionist", 0.05)),
            asyncio.create_task(set_and_read("admin", 0.0)),
        )

        assert results["nutritionist"] == "nutritionist"
        assert results["admin"] == "admin"

    @pytest.mark.asyncio
    async def test_current_depth_isolated_between_tasks(self):
        """Delegation depth should be isolated between concurrent tasks."""
        results = {}

        async def set_and_read(depth: int, label: str, delay: float):
            set_current_depth(depth)
            await asyncio.sleep(delay)
            results[label] = _current_depth.get()

        await asyncio.gather(
            asyncio.create_task(set_and_read(0, "outer", 0.05)),
            asyncio.create_task(set_and_read(2, "inner", 0.0)),
        )

        assert results["outer"] == 0
        assert results["inner"] == 2


# ---------------------------------------------------------------------------
# Delegation guards
# ---------------------------------------------------------------------------


class TestDelegationGuards:
    """Test delegate_to_agent safety checks."""

    @pytest.mark.asyncio
    async def test_max_depth_reached(self):
        """Should reject delegation when depth >= MAX_DELEGATION_DEPTH."""
        set_current_depth(MAX_DELEGATION_DEPTH)
        set_current_runtime(MagicMock())
        set_current_agent("nutritionist")

        result = await delegate_to_agent("admin", "test task")

        assert result["status"] == "error"
        assert "Max delegation depth" in result["message"]

    @pytest.mark.asyncio
    async def test_self_delegation_blocked(self):
        """Should reject delegation to self."""
        set_current_depth(0)
        set_current_runtime(MagicMock())
        set_current_agent("nutritionist")

        result = await delegate_to_agent("nutritionist", "test task")

        assert result["status"] == "error"
        assert "Cannot delegate to yourself" in result["message"]

    @pytest.mark.asyncio
    async def test_no_runtime_available(self):
        """Should return error when runtime is not set."""
        set_current_runtime(None)

        result = await delegate_to_agent("admin", "test task")

        assert result["status"] == "error"
        assert "Runtime not available" in result["message"]

    @pytest.mark.asyncio
    async def test_successful_delegation(self):
        """Should call runtime.execute and return the response."""
        mock_runtime = MagicMock()
        mock_runtime.execute = AsyncMock(return_value="Admin did the thing")

        set_current_depth(0)
        set_current_runtime(mock_runtime)
        set_current_agent("nutritionist")

        result = await delegate_to_agent("admin", "create a shortcut")

        assert result["status"] == "ok"
        assert result["agent"] == "admin"
        assert result["response"] == "Admin did the thing"
        mock_runtime.execute.assert_called_once_with(
            "admin", "create a shortcut",
            source="agent", _delegation_depth=1,
        )

    @pytest.mark.asyncio
    async def test_depth_restored_after_delegation(self):
        """Depth should be restored after delegation returns."""
        mock_runtime = MagicMock()
        mock_runtime.execute = AsyncMock(return_value="done")

        set_current_depth(0)
        set_current_runtime(mock_runtime)
        set_current_agent("nutritionist")

        await delegate_to_agent("admin", "task")

        # Depth should be restored to 0, not left at whatever inner execute set
        assert _current_depth.get() == 0

    @pytest.mark.asyncio
    async def test_depth_restored_on_error(self):
        """Depth should be restored even if delegation fails."""
        mock_runtime = MagicMock()
        mock_runtime.execute = AsyncMock(side_effect=RuntimeError("boom"))

        set_current_depth(0)
        set_current_runtime(mock_runtime)
        set_current_agent("nutritionist")

        result = await delegate_to_agent("admin", "task")

        assert result["status"] == "error"
        assert _current_depth.get() == 0  # restored despite error
