"""MochiBot — main orchestration loop.

Wires together all components and runs the polling loop + scheduler in parallel.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

import yaml

from mochi_agents.comm.base import CommunicationClient
from mochi_agents.config import Settings, get_settings
from mochi_agents.core.agent_runtime import AgentRuntime
from mochi_agents.core.registry import Registry
from mochi_agents.core.router import Router
from mochi_agents.core.scheduler import Scheduler
from mochi_agents.core.tool_runner import ImportlibToolRunner
from mochi_agents.memory.database import close_all, init_db

logger = logging.getLogger(__name__)


class MochiBot:
    """Main bot class — client-agnostic orchestration."""

    def __init__(self, client: CommunicationClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings
        self.registry = Registry()
        self.tool_runner = ImportlibToolRunner()
        self.runtime = AgentRuntime(tool_runner=self.tool_runner)
        self.router = Router(
            registry=self.registry,
            runtime=self.runtime,
            tool_runner=self.tool_runner,
        )
        self.scheduler = Scheduler(runtime=self.runtime, client=self.client)
        self._shutdown_event = asyncio.Event()

    async def start(self) -> None:
        """Initialize all components and run the main loops."""
        self._setup_logging()
        self._setup_signals()

        # Load registry
        registry_path = self.settings.resolve_path(self.settings.system_dir) / "registry.yaml"
        self.registry.load(registry_path)

        # Initialize databases for all registered agents
        data_dir = self.settings.resolve_path(self.settings.data_dir)
        for agent in self.registry.list_agents():
            await init_db(agent.name, data_dir)

        # Register tool modules from mission files
        self._register_tools()

        # Load cron jobs
        self.scheduler.load_cron_jobs()

        # Log startup summary
        agents = self.registry.list_agents()
        agent_names = ", ".join(f"{a.display_name}" for a in agents)
        logger.info(f"🍡 Mochi started — {len(agents)} agents: {agent_names}")
        print(f"🍡 Mochi started — {len(agents)} agents: {agent_names}")

        # Run both loops in parallel
        try:
            await asyncio.gather(
                self._poll_loop(),
                self.scheduler.run(),
            )
        except asyncio.CancelledError:
            logger.info("Mochi shutting down...")
        finally:
            await close_all()
            logger.info("Mochi stopped.")

    async def _poll_loop(self) -> None:
        """Reactive loop: receive messages and route them."""
        logger.info("Poll loop started")

        async for message in self.client.poll():
            if self._shutdown_event.is_set():
                break

            if not message.text:
                continue

            try:
                logger.info(f"Received: [{message.user_id}] {message.text[:100]}")

                response = await self.router.route(
                    text=message.text,
                    user_id=message.user_id,
                )

                await self.client.send(message.chat_id, response)

            except Exception as e:
                logger.error(f"Error processing message: {e}", exc_info=True)
                try:
                    await self.client.send(
                        message.chat_id,
                        "Sorry, something went wrong. Please try again.",
                    )
                except Exception:
                    pass

    def _register_tools(self) -> None:
        """Scan mission files and register tool modules with the ToolRunner."""
        agents_dir = self.settings.resolve_path(self.settings.agents_dir)

        for agent in self.registry.list_agents():
            mission_path = agents_dir / agent.name / "mission.yaml"
            if not mission_path.exists():
                continue

            with open(mission_path) as f:
                mission = yaml.safe_load(f) or {}

            tools_module = mission.get("tools_module")
            if tools_module:
                self.tool_runner.register_agent(agent.name, tools_module)
                tool_count = len(self.tool_runner.get_tool_declarations(agent.name))
                logger.info(f"Agent '{agent.name}': {tool_count} tools from {tools_module}")

    def _setup_logging(self) -> None:
        """Configure logging."""
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    def _setup_signals(self) -> None:
        """Register signal handlers for graceful shutdown."""
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                asyncio.get_event_loop().add_signal_handler(
                    sig, lambda: self._shutdown_event.set()
                )
            except NotImplementedError:
                # Windows doesn't support add_signal_handler
                pass
