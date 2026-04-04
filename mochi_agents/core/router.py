"""Input Router — routes messages to the correct agent.

Supports:
- System commands (/reload)
- Direct routing (slash commands, keyword matching)
- Delegated routing via Manager agent
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from mochi_agents.config import get_settings, reload_settings
from mochi_agents.core.registry import Registry

if TYPE_CHECKING:
    from mochi_agents.core.agent_runtime import AgentRuntime
    from mochi_agents.core.tool_runner import ImportlibToolRunner

logger = logging.getLogger(__name__)


class Router:
    """Routes incoming messages to the appropriate agent."""

    def __init__(
        self,
        registry: Registry,
        runtime: AgentRuntime,
        tool_runner: ImportlibToolRunner,
    ) -> None:
        self.registry = registry
        self.runtime = runtime
        self.tool_runner = tool_runner

    async def route(self, text: str, user_id: str = "") -> str:
        """Route a message and return the agent's response text."""

        # Step 0: System commands
        if text.strip().startswith("/reload"):
            return await self._handle_reload()

        # Step 1: Direct routing — slash commands
        if text.strip().startswith("/"):
            parts = text.strip().split(maxsplit=1)
            command = parts[0][1:]  # Remove the /
            message_body = parts[1] if len(parts) > 1 else ""

            agent = self.registry.get_agent(command)
            if agent:
                logger.info(f"Direct route (slash command): /{command} → {agent.name}")
                return await self.runtime.execute(agent.name, message_body or text)

        # Step 1b: Direct routing — keyword matching
        agent = self._keyword_match(text)
        if agent:
            logger.info(f"Direct route (keyword): → {agent.name}")
            return await self.runtime.execute(agent.name, text)

        # Step 2: Delegated routing via Manager
        logger.info("No direct match — delegating to Manager")
        return await self._delegate_to_manager(text)

    def _keyword_match(self, text: str, threshold: int = 1) -> object | None:
        """Check if any routing keywords appear in the text."""
        words = set(text.lower().split())

        best_agent = None
        best_score = 0

        for agent in self.registry.list_agents():
            if not agent.routing_keys:
                continue
            score = sum(1 for key in agent.routing_keys if key.lower() in words)
            if score >= threshold and score > best_score:
                best_agent = agent
                best_score = score

        return best_agent

    async def _delegate_to_manager(self, text: str) -> str:
        """Use the Manager agent to determine routing, then execute the chosen agent."""
        # Give the Manager the registry summary as context
        registry_summary = self.registry.get_registry_summary()
        extra_context = f"## Available Agents\n\n{registry_summary}"

        manager_response = await self.runtime.execute(
            "manager",
            text,
            extra_context=extra_context,
        )

        # Try to parse routing decision from Manager's response
        route_to = self._parse_routing_decision(manager_response)

        if route_to and route_to != "manager":
            agent = self.registry.get_agent(route_to)
            if agent:
                logger.info(f"Manager routed → {agent.name}")
                return await self.runtime.execute(agent.name, text)

        # Manager handled it directly (no routing needed)
        return manager_response

    def _parse_routing_decision(self, response: str) -> str | None:
        """Try to extract a route_to decision from the Manager's response."""
        # Look for JSON block in the response
        try:
            # Try to find JSON in the response
            start = response.find("{")
            end = response.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(response[start:end])
                return data.get("route_to")
        except (json.JSONDecodeError, ValueError):
            pass
        return None

    async def _handle_reload(self) -> str:
        """Handle the /reload system command."""
        logger.info("Handling /reload command")

        # Reload config
        settings = reload_settings()

        # Reload registry
        registry_path = settings.resolve_path(settings.system_dir) / "registry.yaml"
        self.registry.load(registry_path)

        # Reload tool modules
        self.tool_runner.reload()

        # Reload missions
        self.runtime.reload_missions()

        agents = self.registry.list_agents()
        agent_names = ", ".join(a.display_name for a in agents)

        return f"🔄 Reloaded!\n• Config: ✅\n• Registry: {len(agents)} agents ({agent_names})\n• Tools: ✅\n• Missions: ✅"
