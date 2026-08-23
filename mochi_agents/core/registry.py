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
    aliases: list[str] = field(default_factory=list)
    status: str = "active"


class Registry:
    """Holds the agent directory and provides discovery methods."""

    def __init__(self) -> None:
        self._agents: dict[str, AgentEntry] = {}
        self._alias_map: dict[str, str] = {}  # alias -> agent name

    def load(self, registry_path: Path) -> None:
        """Parse system/registry.yaml + user_registry.yaml and populate agent entries."""
        self._agents.clear()
        self._alias_map.clear()

        # Load system registry (upstream, tracked in git)
        all_entries = []
        if registry_path.exists():
            with open(registry_path) as f:
                data = yaml.safe_load(f) or {}
            all_entries.extend(data.get("agents", []))

        # Load user registry (local, git-ignored)
        user_registry_path = registry_path.parent / "user_registry.yaml"
        if user_registry_path.exists():
            with open(user_registry_path) as f:
                user_data = yaml.safe_load(f) or {}
            all_entries.extend(user_data.get("agents", []))

        for entry in all_entries:
            agent = AgentEntry(
                name=entry["name"],
                display_name=entry.get("display_name", entry["name"]),
                description=entry.get("description", ""),
                routing_keys=entry.get("routing_keys", []),
                aliases=entry.get("aliases", []),
                status=entry.get("status", "active"),
            )
            if agent.status == "active":
                self._agents[agent.name] = agent
                for alias in agent.aliases:
                    self._alias_map[alias.lower()] = agent.name

    def load_aliases_from_missions(self, agents_dir: Path, custom_agents_dir: Path | None = None) -> None:
        """Scan mission files for aliases and merge into the registry."""
        for agent in self._agents.values():
            mission_path = agents_dir / agent.name / "mission.yaml"
            if not mission_path.exists() and custom_agents_dir:
                mission_path = custom_agents_dir / agent.name / "mission.yaml"
            if not mission_path.exists():
                continue
            with open(mission_path) as f:
                mission = yaml.safe_load(f) or {}
            aliases = mission.get("aliases", [])
            agent.aliases = aliases
            for alias in aliases:
                self._alias_map[alias.lower()] = agent.name

    def find_agent_by_key(self, keyword: str) -> AgentEntry | None:
        """Search routing_keys across all active agents for a keyword match."""
        keyword_lower = keyword.lower()
        for agent in self._agents.values():
            if keyword_lower in [k.lower() for k in agent.routing_keys]:
                return agent
        return None

    def get_agent(self, name: str) -> AgentEntry | None:
        """Return a specific agent entry by name or alias."""
        agent = self._agents.get(name)
        if agent:
            return agent
        # Check aliases
        resolved = self._alias_map.get(name.lower())
        if resolved:
            return self._agents.get(resolved)
        return None

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
