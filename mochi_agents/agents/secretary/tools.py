"""Secretary agent tools — geofence and location-reminder management.

All tools are auto-discovered by the ToolRunner via the agent's
``tools_module`` setting in mission.yaml.
"""

from __future__ import annotations

from typing import Any

from mochi_agents.core.location import get_tracker
from mochi_agents.core.shared_tools import _current_agent


# ---------------------------------------------------------------------------
# Helper to get user_id from shared-tools context
# ---------------------------------------------------------------------------

def _user_id() -> str:
    """Best-effort user_id from the agent context.

    For tools that need user_id, callers (the LLM) should pass it
    explicitly. When not provided, we use a sensible default.
    """
    # The ToolRunner injects user_id for context_tools automatically.
    # For LLM-called tools, we accept it as an optional parameter.
    return ""


# ---------------------------------------------------------------------------
# Zone management
# ---------------------------------------------------------------------------

async def set_zone(
    name: str,
    latitude: float,
    longitude: float,
    radius_m: float = 150.0,
    user_id: str = "",
) -> dict[str, Any]:
    """Register or update a named geofence zone at specific coordinates.

    Args:
        name: Human-friendly zone name (e.g., "home", "office", "costco").
        latitude: Center latitude of the zone.
        longitude: Center longitude of the zone.
        radius_m: Radius in metres (default 150).
        user_id: Owner user ID (auto-injected by the runtime).

    Example: set_zone("costco", 37.4275, -122.1697, 200)
    """
    tracker = get_tracker()
    return await tracker.add_geofence(user_id, name, latitude, longitude, radius_m)


async def set_zone_here(
    name: str,
    radius_m: float = 150.0,
    user_id: str = "",
) -> dict[str, Any]:
    """Register a zone at the user's current (last known) location.

    The user must have shared their live location for this to work.

    Args:
        name: Human-friendly zone name (e.g., "home", "office").
        radius_m: Radius in metres (default 150).
        user_id: Owner user ID (auto-injected by the runtime).

    Example: set_zone_here("home")
    """
    tracker = get_tracker()
    pos = tracker.get_position(user_id)
    if pos is None:
        return {
            "status": "error",
            "message": "No location available. Please share your live location first "
                       "(tap 📎 → Location → Share Live Location in Telegram).",
        }
    return await tracker.add_geofence(user_id, name, pos.latitude, pos.longitude, radius_m)


async def remove_zone(
    name: str,
    user_id: str = "",
) -> dict[str, Any]:
    """Delete a geofence zone by name.

    Args:
        name: Zone name to remove.
        user_id: Owner user ID.

    Example: remove_zone("gym")
    """
    tracker = get_tracker()
    removed = await tracker.remove_geofence(user_id, name)
    if removed:
        return {"status": "ok", "message": f"Zone '{name}' removed."}
    return {"status": "error", "message": f"Zone '{name}' not found."}


async def list_zones(
    user_id: str = "",
) -> dict[str, Any]:
    """List all registered geofence zones for the user.

    Returns zone names, coordinates, and radii.
    """
    tracker = get_tracker()
    zones = await tracker.list_geofences(user_id)
    return {"count": len(zones), "zones": zones}


# ---------------------------------------------------------------------------
# Reminder management
# ---------------------------------------------------------------------------

async def add_reminder(
    zone_name: str,
    trigger: str,
    message: str,
    user_id: str = "",
) -> dict[str, Any]:
    """Add a one-shot location reminder for a geofence zone.

    The reminder fires once when the trigger condition is met, then auto-deactivates.

    Args:
        zone_name: Name of the geofence zone (must exist).
        trigger: Either "enter" (arriving) or "exit" (leaving).
        message: The reminder text to send when triggered.
        user_id: Owner user ID.

    Examples:
        add_reminder("costco", "enter", "Buy eggs, milk, and paper towels")
        add_reminder("office", "exit", "Stop by Costco before heading home")
    """
    tracker = get_tracker()

    # Verify zone exists
    zones = await tracker.list_geofences(user_id)
    zone_names = [z["name"] for z in zones]
    if zone_name.lower().strip() not in zone_names:
        return {
            "status": "error",
            "message": f"Zone '{zone_name}' not found. Set it up first with set_zone().",
            "available_zones": zone_names,
        }

    return await tracker.add_reminder(user_id, zone_name, trigger, message)


