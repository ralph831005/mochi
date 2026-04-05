"""Nutritionist agent tools — meal CRUD, macro queries, scheduling.

Convention: this module exports `get_tools()` returning a list of callable tool functions.
The ImportlibToolRunner discovers and registers these automatically.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, func

from mochi_agents.config import get_settings
from mochi_agents.memory.database import get_session_factory
from mochi_agents.memory.models import MealLog, ScheduledJob


def get_tools() -> list:
    """Return domain-specific tool functions for the Nutritionist agent.

    Note: memory tools (save_memory, recall_memories, delete_memory)
    are auto-injected by the ToolRunner for ALL agents.
    """
    return [log_meal, get_today_summary, get_history, search_meals, schedule_job, cancel_job]


async def log_meal(
    user_id: str,
    meal_type: str,
    description: str,
    calories: float = 0,
    protein_g: float = 0,
    carbs_g: float = 0,
    fat_g: float = 0,
) -> dict[str, Any]:
    """Log a meal with estimated macros. Call this whenever the user mentions eating something."""
    settings = get_settings()
    data_dir = settings.resolve_path(settings.data_dir)
    factory = get_session_factory("nutritionist", data_dir)

    async with factory() as session:
        meal = MealLog(
            user_id=user_id,
            meal_type=meal_type.lower(),
            description=description,
            calories=calories,
            protein_g=protein_g,
            carbs_g=carbs_g,
            fat_g=fat_g,
        )
        session.add(meal)
        await session.commit()

    return {
        "status": "logged",
        "meal_type": meal_type,
        "description": description,
        "calories": calories,
        "protein_g": protein_g,
        "carbs_g": carbs_g,
        "fat_g": fat_g,
    }


async def get_today_summary(user_id: str) -> dict[str, Any]:
    """Get today's meal summary with total macros. Call when user asks about today's intake."""
    settings = get_settings()
    data_dir = settings.resolve_path(settings.data_dir)
    factory = get_session_factory("nutritionist", data_dir)

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    async with factory() as session:
        stmt = select(MealLog).where(
            MealLog.user_id == user_id,
            MealLog.logged_at >= today_start,
        ).order_by(MealLog.logged_at)

        result = await session.execute(stmt)
        meals = result.scalars().all()

    if not meals:
        return {"meals": [], "totals": {"calories": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0}}

    totals = {
        "calories": sum(m.calories for m in meals),
        "protein_g": sum(m.protein_g for m in meals),
        "carbs_g": sum(m.carbs_g for m in meals),
        "fat_g": sum(m.fat_g for m in meals),
    }

    meal_list = [
        {
            "meal_type": m.meal_type,
            "description": m.description,
            "calories": m.calories,
            "time": m.logged_at.strftime("%H:%M") if m.logged_at else "unknown",
        }
        for m in meals
    ]

    return {"meals": meal_list, "totals": totals}


async def get_history(user_id: str, days: int = 7) -> dict[str, Any]:
    """Get meal history for the past N days. Call when user asks about past meals."""
    settings = get_settings()
    data_dir = settings.resolve_path(settings.data_dir)
    factory = get_session_factory("nutritionist", data_dir)

    since = datetime.now(timezone.utc) - timedelta(days=days)

    async with factory() as session:
        stmt = select(MealLog).where(
            MealLog.user_id == user_id,
            MealLog.logged_at >= since,
        ).order_by(MealLog.logged_at.desc())

        result = await session.execute(stmt)
        meals = result.scalars().all()

    return {
        "days": days,
        "total_meals": len(meals),
        "meals": [
            {
                "date": m.logged_at.strftime("%Y-%m-%d") if m.logged_at else "unknown",
                "meal_type": m.meal_type,
                "description": m.description,
                "calories": m.calories,
                "protein_g": m.protein_g,
                "carbs_g": m.carbs_g,
                "fat_g": m.fat_g,
            }
            for m in meals
        ],
    }


async def search_meals(user_id: str, query: str) -> dict[str, Any]:
    """Search past meals by description. Call when user asks about a specific food."""
    settings = get_settings()
    data_dir = settings.resolve_path(settings.data_dir)
    factory = get_session_factory("nutritionist", data_dir)

    async with factory() as session:
        stmt = select(MealLog).where(
            MealLog.user_id == user_id,
            MealLog.description.ilike(f"%{query}%"),
        ).order_by(MealLog.logged_at.desc()).limit(20)

        result = await session.execute(stmt)
        meals = result.scalars().all()

    return {
        "query": query,
        "count": len(meals),
        "matches": [
            {
                "date": m.logged_at.strftime("%Y-%m-%d") if m.logged_at else "unknown",
                "meal_type": m.meal_type,
                "description": m.description,
                "calories": m.calories,
            }
            for m in meals
        ],
    }


async def schedule_job(
    user_id: str,
    run_at: str,
    action: str,
    payload: str = "",
) -> dict[str, Any]:
    """Schedule a one-off job (e.g., a reminder). run_at should be ISO 8601 format."""
    import json as _json

    settings = get_settings()
    data_dir = settings.resolve_path(settings.data_dir)
    factory = get_session_factory("nutritionist", data_dir)

    try:
        run_at_dt = datetime.fromisoformat(run_at)
    except ValueError:
        return {"status": "error", "message": f"Invalid datetime format: {run_at}"}

    try:
        payload_dict = _json.loads(payload) if payload else {}
    except _json.JSONDecodeError:
        payload_dict = {"text": payload}

    async with factory() as session:
        job = ScheduledJob(
            agent_name="nutritionist",
            run_at=run_at_dt,
            action=action,
            payload=payload_dict,
            target_user_id=user_id,
            status="pending",
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    return {"status": "scheduled", "job_id": job_id, "run_at": run_at}


async def cancel_job(job_id: int) -> dict[str, Any]:
    """Cancel a pending scheduled job by ID."""
    settings = get_settings()
    data_dir = settings.resolve_path(settings.data_dir)
    factory = get_session_factory("nutritionist", data_dir)

    async with factory() as session:
        stmt = select(ScheduledJob).where(
            ScheduledJob.id == job_id,
            ScheduledJob.status == "pending",
        )
        result = await session.execute(stmt)
        job = result.scalar_one_or_none()

        if not job:
            return {"status": "error", "message": f"No pending job with ID {job_id}"}

        job.status = "cancelled"
        await session.commit()

    return {"status": "cancelled", "job_id": job_id}
