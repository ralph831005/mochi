"""Registry loader — reads system/registry.yaml and provides agent discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class AgentEntry:
    """An agent entry from the registry."""

    name: str
    display_name: str
    description: str
    routing_keys: list[str] = field(default_factory=list)
    status: str = "active"


class Registry:
    """Holds the agent directory and provides discovery methods."""

    def __init__(self) -> None:
        self._agents: dict[str, AgentEntry] = {}

    def load(self, registry_path: Path) -> None:
        """Parse system/registry.yaml and populate agent entries."""
        with open(registry_path) as f:
            data = yaml.safe_load(f) or {}

        self._agents.clear()
        for entry in data.get("agents", []):
            agent = AgentEntry(
                name=entry["name"],
                display_name=entry.get("display_name", entry["name"]),
                description=entry.get("description", ""),
                routing_keys=entry.get("routing_keys", []),
                status=entry.get("status", "active"),
            )
            if agent.status == "active":
                self._agents[agent.name] = agent

    def find_agent_by_key(self, keyword: str) -> AgentEntry | None:
        """Search routing_keys across all active agents for a keyword match."""
        keyword_lower = keyword.lower()
        for agent in self._agents.values():
            if keyword_lower in [k.lower() for k in agent.routing_keys]:
                return agent
        return None

    def get_agent(self, name: str) -> AgentEntry | None:
        """Return a specific agent entry by name."""
        return self._agents.get(name)

    def list_agents(self) -> list[AgentEntry]:
        """Return all active agents."""
        return list(self._agents.values())

    def get_registry_summary(self) -> str:
        """Return a formatted summary of all agents for the Manager's context."""
        lines = []
        for agent in self._agents.values():
            keys = ", ".join(agent.routing_keys) if agent.routing_keys else "(fallback only)"
            lines.append(f"- **{agent.display_name}** ({agent.name}): {agent.description} [keys: {keys}]")
        return "\n".join(lines)
