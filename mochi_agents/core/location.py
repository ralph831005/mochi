"""Location Tracker — geofencing engine with Haversine distance.

Maintains per-user position state, evaluates geofences, and emits
enter/exit transition events. Designed as a singleton accessed via
``get_tracker()``.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, update as sa_update

from mochi_agents.config import get_settings
from mochi_agents.memory.database import get_session_factory
from mochi_agents.memory.models import Geofence, LocationReminder

logger = logging.getLogger(__name__)

# Earth's mean radius in metres
_EARTH_RADIUS_M = 6_371_000

# Hysteresis buffer (metres) to prevent flapping at zone boundaries.
# A user must move this far *past* the boundary to trigger the opposite event.
_HYSTERESIS_M = 15.0


# ---------------------------------------------------------------------------
# Haversine
# ---------------------------------------------------------------------------

def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance in metres between two points."""
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    Δφ = math.radians(lat2 - lat1)
    Δλ = math.radians(lon2 - lon1)

    a = math.sin(Δφ / 2) ** 2 + math.cos(φ1) * math.cos(φ2) * math.sin(Δλ / 2) ** 2
    return _EARTH_RADIUS_M * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Position:
    """Last known position for a user."""
    latitude: float
    longitude: float
    timestamp: float  # unix epoch


@dataclass
class GeofenceEvent:
    """Transition event emitted when a user enters/exits a zone."""
    zone_name: str
    event: str  # "enter" or "exit"
    latitude: float
    longitude: float


# ---------------------------------------------------------------------------
# LocationTracker
# ---------------------------------------------------------------------------