async def remove_reminder(
    reminder_id: int,
    user_id: str = "",
) -> dict[str, Any]:
    """Delete a reminder by its ID.

    Args:
        reminder_id: The numeric ID of the reminder to remove.
        user_id: Owner user ID.

    Example: remove_reminder(3)
    """
    tracker = get_tracker()
    removed = await tracker.remove_reminder(user_id, reminder_id)
    if removed:
        return {"status": "ok", "message": f"Reminder #{reminder_id} deleted."}
    return {"status": "error", "message": f"Reminder #{reminder_id} not found."}


async def list_reminders(
    zone_name: str = "",
    user_id: str = "",
) -> dict[str, Any]:
    """List active location reminders, optionally filtered by zone name.

    Args:
        zone_name: If provided, only show reminders for this zone.
        user_id: Owner user ID.
    """
    tracker = get_tracker()
    reminders = await tracker.list_reminders(user_id, zone_name or None)
    return {"count": len(reminders), "reminders": reminders}


async def get_current_location(
    user_id: str = "",
) -> dict[str, Any]:
    """Return the user's last known GPS position and when it was received.

    Returns latitude, longitude, and a human-readable timestamp.
    """
    import time as _time
    from datetime import datetime, timezone

    tracker = get_tracker()
    pos = tracker.get_position(user_id)
    if pos is None:
        return {
            "status": "unknown",
            "message": "No location data. Share your live location in Telegram "
                       "(tap 📎 → Location → Share Live Location).",
        }

    age_sec = _time.time() - pos.timestamp
    ts = datetime.fromtimestamp(pos.timestamp, tz=timezone.utc)

    return {
        "latitude": pos.latitude,
        "longitude": pos.longitude,
        "timestamp": ts.isoformat(),
        "age_seconds": round(age_sec, 1),
    }


# ---------------------------------------------------------------------------
# Tool registry (discovered by ImportlibToolRunner)
# ---------------------------------------------------------------------------

def get_tools() -> list:
    """Return all secretary tools."""
    return [
        set_zone,
        set_zone_here,
        remove_zone,
        list_zones,
        add_reminder,
        remove_reminder,
        list_reminders,
        get_current_location,
    ]

# ---------------------------------------------------------------------------
# Expirable Items (Bookkeeping)
# ---------------------------------------------------------------------------

async def add_expirable(
    title: str,
    expiration_date: str,
    description: str = "",
    value: float = 0.0,
    is_recurring: bool = False,
    recurrence_rule: str = "",
    user_id: str = "",
) -> dict[str, Any]:
    """Add a new item with an expiration date, like a credit or note.

    Args:
        title: Short title for the item.
        expiration_date: ISO8601 string (e.g., '2023-12-31T23:59:00Z').
        description: Detailed info or terms.
        value: Monetary or point value (e.g., 15.0).
        is_recurring: True if this item renews (e.g., monthly credit).
        recurrence_rule: Describe the frequency, e.g. 'monthly'.
        user_id: Owner user ID.

    Example: add_expirable("Uber Credit", "2026-05-01T00:00:00Z", value=15.0, is_recurring=True, recurrence_rule="monthly")
    """
    from datetime import datetime
    import dateutil.parser
    from mochi_agents.config import get_settings
    from mochi_agents.memory.database import get_session_factory
    from mochi_agents.memory.models import ExpirableItem
    
    settings = get_settings()
    data_dir = settings.resolve_path(settings.data_dir)
    factory = get_session_factory("secretary", data_dir)
    
    try:
        exp_dt = dateutil.parser.isoparse(expiration_date)
    except Exception as e:
        return {"status": "error", "message": f"Invalid date format: {e}"}

    async with factory() as session:
        item = ExpirableItem(
            user_id=user_id,
            title=title,
            description=description,
            value=value,
            expiration_date=exp_dt,
            is_recurring=is_recurring,
            recurrence_rule=recurrence_rule
        )
        session.add(item)
        await session.commit()
        return {"status": "ok", "id": item.id}


