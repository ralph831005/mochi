"""Shared tools — available to ALL agents automatically.

These are system-level tools injected by the ToolRunner regardless of
what's in an agent's tools_module. Domain-specific tools stay in each
agent's own tools.py.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from sqlalchemy import select

from mochi_agents.config import get_settings
from mochi_agents.memory.database import get_session_factory
from mochi_agents.memory.models import MemoryNote


# Coroutine-safe context — each async task gets its own copy.
_current_agent: ContextVar[str] = ContextVar("_current_agent", default="default")
_current_runtime: ContextVar[Any] = ContextVar("_current_runtime", default=None)
_current_depth: ContextVar[int] = ContextVar("_current_depth", default=0)

MAX_DELEGATION_DEPTH = 2


def set_current_agent(agent_name: str) -> None:
    """Set the agent context for shared tools (coroutine-safe via ContextVar)."""
    _current_agent.set(agent_name)


def set_current_runtime(runtime: Any) -> None:
    """Set the runtime reference for delegation (coroutine-safe via ContextVar)."""
    _current_runtime.set(runtime)


def set_current_depth(depth: int) -> None:
    """Set the current delegation depth (coroutine-safe via ContextVar)."""
    _current_depth.set(depth)


def get_tools() -> list:
    """Return shared tool functions available to all agents."""
    return [
        save_memory, recall_memories, delete_memory,
        delegate_to_agent,
        create_shortcut, edit_shortcut, remove_shortcut,
        switch_model, configure_api_key, reconfigure_agent_model,
    ]


async def save_memory(
    category: str,
    content: str,
) -> dict[str, Any]:
    """Save a persistent memory note about the user. Categories: profile, goal, preference.

    Use this when the user shares personal info, sets a goal, or states a preference.
    These are always remembered across all conversations.

    Examples:
    - save_memory(category="profile", content="Height: 175cm, Weight: 72kg")
    - save_memory(category="goal", content="Lose 3kg by June")
    - save_memory(category="preference", content="Vegetarian, dislikes cilantro")
    """
    settings = get_settings()
    data_dir = settings.resolve_path(settings.data_dir)
    factory = get_session_factory(_current_agent.get(), data_dir)

    category = category.lower().strip()
    if category not in ("profile", "goal", "preference"):
        return {"status": "error", "message": f"Invalid category '{category}'. Use: profile, goal, preference"}

    async with factory() as session:
        note = MemoryNote(category=category, content=content)
        session.add(note)
        await session.commit()
        note_id = note.id

    return {"status": "saved", "id": note_id, "category": category, "content": content}


async def recall_memories(
    category: str = "",
) -> dict[str, Any]:
    """Recall stored memory notes. Optionally filter by category (profile, goal, preference).

    Call this when you need to check what you know about the user.
    """
    settings = get_settings()
    data_dir = settings.resolve_path(settings.data_dir)
    factory = get_session_factory(_current_agent.get(), data_dir)

    async with factory() as session:
        stmt = select(MemoryNote).order_by(MemoryNote.category, MemoryNote.id)
        if category:
            stmt = stmt.where(MemoryNote.category == category.lower().strip())

        result = await session.execute(stmt)
        notes = result.scalars().all()

    return {
        "count": len(notes),
        "notes": [
            {"id": n.id, "category": n.category, "content": n.content}
            for n in notes
        ],
    }


async def delete_memory(
    note_id: int,
) -> dict[str, Any]:
    """Delete a memory note by ID. Use when the user says information is no longer relevant."""
    settings = get_settings()
    data_dir = settings.resolve_path(settings.data_dir)
    factory = get_session_factory(_current_agent.get(), data_dir)

    async with factory() as session:
        stmt = select(MemoryNote).where(MemoryNote.id == note_id)
        result = await session.execute(stmt)
        note = result.scalar_one_or_none()

        if not note:
            return {"status": "error", "message": f"No memory note with ID {note_id}"}

        await session.delete(note)
        await session.commit()

    return {"status": "deleted", "id": note_id}


async def delegate_to_agent(
    agent_name: str,
    task: str,
) -> dict[str, Any]:
    """Delegate a task to another specialist agent and get their response.

    Use when a task falls outside your domain or requires another agent's tools.
    The target agent runs with its full context, tools, and memory.

    Examples:
    - delegate_to_agent(agent_name="admin", task="Create a shortcut 'smoothie' for nutritionist...")
    - delegate_to_agent(agent_name="learner", task="I need a tool that can query weather APIs")

    Any system changes the target agent makes will require /approve from the user.
    """
    runtime = _current_runtime.get()
    if runtime is None:
        return {"status": "error", "message": "Runtime not available for delegation"}

    current_agent = _current_agent.get()
    current_depth = _current_depth.get()

    # Guard: max delegation depth
    if current_depth >= MAX_DELEGATION_DEPTH:
        return {
            "status": "error",
            "message": f"Max delegation depth ({MAX_DELEGATION_DEPTH}) reached. "
                       f"Ask the user to invoke the target agent directly.",
        }

    # Guard: no self-delegation
    if agent_name == current_agent:
        return {"status": "error", "message": "Cannot delegate to yourself"}

    try:
        response = await runtime.execute(
            agent_name,
            task,
            source="agent",
            _delegation_depth=current_depth + 1,
        )
        return {"status": "ok", "agent": agent_name, "response": response}
    except Exception as e:
        return {"status": "error", "message": f"Delegation to '{agent_name}' failed: {e}"}
    finally:
        # Restore depth so subsequent tool calls in the same turn
        # use the correct depth (e.g., two delegations in one turn)
        _current_depth.set(current_depth)


async def create_shortcut(
    agent_name: str,
    command: str,
    description: str,
    fields_json: str,
    tool_name: str,
    tool_args_json: str,
) -> dict[str, Any]:
    """Create a new shortcut for an agent. Live immediately, no /reload needed.

    Shortcuts let users perform common actions without LLM calls.

    Args:
        agent_name: Agent that owns this shortcut (e.g., "nutritionist")
        command: Subcommand name (e.g., "smoothie")
        description: Human-readable description (e.g., "Log morning smoothie")
        fields_json: JSON array of field definitions. Each field:
            [{"name": "banana_g", "label": "banana (g)", "type": "number", "default": 100}]
        tool_name: Tool to call on completion (e.g., "log_meal")
        tool_args_json: JSON object of tool args with ${field} substitution:
            {"meal_type": "breakfast", "description": "Smoothie: ${banana_g}g banana"}
    """
    import json
    from mochi_agents.core.shortcut_runner import get_shortcut_runner

    runner = get_shortcut_runner()
    if runner is None:
        return {"status": "error", "message": "ShortcutRunner not initialized"}

    try:
        fields = json.loads(fields_json)
        tool_args = json.loads(tool_args_json)
    except json.JSONDecodeError as e:
        return {"status": "error", "message": f"Invalid JSON: {e}"}

    definition = {
        "command": command,
        "description": description,
        "fields": fields,
        "on_complete": {
            "tool": tool_name,
            "args": tool_args,
        },
    }

    return runner.save_shortcut(agent_name, command, definition)


async def edit_shortcut(
    agent_name: str,
    command: str,
    fields_json: str,
) -> dict[str, Any]:
    """Update the fields of an existing shortcut. Live immediately.

    Use when the user changes ingredients, categories, or input structure.

    Args:
        agent_name: Agent that owns this shortcut
        command: Existing shortcut command name
        fields_json: New JSON array of field definitions:
            [{"name": "spinach_g", "label": "spinach (g)", "type": "number", "default": 50}]
    """
    import json
    from mochi_agents.core.shortcut_runner import get_shortcut_runner

    runner = get_shortcut_runner()
    if runner is None:
        return {"status": "error", "message": "ShortcutRunner not initialized"}

    existing = runner.get_shortcut(agent_name, command)
    if existing is None:
        return {"status": "error", "message": f"Shortcut '/{agent_name} {command}' not found"}

    try:
        fields = json.loads(fields_json)
    except json.JSONDecodeError as e:
        return {"status": "error", "message": f"Invalid JSON: {e}"}

    # Update fields while keeping everything else
    updated = dict(existing)
    updated["fields"] = fields

    return runner.save_shortcut(agent_name, command, updated)


async def remove_shortcut(
    agent_name: str,
    command: str,
) -> dict[str, Any]:
    """Remove a shortcut from an agent. Takes effect immediately.

    Example: remove_shortcut("nutritionist", "smoothie")
    """
    from mochi_agents.core.shortcut_runner import get_shortcut_runner

    runner = get_shortcut_runner()
    if runner is None:
        return {"status": "error", "message": "ShortcutRunner not initialized"}

    return runner.delete_shortcut(agent_name, command)


async def switch_model(
    agent_name: str,
    model: str,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Switch an agent's model at runtime (in-memory, resets on /reload).

    Args:
        agent_name: The agent to reconfigure (e.g., "nutritionist").
        model: The model name to switch to (e.g., "gemini-3.1-pro").
        api_key: Optional named API key from secret.yaml (e.g., "pro").

    Example: switch_model("nutritionist", "gemini-3.1-pro")
    """
    runtime = _current_runtime.get()
    if runtime is None:
        return {"status": "error", "message": "No runtime context available"}

    overrides: dict[str, Any] = {"model": model}
    if api_key:
        overrides["api_key"] = api_key

    effective = runtime.set_model_override(agent_name, **overrides)
    return {
        "status": "ok",
        "agent": agent_name,
        "model": effective.get("model"),
        "api_key": effective.get("api_key", "default"),
        "note": "This is an in-memory override. It resets on /reload or restart.",
    }


