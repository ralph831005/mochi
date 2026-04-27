"""Secretary agent tools — geofence and location-reminder management.

All tools are auto-discovered by the ToolRunner via the agent's
``tools_module`` setting in mission.yaml.
"""

from __future__ import annotations

from typing import Any

import logging
import urllib.request
import json as _json

logger = logging.getLogger(__name__)

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


async def geocode_address(address: str) -> dict[str, Any]:
    """Convert a street address or place name to latitude/longitude coordinates.

    Call this when the user mentions a location by address or place name
    instead of raw coordinates. Use the returned lat/lng with set_zone.

    Args:
        address: A street address or place name.
            Example: "1600 Amphitheatre Parkway, Mountain View, CA"
            Example: "Costco Sunnyvale"
    """
    try:
        encoded = urllib.request.quote(address)
        url = (
            f"https://nominatim.openstreetmap.org/search?"
            f"q={encoded}&format=json&limit=1&addressdetails=1"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "MochiAgent/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read().decode())

        if not data:
            return {
                "status": "not_found",
                "message": f"Could not find coordinates for '{address}'. Try a more specific address.",
            }

        result = data[0]
        lat = float(result["lat"])
        lon = float(result["lon"])
        display = result.get("display_name", address)

        logger.info(f"Geocoded '{address}' → ({lat}, {lon})")

        return {
            "status": "ok",
            "latitude": lat,
            "longitude": lon,
            "display_name": display,
            "message": f"Found: {display} ({lat:.5f}, {lon:.5f}). Use set_zone to create a geofence here.",
        }
    except Exception as e:
        logger.warning(f"Geocoding failed for '{address}': {e}")
        return {"status": "error", "message": f"Geocoding failed: {e}"}


async def set_zone_from_address(
    name: str,
    address: str,
    radius_m: float = 150.0,
    user_id: str = "",
) -> dict[str, Any]:
    """Create a geofence zone from a street address or place name.

    This combines geocoding and zone creation in one step.
    Prefer this over separate geocode_address + set_zone calls.

    Args:
        name: Human-friendly zone name (e.g., "costco", "dentist").
        address: Street address or place name to geocode.
            Example: "1600 Amphitheatre Parkway, Mountain View, CA"
        radius_m: Radius in metres (default 150).
        user_id: Owner user ID (auto-injected by the runtime).
    """
    geo_result = await geocode_address(address)

    if geo_result.get("status") != "ok":
        return geo_result

    lat = geo_result["latitude"]
    lon = geo_result["longitude"]
    display = geo_result["display_name"]

    tracker = get_tracker()
    zone_result = await tracker.add_geofence(user_id, name, lat, lon, radius_m)

    return {
        "status": "ok",
        "zone": name,
        "latitude": lat,
        "longitude": lon,
        "display_name": display,
        "message": f"Zone '{name}' created at {display} ({lat:.5f}, {lon:.5f}), radius {radius_m}m.",
    }


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
        # Location / geofence
        set_zone,
        set_zone_here,
        set_zone_from_address,
        geocode_address,
        remove_zone,
        list_zones,
        add_reminder,
        remove_reminder,
        list_reminders,
        get_current_location,
        # Bookkeeping / expirables
        add_expirable,
        use_expirable,
        list_expirables,
        remove_expirable,
    ]


# ---------------------------------------------------------------------------
# Expirable Items (Bookkeeping)
# ---------------------------------------------------------------------------

