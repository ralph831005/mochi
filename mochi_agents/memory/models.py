"""SQLAlchemy models for agent memory.

All agents share the same Base, so tables are created per-agent database.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Shared declarative base for all agent models."""
    pass


# ---------------------------------------------------------------------------
# Shared models (available in all agent databases)
# ---------------------------------------------------------------------------

class ConversationMessage(Base):
    """Stores conversation history for memory injection."""

    __tablename__ = "conversation_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    role: Mapped[str] = mapped_column(String(20))  # user, assistant, system
    content: Mapped[str] = mapped_column(Text)
    archived: Mapped[bool] = mapped_column(Integer, default=False)  # SQLite stores as 0/1
    is_summary: Mapped[bool] = mapped_column(Integer, default=False)  # rolling summary flag
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class ScheduledJob(Base):
    """Persists one-off scheduled jobs so they survive restarts."""

    __tablename__ = "scheduled_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_name: Mapped[str] = mapped_column(String(100))
    run_at: Mapped[datetime] = mapped_column(DateTime)
    action: Mapped[str] = mapped_column(String(200))
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    target_user_id: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending, completed, cancelled
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class MemoryNote(Base):
    """Persistent memory notes: profile facts, goals, preferences.

    Unlike conversation messages (which get archived/summarized),
    these are always injected into the system instruction.

    Categories: profile, goal, preference
    """

    __tablename__ = "memory_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    category: Mapped[str] = mapped_column(String(50))  # profile, goal, preference
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Nutritionist-specific models
# ---------------------------------------------------------------------------

class MealLog(Base):
    """Tracks individual meal entries with macro estimates."""

    __tablename__ = "meal_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(50))
    meal_type: Mapped[str] = mapped_column(String(20))  # breakfast, lunch, dinner, snack
    description: Mapped[str] = mapped_column(Text)
    calories: Mapped[float] = mapped_column(Float, default=0)
    protein_g: Mapped[float] = mapped_column(Float, default=0)
    carbs_g: Mapped[float] = mapped_column(Float, default=0)
    fat_g: Mapped[float] = mapped_column(Float, default=0)
    logged_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
