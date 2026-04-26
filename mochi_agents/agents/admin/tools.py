"""Admin agent tools — agent lifecycle, file operations, deployment.

Security: file operations are restricted to agents/ and system/ directories.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

import yaml

from mochi_agents.config import get_settings

logger = logging.getLogger(__name__)

# Directories the Admin is allowed to write to (relative to project root)
_ALLOWED_DIRS = {"agents", "system", "workflows"}
_FORBIDDEN_FILES = {"secret.yaml"}


def _get_project_root() -> Path:
    """Get the project root directory."""
    settings = get_settings()
    return settings.resolve_path(".")


def _validate_path(path_str: str) -> tuple[bool, Path, str]:
    """Validate that a path is within allowed directories.

    Returns (is_valid, resolved_path, error_message).
    """
    root = _get_project_root()
    target = (root / path_str).resolve()

    # Must be under the project root
    if not str(target).startswith(str(root)):
        return False, target, f"Path escapes project root: {path_str}"

    # Must be in an allowed directory
    relative = target.relative_to(root)
    top_dir = relative.parts[0] if relative.parts else ""
    if top_dir not in _ALLOWED_DIRS:
        return False, target, f"Writes restricted to {_ALLOWED_DIRS}. Got: {top_dir}/"

    # Must not be a forbidden file
    if relative.name in _FORBIDDEN_FILES:
        return False, target, f"Cannot modify protected file: {relative.name}"

    return True, target, ""


def get_tools() -> list:
    """Return Admin-specific tools."""
    return [
        create_agent,
        edit_file,
        read_file,
        list_agents,
        add_to_registry,
        git_commit,
        trigger_reload,
        trigger_restart,
        add_alias,
        remove_alias,
        toggle_search,
        toggle_thinking,
    ]


async def toggle_search(
    agent_name: str,
    enabled: bool,
) -> dict[str, Any]:
    """Enable or disable Google Search grounding for an agent.

    When enabled, the agent can search the web for up-to-date information.
    Changes take effect after /reload.

    Args:
        agent_name: The agent's internal name. Example: "nutritionist"
        enabled: True to enable search, False to disable.
    """
    from mochi_agents.config import reload_settings

    settings = get_settings()
    config_path = settings.project_root / "config.yaml"

    config_data = {}
    if config_path.exists():
        with open(config_path) as f:
            config_data = yaml.safe_load(f) or {}

    grounding = config_data.setdefault("google_search_grounding", {})
    exclude = grounding.setdefault("exclude_agents", [])

    if enabled and agent_name in exclude:
        exclude.remove(agent_name)
        action = "enabled"
    elif not enabled and agent_name not in exclude:
        exclude.append(agent_name)
        action = "disabled"
    else:
        state = "enabled" if agent_name not in exclude else "disabled"
        return {"status": "no_change", "message": f"Search is already {state} for {agent_name}"}

    with open(config_path, "w") as f:
        yaml.dump(config_data, f, default_flow_style=False, sort_keys=False)

    reload_settings()
    logger.info(f"Admin {action} search for '{agent_name}'")

    return {
        "status": action,
        "agent": agent_name,
        "message": f"Search {action} for {agent_name}. Send /reload to apply.",
    }


async def toggle_thinking(
    agent_name: str,
    level: str,
) -> dict[str, Any]:
    """Set the thinking/reasoning level for an agent.

    Higher levels produce deeper reasoning but increase latency.
    Changes take effect after /reload.

    Args:
        agent_name: The agent's internal name. Example: "learner"
        level: Thinking level — "off", "minimal", "low", "medium", or "high".
    """
    from mochi_agents.config import reload_settings

    valid_levels = {"off", "minimal", "low", "medium", "high"}
    level = level.lower().strip()
    if level not in valid_levels:
        return {"status": "error", "message": f"Invalid level '{level}'. Use: {', '.join(sorted(valid_levels))}"}

    settings = get_settings()
    config_path = settings.project_root / "config.yaml"

    config_data = {}
    if config_path.exists():
        with open(config_path) as f:
            config_data = yaml.safe_load(f) or {}

    thinking = config_data.setdefault("thinking", {})
    agents_map = thinking.setdefault("agents", {})

    if level == "off":
        if agent_name in agents_map:
            del agents_map[agent_name]
            action = "disabled"
        else:
            return {"status": "no_change", "message": f"Thinking is already off for {agent_name}"}
    else:
        old_level = agents_map.get(agent_name)
        if old_level == level:
            return {"status": "no_change", "message": f"Thinking is already '{level}' for {agent_name}"}
        agents_map[agent_name] = level
        action = f"set to '{level}'"

    with open(config_path, "w") as f:
        yaml.dump(config_data, f, default_flow_style=False, sort_keys=False)

    reload_settings()
    logger.info(f"Admin {action} thinking for '{agent_name}'")

    return {
        "status": "updated",
        "agent": agent_name,
        "level": level,
        "message": f"Thinking {action} for {agent_name}. Send /reload to apply.",
    }


async def add_alias(
    alias: str,
    agent_name: str,
    shortcut: str,
) -> dict[str, Any]:
    """Register a global command alias for a shortcut.

    After registration, users can type /alias instead of /agent shortcut.
    Changes are live immediately (no /reload needed).

    Example: add_alias("smoothie", "nutritionist", "smoothie")
    → Users can type /smoothie instead of /noa smoothie
    """
    from mochi_agents.core.shortcut_runner import get_alias_registry
    registry = get_alias_registry()
    return registry.add(alias.lower().strip(), agent_name, shortcut)


async def remove_alias(
    alias: str,
) -> dict[str, Any]:
    """Remove a global command alias.

    Example: remove_alias("smoothie") → /smoothie no longer works
    """
    from mochi_agents.core.shortcut_runner import get_alias_registry
    registry = get_alias_registry()
    return registry.remove(alias.lower().strip())


async def create_agent(
    name: str,
    display_name: str,
    description: str,
    aliases: str = "",
    routing_keys: str = "",
    model: str = "gemini-2.0-flash",
    temperature: float = 0.3,
) -> dict[str, Any]:
    """Create a new agent with mission files. Does NOT register it — use add_to_registry for that.

    Args:
        name: Agent identifier (lowercase, no spaces). Example: "weather"
        display_name: Human-readable name. Example: "Weather"
        description: What the agent does. Example: "Provides weather forecasts"
        aliases: Comma-separated short aliases. Example: "w,weather"
        routing_keys: Comma-separated routing keywords. Example: "weather,forecast,rain"
        model: LLM model name. Default: gemini-2.0-flash
        temperature: LLM temperature. Default: 0.3
    """
    root = _get_project_root()
    agent_dir = root / "agents" / name

    if agent_dir.exists():
        return {"status": "error", "message": f"Agent directory already exists: agents/{name}/"}

    # Parse comma-separated values
    alias_list = [a.strip() for a in aliases.split(",") if a.strip()] if aliases else []
    key_list = [k.strip() for k in routing_keys.split(",") if k.strip()] if routing_keys else []

    # Build mission.yaml
    mission = {
        "name": name,
        "display_name": display_name,
        "description": description,
        "model_config": {
            "provider": "google",
            "model": model,
            "temperature": temperature,
            "max_tokens": 2048,
        },
        "capabilities": ["text_prompt"],
    }
    if alias_list:
        mission["aliases"] = alias_list
    if key_list:
        mission["routing_keys"] = key_list

    # Create directory and files
    agent_dir.mkdir(parents=True, exist_ok=True)

    with open(agent_dir / "mission.yaml", "w") as f:
        yaml.dump(mission, f, default_flow_style=False, sort_keys=False)

    # Create a starter mission prompt
    prompt = (
        f"# {display_name} Agent — System Prompt\n\n"
        f"You are **{display_name}**, an agent in the Mochi multi-agent system.\n\n"
        f"{description}\n\n"
        f"## Your Responsibilities\n\n"
        f"1. Help the user with tasks related to your domain.\n"
        f"2. Use your tools proactively when appropriate.\n"
        f"3. Remember user preferences using `save_memory`.\n"
    )
    (agent_dir / "mission_prompt.md").write_text(prompt)

    logger.info(f"Created agent scaffold: agents/{name}/")

    return {
        "status": "created",
        "path": f"agents/{name}/",
        "files": ["mission.yaml", "mission_prompt.md"],
        "next_step": f"Call add_to_registry to register '{name}', then trigger_reload to activate.",
    }


async def edit_file(
    path: str,
    content: str,
) -> dict[str, Any]:
    """Write content to a file. Restricted to agents/ and system/ directories.

    Args:
        path: Relative path from project root. Example: "agents/weather/mission.yaml"
        content: Full file content to write.
    """
    valid, target, error = _validate_path(path)
    if not valid:
        return {"status": "error", "message": error}

    # Create parent directory if needed
    target.parent.mkdir(parents=True, exist_ok=True)

    target.write_text(content)
    logger.info(f"Admin wrote file: {path}")

    return {"status": "written", "path": path, "bytes": len(content)}


async def read_file(
    path: str,
) -> dict[str, Any]:
    """Read a file's contents. Restricted to agents/ and system/ directories.

    Args:
        path: Relative path from project root. Example: "agents/nutritionist/mission.yaml"
    """
    valid, target, error = _validate_path(path)
    if not valid:
        return {"status": "error", "message": error}

    if not target.exists():
        return {"status": "error", "message": f"File not found: {path}"}

    content = target.read_text()
    return {"status": "ok", "path": path, "content": content}


async def list_agents() -> dict[str, Any]:
    """List all registered agents with their display names, descriptions, and aliases."""
    root = _get_project_root()
    registry_path = root / "system" / "registry.yaml"

    if not registry_path.exists():
        return {"status": "error", "message": "registry.yaml not found"}

    with open(registry_path) as f:
        data = yaml.safe_load(f) or {}

    agents = []
    for entry in data.get("agents", []):
        agents.append({
            "name": entry["name"],
            "display_name": entry.get("display_name", entry["name"]),
            "description": entry.get("description", ""),
            "status": entry.get("status", "active"),
        })

    return {"count": len(agents), "agents": agents}


async def add_to_registry(
    name: str,
    display_name: str,
    description: str,
    routing_keys: str = "",
) -> dict[str, Any]:
    """Add a new agent entry to system/registry.yaml.

    Args:
        name: Agent identifier. Example: "weather"
        display_name: Human-readable name. Example: "Weather"
        description: What the agent does.
        routing_keys: Comma-separated keywords. Example: "weather,forecast,rain"
    """
    root = _get_project_root()
    registry_path = root / "system" / "registry.yaml"

    with open(registry_path) as f:
        data = yaml.safe_load(f) or {"agents": []}

    # Check if already registered
    existing = [a["name"] for a in data.get("agents", [])]
    if name in existing:
        return {"status": "error", "message": f"Agent '{name}' is already in the registry"}

    key_list = [k.strip() for k in routing_keys.split(",") if k.strip()] if routing_keys else []

    entry = {
        "name": name,
        "display_name": display_name,
        "description": description,
        "routing_keys": key_list,
        "status": "active",
    }
    data.setdefault("agents", []).append(entry)

    with open(registry_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    logger.info(f"Admin added '{name}' to registry")
    return {"status": "registered", "name": name}


async def git_commit(
    message: str,
) -> dict[str, Any]:
    """Create a git commit with ALL current changes. Always call this before editing files.

    Args:
        message: Commit message. Example: "Admin: creating weather agent"
    """
    root = _get_project_root()

    try:
        # Stage all changes
        subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True, check=True)

        # Check if there are changes to commit
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=root, capture_output=True,
        )

        if result.returncode == 0:
            return {"status": "ok", "message": "No changes to commit"}

        # Commit
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=root, capture_output=True, check=True,
        )

        logger.info(f"Admin git commit: {message}")
        return {"status": "committed", "message": message}

    except subprocess.CalledProcessError as e:
        return {"status": "error", "message": f"Git error: {e.stderr.decode() if e.stderr else str(e)}"}


async def trigger_reload() -> dict[str, Any]:
    """Trigger a /reload to pick up config and registry changes. Use after editing YAML files."""
    from mochi_agents.config import reload_settings
    from mochi_agents.core.registry import Registry

    # This is a simplified reload — the full reload happens through the Router
    # We set a flag that the bot checks
    logger.info("Admin triggered reload")
    return {
        "status": "ok",
        "message": "Send /reload in chat to apply changes, or wait for the next message cycle.",
        "hint": "For immediate effect, tell the user to type /reload.",
    }


async def trigger_restart() -> dict[str, Any]:
    """Trigger a /restart to pick up Python code changes. Use after modifying .py files."""
    logger.info("Admin triggered restart request")
    return {
        "status": "ok",
        "message": "Send /restart in chat to restart the bot, or wait for the next message cycle.",
        "hint": "For immediate effect, tell the user to type /restart.",
    }