async def add_expirable(
    title: str,
    expiration_date: str,
    value: float = 0.0,
    description: str = "",
    category: str = "",
    user_id: str = "",
) -> dict[str, Any]:
    """Add a new item with an expiration date (credit, coupon, subscription, etc.).

    Args:
        title: Short title (e.g., "Uber Credit", "Costco Coupon").
        expiration_date: When it expires. Accepts flexible formats:
            "2026-05-01", "May 1 2026", "end of month", "2026-05-01T00:00:00Z".
        value: Monetary or point value (e.g., 15.0). Optional.
        description: Extra details or terms. Optional.
        category: Grouping label (e.g., "credit", "coupon", "subscription"). Optional.
        user_id: Owner user ID (auto-injected).

    Examples:
        add_expirable("Uber Credit", "2026-05-01", value=15.0, category="credit")
        add_expirable("Costco Coupon", "2026-06-30", value=5.0, category="coupon")
    """
    from datetime import datetime
    from mochi_agents.config import get_settings
    from mochi_agents.memory.database import get_session_factory
    from mochi_agents.memory.models import ExpirableItem

    settings = get_settings()
    local_tz = settings.get_tz()

    # Parse expiration date flexibly
    try:
        import dateutil.parser
        exp_dt = dateutil.parser.parse(expiration_date)
        # Ensure timezone-aware (default to local timezone, not UTC)
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=local_tz)
    except Exception as e:
        return {"status": "error", "message": f"Could not parse date '{expiration_date}': {e}"}

    # Reject dates in the past (compare in local time)
    now = datetime.now(local_tz)
    if exp_dt < now:
        return {
            "status": "error",
            "message": f"Expiration date {exp_dt.strftime('%Y-%m-%d')} is in the past. "
                       f"Today is {now.strftime('%Y-%m-%d')}. Please use a future date.",
        }

    data_dir = settings.resolve_path(settings.data_dir)
    factory = get_session_factory("secretary", data_dir)

    # Coerce value — LLM often sends strings like '25.0'
    numeric_value = None
    if value:
        try:
            numeric_value = float(value)
        except (ValueError, TypeError):
            numeric_value = None

    try:
        async with factory() as session:
            item = ExpirableItem(
                user_id=user_id,
                title=title,
                description=description or None,
                value=numeric_value,
                category=category.lower().strip() if category else None,
                expiration_date=exp_dt,
                status="active",
            )
            session.add(item)
            await session.commit()
            item_id = item.id
    except Exception as e:
        return {"status": "error", "message": f"Failed to save: {type(e).__name__}: {e}"}

    return {
        "status": "ok",
        "id": item_id,
        "title": title,
        "expiration_date": exp_dt.isoformat(),
    }


async def use_expirable(
    item_id: int,
    user_id: str = "",
) -> dict[str, Any]:
    """Mark an expirable item as used.

    Args:
        item_id: The numeric ID of the item.
        user_id: Owner user ID.

    Example: use_expirable(3)
    """
    from sqlalchemy import select
    from mochi_agents.config import get_settings
    from mochi_agents.memory.database import get_session_factory
    from mochi_agents.memory.models import ExpirableItem

    settings = get_settings()
    data_dir = settings.resolve_path(settings.data_dir)
    factory = get_session_factory("secretary", data_dir)

    async with factory() as session:
        stmt = select(ExpirableItem).where(
            ExpirableItem.id == item_id,
            ExpirableItem.user_id == user_id,
        )
        res = await session.execute(stmt)
        item = res.scalar_one_or_none()
        if not item:
            return {"status": "error", "message": f"Item #{item_id} not found."}

        item.status = "used"
        await session.commit()
        return {"status": "ok", "message": f"'{item.title}' marked as used."}


