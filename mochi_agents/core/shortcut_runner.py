"""Shortcut Runner — executes LLM-free shortcut flows.

Shortcuts are defined in agent mission.yaml files and provide fast,
deterministic interactions without LLM API calls. They collect
structured input from users and call tools directly.

Also contains the AliasRegistry for global command aliases.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from mochi_agents.config import get_settings

if TYPE_CHECKING:
    from mochi_agents.core.registry import Registry
    from mochi_agents.core.tool_runner import CompositeToolRunner

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Alias Registry
# ---------------------------------------------------------------------------

class AliasRegistry:
    """Manages global command aliases → agent+shortcut mappings.

    Persisted to data/aliases.yaml. Changes are immediately written to disk
    and reflected in the in-memory map (no /reload needed).
    """

    def __init__(self, data_dir: Path) -> None:
        self._aliases: dict[str, dict[str, str]] = {}
        self._file_path = data_dir / "aliases.yaml"
        self._load()

    def _load(self) -> None:
        """Load aliases from disk."""
        if self._file_path.exists():
            with open(self._file_path) as f:
                self._aliases = yaml.safe_load(f) or {}

    def _save(self) -> None:
        """Persist aliases to disk."""
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._file_path, "w") as f:
            yaml.dump(self._aliases, f, default_flow_style=False, sort_keys=False)

    def add(self, alias: str, agent_name: str, shortcut: str) -> dict[str, Any]:
        """Register a global alias. Returns status dict."""
        alias = alias.lower().strip()
        self._aliases[alias] = {"agent": agent_name, "shortcut": shortcut}
        self._save()
        logger.info(f"Alias registered: /{alias} → {agent_name}:{shortcut}")
        return {"status": "ok", "alias": alias, "agent": agent_name, "shortcut": shortcut}

    def remove(self, alias: str) -> dict[str, Any]:
        """Remove a global alias. Returns status dict."""
        alias = alias.lower().strip()
        if alias not in self._aliases:
            return {"status": "error", "message": f"Alias '/{alias}' not found"}
        del self._aliases[alias]
        self._save()
        logger.info(f"Alias removed: /{alias}")
        return {"status": "ok", "alias": alias}

    def get(self, alias: str) -> dict[str, str] | None:
        """Look up an alias. Returns {agent, shortcut} or None."""
        return self._aliases.get(alias.lower().strip())

    def list_all(self) -> dict[str, dict[str, str]]:
        """Return all registered aliases."""
        return dict(self._aliases)


# Module-level singleton
_alias_registry: AliasRegistry | None = None


def get_alias_registry() -> AliasRegistry:
    """Get or create the global AliasRegistry singleton."""
    global _alias_registry
    if _alias_registry is None:
        settings = get_settings()
        data_dir = settings.resolve_path(settings.data_dir)
        _alias_registry = AliasRegistry(data_dir)
    return _alias_registry


def init_alias_registry(data_dir: Path) -> AliasRegistry:
    """Initialize the AliasRegistry with an explicit data_dir."""
    global _alias_registry
    _alias_registry = AliasRegistry(data_dir)
    return _alias_registry


# ShortcutRunner singleton
_shortcut_runner_instance: ShortcutRunner | None = None


def get_shortcut_runner() -> ShortcutRunner | None:
    """Get the global ShortcutRunner instance."""
    return _shortcut_runner_instance


def set_shortcut_runner(runner: ShortcutRunner) -> None:
    """Set the global ShortcutRunner instance."""
    global _shortcut_runner_instance
    _shortcut_runner_instance = runner


# ---------------------------------------------------------------------------
# Shortcut Session
# ---------------------------------------------------------------------------

@dataclass
class ShortcutSession:
    """Tracks an active shortcut flow for a user."""

    user_id: str
    agent_name: str
    shortcut_name: str
    fields: list[dict[str, Any]]
    on_complete: dict[str, Any]
    created_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Shortcut Runner
# ---------------------------------------------------------------------------

class ShortcutRunner:
    """Executes shortcut flows without LLM calls.

    Lifecycle:
    1. User triggers a shortcut (e.g., /noa smoothie)
    2. Runner sends a template prompt listing expected fields
    3. User replies with space-separated values
    4. Runner parses values, substitutes into on_complete args, calls the tool
    5. Returns formatted result
    """

    SESSION_TIMEOUT = 120  # seconds

    def __init__(
        self,
        tool_runner: CompositeToolRunner,
        registry: Registry,
    ) -> None:
        self._tool_runner = tool_runner
        self._registry = registry
        self._shortcuts: dict[str, dict[str, dict]] = {}  # agent → {command → definition}
        self._sessions: dict[str, ShortcutSession] = {}   # user_id → active session

    def load_shortcuts(self) -> None:
        """Load shortcut definitions from mission.yaml (static) + data dir (dynamic)."""
        settings = get_settings()
        agents_dir = settings.resolve_path(settings.agents_dir)
        self._shortcuts.clear()

        if not agents_dir.exists():
            return

        # Layer 1: Static shortcuts from mission.yaml
        for mission_path in agents_dir.glob("*/mission.yaml"):
            agent_name = mission_path.parent.name
            with open(mission_path) as f:
                mission = yaml.safe_load(f) or {}

            for shortcut_def in mission.get("shortcuts", []):
                command = shortcut_def.get("command", "")
                if not command:
                    continue
                self._shortcuts.setdefault(agent_name, {})[command] = shortcut_def
                logger.info(f"Shortcut registered (static): /{agent_name} {command}")

        # Layer 2: Dynamic shortcuts from data dir (override static)
        self._load_data_shortcuts()

        total = sum(len(v) for v in self._shortcuts.values())
        logger.info(f"Loaded {total} shortcut(s) across {len(self._shortcuts)} agent(s)")

    def _load_data_shortcuts(self) -> None:
        """Load dynamic shortcuts from data/shortcuts/*.yaml."""
        settings = get_settings()
        data_dir = settings.resolve_path(settings.data_dir)
        shortcuts_dir = data_dir / "shortcuts"

        if not shortcuts_dir.exists():
            return

        for shortcut_file in shortcuts_dir.glob("*.yaml"):
            agent_name = shortcut_file.stem
            with open(shortcut_file) as f:
                data = yaml.safe_load(f) or {}

            for command, definition in data.items():
                self._shortcuts.setdefault(agent_name, {})[command] = definition
                logger.info(f"Shortcut registered (dynamic): /{agent_name} {command}")

    def save_shortcut(self, agent_name: str, command: str, definition: dict) -> dict[str, Any]:
        """Save a shortcut to the data layer and update in-memory cache."""
        agent = self._registry.get_agent(agent_name)
        if agent:
            agent_name = agent.name
            
        settings = get_settings()
        data_dir = settings.resolve_path(settings.data_dir)
        shortcuts_dir = data_dir / "shortcuts"
        shortcuts_dir.mkdir(parents=True, exist_ok=True)

        file_path = shortcuts_dir / f"{agent_name}.yaml"

        # Load existing
        existing: dict = {}
        if file_path.exists():
            with open(file_path) as f:
                existing = yaml.safe_load(f) or {}

        # Update
        existing[command] = definition

        # Write
        with open(file_path, "w") as f:
            yaml.dump(existing, f, default_flow_style=False, sort_keys=False)

        # Update in-memory
        self._shortcuts.setdefault(agent_name, {})[command] = definition
        logger.info(f"Shortcut saved: /{agent_name} {command}")
        return {"status": "ok", "agent": agent_name, "command": command}

    def delete_shortcut(self, agent_name: str, command: str) -> dict[str, Any]:
        """Remove a shortcut from the data layer and in-memory cache."""
        agent = self._registry.get_agent(agent_name)
        if agent:
            agent_name = agent.name
            
        settings = get_settings()
        data_dir = settings.resolve_path(settings.data_dir)
        file_path = data_dir / "shortcuts" / f"{agent_name}.yaml"

        # Remove from file
        if file_path.exists():
            with open(file_path) as f:
                existing = yaml.safe_load(f) or {}
            if command in existing:
                del existing[command]
                with open(file_path, "w") as f:
                    yaml.dump(existing, f, default_flow_style=False, sort_keys=False)

        # Remove from in-memory
        agent_shortcuts = self._shortcuts.get(agent_name, {})
        if command in agent_shortcuts:
            del agent_shortcuts[command]
            logger.info(f"Shortcut deleted: /{agent_name} {command}")
            return {"status": "ok", "agent": agent_name, "command": command}

        return {"status": "error", "message": f"Shortcut '/{agent_name} {command}' not found"}

    def get_shortcut(self, agent_name: str, command: str) -> dict | None:
        """Get a shortcut definition by agent and command name."""
        return self._shortcuts.get(agent_name, {}).get(command)

    def find_shortcut_agent(self, command: str) -> str | None:
        """Find the agent name that owns a specific shortcut command.
        
        This enables global shortcut execution without requiring explicit AliasRegistry mapping.
        """
        for agent_name, agent_shortcuts in self._shortcuts.items():
            if command in agent_shortcuts:
                return agent_name
        return None

    def list_shortcuts(self, agent_name: str) -> list[dict]:
        """List all shortcuts for an agent."""
        agent_shortcuts = self._shortcuts.get(agent_name, {})
        return [
            {"command": cmd, "description": defn.get("description", cmd)}
            for cmd, defn in agent_shortcuts.items()
        ]

    def has_active_session(self, user_id: str) -> bool:
        """Check if a user has an active shortcut session."""
        session = self._sessions.get(user_id)
        if session is None:
            return False
        # Check timeout
        if time.time() - session.created_at > self.SESSION_TIMEOUT:
            del self._sessions[user_id]
            return False
        return True

    async def start_shortcut(self, user_id: str, agent_name: str, raw_command: str) -> Any:
        """Start a shortcut flow. Returns the template prompt, or None if not found."""
        from mochi_agents.core.router import RouteResponse
        
        parts = raw_command.strip().split()
        command = parts[0]
        args = parts[1:]
        
        shortcut = self.get_shortcut(agent_name, command)
        if not shortcut:
            return None

        fields = shortcut.get("fields", [])
        on_complete = shortcut.get("on_complete", {})

        if not fields or not on_complete:
            return None

        # Inline execution if exact parameter count is provided
        if len(args) == len(fields):
            temp_session = ShortcutSession(
                user_id=user_id,
                agent_name=agent_name,
                shortcut_name=command,
                fields=fields,
                on_complete=on_complete,
            )
            self._sessions[user_id] = temp_session
            text_input = " ".join(args)
            return await self.handle_input(user_id, text_input)

        # Create session
        self._sessions[user_id] = ShortcutSession(
            user_id=user_id,
            agent_name=agent_name,
            shortcut_name=command,
            fields=fields,
            on_complete=on_complete,
        )

        # Build template prompt
        description = shortcut.get("description", command)
        labels = " | ".join(f"{f.get('label', f['name'])}" for f in fields)
        defaults = " | ".join(str(f.get("default", "?")) for f in fields)
        example = " ".join(str(f.get("default", "0")) for f in fields)

        prompt = (
            f"**{description}**\n\n"
            f"Reply with: `{labels}`\n"
            f"Defaults: `{defaults}`\n\n"
            f"Example: `{example}`\n\n"
            f"*Type cancel to abort.*"
        )
        
        callback_data = f"/{agent_name} {command} {example}"
        if len(callback_data) <= 64:
            keyboard = [[{"text": f"✅ Use Defaults ({example})", "callback_data": callback_data}]]
            return RouteResponse(text=prompt, keyboard=keyboard)

        return RouteResponse(text=prompt)

    async def handle_input(self, user_id: str, text: str) -> str | None:
        """Handle user input during an active shortcut session.

        Returns the result string, or None if no active session.
        """
        session = self._sessions.get(user_id)
        if session is None:
            return None

        # Check timeout
        if time.time() - session.created_at > self.SESSION_TIMEOUT:
            del self._sessions[user_id]
            return "⏰ Shortcut timed out. Start again with the command."

        # Cancel
        if text.strip().lower() == "cancel":
            del self._sessions[user_id]
            return "❌ Shortcut cancelled."

        # Slash command → auto-cancel and let Router handle it
        if text.strip().startswith("/"):
            del self._sessions[user_id]
            return None  # fall through to normal routing

        # Parse values
        values = text.strip().split()
        fields = session.fields

        if len(values) != len(fields):
            labels = " | ".join(f.get("label", f["name"]) for f in fields)
            return (
                f"Expected {len(fields)} values ({labels}), got {len(values)}.\n"
                f"Try again or type `cancel`."
            )

        # Convert values to the right types
        parsed = {}
        for field_def, raw_value in zip(fields, values):
            name = field_def["name"]
            field_type = field_def.get("type", "string")
            try:
                if field_type == "number":
                    parsed[name] = float(raw_value) if "." in raw_value else int(raw_value)
                elif field_type == "integer":
                    parsed[name] = int(raw_value)
                else:
                    parsed[name] = raw_value
            except (ValueError, TypeError):
                del self._sessions[user_id]
                return f"❌ Invalid value for {field_def.get('label', name)}: `{raw_value}` (expected {field_type})"

        # Build tool args by substituting variables
        tool_name = session.on_complete.get("tool", "")
        arg_templates = session.on_complete.get("args", {})
        final_args = {}
        for key, template in arg_templates.items():
            if isinstance(template, str):
                # Substitute ${var} and $var patterns
                result = template
                for var_name, var_value in parsed.items():
                    result = result.replace(f"${{{var_name}}}", str(var_value))
                    result = result.replace(f"${var_name}", str(var_value))
                # Substitute $user_id
                result = result.replace("$user_id", session.user_id)
                final_args[key] = result
            else:
                final_args[key] = template

        # Execute the tool directly
        try:
            from mochi_agents.core.shared_tools import set_current_agent
            set_current_agent(session.agent_name)

            result = await self._tool_runner.execute(
                session.agent_name, tool_name, final_args
            )

            # Clean up session
            del self._sessions[user_id]

            if result.success:
                return f"✅ **Done!** {session.shortcut_name}: {self._format_summary(parsed, fields)}"
            else:
                return f"⚠️ Shortcut completed with warning: {result.error}"
        except Exception as e:
            del self._sessions[user_id]
            logger.error(f"Shortcut tool execution failed: {e}", exc_info=True)
            return f"❌ Shortcut failed: {e}"

    def _format_summary(self, parsed: dict, fields: list[dict]) -> str:
        """Format a human-readable summary of collected values."""
        parts = []
        for f in fields:
            name = f["name"]
            label = f.get("label", name)
            unit = f.get("unit", "")
            value = parsed.get(name, "?")
            parts.append(f"{value}{unit} {label}" if unit else f"{label}: {value}")
        return ", ".join(parts)

    def reload(self) -> None:
        """Reload shortcut definitions from mission files."""
        self.load_shortcuts()