async def remove_expirable(
    item_id: int,
    user_id: str = "",
) -> dict[str, Any]:
    """Delete an expirable item.
    """
    from sqlalchemy import select
    from mochi_agents.config import get_settings
    from mochi_agents.memory.database import get_session_factory
    from mochi_agents.memory.models import ExpirableItem
    
    settings = get_settings()
    data_dir = settings.resolve_path(settings.data_dir)
    factory = get_session_factory("secretary", data_dir)
    
    async with factory() as session:
        stmt = select(ExpirableItem).where(ExpirableItem.id == item_id, ExpirableItem.user_id == user_id)
        res = await session.execute(stmt)
        item = res.scalar_one_or_none()
        if not item:
            return {"status": "error", "message": f"Item {item_id} not found."}
        
        await session.delete(item)
        await session.commit()
        return {"status": "ok", "message": "Item deleted."}


async def use_expirable(
    item_id: int,
    user_id: str = "",
) -> dict[str, Any]:
    """Mark an expirable item as used.

    If it's recurring, the agent can manually create the next one using add_expirable, 
    or the user can ask to create it.
    """
    from sqlalchemy import select
    from mochi_agents.config import get_settings
    from mochi_agents.memory.database import get_session_factory
    from mochi_agents.memory.models import ExpirableItem
    
    settings = get_settings()
    data_dir = settings.resolve_path(settings.data_dir)
    factory = get_session_factory("secretary", data_dir)
    
    async with factory() as session:
        stmt = select(ExpirableItem).where(ExpirableItem.id == item_id, ExpirableItem.user_id == user_id)
        res = await session.execute(stmt)
        item = res.scalar_one_or_none()
        if not item:
            return {"status": "error", "message": f"Item {item_id} not found."}
        
        item.status = "used"
        await session.commit()
        return {"status": "ok", "message": f"Item '{item.title}' marked as used."}


async def list_expirables(
    status: str = "active",
    user_id: str = "",
) -> dict[str, Any]:
    """List expirable items for the user. Status can be 'active', 'used', or 'expired'."""
    from sqlalchemy import select
    from mochi_agents.config import get_settings
    from mochi_agents.memory.database import get_session_factory
    from mochi_agents.memory.models import ExpirableItem
    
    settings = get_settings()
    data_dir = settings.resolve_path(settings.data_dir)
    factory = get_session_factory("secretary", data_dir)
    
    async with factory() as session:
        stmt = select(ExpirableItem).where(ExpirableItem.user_id == user_id, ExpirableItem.status == status).order_by(ExpirableItem.expiration_date)
        res = await session.execute(stmt)
        items = res.scalars().all()
        
        return {
            "count": len(items),
            "items": [
                {
                    "id": i.id,
                    "title": i.title,
                    "value": i.value,
                    "expiration_date": i.expiration_date.isoformat(),
                    "is_recurring": i.is_recurring,
                    "recurrence_rule": i.recurrence_rule,
                    "status": i.status
                } for i in items
            ]
        }


async def check_expiring_soon(
    user_id: str = "",
) -> dict[str, Any]:
    """Check for items expiring in the next 7 days and not used.
    Designed to be run as a cron job to send an alert.
    """
    from sqlalchemy import select
    from datetime import datetime, timezone, timedelta
    from mochi_agents.config import get_settings
    from mochi_agents.memory.database import get_session_factory
    from mochi_agents.memory.models import ExpirableItem
    
    settings = get_settings()
    data_dir = settings.resolve_path(settings.data_dir)
    factory = get_session_factory("secretary", data_dir)
    
    now = datetime.now(timezone.utc)
    next_week = now + timedelta(days=7)
    
    async with factory() as session:
        stmt = select(ExpirableItem).where(
            ExpirableItem.user_id == user_id,
            ExpirableItem.status == "active",
            ExpirableItem.expiration_date <= next_week
        ).order_by(ExpirableItem.expiration_date)
        res = await session.execute(stmt)
        items = res.scalars().all()
        
        if not items:
            return {"status": "ok", "message": "Nothing expiring in the next 7 days."}
            
        return {
            "status": "alert",
            "message": "You have items expiring soon!",
            "items": [
                {
                    "id": i.id,
                    "title": i.title,
                    "value": i.value,
                    "expiration_date": i.expiration_date.isoformat()
                } for i in items
            ]
        }
