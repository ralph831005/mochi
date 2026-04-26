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
        self._shared_tools: dict[str, ToolDeclaration] = {}
        self._load_shared_tools()

    def _load_shared_tools(self) -> None:
        """Load tools from shared_tools.py that apply to ALL agents."""
        from mochi_agents.core.shared_tools import get_tools

        for tool_fn in get_tools():
            name = tool_fn.__name__
            doc = inspect.getdoc(tool_fn) or ""
            hints = {
                k: v.__name__ if hasattr(v, "__name__") else str(v)
                for k, v in tool_fn.__annotations__.items()
                if k != "return"
            }
            self._shared_tools[name] = ToolDeclaration(
                name=name,
                description=doc,
                parameters=hints,
                function=tool_fn,
            )

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

        # Merge shared tools (agent-specific tools take precedence)
        merged = dict(self._shared_tools)
        merged.update(declarations)
        self._cache[agent_name] = merged

    async def execute(self, agent_name: str, tool_name: str, args: dict) -> ToolResult:
        """Execute a tool by name."""
        # Ensure agents without a tools_module still get shared tools
        if agent_name not in self._cache:
            self._cache[agent_name] = dict(self._shared_tools)

        declarations = self._cache.get(agent_name, {})
        tool = declarations.get(tool_name)

        if tool is None or tool.function is None:
            return ToolResult(success=False, error=f"Tool '{tool_name}' not found for agent '{agent_name}'")

        # Set agent context for shared tools
        from mochi_agents.core.shared_tools import set_current_agent
        set_current_agent(agent_name)

        try:
            result = tool.function(**args)
            # Support both sync and async tools
            if inspect.isawaitable(result):
                result = await result
            return ToolResult(success=True, data=result)
        except Exception as e:
            return ToolResult(success=False, error=f"{type(e).__name__}: {e}")

    def get_tool_declarations(self, agent_name: str) -> list[ToolDeclaration]:
        """Return available tools for the agent (shared + domain-specific)."""
        # Ensure agents without a tools_module still get shared tools
        if agent_name not in self._cache:
            self._cache[agent_name] = dict(self._shared_tools)
        return list(self._cache.get(agent_name, {}).values())

    def reload(self, agent_name: str | None = None) -> None:
        """Reload tool modules by purging sys.modules cache and re-importing."""
        agents = [agent_name] if agent_name else list(self._modules_path.keys())

        for name in agents:
            module_path = self._modules_path.get(name)
            if module_path and module_path in sys.modules:
                del sys.modules[module_path]
            self._load_tools(name)


