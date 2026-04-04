"""ToolRunner protocol and ImportlibToolRunner (MVP implementation).

The ToolRunner abstraction allows swapping tool execution strategies
(e.g., importlib in-process → subprocess sandboxed) without changing agent code.
"""

from __future__ import annotations

import importlib
import inspect
import sys
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class ToolDeclaration:
    """Describes a tool for LLM function-calling registration."""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    function: Any = None  # Reference to the actual callable


@dataclass
class ToolResult:
    """Standard return type from tool execution."""

    success: bool
    data: Any = None
    error: str | None = None


@runtime_checkable
class ToolRunner(Protocol):
    """Interface for tool execution — swap implementations without touching agents."""

    async def execute(self, agent_name: str, tool_name: str, args: dict) -> ToolResult:
        """Execute a named tool with the given arguments."""
        ...

    def get_tool_declarations(self, agent_name: str) -> list[ToolDeclaration]:
        """Return the list of tools available for an agent."""
        ...

    def reload(self, agent_name: str | None = None) -> None:
        """Reload tool modules. If agent_name is None, reload all."""
        ...


class ImportlibToolRunner:
    """MVP ToolRunner: loads tools via importlib from the agent's tools_module.

    Each agent's mission.yaml declares:
        tools_module: mochi_agents.agents.nutritionist.tools

    The tools module must export a `get_tools()` function returning a list of callables.
    """

    def __init__(self) -> None:
        self._cache: dict[str, dict[str, ToolDeclaration]] = {}
        self._modules_path: dict[str, str] = {}

    def register_agent(self, agent_name: str, tools_module: str) -> None:
        """Register the tools module path for a given agent."""
        self._modules_path[agent_name] = tools_module
        self._load_tools(agent_name)

    def _load_tools(self, agent_name: str) -> None:
        """Import the module and cache tool declarations."""
        module_path = self._modules_path.get(agent_name)
        if not module_path:
            self._cache[agent_name] = {}
            return

        module = importlib.import_module(module_path)
        if not hasattr(module, "get_tools"):
            self._cache[agent_name] = {}
            return

        tools = module.get_tools()
        declarations: dict[str, ToolDeclaration] = {}

        for tool_fn in tools:
            name = tool_fn.__name__
            doc = inspect.getdoc(tool_fn) or ""
            # Extract parameter info from type hints
            hints = {
                k: v.__name__ if hasattr(v, "__name__") else str(v)
                for k, v in tool_fn.__annotations__.items()
                if k != "return"
            }
            declarations[name] = ToolDeclaration(
                name=name,
                description=doc,
                parameters=hints,
                function=tool_fn,
            )

        self._cache[agent_name] = declarations

    async def execute(self, agent_name: str, tool_name: str, args: dict) -> ToolResult:
        """Execute a tool by name."""
        declarations = self._cache.get(agent_name, {})
        tool = declarations.get(tool_name)

        if tool is None or tool.function is None:
            return ToolResult(success=False, error=f"Tool '{tool_name}' not found for agent '{agent_name}'")

        try:
            result = tool.function(**args)
            # Support both sync and async tools
            if inspect.isawaitable(result):
                result = await result
            return ToolResult(success=True, data=result)
        except Exception as e:
            return ToolResult(success=False, error=f"{type(e).__name__}: {e}")

    def get_tool_declarations(self, agent_name: str) -> list[ToolDeclaration]:
        """Return available tools for the agent."""
        return list(self._cache.get(agent_name, {}).values())

    def reload(self, agent_name: str | None = None) -> None:
        """Reload tool modules by purging sys.modules cache and re-importing."""
        agents = [agent_name] if agent_name else list(self._modules_path.keys())

        for name in agents:
            module_path = self._modules_path.get(name)
            if module_path and module_path in sys.modules:
                del sys.modules[module_path]
            self._load_tools(name)
