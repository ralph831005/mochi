"""SQLite connection manager — one database per agent."""

from __future__ import annotations

from pathlib import Path
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

# Cache engines by agent name to avoid re-creation
_engines: dict[str, AsyncEngine] = {}
_session_factories: dict[str, async_sessionmaker[AsyncSession]] = {}


def get_engine(agent_name: str, data_dir: Path) -> AsyncEngine:
    """Create or return a cached async SQLAlchemy engine for the given agent."""
    if agent_name not in _engines:
        db_path = data_dir / f"{agent_name}.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _engines[agent_name] = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}",
            echo=False,
        )
    return _engines[agent_name]


def get_session_factory(agent_name: str, data_dir: Path) -> async_sessionmaker[AsyncSession]:
    """Create or return a cached async session factory for the given agent."""
    if agent_name not in _session_factories:
        engine = get_engine(agent_name, data_dir)
        _session_factories[agent_name] = async_sessionmaker(engine, expire_on_commit=False)
    return _session_factories[agent_name]


async def get_session(agent_name: str, data_dir: Path) -> AsyncGenerator[AsyncSession, None]:
    """Async context manager yielding a session for the given agent."""
    factory = get_session_factory(agent_name, data_dir)
    async with factory() as session:
        yield session


async def init_db(agent_name: str, data_dir: Path) -> None:
    """Create all tables for the given agent's database."""
    from mochi_agents.memory.models import Base
    from sqlalchemy import text

    engine = get_engine(agent_name, data_dir)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # Migration: add source column if missing (for existing databases)
        result = await conn.execute(text("PRAGMA table_info(conversation_messages)"))
        columns = [row[1] for row in result]
        if "source" not in columns:
            await conn.execute(
                text("ALTER TABLE conversation_messages ADD COLUMN source VARCHAR(20) DEFAULT 'user'")
            )

        # Migration: expirable_items schema evolution
        # The old table had NOT NULL columns (is_recurring, recurrence_rule) that
        # the new model doesn't include. We need to recreate the table cleanly.
        result = await conn.execute(text("PRAGMA table_info(expirable_items)"))
        columns = [row[1] for row in result]
        if columns:  # table exists
            has_old_cols = "is_recurring" in columns
            has_new_cols = "category" in columns
            if has_old_cols or not has_new_cols:
                # Migrate: save data → drop → recreate → restore
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS _expirable_items_backup AS
                    SELECT id, user_id, title, description, value, expiration_date, status, created_at
                    FROM expirable_items
                """))
                await conn.execute(text("DROP TABLE expirable_items"))
                # create_all above will recreate with new schema on next call
                # But since we're inside begin(), we need to create it now
                await conn.execute(text("""
                    CREATE TABLE expirable_items (
                        id INTEGER NOT NULL PRIMARY KEY,
                        user_id VARCHAR(50) NOT NULL,
                        title VARCHAR(200) NOT NULL,
                        description TEXT,
                        value FLOAT,
                        category VARCHAR(50),
                        expiration_date DATETIME NOT NULL,
                        status VARCHAR(20) NOT NULL DEFAULT 'active',
                        created_at DATETIME NOT NULL DEFAULT (datetime('now'))
                    )
                """))
                # Restore any existing data
                await conn.execute(text("""
                    INSERT INTO expirable_items (id, user_id, title, description, value, expiration_date, status, created_at)
                    SELECT id, user_id, title, description, value, expiration_date, status, created_at
                    FROM _expirable_items_backup
                """))
                await conn.execute(text("DROP TABLE _expirable_items_backup"))


async def close_all() -> None:
    """Dispose all cached engines (for graceful shutdown)."""
    for engine in _engines.values():
        await engine.dispose()
    _engines.clear()
    _session_factories.clear()
