"""Scheduler — runs recurring cron jobs and one-off scheduled tasks.

Runs as a parallel asyncio task alongside the polling loop.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from croniter import croniter

from mochi_agents.config import get_settings
from mochi_agents.memory.database import get_session_factory
from mochi_agents.memory.models import ScheduledJob

if TYPE_CHECKING:
    from mochi_agents.comm.base import CommunicationClient
    from mochi_agents.core.agent_runtime import AgentRuntime

logger = logging.getLogger(__name__)


class Scheduler:
    """Handles recurring cron jobs and one-off scheduled tasks."""

    def __init__(
        self,
        runtime: AgentRuntime,
        client: CommunicationClient,
    ) -> None:
        self.runtime = runtime
        self.client = client
        self._cron_jobs: list[dict[str, Any]] = []
        self._last_cron_check: dict[str, datetime] = {}
        self._tick_interval = 30  # seconds

    def load_cron_jobs(self) -> None:
        """Scan all agent mission files for `schedules` blocks."""
        settings = get_settings()
        agents_dir = settings.resolve_path(settings.agents_dir)
        self._cron_jobs.clear()

        if not agents_dir.exists():
            return

        for mission_path in agents_dir.glob("*/mission.yaml"):
            agent_name = mission_path.parent.name
            with open(mission_path) as f:
                mission = yaml.safe_load(f) or {}

            for schedule in mission.get("schedules", []):
                self._cron_jobs.append({
                    "agent_name": agent_name,
                    "name": schedule.get("name", "unnamed"),
                    "cron": schedule["cron"],
                    "action": schedule.get("action", ""),
                    "target_user_ids": schedule.get("target_user_ids", "all"),
                })
                logger.info(f"Registered cron job: {schedule.get('name')} ({schedule['cron']}) for {agent_name}")

    async def run(self) -> None:
        """Main scheduler loop — checks cron jobs and one-off jobs every tick."""
        logger.info(f"Scheduler started ({len(self._cron_jobs)} cron jobs registered)")

        # Fire any past-due one-off jobs immediately
        await self._fire_past_due_jobs()

        while True:
            try:
                await self._check_cron_jobs()
                await self._check_oneoff_jobs()
            except Exception as e:
                logger.error(f"Scheduler tick error: {e}", exc_info=True)

            await asyncio.sleep(self._tick_interval)

    async def _check_cron_jobs(self) -> None:
        """Check if any cron jobs should fire now."""
        now = datetime.now(timezone.utc)

        for job in self._cron_jobs:
            key = f"{job['agent_name']}:{job['name']}"
            last_check = self._last_cron_check.get(key, now - __import__('datetime').timedelta(seconds=self._tick_interval + 1))

            cron = croniter(job["cron"], last_check)
            next_fire = cron.get_next(datetime).replace(tzinfo=timezone.utc)

            if next_fire <= now:
                logger.info(f"Cron job firing: {job['name']} for agent {job['agent_name']}")
                self._last_cron_check[key] = now

                try:
                    user_id = self._resolve_target_user_id(job["target_user_ids"])
                    response = await self.runtime.execute(
                        job["agent_name"],
                        f"[SCHEDULED] Execute action: {job['action']}",
                        user_id=user_id,
                        source="scheduler",
                    )
                    await self._send_to_targets(job["target_user_ids"], f"📋 {response}")
                except Exception as e:
                    logger.error(f"Cron job failed: {job['name']}: {e}", exc_info=True)

    async def _check_oneoff_jobs(self) -> None:
        """Check for pending one-off jobs that are due."""
        settings = get_settings()
        data_dir = settings.resolve_path(settings.data_dir)
        now = datetime.now(timezone.utc)

        # Check all agent databases for pending jobs
        agents_dir = settings.resolve_path(settings.agents_dir)
        if not agents_dir.exists():
            return

        for mission_path in agents_dir.glob("*/mission.yaml"):
            agent_name = mission_path.parent.name
            await self._process_agent_jobs(agent_name, data_dir, now)

    async def _process_agent_jobs(self, agent_name: str, data_dir: Path, now: datetime) -> None:
        """Process pending one-off jobs for a specific agent."""
        from sqlalchemy import select

        factory = get_session_factory(agent_name, data_dir)

        async with factory() as session:
            stmt = select(ScheduledJob).where(
                ScheduledJob.status == "pending",
                ScheduledJob.run_at <= now,
            )
            result = await session.execute(stmt)
            due_jobs = result.scalars().all()

            for job in due_jobs:
                logger.info(f"One-off job firing: {job.action} (ID: {job.id})")

                try:
                    payload_text = ""
                    if job.payload:
                        payload_text = str(job.payload.get("text", job.payload))

                    response = await self.runtime.execute(
                        agent_name,
                        f"[SCHEDULED REMINDER] {job.action}: {payload_text}",
                        user_id=str(job.target_user_id),
                        source="scheduler",
                    )
                    await self.client.send(job.target_user_id, f"🔔 {response}")

                    job.status = "completed"
                except Exception as e:
                    logger.error(f"One-off job failed (ID: {job.id}): {e}", exc_info=True)

            await session.commit()

    async def _fire_past_due_jobs(self) -> None:
        """On startup, fire any one-off jobs that were missed during downtime."""
        settings = get_settings()
        data_dir = settings.resolve_path(settings.data_dir)
        now = datetime.now(timezone.utc)

        agents_dir = settings.resolve_path(settings.agents_dir)
        if not agents_dir.exists():
            return

        for mission_path in agents_dir.glob("*/mission.yaml"):
            agent_name = mission_path.parent.name
            await self._process_agent_jobs(agent_name, data_dir, now)

    async def _send_to_targets(self, target_user_ids: Any, message: str) -> None:
        """Send a message to target users."""
        settings = get_settings()

        if target_user_ids == "all":
            targets = settings.allowed_user_ids
        elif isinstance(target_user_ids, list):
            targets = target_user_ids
        else:
            targets = [target_user_ids]

        for user_id in targets:
            try:
                await self.client.send(str(user_id), message)
            except Exception as e:
                logger.error(f"Failed to send scheduled message to {user_id}: {e}")

    def reload(self) -> None:
        """Reload cron job definitions from mission files."""
        self.load_cron_jobs()

    def _resolve_target_user_id(self, target_user_ids: Any) -> str:
        """Extract a single user_id from target specification for context tools."""
        if isinstance(target_user_ids, list) and target_user_ids:
            return str(target_user_ids[0])
        if target_user_ids == "all":
            settings = get_settings()
            return str(settings.allowed_user_ids[0]) if settings.allowed_user_ids else ""
        return str(target_user_ids) if target_user_ids else ""
