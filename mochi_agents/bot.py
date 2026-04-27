"""MochiBot — main orchestration loop.

Wires together all components and runs the polling loop + scheduler in parallel.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import Any

import yaml

from mochi_agents.comm.base import CommunicationClient
from mochi_agents.config import Settings, get_settings
from mochi_agents.core.agent_runtime import AgentRuntime
from mochi_agents.core.registry import Registry
from mochi_agents.core.router import Router
from mochi_agents.core.scheduler import Scheduler
from mochi_agents.core.shortcut_runner import ShortcutRunner, init_alias_registry, set_shortcut_runner
from mochi_agents.core.tool_runner import (
    CompositeToolRunner,
    ImportlibToolRunner,
    MCPToolRunner,
)
from mochi_agents.core.workflow_engine import WorkflowEngine
from mochi_agents.memory.database import close_all, init_db

logger = logging.getLogger(__name__)


class MochiBot:
    """Main bot class — client-agnostic orchestration."""

    def __init__(self, client: CommunicationClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings
        self.registry = Registry()
        self._importlib_runner = ImportlibToolRunner()
        self._mcp_runner = MCPToolRunner()
        self.tool_runner = CompositeToolRunner()
        self.tool_runner.add_runner(self._importlib_runner)   # local tools first
        self.tool_runner.add_runner(self._mcp_runner)         # then remote MCP
        self.runtime = AgentRuntime(tool_runner=self.tool_runner)
        self.workflow_engine = WorkflowEngine(runtime=self.runtime, tool_runner=self.tool_runner)
        self.shortcut_runner = ShortcutRunner(tool_runner=self.tool_runner, registry=self.registry)
        self.router = Router(
            registry=self.registry,
            runtime=self.runtime,
            tool_runner=self.tool_runner,
            workflow_engine=self.workflow_engine,
            shortcut_runner=self.shortcut_runner,
            on_reload=self._sync_commands,
            send_fn=self.client.send,
            cleanup_fn=self._cleanup,
        )
        self.scheduler = Scheduler(runtime=self.runtime, client=self.client)
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        """Initialize all components and run the main loops."""
        self._setup_logging()
        self._setup_signals()

        # Load registry
        registry_path = self.settings.resolve_path(self.settings.system_dir) / "registry.yaml"
        self.registry.load(registry_path)

        # Load aliases from mission files
        agents_dir = self.settings.resolve_path(self.settings.agents_dir)
        custom_agents_dir = self.settings.resolve_path(self.settings.custom_agents_dir)
        self.registry.load_aliases_from_missions(agents_dir, custom_agents_dir)

        # Initialize databases for all registered agents
        data_dir = self.settings.resolve_path(self.settings.data_dir)
        for agent in self.registry.list_agents():
            await init_db(agent.name, data_dir)

        # Register tool modules from mission files
        self._register_tools()

        # Connect to any registered MCP servers
        await self._mcp_runner.connect_all()

        # Load cron jobs
        self.scheduler.load_cron_jobs()

        # Load workflows
        self.workflow_engine.load()

        # Initialize alias registry and load shortcuts
        init_alias_registry(data_dir)
        set_shortcut_runner(self.shortcut_runner)
        self.shortcut_runner.load_shortcuts()

        # Register slash commands with Telegram
        await self._sync_commands()

        # Start dashboard web server
        self._dashboard_runner = None
        if self.settings.dashboard_enabled:
            from mochi_agents.web.server import start_dashboard
            self._dashboard_runner = await start_dashboard(self, port=self.settings.dashboard_port)

        # Log startup summary
        agents = self.registry.list_agents()
        agent_names = ", ".join(f"{a.display_name}" for a in agents)
        logger.info(f"🍡 Mochi started — {len(agents)} agents: {agent_names}")
        print(f"🍡 Mochi started — {len(agents)} agents: {agent_names}")

        # Run both loops in parallel
        poll_task = asyncio.create_task(self._poll_loop())
        scheduler_task = asyncio.create_task(self.scheduler.run())
        self._tasks = [poll_task, scheduler_task]

        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("Mochi shutting down...")
            await self._cleanup()
            logger.info("🍡 Mochi stopped.")

    async def _cleanup(self) -> None:
        """Close connections and clean up resources."""
        if self._dashboard_runner:
            await self._dashboard_runner.cleanup()
        await self._mcp_runner.close()
        if hasattr(self.client, 'ack'):
            await self.client.ack()
        if hasattr(self.client, 'close'):
            await self.client.close()
        await close_all()

    async def _sync_commands(self) -> None:
        """Push slash commands to the communication client (e.g., Telegram menu)."""
        if hasattr(self.client, 'set_commands'):
            agents = []
            for a in self.registry.list_agents():
                if a.name == "manager":
                    continue  # Manager is internal
                agents.append({"name": a.name, "description": a.description})
                # Register aliases as separate commands
                for alias in a.aliases:
                    agents.append({"name": alias, "description": f"{a.display_name} (shortcut)"})
            await self.client.set_commands(agents)

    async def _poll_loop(self) -> None:
        """Reactive loop: receive messages and route them."""
        from mochi_agents.core.location import get_tracker
        from mochi_agents.core.router import RouteResponse

        logger.info("Poll loop started")
        tracker = get_tracker()
        location_acked: set[str] = set()  # user_ids we've already acknowledged

        async for message in self.client.poll():
            try:
                # --- Location update (live location shares) ---
                if message.location:
                    lat = message.location["latitude"]
                    lon = message.location["longitude"]
                    logger.debug(
                        f"Location update: [{message.user_id}] "
                        f"({lat:.5f}, {lon:.5f})"
                    )

                    events = await tracker.update_position(
                        message.user_id, lat, lon,
                    )

                    # Fire proactive reminders for any geofence transitions
                    for event in events:
                        await self._handle_geofence_event(
                            event, message.user_id, message.chat_id,
                        )

                    # Acknowledge only the FIRST location update per user session
                    if message.user_id not in location_acked:
                        location_acked.add(message.user_id)
                        zones = await tracker.list_geofences(message.user_id)
                        if zones:
                            zone_list = ", ".join(z["name"] for z in zones)
                            await self.client.send(
                                message.chat_id,
                                f"📍 Location received! Tracking against "
                                f"{len(zones)} zone(s): {zone_list}\n"
                                f"I'll notify you on enter/exit events.",
                            )
                        else:
                            await self.client.send(
                                message.chat_id,
                                "📍 Location received! I'm tracking your position.\n\n"
                                "You don't have any zones set up yet. "
                                "Talk to Sora to create some:\n"
                                '• "Set my home at my current location"\n'
                                '• "Set office at my current location"\n'
                                '• "Set Costco at 37.43, -122.17"\n\n'
                                "Then add reminders like:\n"
                                '• "Remind me to buy eggs when I\'m at Costco"\n'
                                '• "When I leave office, remind me to stop by Costco"',
                            )

                    # If message also has text, route it normally (unusual)
                    if not message.text:
                        continue

                # --- Text messages ---
                if not message.text:
                    continue

                logger.info(f"Received: [{message.user_id}] {message.text[:100]}")

                # Set up retry notification so user knows we're retrying, not hanging
                from mochi_agents.core.agent_runtime import set_retry_notify, _RETRY_DELAYS

                async def _notify_retry(attempt: int, delay: int) -> None:
                    try:
                        await self.client.send(
                            message.chat_id,
                            f"⏳ Gemini API error — retrying in {delay}s (attempt {attempt}/{len(_RETRY_DELAYS)})...",
                        )
                    except Exception:
                        pass

                set_retry_notify(_notify_retry)

                response = await self.router.route(
                    text=message.text,
                    user_id=message.user_id,
                    chat_id=message.chat_id,
                )

                # Send with keyboard if RouteResponse has buttons and client supports it
                if (
                    isinstance(response, RouteResponse)
                    and response.keyboard
                    and hasattr(self.client, "send_with_keyboard")
                ):
                    await self.client.send_with_keyboard(
                        message.chat_id, response.text, response.keyboard
                    )
                else:
                    await self.client.send(message.chat_id, str(response))

            except Exception as e:
                logger.error(f"Error processing message: {e}", exc_info=True)
                try:
                    # Give a clear message when it's a Gemini API issue
                    error_str = str(e)
                    if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                        user_msg = (
                            "⚠️ Your Gemini API key has hit its rate limit. "
                            "I've already retried a few times. Please wait a few minutes or check your quota at "
                            "https://aistudio.google.com/apikey"
                        )
                    elif "503" in error_str or "UNAVAILABLE" in error_str:
                        user_msg = (
                            "⚠️ The Gemini API is currently overloaded (503). "
                            "This is on Google's side. I've already retried — please try again in a few minutes."
                        )
                    elif "ServerError" in type(e).__name__ or "google.genai" in error_str:
                        user_msg = f"⚠️ The Gemini API returned an error: {type(e).__name__}. Please try again later."
                    else:
                        user_msg = "Sorry, something went wrong. Please try again."
                    await self.client.send(message.chat_id, user_msg)
                except Exception:
                    pass


    async def _handle_geofence_event(
        self,
        event: Any,
        user_id: str,
        chat_id: str,
    ) -> None:
        """Handle a geofence enter/exit event — send proactive reminders."""
        from mochi_agents.core.location import get_tracker

        tracker = get_tracker()

        # Fetch and fire matching one-shot reminders
        reminders = await tracker.get_triggered_reminders(
            user_id, event.zone_name, event.event,
        )

        if not reminders:
            # No active reminders for this transition — log and skip
            logger.info(
                f"Geofence {event.event} '{event.zone_name}' for user {user_id} — no active reminders"
            )
            return

        # Build a proactive message via the secretary agent
        reminder_texts = "\n".join(f"- {r['message']}" for r in reminders)
        prompt = (
            f"[LOCATION EVENT] The user just **{event.event}ed** the zone "
            f"**{event.zone_name}**.\n\n"
            f"Active reminders that just fired:\n{reminder_texts}\n\n"
            f"Deliver these reminders in a brief, friendly message. "
            f"Don't add extra commentary — just relay the reminders clearly."
        )

        try:
            response = await self.runtime.execute(
                "secretary",
                prompt,
                user_id=user_id,
                source="location",
            )
            emoji = "📍" if event.event == "enter" else "🚶"
            await self.client.send(chat_id, f"{emoji} {response}")
        except Exception as e:
            logger.error(f"Failed to deliver geofence reminder: {e}", exc_info=True)
            # Fall back to raw reminder text
            emoji = "📍" if event.event == "enter" else "🚶"
            fallback = f"{emoji} **{event.event.title()}ing {event.zone_name}**\n{reminder_texts}"
            try:
                await self.client.send(chat_id, fallback)
            except Exception:
                pass

    def _register_tools(self) -> None:
        """Scan mission files and register tool modules with the ToolRunner."""
        agents_dir = self.settings.resolve_path(self.settings.agents_dir)

        for agent in self.registry.list_agents():
            mission_path = agents_dir / agent.name / "mission.yaml"
            if not mission_path.exists():
                continue

            # Use agent_runtime's merged mission (mission.yaml + config overrides)
            mission = self.runtime.load_mission(agent.name)

            tools_module = mission.get("tools_module")
            if tools_module:
                self._importlib_runner.register_agent(agent.name, tools_module)
                tool_count = len(self._importlib_runner.get_tool_declarations(agent.name))
                logger.info(f"Agent '{agent.name}': {tool_count} local tools from {tools_module}")

            mcp_servers = mission.get("mcp_servers")
            if mcp_servers and isinstance(mcp_servers, list):
                normalized = []
                for item in mcp_servers:
                    if isinstance(item, str):
                        normalized.append({"url": item})
                    elif isinstance(item, dict):
                        normalized.append(item)
                self._mcp_runner.register_agent(agent.name, normalized)
                logger.info(f"Agent '{agent.name}': {len(normalized)} MCP server(s) registered")

    def _setup_logging(self) -> None:
        """Configure logging."""
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    def _setup_signals(self) -> None:
        """Register signal handlers for graceful shutdown."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._signal_shutdown)
            except NotImplementedError:
                # Windows/WSL fallback — KeyboardInterrupt handled in cli.py
                pass

    def _signal_shutdown(self) -> None:
        """Cancel all running tasks to trigger graceful shutdown."""
        logger.info("Received shutdown signal")
        for task in self._tasks:
            task.cancel()
