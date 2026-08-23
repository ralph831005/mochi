"""Tests for ExpirableItem CRUD tools and auto-expire logic."""

from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from mochi_agents.memory.models import ExpirableItem


# ---------------------------------------------------------------------------
# Helpers — lightweight in-memory mock for the DB session
# ---------------------------------------------------------------------------

class FakeSession:
    """In-memory session mock that supports basic add/execute/commit/delete."""

    def __init__(self):
        self._items: list[ExpirableItem] = []
        self._next_id = 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def add(self, item):
        item.id = self._next_id
        self._next_id += 1
        self._items.append(item)

    async def flush(self):
        pass

    async def commit(self):
        pass

    async def delete(self, item):
        self._items = [i for i in self._items if i.id != item.id]

    async def execute(self, stmt):
        """Minimal execute that returns items filtered to match simple where clauses."""
        return FakeResult(self._items)


class FakeResult:
    def __init__(self, items):
        self._items = items

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None

    def scalars(self):
        return self

    def all(self):
        return list(self._items)


def _make_factory(items=None):
    """Create a mock session factory pre-loaded with items."""
    session = FakeSession()
    if items:
        for item in items:
            session.add(item)

    def factory():
        return session

    return factory, session


def _make_item(
    title="Test Credit",
    value=15.0,
    category="credit",
    days_from_now=30,
    status="active",
    user_id="user1",
):
    """Create an ExpirableItem with defaults."""
    return ExpirableItem(
        user_id=user_id,
        title=title,
        value=value,
        category=category,
        expiration_date=datetime.now(timezone.utc) + timedelta(days=days_from_now),
        status=status,
    )


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------

class TestExpirableItemModel:
    def test_model_fields(self):
        """ExpirableItem should have expected fields."""
        item = ExpirableItem(
            user_id="u1",
            title="Uber Credit",
            description="Monthly ride credit",
            value=15.0,
            category="credit",
            expiration_date=datetime(2026, 5, 1, tzinfo=timezone.utc),
            status="active",
        )
        assert item.title == "Uber Credit"
        assert item.value == 15.0
        assert item.category == "credit"
        assert item.status == "active"
        assert item.description == "Monthly ride credit"

    def test_model_nullable_fields(self):
        """Optional fields should accept None."""
        item = ExpirableItem(
            user_id="u1",
            title="Test",
            expiration_date=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )
        assert item.description is None
        assert item.value is None
        assert item.category is None

    def test_model_no_recurring_fields(self):
        """ExpirableItem should NOT have is_recurring or recurrence_rule."""
        assert not hasattr(ExpirableItem, "is_recurring")
        assert not hasattr(ExpirableItem, "recurrence_rule")


# ---------------------------------------------------------------------------
# Tool function tests (mocked DB)
# ---------------------------------------------------------------------------

class TestAddExpirable:
    @pytest.mark.asyncio
    async def test_add_with_iso_date(self):
        from mochi_agents.agents.secretary.tools import add_expirable

        session = FakeSession()

        with patch("mochi_agents.config.get_settings") as mock_settings, \
             patch("mochi_agents.memory.database.get_session_factory") as mock_factory:

            mock_s = MagicMock()
            mock_s.resolve_path.return_value = "/tmp"
            mock_s.data_dir = "data"
            mock_s.get_tz.return_value = timezone.utc  # use UTC in tests
            mock_settings.return_value = mock_s
            mock_factory.return_value = lambda: session

            result = await add_expirable(
                title="Uber Credit",
                expiration_date="2026-05-01",
                value=15.0,
                category="credit",
                user_id="user1",
            )

            assert result["status"] == "ok"
            assert result["title"] == "Uber Credit"
            assert "id" in result

    @pytest.mark.asyncio
    async def test_add_invalid_date(self):
        from mochi_agents.agents.secretary.tools import add_expirable

        result = await add_expirable(
            title="Bad",
            expiration_date="not-a-date",
            user_id="user1",
        )
        assert result["status"] == "error"
        assert "Could not parse" in result["message"]


class TestAutoExpire:
    def test_expiration_date_past(self):
        """Items past their expiration date should be identifiable."""
        item = _make_item(days_from_now=-1, status="active")
        now = datetime.now(timezone.utc)
        assert item.expiration_date < now

    def test_expiration_date_future(self):
        """Active items with future dates should remain active."""
        item = _make_item(days_from_now=30, status="active")
        now = datetime.now(timezone.utc)
        assert item.expiration_date > now


class TestDaysLeftCalculation:
    def test_30_days(self):
        item = _make_item(days_from_now=30)
        now = datetime.now(timezone.utc)
        days_left = (item.expiration_date.replace(tzinfo=timezone.utc) - now).days
        assert 29 <= days_left <= 30

    def test_0_days(self):
        item = _make_item(days_from_now=0)
        now = datetime.now(timezone.utc)
        days_left = max(0, (item.expiration_date.replace(tzinfo=timezone.utc) - now).days)
        assert days_left == 0

    def test_negative_days_clamped(self):
        item = _make_item(days_from_now=-5)
        now = datetime.now(timezone.utc)
        days_left = max(0, (item.expiration_date.replace(tzinfo=timezone.utc) - now).days)
        assert days_left == 0


class TestCheckExpiringItems:
    @pytest.mark.asyncio
    async def test_no_items_returns_empty(self):
        from mochi_agents.agents.secretary.tools import check_expiring_items

        session = FakeSession()

        with patch("mochi_agents.config.get_settings") as ms, \
             patch("mochi_agents.memory.database.get_session_factory") as mf:
            ms.return_value = MagicMock(resolve_path=lambda _: "/tmp", data_dir="data")
            mf.return_value = lambda: session

            result = await check_expiring_items(user_id="user1")
            assert result == ""  # empty = nothing to report


class TestGetTools:
    def test_all_tools_registered(self):
        from mochi_agents.agents.secretary.tools import get_tools

        tools = get_tools()
        names = [t.__name__ for t in tools]

        # Location tools
        assert "set_zone" in names
        assert "set_zone_here" in names
        assert "list_zones" in names

        # Bookkeeping tools
        assert "add_expirable" in names
        assert "use_expirable" in names
        assert "list_expirables" in names
        assert "remove_expirable" in names
