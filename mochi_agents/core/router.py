"""Input Router — routes messages to the correct agent.

Supports:
- System commands (/reload)
- Direct routing (slash commands, keyword matching)
- Delegated routing via Manager agent
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mochi_agents.config import get_settings, reload_settings
from mochi_agents.core.registry import Registry

if TYPE_CHECKING:
    from typing import Callable, Awaitable

    from mochi_agents.core.agent_runtime import AgentRuntime
    from mochi_agents.core.shortcut_runner import ShortcutRunner
    from mochi_agents.core.tool_runner import ImportlibToolRunner
    from mochi_agents.core.workflow_engine import WorkflowEngine

logger = logging.getLogger(__name__)


@dataclass
class RouteResponse:
    """Response from routing — may include keyboard buttons for Telegram."""

    text: str
    keyboard: list[list[dict[str, str]]] | None = None  # [[{text, callback_data}]]

    def __str__(self) -> str:
        return self.text

class Router:
    """Routes incoming messages to the appropriate agent."""

    def __init__(
        self,
        registry: Registry,
        runtime: AgentRuntime,
        tool_runner: ImportlibToolRunner,
        workflow_engine: WorkflowEngine | None = None,
        shortcut_runner: ShortcutRunner | None = None,
        on_reload: Callable[[], Awaitable[None]] | None = None,
        send_fn: Callable[[str, str], Awaitable[None]] | None = None,
        cleanup_fn: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.registry = registry
        self.runtime = runtime
        self.tool_runner = tool_runner
        self.workflow_engine = workflow_engine
        self.shortcut_runner = shortcut_runner
        self._on_reload = on_reload
        self._send_fn = send_fn
        self._cleanup_fn = cleanup_fn
        self._active_sessions: dict[str, tuple[str, float]] = {}  # user_id → (agent_name, timestamp)
        self._session_timeout = 300  # 5 minutes

    def _get_active_agent(self, user_id: str) -> str | None:
        """Get the active agent for a user, or None if session expired."""
        if user_id not in self._active_sessions:
            return None
        agent_name, last_time = self._active_sessions[user_id]
        if time.time() - last_time > self._session_timeout:
            del self._active_sessions[user_id]
            return None
        return agent_name

    def _set_active_agent(self, user_id: str, agent_name: str) -> None:
        """Set or refresh the active agent session for a user."""
        self._active_sessions[user_id] = (agent_name, time.time())

    def _clear_active_agent(self, user_id: str) -> None:
        """Clear the active agent session for a user."""
        self._active_sessions.pop(user_id, None)

    async def route(self, text: str, user_id: str = "", chat_id: str = "") -> str:
        """Route a message and return the agent's response text."""

        # Step 0a: Active shortcut session takes highest priority (no LLM)
        if self.shortcut_runner and self.shortcut_runner.has_active_session(user_id):
            result = await self.shortcut_runner.handle_input(user_id, text)
            if result:
                return result

        # Step 0b: System commands (don't affect sessions)
        stripped = text.strip()
        if stripped.startswith("/reload"):
            return await self._handle_reload()
        if stripped.startswith("/restart"):
            return await self._handle_restart(chat_id)
        if stripped.startswith("/approve"):
            return await self._handle_approve(stripped)
        if stripped.startswith("/reject"):
            return await self._handle_reject(stripped)

        # Step 1: Slash commands
        if text.strip().startswith("/"):
            parts = text.strip().split(maxsplit=1)
            command = parts[0][1:]  # Remove the /
            message_body = parts[1] if len(parts) > 1 else ""

            # Step 1a: Check for agent shortcut subcommand (e.g., /noa smoothie 30 50)
            if self.shortcut_runner and message_body:
                agent = self.registry.get_agent(command)
                if agent:
                    shortcut_prompt = await self.shortcut_runner.start_shortcut(
                        user_id, agent.name, message_body
                    )
                    if shortcut_prompt:
                        logger.info(f"Shortcut: /{command} {message_body}")
                        return shortcut_prompt

            # Step 1b: Check global alias (e.g., /smoothie 30 50)
            if self.shortcut_runner:
                from mochi_agents.core.shortcut_runner import get_alias_registry
                alias_entry = get_alias_registry().get(command)
                
                target_agent = None
                target_cmd = None
                
                if alias_entry:
                    target_agent = alias_entry["agent"]
                    target_cmd = f"{alias_entry['shortcut']} {message_body}".strip()
                else:
                    # Auto-promote: check if any agent inherently owns this shortcut
                    owner_agent = self.shortcut_runner.find_shortcut_agent(command)
                    if owner_agent:
                        target_agent = owner_agent
                        target_cmd = f"{command} {message_body}".strip()
                        
                if target_agent and target_cmd:
                    shortcut_prompt = await self.shortcut_runner.start_shortcut(
                        user_id, target_agent, target_cmd
                    )
                    if shortcut_prompt:
                        logger.info(f"Global shortcut: /{command} → {target_agent}:{target_cmd}")
                        return shortcut_prompt

            # Step 1c: Bare agent command → list shortcuts if available, else LLM
            agent = self.registry.get_agent(command)
            if agent:
                logger.info(f"Direct route (slash command): /{command} → {agent.name}")
                if not message_body and self.shortcut_runner:
                    shortcuts = self.shortcut_runner.list_shortcuts(agent.name)
                    if shortcuts:
                        lines = [f"**{agent.display_name}** — Quick commands:"]
                        buttons = []
                        for s in shortcuts:
                            lines.append(f"  • `/{command} {s['command']}` — {s['description']}")
                            buttons.append([
                                {"text": f"⚡ {s['command']}", "callback_data": f"/{command} {s['command']}"}
                            ])
                        lines.append(f"\nOr just type a message to talk to {agent.display_name}.")
                        buttons.append([
                            {"text": f"💬 Talk to {agent.display_name}", "callback_data": f"/{command}"}
                        ])
                        return RouteResponse(
                            text="\n".join(lines),
                            keyboard=buttons,
                        )
                if not message_body:
                    message_body = f"The user invoked /{command}. Provide a brief summary of what you can do, or show today's status."
                self._set_active_agent(user_id, agent.name)
                return await self.runtime.execute(agent.name, message_body, user_id=user_id)

        # Step 2: Greeting prefix → always switch session
        agent, body = self._greeting_match(text)
        if agent:
            logger.info(f"Direct route (greeting): → {agent.name}")
            self._set_active_agent(user_id, agent.name)
            return await self.runtime.execute(agent.name, body or text, user_id=user_id)

        # Step 3: Active session → stay with current agent unless strong keyword override
        active_agent = self._get_active_agent(user_id)
        if active_agent:
            # Check for strong keyword match to a DIFFERENT agent (higher threshold)
            keyword_agent = self._keyword_match(text, threshold=2)
            if keyword_agent and keyword_agent.name != active_agent:
                logger.info(f"Session override: {active_agent} → {keyword_agent.name} (strong keyword match)")
                self._set_active_agent(user_id, keyword_agent.name)
                return await self.runtime.execute(keyword_agent.name, text, user_id=user_id)

            # Stay with active agent
            logger.info(f"Sticky session: → {active_agent}")
            self._set_active_agent(user_id, active_agent)  # refresh timestamp
            return await self.runtime.execute(active_agent, text, user_id=user_id)

        # Step 4: Keyword matching (no active session)
        agent = self._keyword_match(text)
        if agent:
            logger.info(f"Direct route (keyword): → {agent.name}")
            self._set_active_agent(user_id, agent.name)
            return await self.runtime.execute(agent.name, text, user_id=user_id)

        # Step 5: Delegated routing via Manager
        logger.info("No direct match — delegating to Manager")
        return await self._delegate_to_manager(text, user_id=user_id)

    def _keyword_match(self, text: str, threshold: int = 1) -> object | None:
        """Check if any routing keywords appear in the text."""
        words = set(text.lower().split())

        best_agent = None
        best_score = 0

        for agent in self.registry.list_agents():
            if not agent.routing_keys:
                continue
            score = sum(1 for key in agent.routing_keys if key.lower() in words)
            if score >= threshold and score > best_score:
                best_agent = agent
                best_score = score

        return best_agent

    _GREETING_PATTERN = re.compile(
        r"^(?:hey|hi|hello|yo|sup)\s+(.+?)(?:[,!.?]|\s|$)(.*)",
        re.IGNORECASE,
    )

    def _greeting_match(self, text: str) -> tuple[object | None, str]:
        """Check if message starts with a greeting + agent name/alias.

        Returns (agent, remaining_message) or (None, "").
        Examples: 'hey Nutritionist how are you' → (nutritionist_agent, 'how are you')
                  'hi n what did I eat' → (nutritionist_agent, 'what did I eat')
        """
        match = self._GREETING_PATTERN.match(text.strip())
        if not match:
            return None, ""

        name = match.group(1).strip().rstrip(",!.?")
        rest = match.group(2).strip()

        # Try resolving by name/alias
        agent = self.registry.get_agent(name)
        if agent:
            return agent, rest

        # Try display_name (case-insensitive)
        for a in self.registry.list_agents():
            if a.display_name.lower() == name.lower():
                return a, rest

        return None, ""

    async def _delegate_to_manager(self, text: str, user_id: str = "") -> str:
        """Use the Manager agent to determine routing, then execute the chosen agent."""
        from pydantic import BaseModel, Field

        class RoutingDecision(BaseModel):
            route_to: str = Field(description="Agent name to route to, or 'none' if no agent can handle it")
            reason: str = Field(description="Brief explanation of routing choice")

        # Give the Manager the registry summary as context
        registry_summary = self.registry.get_registry_summary()
        workflow_summary = self.workflow_engine.get_workflow_summary() if self.workflow_engine else ""
        extra_context = f"## Available Agents\n\n{registry_summary}"
        if workflow_summary:
            extra_context += f"\n\n{workflow_summary}"

        manager_response = await self.runtime.execute(
            "manager",
            text,
            extra_context=extra_context,
            user_id=user_id,
            response_schema=RoutingDecision,
        )

        # Parse the structured JSON response
        route_to = None
        try:
            data = json.loads(manager_response)
            route_to = data.get("route_to", "").lower().strip()
            reason = data.get("reason", "")
            logger.info(f"Manager routing decision: {route_to} — {reason}")
        except (json.JSONDecodeError, ValueError):
            logger.warning(f"Manager returned non-JSON: {manager_response[:100]}")

        if route_to and route_to not in ("manager", "none", ""):
            # Check if it's a workflow
            if self.workflow_engine and self.workflow_engine.get_workflow(route_to):
                logger.info(f"Manager routed → workflow '{route_to}'")
                return await self.workflow_engine.execute(route_to, user_id=user_id)

            agent = self.registry.get_agent(route_to)
            if agent:
                logger.info(f"Manager routed → {agent.name}")
                self._set_active_agent(user_id, agent.name)
                return await self.runtime.execute(agent.name, text, user_id=user_id)

        # If Manager says no agent can handle it, delegate to Learner
        if route_to == "none":
            learner = self.registry.get_agent("learner")
            if learner:
                logger.info("No agent found — delegating to Learner")
                return await self.runtime.execute("learner", text, user_id=user_id)

        # Manager handled it directly (conversational — no session started)
        return manager_response

    async def _handle_reload(self) -> str:
        """Handle the /reload system command."""
        logger.info("Handling /reload command")

        # Reload config
        settings = reload_settings()

        # Reload registry
        registry_path = settings.resolve_path(settings.system_dir) / "registry.yaml"
        self.registry.load(registry_path)

        # Reload aliases from mission files
        agents_dir = settings.resolve_path(settings.agents_dir)
        self.registry.load_aliases_from_missions(agents_dir)

        # Reload tool modules
        self.tool_runner.reload()

        # Reload missions
        self.runtime.reload_missions()

        # Reload workflows
        if self.workflow_engine:
            self.workflow_engine.load()

        # Reload shortcuts
        if self.shortcut_runner:
            self.shortcut_runner.reload()

        agents = self.registry.list_agents()
        agent_names = ", ".join(a.display_name for a in agents)

        # Sync Telegram commands
        if self._on_reload:
            await self._on_reload()

        return f"🔄 Reloaded!\n• Config: ✅\n• Registry: {len(agents)} agents ({agent_names})\n• Tools: ✅\n• Missions: ✅\n• Commands: ✅"

    async def _handle_restart(self, chat_id: str = "") -> str:
        """Handle the /restart system command — self-restart via os.execv."""
        logger.info("Handling /restart command")

        # Notify user before shutting down (Telegram only, not dashboard)
        if self._send_fn and chat_id and chat_id != "dashboard":
            await self._send_fn(chat_id, "🔄 Restarting Mochi...")

        # For dashboard requests: schedule restart after a short delay
        # so the HTTP response can be sent back to the browser first
        if chat_id == "dashboard":
            import asyncio
            async def _deferred_restart():
                await asyncio.sleep(0.5)
                if self._cleanup_fn:
                    await self._cleanup_fn()
                logger.info("Executing os.execv to restart (from dashboard)")
                os.execv(
                    sys.executable,
                    [sys.executable, "-m", "mochi_agents"],
                )
            asyncio.get_event_loop().create_task(_deferred_restart())
            return "🔄 Restarting Mochi... Dashboard will reconnect shortly."

        # Clean up (close DB connections, HTTP clients)
        if self._cleanup_fn:
            await self._cleanup_fn()

        # Replace current process with a fresh one
        logger.info("Executing os.execv to restart")
        os.execv(
            sys.executable,
            [sys.executable, "-m", "mochi_agents"],
        )

        # This line is never reached
        return ""

    async def _handle_approve(self, text: str) -> str:
        """Approve and deploy a Learner draft."""
        import shutil

        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            return "Usage: /approve <name>"

        name = parts[1].strip()
        settings = get_settings()
        root = settings.resolve_path(".")

        # Check for workflow draft
        workflow_draft = root / "workflows" / f"{name}.yaml.draft"
        if workflow_draft.exists():
            target = workflow_draft.with_suffix("")  # Remove .draft
            shutil.move(str(workflow_draft), str(target))
            if self.workflow_engine:
                self.workflow_engine.load()
            logger.info(f"Approved workflow: {name}")
            return f"✅ Workflow **{name}** approved and deployed! It's now active."

        # Check for MCP python draft
        mcp_draft = root / "skills_server" / "skills" / f"{name}.py.draft"
        if mcp_draft.exists():
            # 1. Parse header for target agent
            target_agent = "manager"  # Default fallback
            with open(mcp_draft, "r") as f:
                for line in f:
                    if line.startswith("# TARGET_AGENT:"):
                        target_agent = line.split(":", 1)[1].strip()
                        break
                        
            # 2. Rename draft file
            target_file = mcp_draft.with_suffix("") # Remove .draft
            shutil.move(str(mcp_draft), str(target_file))
            
            # 3. Update agent's config via config.yaml overrides (not mission.yaml)
            from mochi_agents.config import get_settings, save_agent_override

            settings = get_settings()
            overrides = settings.agent_overrides.get(target_agent, {})
            servers = overrides.get("mcp_servers", [{"url": "http://127.0.0.1:8001"}])

            # Find the localhost 8001 server
            server_entry = None
            for entry in servers:
                if isinstance(entry, str) and entry == "http://127.0.0.1:8001":
                    idx = servers.index(entry)
                    servers[idx] = {"url": "http://127.0.0.1:8001"}
                    server_entry = servers[idx]
                    break
                elif isinstance(entry, dict) and entry.get("url") == "http://127.0.0.1:8001":
                    server_entry = entry
                    break

            if not server_entry:
                server_entry = {"url": "http://127.0.0.1:8001"}
                servers.append(server_entry)

            # Add tool to whitelist
            tools_list = server_entry.setdefault("tools", [])
            if name not in tools_list:
                tools_list.append(name)

            save_agent_override(target_agent, "mcp_servers", servers)
            logger.info(f"Updated {target_agent} config.yaml override with new MCP tool {name}")
                
            return await self._handle_restart()

        # Check for agent draft
        agent_draft = root / "agents" / f"{name}.draft"
        if agent_draft.exists():
            target = root / "agents" / name
            if target.exists():
                return f"❌ Agent '{name}' already exists. Remove it first or use a different name."
            shutil.move(str(agent_draft), str(target))

            # Auto-register in registry
            mission_path = target / "mission.yaml"
            if mission_path.exists():
                import yaml as _yaml
                with open(mission_path) as f:
                    mission = _yaml.safe_load(f) or {}

                registry_path = root / "system" / "registry.yaml"
                with open(registry_path) as f:
                    reg_data = _yaml.safe_load(f) or {"agents": []}

                existing = [a["name"] for a in reg_data.get("agents", [])]
                if name not in existing:
                    reg_data.setdefault("agents", []).append({
                        "name": name,
                        "display_name": mission.get("display_name", name),
                        "description": mission.get("description", ""),
                        "routing_keys": mission.get("routing_keys", []),
                        "status": "active",
                    })
                    with open(registry_path, "w") as f:
                        _yaml.dump(reg_data, f, default_flow_style=False, sort_keys=False)

            return await self._handle_restart()

        return f"❌ No draft found named **{name}**. Check /list."

    async def _handle_reject(self, text: str) -> str:
        """Reject and delete a Learner draft."""
        import shutil

        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            return "Usage: /reject <name>"

        name = parts[1].strip()
        settings = get_settings()
        root = settings.resolve_path(".")
        
        mcp_draft = root / "skills_server" / "skills" / f"{name}.py.draft"
        if mcp_draft.exists():
            mcp_draft.unlink()
            return f"🗑️ Rejected and deleted MCP Python draft: **{name}**."

        # Check for workflow draft
        workflow_draft = root / "workflows" / f"{name}.yaml.draft"
        if workflow_draft.exists():
            workflow_draft.unlink()
            logger.info(f"Rejected workflow draft: {name}")
            return f"🗑️ Workflow draft **{name}** rejected and deleted."

        # Check for agent draft
        agent_draft = root / "agents" / f"{name}.draft"
        if agent_draft.exists():
            shutil.rmtree(agent_draft)
            logger.info(f"Rejected agent draft: {name}")
            return f"🗑️ Agent draft **{name}** rejected and deleted."

        return f"❌ No draft found for '{name}'."
