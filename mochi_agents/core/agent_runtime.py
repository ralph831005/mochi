"""Agent Runtime — the core execution engine.

Given an agent name and a user message, the runtime:
1. Loads Soul + Mission + Memory
2. Discovers tools via ToolRunner
3. Calls the LLM
4. Executes tool calls
5. Persists conversation history
6. Returns the final response
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import yaml
from google.genai import types

from mochi_agents.config import get_settings
from mochi_agents.core.model_factory import create_client, get_generation_config, get_model_name
from mochi_agents.core.tool_runner import ImportlibToolRunner, ToolDeclaration
from mochi_agents.memory.database import get_session_factory
from mochi_agents.memory.models import ConversationMessage

logger = logging.getLogger(__name__)


class AgentRuntime:
    """Executes an agent: assembles context, calls LLM, runs tools, persists memory."""

    def __init__(self, tool_runner: ImportlibToolRunner) -> None:
        self.tool_runner = tool_runner
        self._mission_cache: dict[str, dict[str, Any]] = {}

    def load_mission(self, agent_name: str) -> dict[str, Any]:
        """Load and cache an agent's mission.yaml."""
        settings = get_settings()
        agents_dir = settings.resolve_path(settings.agents_dir)
        mission_path = agents_dir / agent_name / "mission.yaml"

        with open(mission_path) as f:
            mission = yaml.safe_load(f) or {}

        # Load mission_prompt.md if it exists
        prompt_path = agents_dir / agent_name / "mission_prompt.md"
        if prompt_path.exists():
            mission["mission_prompt"] = prompt_path.read_text()

        self._mission_cache[agent_name] = mission
        return mission

    def get_mission(self, agent_name: str) -> dict[str, Any]:
        """Get mission from cache, loading if needed."""
        if agent_name not in self._mission_cache:
            self.load_mission(agent_name)
        return self._mission_cache[agent_name]

    def _load_soul(self) -> str:
        """Load the shared soul.md file."""
        settings = get_settings()
        soul_path = settings.resolve_path(settings.system_dir) / "soul.md"
        if soul_path.exists():
            return soul_path.read_text()
        return ""

    def _build_system_instruction(self, agent_name: str, extra_context: str = "") -> str:
        """Assemble system instruction = Soul + Mission prompt + extra context."""
        soul = self._load_soul()
        mission = self.get_mission(agent_name)
        mission_prompt = mission.get("mission_prompt", "")

        parts = [soul, mission_prompt]
        if extra_context:
            parts.append(extra_context)

        return "\n\n---\n\n".join(part for part in parts if part)

    async def _load_memory(self, agent_name: str, limit: int = 20) -> list[dict[str, str]]:
        """Load recent conversation messages from the agent's SQLite."""
        settings = get_settings()
        data_dir = settings.resolve_path(settings.data_dir)
        factory = get_session_factory(agent_name, data_dir)

        from sqlalchemy import select

        async with factory() as session:
            stmt = (
                select(ConversationMessage)
                .order_by(ConversationMessage.id.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            messages = list(reversed(result.scalars().all()))

        return [{"role": msg.role, "content": msg.content} for msg in messages]

    async def _save_message(self, agent_name: str, role: str, content: str) -> None:
        """Save a conversation message to the agent's SQLite."""
        settings = get_settings()
        data_dir = settings.resolve_path(settings.data_dir)
        factory = get_session_factory(agent_name, data_dir)

        async with factory() as session:
            msg = ConversationMessage(role=role, content=content)
            session.add(msg)
            await session.commit()

    def _build_tool_functions(self, agent_name: str) -> list[Any] | None:
        """Get tool declarations and convert to Google GenAI function format."""
        declarations = self.tool_runner.get_tool_declarations(agent_name)
        if not declarations:
            return None

        # Return the actual callable functions for google-genai's automatic schema extraction
        return [d.function for d in declarations if d.function is not None]

    async def execute(
        self,
        agent_name: str,
        user_message: str,
        extra_context: str = "",
    ) -> str:
        """Execute an agent with a user message. Returns the final response text."""
        mission = self.get_mission(agent_name)
        model_config = mission.get("model_config", {})

        # Build system instruction
        system_instruction = self._build_system_instruction(agent_name, extra_context)

        # Load conversation memory
        memory = await self._load_memory(agent_name)

        # Build contents: memory + new user message
        contents = []
        for msg in memory:
            contents.append(
                types.Content(
                    role=msg["role"] if msg["role"] != "assistant" else "model",
                    parts=[types.Part.from_text(text=msg["content"])],
                )
            )
        contents.append(
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=user_message)],
            )
        )

        # Save user message to memory
        await self._save_message(agent_name, "user", user_message)

        # Get tools
        tool_functions = self._build_tool_functions(agent_name)

        # Create LLM client and call
        client = create_client(model_config)
        model_name = get_model_name(model_config)
        gen_config = get_generation_config(model_config)

        # Build the config with system instruction
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=gen_config.temperature,
            max_output_tokens=gen_config.max_output_tokens,
            tools=tool_functions,
        )

        logger.info(f"Executing agent '{agent_name}' with model '{model_name}'")

        response = client.models.generate_content(
            model=model_name,
            contents=contents,
            config=config,
        )

        # Handle tool calls in the response
        response_text = await self._handle_response(agent_name, response, client, model_name, contents, config)

        # Save assistant response to memory
        await self._save_message(agent_name, "assistant", response_text)

        return response_text

    async def _handle_response(
        self,
        agent_name: str,
        response: Any,
        client: Any,
        model_name: str,
        contents: list,
        config: Any,
    ) -> str:
        """Process LLM response, executing tool calls if present."""
        # Check if response has function calls
        if not response.candidates:
            return "I couldn't generate a response. Please try again."

        candidate = response.candidates[0]

        # Check for function calls in the response parts
        function_calls = []
        text_parts = []

        for part in candidate.content.parts:
            if part.function_call:
                function_calls.append(part.function_call)
            elif part.text:
                text_parts.append(part.text)

        if not function_calls:
            return "".join(text_parts) if text_parts else "I couldn't generate a response."

        # Execute tool calls
        tool_results = []
        for fc in function_calls:
            tool_name = fc.name
            args = dict(fc.args) if fc.args else {}

            logger.info(f"Agent '{agent_name}' calling tool '{tool_name}' with args: {args}")
            result = await self.tool_runner.execute(agent_name, tool_name, args)

            if result.success:
                tool_results.append(
                    types.Part.from_function_response(
                        name=tool_name,
                        response={"result": result.data if result.data is not None else "OK"},
                    )
                )
            else:
                tool_results.append(
                    types.Part.from_function_response(
                        name=tool_name,
                        response={"error": result.error},
                    )
                )

        # Send tool results back to the LLM for final response
        contents.append(candidate.content)
        contents.append(
            types.Content(role="user", parts=tool_results)
        )

        followup = client.models.generate_content(
            model=model_name,
            contents=contents,
            config=config,
        )

        if followup.candidates and followup.candidates[0].content.parts:
            return "".join(
                p.text for p in followup.candidates[0].content.parts if p.text
            )

        return "Tool executed successfully."

    def reload_missions(self) -> None:
        """Clear the mission cache so missions are re-loaded on next access."""
        self._mission_cache.clear()
