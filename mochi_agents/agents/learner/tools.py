"""Learner agent tools — skill discovery, workflow drafting, agent scaffolding.

Drafts are placed with a .draft suffix and require human approval before deployment.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

import yaml

from mochi_agents.config import get_settings

logger = logging.getLogger(__name__)


def _get_project_root() -> Path:
    settings = get_settings()
    return settings.resolve_path(".")


def get_tools() -> list:
    """Return Learner-specific tools."""
    return [draft_workflow, draft_agent, draft_mcp_tool, list_existing_skills]


async def draft_workflow(
    name: str,
    description: str,
    agent: str,
    steps_yaml: str,
) -> dict[str, Any]:
    """Create a YAML workflow draft for human approval.

    Args:
        name: Workflow identifier (lowercase, no spaces). Example: "grocery_list"
        description: What this workflow does.
        agent: Which agent executes the steps. Example: "nutritionist"
        steps_yaml: YAML string defining the steps list. Must be valid YAML.
    """
    root = _get_project_root()
    draft_path = root / "workflows" / f"{name}.yaml.draft"

    # Parse the steps YAML to validate it
    try:
        steps = yaml.safe_load(steps_yaml)
        if not isinstance(steps, list):
            return {"status": "error", "message": "steps_yaml must be a YAML list of step objects"}
    except yaml.YAMLError as e:
        return {"status": "error", "message": f"Invalid YAML in steps: {e}"}

    workflow = {
        "name": name,
        "description": description,
        "agent": agent,
        "steps": steps,
    }

    draft_path.parent.mkdir(parents=True, exist_ok=True)
    with open(draft_path, "w") as f:
        yaml.dump(workflow, f, default_flow_style=False, sort_keys=False)

    logger.info(f"Learner drafted workflow: {draft_path}")

    return {
        "status": "drafted",
        "path": f"workflows/{name}.yaml.draft",
        "approval": f"Tell the user to type /approve {name} to deploy, or /reject {name} to discard.",
    }


async def draft_agent(
    name: str,
    display_name: str,
    description: str,
    routing_keys: str = "",
) -> dict[str, Any]:
    """Scaffold a new agent as a draft for human approval.

    Args:
        name: Agent identifier (lowercase, no spaces). Example: "travel"
        display_name: Human-readable name. Example: "Travel Planner"
        description: What the agent does.
        routing_keys: Comma-separated keywords. Example: "travel,trip,flight,hotel"
    """
    root = _get_project_root()
    draft_dir = root / "agents" / f"{name}.draft"

    if draft_dir.exists():
        return {"status": "error", "message": f"Draft already exists: agents/{name}.draft/"}

    key_list = [k.strip() for k in routing_keys.split(",") if k.strip()] if routing_keys else []

    mission = {
        "name": name,
        "display_name": display_name,
        "description": description,
        "aliases": [name[0]],  # First letter as shortcut
        "routing_keys": key_list,
        "model_config": {
            "provider": "google",
            "model": "gemini-2.0-flash",
            "temperature": 0.3,
            "max_tokens": 2048,
        },
        "capabilities": ["text_prompt"],
    }

    draft_dir.mkdir(parents=True, exist_ok=True)

    with open(draft_dir / "mission.yaml", "w") as f:
        yaml.dump(mission, f, default_flow_style=False, sort_keys=False)

    prompt = (
        f"# {display_name} Agent — System Prompt\n\n"
        f"You are **{display_name}**, an agent in the Mochi multi-agent system.\n\n"
        f"{description}\n\n"
        f"## Your Responsibilities\n\n"
        f"1. Help the user with tasks related to your domain.\n"
        f"2. Use your tools proactively when appropriate.\n"
        f"3. Remember user preferences using `save_memory`.\n"
    )
    (draft_dir / "mission_prompt.md").write_text(prompt)

    logger.info(f"Learner drafted agent: {draft_dir}")

    return {
        "status": "drafted",
        "path": f"agents/{name}.draft/",
        "files": ["mission.yaml", "mission_prompt.md"],
        "approval": f"Tell the user to type /approve {name} to deploy, or /reject {name} to discard.",
    }


async def draft_mcp_tool(
    name: str,
    description: str,
    python_code: str,
    target_agent: str,
) -> dict[str, Any]:
    """Draft a new Python tool as an MCP service for human approval.

    The python_code MUST import mcp from skills_server.server and use the @mcp.tool()
    decorator. Do not provide a generic python function without the decorator.

    Example python_code:
        from skills_server.server import mcp
        import urllib.request
        
        @mcp.tool()
        def get_dog_image() -> str:
            '''Get a random dog image URL.'''
            return "https://..."

    Args:
        name: Tool identifier (lowercase, no spaces). Example: "dog_api"
        description: What this tool does.
        python_code: The raw python script implementing the tool.
        target_agent: Which agent this tool belongs to. Example: "nutritionist"
    """
    root = _get_project_root()
    draft_path = root / "skills_server" / "skills" / f"{name}.py.draft"

    draft_path.parent.mkdir(parents=True, exist_ok=True)

    with open(draft_path, "w") as f:
        f.write(f"# TARGET_AGENT: {target_agent}\n")
        f.write(f"# DESCRIPTION: {description}\n\n")
        f.write(python_code)

    logger.info(f"Learner drafted MCP tool: {draft_path}")

    return {
        "status": "drafted",
        "path": f"skills_server/skills/{name}.py.draft",
        "approval": f"Tell the user to type /approve {name} to deploy, or /reject {name} to discard.",
    }


async def list_existing_skills() -> dict[str, Any]:
    """List all existing agents and workflows to avoid creating duplicates."""
    root = _get_project_root()

    # List agents
    registry_path = root / "system" / "registry.yaml"
    agents = []
    if registry_path.exists():
        with open(registry_path) as f:
            data = yaml.safe_load(f) or {}
        agents = [
            {"name": a["name"], "description": a.get("description", "")}
            for a in data.get("agents", [])
        ]

    # List workflows
    workflows = []
    workflows_dir = root / "workflows"
    if workflows_dir.exists():
        for path in workflows_dir.glob("*.yaml"):
            with open(path) as f:
                w = yaml.safe_load(f) or {}
            workflows.append({
                "name": w.get("name", path.stem),
                "description": w.get("description", ""),
            })

    # List drafts
    drafts = []
    for path in workflows_dir.glob("*.yaml.draft") if workflows_dir.exists() else []:
        drafts.append({"type": "workflow", "name": path.stem.replace(".yaml", "")})
    for path in (root / "agents").glob("*.draft"):
        drafts.append({"type": "agent", "name": path.stem.replace(".draft", "")})

    return {
        "agents": agents,
        "workflows": workflows,
        "pending_drafts": drafts,
    }
