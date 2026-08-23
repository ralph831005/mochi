"""Scheduler — runs recurring cron jobs and one-off scheduled tasks.

Runs as a parallel asyncio task alongside the polling loop.

Cron schedules in mission.yaml support two modes:

1. **LLM mode** (default): sends the action as text prompt to the agent.
   ```yaml
   schedules:
     - name: check_expiring_items
       cron: "0 9 * * *"
       action: check_expiring_items
       target_user_ids: all
   ```

2. **Direct tool mode**: calls a tool function directly with args.
   Supports dynamic date templates: {end_of_month}, {start_of_next_month}.
   ```yaml
   schedules:
     - name: monthly_uber_credit
       cron: "0 0 1 * *"
       action: add_expirable
       mode: tool
       args:
         title: "Uber Credit"
         value: 25.0
         category: "credit"
         expiration_date: "{end_of_month}"
       target_user_ids: all
   ```
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
        """Scan agent mission files AND config.yaml for cron schedules.

        Two sources are merged:
        1. Agent mission.yaml `schedules` blocks — framework defaults (committed)
        2. config.yaml `schedules` list — user-specific (not committed)

        Config schedules require an `agent` key to specify the target agent.
        """
        settings = get_settings()
        agents_dir = settings.resolve_path(settings.agents_dir)
        self._cron_jobs.clear()

        # Source 1: Agent mission.yaml files
        if agents_dir.exists():
            for mission_path in agents_dir.glob("*/mission.yaml"):
                agent_name = mission_path.parent.name
                with open(mission_path) as f:
                    mission = yaml.safe_load(f) or {}

                for schedule in mission.get("schedules", []):
                    self._register_job(agent_name, schedule, source="mission")

        # Source 2: config.yaml user schedules
        for schedule in settings.schedules:
            agent_name = schedule.get("agent", "")
            if not agent_name:
                logger.warning(f"Skipping config schedule '{schedule.get('name', '?')}' — missing 'agent' key")
                continue
            self._register_job(agent_name, schedule, source="config")

    def _register_job(self, agent_name: str, schedule: dict, source: str = "mission") -> None:
        """Register a single cron job from a schedule dict."""
        job = {
            "agent_name": agent_name,
            "name": schedule.get("name", "unnamed"),
            "cron": schedule["cron"],
            "action": schedule.get("action", ""),
            "mode": schedule.get("mode", "llm"),  # "llm" or "tool"
            "args": schedule.get("args", {}),
            "target_user_ids": schedule.get("target_user_ids", "all"),
        }
        self._cron_jobs.append(job)
        mode_label = "tool" if job["mode"] == "tool" else "llm"
        logger.info(f"Registered cron job: {job['name']} ({job['cron']}) [{mode_label}] for {agent_name} [from {source}]")

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
                logger.info(f"Cron job firing: {job['name']} for agent {job['agent_name']} (mode={job.get('mode', 'llm')})")
                self._last_cron_check[key] = now

                try:
                    if job.get("mode") == "tool":
                        await self._execute_tool_job(job)
                    else:
                        await self._execute_llm_job(job)
                except Exception as e:
                    logger.error(f"Cron job failed: {job['name']}: {e}", exc_info=True)

    async def _execute_llm_job(self, job: dict) -> None:
        """Execute a cron job by sending it as text to the LLM agent."""
        user_id = self._resolve_target_user_id(job["target_user_ids"])
        response = await self.runtime.execute(
            job["agent_name"],
            f"[SCHEDULED] Execute action: {job['action']}",
            user_id=user_id,
            source="scheduler",
        )
        if response and response.strip():
            await self._send_to_targets(job["target_user_ids"], f"📋 {response}")

    async def _execute_tool_job(self, job: dict) -> None:
        """Execute a cron job by calling a tool function directly with args."""
        tool_name = job["action"]
        raw_args = dict(job.get("args", {}))

        # Resolve dynamic date templates
        resolved_args = {k: self._resolve_template(v) for k, v in raw_args.items()}

        # Inject user_id
        user_id = self._resolve_target_user_id(job["target_user_ids"])
        resolved_args["user_id"] = user_id

        logger.info(f"Tool job: {tool_name}({resolved_args})")
        result = await self.runtime.tool_runner.execute(
            job["agent_name"], tool_name, resolved_args
        )

        if result.success:
            msg = f"✅ Auto-created: **{resolved_args.get('title', tool_name)}**"
            if resolved_args.get('value'):
                msg += f" (${resolved_args['value']})"
            if resolved_args.get('expiration_date'):
                msg += f" — expires {resolved_args['expiration_date']}"
            await self._send_to_targets(job["target_user_ids"], msg)
        else:
            logger.error(f"Tool job {tool_name} failed: {result.error}")

    @staticmethod
    def _resolve_template(value: Any) -> Any:
        """Resolve dynamic date templates in string values.

        Supported templates:
            {end_of_month}          - last day of current month (YYYY-MM-DD)
            {start_of_next_month}   - first day of next month (YYYY-MM-DD)
            {today}                 - today's date (YYYY-MM-DD)
            {today+N}               - N days from today (YYYY-MM-DD)

        All dates use the configured local timezone.
        """
        import calendar
        import re

        if not isinstance(value, str) or "{" not in value:
            return value

        settings = get_settings()
        local_tz = settings.get_tz()
        now = datetime.now(local_tz)

        replacements = {
            "{today}": now.strftime("%Y-%m-%d"),
            "{end_of_month}": now.replace(
                day=calendar.monthrange(now.year, now.month)[1]
            ).strftime("%Y-%m-%d"),
        }

        # {start_of_next_month}
        if now.month == 12:
            next_month = now.replace(year=now.year + 1, month=1, day=1)
        else:
            next_month = now.replace(month=now.month + 1, day=1)
        replacements["{start_of_next_month}"] = next_month.strftime("%Y-%m-%d")

        for template, resolved in replacements.items():
            value = value.replace(template, resolved)

        # {today+N} pattern
        match = re.search(r"\{today\+(\d+)\}", value)
        if match:
            from datetime import timedelta
            days = int(match.group(1))
            future = (now + timedelta(days=days)).strftime("%Y-%m-%d")
            value = re.sub(r"\{today\+\d+\}", future, value)

        return value

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
