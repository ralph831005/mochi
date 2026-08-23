"""Tests for the location tracker, geofencing, and Haversine distance."""

from __future__ import annotations

import math
import pytest

from mochi_agents.core.location import (
    GeofenceEvent,
    LocationTracker,
    Position,
    haversine,
)


# ---------------------------------------------------------------------------
# Haversine tests
# ---------------------------------------------------------------------------

class TestHaversine:
    """Test the Haversine distance formula."""

    def test_same_point(self):
        """Distance from a point to itself should be zero."""
        assert haversine(37.7749, -122.4194, 37.7749, -122.4194) == 0.0

    def test_known_distance_sf_to_la(self):
        """SF to LA is ~559 km."""
        dist = haversine(37.7749, -122.4194, 34.0522, -118.2437)
        assert 550_000 < dist < 570_000, f"SF→LA = {dist:.0f}m"

    def test_short_distance(self):
        """Two points ~100m apart."""
        # Move ~0.001 degrees lat ≈ 111m
        dist = haversine(37.0, -122.0, 37.001, -122.0)
        assert 100 < dist < 120, f"Short distance = {dist:.1f}m"

    def test_symmetry(self):
        """Distance is symmetric: d(A,B) == d(B,A)."""
        d1 = haversine(37.7749, -122.4194, 34.0522, -118.2437)
        d2 = haversine(34.0522, -118.2437, 37.7749, -122.4194)
        assert abs(d1 - d2) < 0.01

    def test_cross_equator(self):
        """Distance crossing the equator."""
        dist = haversine(1.0, 100.0, -1.0, 100.0)
        # ~222 km
        assert 220_000 < dist < 225_000


# ---------------------------------------------------------------------------
# LocationTracker — unit tests (in-memory, no DB)
# ---------------------------------------------------------------------------

class TestLocationTrackerInMemory:
    """Test position management without DB (geofence list returns empty)."""

    def test_no_initial_position(self):
        tracker = LocationTracker()
        assert tracker.get_position("user1") is None

    @pytest.mark.asyncio
    async def test_update_stores_position(self):
        tracker = LocationTracker()
        # Monkey-patch list_geofences to avoid DB
        async def _empty(uid): return []
        tracker.list_geofences = _empty
        events = await tracker.update_position("user1", 37.7749, -122.4194)
        assert events == []
        pos = tracker.get_position("user1")
        assert pos is not None
        assert pos.latitude == 37.7749
        assert pos.longitude == -122.4194

    @pytest.mark.asyncio
    async def test_update_overwrites_position(self):
        tracker = LocationTracker()
        async def _empty(uid): return []
        tracker.list_geofences = _empty
        await tracker.update_position("user1", 37.0, -122.0)
        await tracker.update_position("user1", 38.0, -121.0)
        pos = tracker.get_position("user1")
        assert pos.latitude == 38.0
        assert pos.longitude == -121.0


# ---------------------------------------------------------------------------
# Geofence transition detection
# ---------------------------------------------------------------------------