class LocationTracker:
    """Core geofencing engine.

    Per-user state is held in memory; geofence definitions live in the
    secretary agent's SQLite database.
    """

    def __init__(self) -> None:
        # user_id → Position
        self._positions: dict[str, Position] = {}
        # user_id → {zone_name: is_inside}
        self._zone_states: dict[str, dict[str, bool]] = {}

    # -- Position management -------------------------------------------------

    def get_position(self, user_id: str) -> Position | None:
        return self._positions.get(user_id)

    async def update_position(
        self, user_id: str, lat: float, lon: float,
    ) -> list[GeofenceEvent]:
        """Update a user's position and return any geofence transition events."""
        self._positions[user_id] = Position(
            latitude=lat, longitude=lon, timestamp=time.time(),
        )

        events = await self._check_geofences(user_id, lat, lon)
        if events:
            logger.info(
                f"Geofence events for user {user_id}: "
                + ", ".join(f"{e.zone_name}:{e.event}" for e in events)
            )
        return events

    # -- Geofence CRUD (DB-backed) -------------------------------------------

    async def add_geofence(
        self,
        user_id: str,
        name: str,
        lat: float,
        lon: float,
        radius_m: float = 150.0,
    ) -> dict[str, Any]:
        """Create or update a named geofence zone for a user."""
        settings = get_settings()
        data_dir = settings.resolve_path(settings.data_dir)
        factory = get_session_factory("secretary", data_dir)

        name_lower = name.lower().strip()

        async with factory() as session:
            # Upsert: check if zone already exists
            stmt = select(Geofence).where(
                Geofence.user_id == user_id,
                Geofence.name == name_lower,
            )
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()

            if existing:
                existing.latitude = lat
                existing.longitude = lon
                existing.radius_m = radius_m
                zone_id = existing.id
            else:
                zone = Geofence(
                    user_id=user_id,
                    name=name_lower,
                    latitude=lat,
                    longitude=lon,
                    radius_m=radius_m,
                )
                session.add(zone)
                await session.flush()
                zone_id = zone.id

            await session.commit()

        # Initialise zone state for this user based on current position
        if user_id in self._positions:
            pos = self._positions[user_id]
            dist = haversine(pos.latitude, pos.longitude, lat, lon)
            self._zone_states.setdefault(user_id, {})[name_lower] = dist <= radius_m

        logger.info(f"Geofence '{name_lower}' set for user {user_id} at ({lat}, {lon}) r={radius_m}m")
        return {"status": "ok", "id": zone_id, "name": name_lower}

    async def remove_geofence(self, user_id: str, name: str) -> bool:
        """Remove a geofence zone. Returns True if deleted."""
        settings = get_settings()
        data_dir = settings.resolve_path(settings.data_dir)
        factory = get_session_factory("secretary", data_dir)

        name_lower = name.lower().strip()

        async with factory() as session:
            stmt = select(Geofence).where(
                Geofence.user_id == user_id,
                Geofence.name == name_lower,
            )
            result = await session.execute(stmt)
            zone = result.scalar_one_or_none()

            if not zone:
                return False

            await session.delete(zone)
            await session.commit()

        # Clean up in-memory state
        if user_id in self._zone_states:
            self._zone_states[user_id].pop(name_lower, None)

        return True

    async def list_geofences(self, user_id: str) -> list[dict[str, Any]]:
        """List all geofences for a user."""
        settings = get_settings()
        data_dir = settings.resolve_path(settings.data_dir)
        factory = get_session_factory("secretary", data_dir)

        async with factory() as session:
            stmt = (
                select(Geofence)
                .where(Geofence.user_id == user_id)
                .order_by(Geofence.name)
            )
            result = await session.execute(stmt)
            zones = result.scalars().all()

        return [
            {
                "id": z.id,
                "name": z.name,
                "latitude": z.latitude,
                "longitude": z.longitude,
                "radius_m": z.radius_m,
            }
            for z in zones
        ]

    # -- Reminder CRUD -------------------------------------------------------

    async def add_reminder(
        self,
        user_id: str,
        zone_name: str,
        trigger: str,
        message: str,
    ) -> dict[str, Any]:
        """Add a one-shot location reminder."""
        settings = get_settings()
        data_dir = settings.resolve_path(settings.data_dir)
        factory = get_session_factory("secretary", data_dir)

        zone_name_lower = zone_name.lower().strip()
        trigger_lower = trigger.lower().strip()

        if trigger_lower not in ("enter", "exit"):
            return {"status": "error", "message": "Trigger must be 'enter' or 'exit'"}

        async with factory() as session:
            reminder = LocationReminder(
                user_id=user_id,
                zone_name=zone_name_lower,
                trigger=trigger_lower,
                message=message,
                status="active",
            )
            session.add(reminder)
            await session.commit()
            reminder_id = reminder.id

        return {"status": "ok", "id": reminder_id}

    async def remove_reminder(self, user_id: str, reminder_id: int) -> bool:
        """Delete a reminder by ID. Returns True if deleted."""
        settings = get_settings()
        data_dir = settings.resolve_path(settings.data_dir)
        factory = get_session_factory("secretary", data_dir)

        async with factory() as session:
            stmt = select(LocationReminder).where(
                LocationReminder.id == reminder_id,
                LocationReminder.user_id == user_id,
            )
            result = await session.execute(stmt)
            reminder = result.scalar_one_or_none()

            if not reminder:
                return False

            await session.delete(reminder)
            await session.commit()

        return True

    async def list_reminders(
        self, user_id: str, zone_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """List active reminders, optionally filtered by zone."""
        settings = get_settings()
        data_dir = settings.resolve_path(settings.data_dir)
        factory = get_session_factory("secretary", data_dir)

        async with factory() as session:
            stmt = (
                select(LocationReminder)
                .where(
                    LocationReminder.user_id == user_id,
                    LocationReminder.status == "active",
                )
                .order_by(LocationReminder.zone_name, LocationReminder.id)
            )
            if zone_name:
                stmt = stmt.where(LocationReminder.zone_name == zone_name.lower().strip())

            result = await session.execute(stmt)
            reminders = result.scalars().all()

        return [
            {
                "id": r.id,
                "zone_name": r.zone_name,
                "trigger": r.trigger,
                "message": r.message,
            }
            for r in reminders
        ]

    async def get_triggered_reminders(
        self, user_id: str, zone_name: str, trigger: str,
    ) -> list[dict[str, Any]]:
        """Find active reminders matching the zone + trigger, and mark them as fired."""
        settings = get_settings()
        data_dir = settings.resolve_path(settings.data_dir)
        factory = get_session_factory("secretary", data_dir)

        zone_lower = zone_name.lower().strip()

        async with factory() as session:
            stmt = select(LocationReminder).where(
                LocationReminder.user_id == user_id,
                LocationReminder.zone_name == zone_lower,
                LocationReminder.trigger == trigger,
                LocationReminder.status == "active",
            )
            result = await session.execute(stmt)
            reminders = result.scalars().all()

            fired = []
            for r in reminders:
                fired.append({
                    "id": r.id,
                    "zone_name": r.zone_name,
                    "trigger": r.trigger,
                    "message": r.message,
                })
                r.status = "fired"

            await session.commit()

        return fired

    # -- Geofence evaluation -------------------------------------------------

    async def _check_geofences(
        self, user_id: str, lat: float, lon: float,
    ) -> list[GeofenceEvent]:
        """Evaluate all geofences for a user and return transition events."""
        geofences = await self.list_geofences(user_id)
        if not geofences:
            return []

        user_states = self._zone_states.setdefault(user_id, {})
        events: list[GeofenceEvent] = []

        for gf in geofences:
            name = gf["name"]
            dist = haversine(lat, lon, gf["latitude"], gf["longitude"])
            radius = gf["radius_m"]
            was_inside = user_states.get(name)

            # First time seeing this zone — initialise without firing event
            if was_inside is None:
                user_states[name] = dist <= radius
                continue

            # Apply hysteresis to prevent flapping
            if was_inside and dist > radius + _HYSTERESIS_M:
                # Exited zone
                user_states[name] = False
                events.append(GeofenceEvent(
                    zone_name=name, event="exit", latitude=lat, longitude=lon,
                ))
            elif not was_inside and dist < radius - _HYSTERESIS_M:
                # Entered zone
                user_states[name] = True
                events.append(GeofenceEvent(
                    zone_name=name, event="enter", latitude=lat, longitude=lon,
                ))

        return events


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_tracker: LocationTracker | None = None


def get_tracker() -> LocationTracker:
    """Return the global LocationTracker singleton, creating it if needed."""
    global _tracker
    if _tracker is None:
        _tracker = LocationTracker()
    return _tracker

