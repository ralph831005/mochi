"""Shared tools — available to ALL agents automatically.

These are system-level tools injected by the ToolRunner regardless of
what's in an agent's tools_module. Domain-specific tools stay in each
agent's own tools.py.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from mochi_agents.config import get_settings
from mochi_agents.memory.database import get_session_factory
from mochi_agents.memory.models import MemoryNote


# This is set by the ToolRunner before execution so tools know which agent is calling
_current_agent: str = "default"


def set_current_agent(agent_name: str) -> None:
    """Set the agent context for shared tools."""
    global _current_agent
    _current_agent = agent_name


def get_tools() -> list:
    """Return shared tool functions available to all agents."""
    return [save_memory, recall_memories, delete_memory]


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
    factory = get_session_factory(_current_agent, data_dir)

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
    factory = get_session_factory(_current_agent, data_dir)

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
    factory = get_session_factory(_current_agent, data_dir)

    async with factory() as session:
        stmt = select(MemoryNote).where(MemoryNote.id == note_id)
        result = await session.execute(stmt)
        note = result.scalar_one_or_none()

        if not note:
            return {"status": "error", "message": f"No memory note with ID {note_id}"}

        await session.delete(note)
        await session.commit()

    return {"status": "deleted", "id": note_id}