class TestGeofenceTransitions:
    """Test enter/exit detection with geofences (mocked DB)."""

    @pytest.fixture
    def tracker_with_zones(self):
        """Return a tracker with pre-loaded zone data (no DB)."""
        tracker = LocationTracker()

        zones = [
            {"id": 1, "name": "home", "latitude": 37.0, "longitude": -122.0, "radius_m": 150},
            {"id": 2, "name": "office", "latitude": 37.5, "longitude": -122.5, "radius_m": 150},
            {"id": 3, "name": "costco", "latitude": 38.0, "longitude": -123.0, "radius_m": 200},
        ]

        # Monkey-patch to avoid DB
        async def _list(uid):
            return zones
        tracker.list_geofences = _list

        return tracker

    @pytest.mark.asyncio
    async def test_first_position_no_events(self, tracker_with_zones):
        """First position should initialise state, not fire events."""
        # Position near home
        events = await tracker_with_zones.update_position("u1", 37.0001, -122.0001)
        assert events == []

    @pytest.mark.asyncio
    async def test_enter_zone(self, tracker_with_zones):
        """Moving from outside to inside a zone fires 'enter'."""
        tracker = tracker_with_zones

        # Start far from home (initialises as outside)
        await tracker.update_position("u1", 40.0, -120.0)

        # Move to home
        events = await tracker.update_position("u1", 37.0, -122.0)

        enter_events = [e for e in events if e.event == "enter"]
        assert any(e.zone_name == "home" for e in enter_events)

    @pytest.mark.asyncio
    async def test_exit_zone(self, tracker_with_zones):
        """Moving from inside to outside a zone fires 'exit'."""
        tracker = tracker_with_zones

        # Start at home (inside)
        await tracker.update_position("u1", 37.0, -122.0)

        # Move far away
        events = await tracker.update_position("u1", 40.0, -120.0)

        exit_events = [e for e in events if e.event == "exit"]
        assert any(e.zone_name == "home" for e in exit_events)

    @pytest.mark.asyncio
    async def test_no_event_while_staying(self, tracker_with_zones):
        """Staying inside a zone should not fire events."""
        tracker = tracker_with_zones

        # Start at home
        await tracker.update_position("u1", 37.0, -122.0)

        # Move slightly within home radius
        events = await tracker.update_position("u1", 37.0001, -122.0001)
        assert events == []

    @pytest.mark.asyncio
    async def test_hysteresis_prevents_flapping(self, tracker_with_zones):
        """Position near boundary should not cause rapid enter/exit."""
        tracker = tracker_with_zones

        # Start inside home
        await tracker.update_position("u1", 37.0, -122.0)

        # Move to just outside the radius (within hysteresis buffer)
        # 150m radius → need haversine > 150 + 15 to exit
        # ~0.0014 degrees lat ≈ 155m — inside hysteresis
        events = await tracker.update_position("u1", 37.0014, -122.0)
        assert not any(e.zone_name == "home" and e.event == "exit" for e in events)

    @pytest.mark.asyncio
    async def test_multiple_zones_independent(self, tracker_with_zones):
        """Entering one zone while outside another."""
        tracker = tracker_with_zones

        # Start far from everything
        await tracker.update_position("u1", 40.0, -120.0)

        # Move to Costco (should enter costco, stay outside home and office)
        events = await tracker.update_position("u1", 38.0, -123.0)

        zone_names = {e.zone_name for e in events}
        assert "costco" in zone_names
        assert "home" not in zone_names


# ---------------------------------------------------------------------------
# IncomingMessage location parsing
# ---------------------------------------------------------------------------

class TestIncomingMessageLocation:
    """Test location field on IncomingMessage."""

    def test_location_field_default_none(self):
        from mochi_agents.comm.base import IncomingMessage
        msg = IncomingMessage(user_id="1", chat_id="1", text="hello")
        assert msg.location is None

    def test_location_field_set(self):
        from mochi_agents.comm.base import IncomingMessage
        loc = {"latitude": 37.0, "longitude": -122.0, "live_period": 3600}
        msg = IncomingMessage(user_id="1", chat_id="1", location=loc)
        assert msg.location == loc
        assert msg.text is None


# ---------------------------------------------------------------------------
# Telegram _parse_message location
# ---------------------------------------------------------------------------

class TestTelegramLocationParsing:
    """Test Telegram client location message parsing."""

    def test_parse_location_message(self):
        from mochi_agents.comm.telegram import TelegramClient
        client = TelegramClient(token="test", allowed_user_ids=[])

        update = {
            "update_id": 1,
            "message": {
                "message_id": 123,
                "from": {"id": 555},
                "chat": {"id": 555},
                "date": 1700000000,
                "location": {
                    "latitude": 37.7749,
                    "longitude": -122.4194,
                    "live_period": 3600,
                },
            },
        }

        msg = client._parse_update(update)
        assert msg is not None
        assert msg.location is not None
        assert msg.location["latitude"] == 37.7749
        assert msg.location["longitude"] == -122.4194
        assert msg.location["live_period"] == 3600
        assert msg.text is None

    def test_parse_edited_message_location(self):
        from mochi_agents.comm.telegram import TelegramClient
        client = TelegramClient(token="test", allowed_user_ids=[])

        update = {
            "update_id": 2,
            "edited_message": {
                "message_id": 123,
                "from": {"id": 555},
                "chat": {"id": 555},
                "date": 1700000000,
                "edit_date": 1700000060,
                "location": {
                    "latitude": 37.7750,
                    "longitude": -122.4195,
                    "live_period": 3600,
                },
            },
        }

        msg = client._parse_update(update)
        assert msg is not None
        assert msg.location is not None
        assert msg.location["latitude"] == 37.7750
        assert msg.text is None

    def test_parse_text_message_no_location(self):
        from mochi_agents.comm.telegram import TelegramClient
        client = TelegramClient(token="test", allowed_user_ids=[])

        update = {
            "update_id": 3,
            "message": {
                "message_id": 456,
                "from": {"id": 555},
                "chat": {"id": 555},
                "date": 1700000000,
                "text": "hello",
            },
        }

        msg = client._parse_update(update)
        assert msg is not None
        assert msg.location is None
        assert msg.text == "hello"