class MCPToolRunner:
    """ToolRunner that connects to external MCP servers via the official SDK.

    Supports two transport modes:
    - streamable-http: for remote servers (config has 'url')
    - stdio: for local subprocess servers (config has 'command' + 'args')
    """

    def __init__(self) -> None:
        self._server_configs: dict[str, list[dict]] = {}   # agent_name → [{'url': url, 'tools': [...]}]
        self._cache: dict[str, dict[str, ToolDeclaration]] = {}
        self._sessions: dict[str, list[Any]] = {}      # agent_name → [session, ...]
        self._contexts: dict[str, list[Any]] = {}       # keep refs for cleanup
        self._connected: bool = False

    def register_agent(self, agent_name: str, server_configs: list[dict]) -> None:
        """Store MCP server configs for an agent. Call connect_all() after."""
        self._server_configs[agent_name] = server_configs

    async def connect_all(self) -> None:
        """Open client sessions to all registered MCP servers and discover tools."""
        import logging
        logger = logging.getLogger(__name__)

        for agent_name, configs in self._server_configs.items():
            declarations: dict[str, ToolDeclaration] = {}
            sessions = []
            contexts = []

            for config in configs:
                whitelist = config.get("tools")
                label = config.get("url") or config.get("name", config.get("command", "?"))
                try:
                    # Auto-detect transport from config
                    if config.get("url"):
                        session, ctx = await self._connect_http(config["url"])
                    elif config.get("command"):
                        session, ctx = await self._connect_stdio(config)
                    else:
                        logger.warning(f"MCP: skipping config with no url or command for '{agent_name}'")
                        continue

                    sessions.append((session, label))
                    contexts.append(ctx)

                    # Discover tools
                    tools_result = await session.list_tools()
                    added_count = 0
                    for tool in tools_result.tools:
                        # Apply whitelist if present
                        if whitelist is not None and tool.name not in whitelist:
                            continue

                        # Convert MCP tool schema → ToolDeclaration
                        params = {}
                        if tool.inputSchema and "properties" in tool.inputSchema:
                            params = tool.inputSchema
                        declarations[tool.name] = ToolDeclaration(
                            name=tool.name,
                            description=tool.description or "",
                            parameters=params,
                            function=None,  # remote — no local callable
                        )
                        added_count += 1

                    logger.info(
                        f"MCP: agent '{agent_name}' connected to {label} "
                        f"— {added_count}/{len(tools_result.tools)} tools allowed"
                    )
                except Exception as e:
                    logger.warning(f"MCP: failed to connect to {label} for '{agent_name}': {e}")

            self._cache[agent_name] = declarations
            self._sessions[agent_name] = sessions
            self._contexts[agent_name] = contexts

        self._connected = True

    async def _connect_http(self, url: str) -> tuple[Any, Any]:
        """Open a single MCP client session via streamable-http transport."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        transport_ctx = streamablehttp_client(url)
        read_stream, write_stream, _ = await transport_ctx.__aenter__()

        session_ctx = ClientSession(read_stream, write_stream)
        session = await session_ctx.__aenter__()
        await session.initialize()

        return session, (session_ctx, transport_ctx)

    async def _connect_stdio(self, config: dict) -> tuple[Any, Any]:
        """Open a single MCP client session via stdio transport (subprocess)."""
        import os
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client, StdioServerParameters

        command = config["command"]
        args = config.get("args", [])

        # Build environment: inherit current env + any extras from config
        env = dict(os.environ)
        if config.get("env"):
            env.update(config["env"])

        server_params = StdioServerParameters(
            command=command,
            args=args,
            env=env,
        )

        transport_ctx = stdio_client(server_params)
        read_stream, write_stream = await transport_ctx.__aenter__()

        session_ctx = ClientSession(read_stream, write_stream)
        session = await session_ctx.__aenter__()
        await session.initialize()

        return session, (session_ctx, transport_ctx)

    async def execute(self, agent_name: str, tool_name: str, args: dict) -> ToolResult:
        """Execute a tool on the remote MCP server."""
        if not self._connected:
            return ToolResult(success=False, error="MCP sessions not connected yet")

        # Find which session owns this tool
        sessions = self._sessions.get(agent_name, [])
        for session, url in sessions:
            try:
                result = await session.call_tool(tool_name, arguments=args)

                # MCP call_tool returns a CallToolResult with content list
                # Extract text content
                text_parts = []
                for content in result.content:
                    if hasattr(content, "text"):
                        text_parts.append(content.text)

                data = "\n".join(text_parts) if text_parts else str(result.content)
                return ToolResult(success=not result.isError, data=data)
            except Exception:
                # This session might not have this tool, try next
                continue

        return ToolResult(
            success=False,
            error=f"MCP tool '{tool_name}' not found for agent '{agent_name}'",
        )

    def get_tool_declarations(self, agent_name: str) -> list[ToolDeclaration]:
        """Return discovered MCP tools for the agent."""
        return list(self._cache.get(agent_name, {}).values())

    def reload(self, agent_name: str | None = None) -> None:
        """Reload is a no-op synchronously; use connect_all() for async reload."""
        pass

    async def close(self) -> None:
        """Cleanly shut down all MCP client sessions."""
        import logging
        logger = logging.getLogger(__name__)

        for agent_name, ctx_list in self._contexts.items():
            for session_ctx, transport_ctx in ctx_list:
                try:
                    await session_ctx.__aexit__(None, None, None)
                    await transport_ctx.__aexit__(None, None, None)
                except Exception as e:
                    logger.warning(f"MCP: error closing session for '{agent_name}': {e}")

        self._sessions.clear()
        self._contexts.clear()
        self._cache.clear()
        self._connected = False


class CompositeToolRunner:
    """Aggregates multiple ToolRunner instances behind the ToolRunner interface.

    The AgentRuntime sees a single ToolRunner. Internally, this delegates to
    ImportlibToolRunner (local tools) and MCPToolRunner (remote MCP tools).
    First runner that owns the tool wins.
    """

    def __init__(self) -> None:
        self._runners: list = []

    def add_runner(self, runner: Any) -> None:
        """Add a ToolRunner backend."""
        self._runners.append(runner)

    async def execute(self, agent_name: str, tool_name: str, args: dict) -> ToolResult:
        """Execute a tool, delegating to whichever runner owns it."""
        for runner in self._runners:
            decls = runner.get_tool_declarations(agent_name)
            if any(d.name == tool_name for d in decls):
                return await runner.execute(agent_name, tool_name, args)
        return ToolResult(
            success=False,
            error=f"Tool '{tool_name}' not found in any runner for '{agent_name}'.",
        )

    def get_tool_declarations(self, agent_name: str) -> list[ToolDeclaration]:
        """Merge declarations from all runners (first occurrence wins)."""
        merged: list[ToolDeclaration] = []
        seen: set[str] = set()
        for runner in self._runners:
            for d in runner.get_tool_declarations(agent_name):
                if d.name not in seen:
                    seen.add(d.name)
                    merged.append(d)
        return merged

    def reload(self, agent_name: str | None = None) -> None:
        """Delegate reload to all runners."""
        for runner in self._runners:
            runner.reload(agent_name)