async def configure_api_key(
    name: str,
    action: str = "set",
    key: str | None = None,
) -> dict[str, Any]:
    """Dynamically add or remove an API key.

    Args:
        name: Name of the key (e.g., 'pro', 'claude').
        action: Either 'set' or 'remove'.
        key: The actual API key value (required if action='set').

    Example: configure_api_key("claude", "set", "sk-ant-...")
    """
    from mochi_agents.config import set_api_key, remove_api_key

    if action == "set":
        if not key:
            return {"status": "error", "message": "Key value is required when action='set'"}
        set_api_key(name, key)
        return {"status": "ok", "message": f"API key '{name}' saved and loaded successfully."}
    elif action == "remove":
        removed = remove_api_key(name)
        if removed:
            return {"status": "ok", "message": f"API key '{name}' removed."}
        else:
            return {"status": "error", "message": f"API key '{name}' not found."}
    else:
        return {"status": "error", "message": f"Invalid action: {action}"}


async def reconfigure_agent_model(
    agent_name: str,
    model: str | None = None,
    api_key: str | None = None,
    temperature: float | None = None,
) -> dict[str, Any]:
    """Permanently update an agent's model configuration.

    Args:
        agent_name: The agent to configure (e.g., "nutritionist").
        model: Optional new model name (e.g., "gemini-3.1-pro").
        api_key: Optional new named API key from secret.yaml (e.g., "pro").
        temperature: Optional numerical temperature (e.g., 0.5).

    Example: reconfigure_agent_model("nutritionist", model="gemini-3.1-pro", api_key="pro", temperature=0.7)
    """
    runtime = _current_runtime.get()
    if runtime is None:
        return {"status": "error", "message": "No runtime context available"}

    updates: dict[str, Any] = {}
    if model is not None:
        updates["model"] = model
    if api_key is not None:
        updates["api_key"] = api_key
    if temperature is not None:
        updates["temperature"] = float(temperature)

    if not updates:
        return {"status": "error", "message": "No fields to update provided."}

    try:
        mission = runtime.update_model_config(agent_name, updates)
        return {
            "status": "ok",
            "agent": agent_name,
            "new_config": mission.get("model_config", {}),
            "message": f"Successfully updated and saved model configuration for '{agent_name}'.",
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