async def list_expirables(
    status: str = "",
    category: str = "",
    user_id: str = "",
) -> dict[str, Any]:
    """List expirable items, optionally filtered by status and/or category.

    Also auto-expires any active items past their expiration date.

    Args:
        status: Filter by status: "active", "used", "expired", or "" for all.
        category: Filter by category (e.g., "credit", "coupon"), or "" for all.
        user_id: Owner user ID.
    """
    from datetime import datetime, timezone
    from sqlalchemy import select
    from mochi_agents.config import get_settings
    from mochi_agents.memory.database import get_session_factory
    from mochi_agents.memory.models import ExpirableItem

    settings = get_settings()
    data_dir = settings.resolve_path(settings.data_dir)
    factory = get_session_factory("secretary", data_dir)

    now = datetime.now(timezone.utc)

    async with factory() as session:
        # Auto-expire active items that are past their date
        expire_stmt = (
            select(ExpirableItem)
            .where(
                ExpirableItem.user_id == user_id,
                ExpirableItem.status == "active",
                ExpirableItem.expiration_date <= now,
            )
        )
        expire_res = await session.execute(expire_stmt)
        for stale in expire_res.scalars().all():
            stale.status = "expired"
        await session.commit()

        # Build filtered query
        stmt = (
            select(ExpirableItem)
            .where(ExpirableItem.user_id == user_id)
            .order_by(ExpirableItem.expiration_date)
        )
        if status:
            stmt = stmt.where(ExpirableItem.status == status.lower().strip())
        if category:
            stmt = stmt.where(ExpirableItem.category == category.lower().strip())

        res = await session.execute(stmt)
        items = res.scalars().all()

    return {
        "count": len(items),
        "items": [
            {
                "id": i.id,
                "title": i.title,
                "description": i.description,
                "value": i.value,
                "category": i.category,
                "expiration_date": i.expiration_date.isoformat(),
                "status": i.status,
                "days_left": max(0, (i.expiration_date.replace(tzinfo=timezone.utc) - now).days)
                    if i.status == "active" else 0,
            }
            for i in items
        ],
    }


async def remove_expirable(
    item_id: int,
    user_id: str = "",
) -> dict[str, Any]:
    """Delete an expirable item permanently.

    Args:
        item_id: The numeric ID of the item to remove.
        user_id: Owner user ID.

    Example: remove_expirable(5)
    """
    from sqlalchemy import select
    from mochi_agents.config import get_settings
    from mochi_agents.memory.database import get_session_factory
    from mochi_agents.memory.models import ExpirableItem

    settings = get_settings()
    data_dir = settings.resolve_path(settings.data_dir)
    factory = get_session_factory("secretary", data_dir)

    async with factory() as session:
        stmt = select(ExpirableItem).where(
            ExpirableItem.id == item_id,
            ExpirableItem.user_id == user_id,
        )
        res = await session.execute(stmt)
        item = res.scalar_one_or_none()
        if not item:
            return {"status": "error", "message": f"Item #{item_id} not found."}

        await session.delete(item)
        await session.commit()
        return {"status": "ok", "message": f"'{item.title}' deleted."}


# ---------------------------------------------------------------------------
# Cron action: check_expiring_items (called by Scheduler)
# ---------------------------------------------------------------------------

async def check_expiring_items(
    user_id: str = "",
) -> str:
    """Check for items expiring within 3 days and return a reminder message.

    Called by the Scheduler cron job. Returns a formatted message for the user.
    """
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import select
    from mochi_agents.config import get_settings
    from mochi_agents.memory.database import get_session_factory
    from mochi_agents.memory.models import ExpirableItem

    settings = get_settings()
    data_dir = settings.resolve_path(settings.data_dir)
    factory = get_session_factory("secretary", data_dir)

    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=3)

    async with factory() as session:
        # Auto-expire first
        expire_stmt = (
            select(ExpirableItem)
            .where(
                ExpirableItem.user_id == user_id,
                ExpirableItem.status == "active",
                ExpirableItem.expiration_date <= now,
            )
        )
        expire_res = await session.execute(expire_stmt)
        for stale in expire_res.scalars().all():
            stale.status = "expired"
        await session.commit()

        # Find items expiring soon
        stmt = (
            select(ExpirableItem)
            .where(
                ExpirableItem.user_id == user_id,
                ExpirableItem.status == "active",
                ExpirableItem.expiration_date <= cutoff,
                ExpirableItem.expiration_date > now,
            )
            .order_by(ExpirableItem.expiration_date)
        )
        res = await session.execute(stmt)
        items = res.scalars().all()

    if not items:
        return ""  # empty = nothing to report, scheduler skips

    lines = ["⏰ **Expiring soon:**"]
    for item in items:
        days = (item.expiration_date.replace(tzinfo=timezone.utc) - now).days
        val = f" (${item.value:.2f})" if item.value else ""
        day_label = "today" if days == 0 else f"in {days} day{'s' if days != 1 else ''}"
        lines.append(f"• **{item.title}**{val} — expires {day_label}")

    return "\n".join(lines)

