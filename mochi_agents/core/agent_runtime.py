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

import inspect
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
        self._model_overrides: dict[str, dict[str, Any]] = {}  # runtime overrides

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

    def get_effective_model_config(self, agent_name: str) -> dict[str, Any]:
        """Get model_config with any runtime overrides merged in."""
        mission = self.get_mission(agent_name)
        base = dict(mission.get("model_config", {}))
        overrides = self._model_overrides.get(agent_name, {})
        base.update(overrides)
        return base

    def set_model_override(self, agent_name: str, **kwargs: Any) -> dict[str, Any]:
        """Set runtime model overrides (e.g., model, api_key). In-memory only."""
        if agent_name not in self._model_overrides:
            self._model_overrides[agent_name] = {}
        self._model_overrides[agent_name].update(kwargs)
        effective = self.get_effective_model_config(agent_name)
        logger.info(f"Model override for '{agent_name}': {self._model_overrides[agent_name]} → effective: {effective.get('model')}")
        return effective

    def clear_model_overrides(self, agent_name: str | None = None) -> None:
        """Clear runtime overrides. If agent_name is None, clear all."""
        if agent_name:
            self._model_overrides.pop(agent_name, None)
        else:
            self._model_overrides.clear()

    def update_model_config(self, agent_name: str, updates: dict[str, Any]) -> dict[str, Any]:
        """Permanently update an agent's model_config in mission.yaml."""
        settings = get_settings()
        mission_path = settings.resolve_path(settings.agents_dir) / agent_name / "mission.yaml"
        
        if not mission_path.exists():
            raise FileNotFoundError(f"Mission file not found for agent '{agent_name}'")

        with open(mission_path) as f:
            mission = yaml.safe_load(f) or {}

        if "model_config" not in mission:
            mission["model_config"] = {}

        mission["model_config"].update(updates)

        with open(mission_path, "w") as f:
            yaml.dump(mission, f, sort_keys=False)

        # Clear ephemeral overrides so the new base applies immediately
        self.clear_model_overrides(agent_name)
        
        # Reload cache
        return self.load_mission(agent_name)

    def _load_soul(self) -> str:
        """Load the shared soul.md file."""
        settings = get_settings()
        soul_path = settings.resolve_path(settings.system_dir) / "soul.md"
        if soul_path.exists():
            return soul_path.read_text()
        return ""

    async def _build_system_instruction(self, agent_name: str, extra_context: str = "", user_id: str = "") -> str:
        """Assemble system instruction = Soul + Mission + Memory Notes + Summary + Context Data + extra."""
        soul = self._load_soul()
        mission = self.get_mission(agent_name)
        mission_prompt = mission.get("mission_prompt", "")

        parts = [soul, mission_prompt]

        # Inject persistent memory notes (profile, goals, preferences)
        memory_notes = await self._load_memory_notes(agent_name)
        if memory_notes:
            parts.append(memory_notes)

        # Inject rolling conversation summary
        summary = await self._load_conversation_summary(agent_name)
        if summary:
            parts.append(summary)

        # Inject context from auto-called tools (e.g., today's meal log)
        context_data = await self._load_context_tools(agent_name, user_id=user_id)
        if context_data:
            parts.append(context_data)

        if extra_context:
            parts.append(extra_context)

        return "\n\n---\n\n".join(part for part in parts if part)

    async def _load_context_tools(self, agent_name: str, user_id: str = "") -> str:
        """Auto-call tools listed in mission.yaml context_tools and format results.

        Runtime context variables (e.g., user_id) are auto-injected into tool
        args by introspecting the tool function's signature.
        """
        mission = self.get_mission(agent_name)
        context_tools = mission.get("context_tools", [])

        if not context_tools:
            return ""

        # Runtime context variables available for auto-injection
        context_vars = {"user_id": user_id}

        sections = []
        for entry in context_tools:
            tool_name = entry.get("tool", "")
            label = entry.get("label", tool_name)
            explicit_args = entry.get("args", {})

            try:
                # Build args: explicit config first, then auto-inject matching
                # context variables based on the tool's function signature
                args = dict(explicit_args)
                declarations = self.tool_runner.get_tool_declarations(agent_name)
                tool_decl = next((d for d in declarations if d.name == tool_name), None)
                if tool_decl and tool_decl.function:
                    sig = inspect.signature(tool_decl.function)
                    for param_name in sig.parameters:
                        if param_name not in args and param_name in context_vars:
                            args[param_name] = context_vars[param_name]

                result = await self.tool_runner.execute(agent_name, tool_name, args)
                if result.success and result.data:
                    if isinstance(result.data, (dict, list)):
                        data_str = json.dumps(result.data, indent=2, default=str)
                    else:
                        data_str = str(result.data)
                    sections.append(f"## {label}\n\n```\n{data_str}\n```")
                else:
                    sections.append(f"## {label}\n\nNo data available.")
            except Exception as e:
                logger.warning(f"Context tool '{tool_name}' failed for '{agent_name}': {e}")

        return "\n\n".join(sections) if sections else ""

    async def _load_conversation_summary(self, agent_name: str) -> str:
        """Load the rolling conversation summary for the system instruction."""
        from sqlalchemy import select

        settings = get_settings()
        data_dir = settings.resolve_path(settings.data_dir)
        factory = get_session_factory(agent_name, data_dir)

        async with factory() as session:
            stmt = (
                select(ConversationMessage)
                .where(ConversationMessage.is_summary == True)
                .order_by(ConversationMessage.id.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            summary_msg = result.scalar_one_or_none()

        if not summary_msg:
            return ""

        return f"## Conversation Summary\n\n{summary_msg.content}"

    async def _load_memory_notes(self, agent_name: str) -> str:
        """Load persistent memory notes and format them for the system instruction."""
        from sqlalchemy import select
        from mochi_agents.memory.models import MemoryNote

        settings = get_settings()
        data_dir = settings.resolve_path(settings.data_dir)
        factory = get_session_factory(agent_name, data_dir)

        async with factory() as session:
            stmt = select(MemoryNote).order_by(MemoryNote.category, MemoryNote.id)
            result = await session.execute(stmt)
            notes = result.scalars().all()

        if not notes:
            return ""

        # Group by category
        categories: dict[str, list[str]] = {}
        for note in notes:
            categories.setdefault(note.category, []).append(note.content)

        # Format as markdown sections
        section_titles = {
            "profile": "User Profile",
            "goal": "Active Goals",
            "preference": "Dietary Preferences",
        }

        lines = ["## Remembered Context"]
        for cat, items in categories.items():
            title = section_titles.get(cat, cat.title())
            lines.append(f"\n### {title}")
            for item in items:
                lines.append(f"- {item}")

        return "\n".join(lines)

    async def _load_memory(self, agent_name: str, limit: int = 10) -> list[dict[str, str]]:
        """Load recent non-archived user/assistant messages for the contents array.

        Only loads user-originated messages to keep conversation memory clean
        from synthetic prompts (scheduler, agent delegation). The rolling
        summary is injected into system_instruction, not here.
        """
        settings = get_settings()
        data_dir = settings.resolve_path(settings.data_dir)
        factory = get_session_factory(agent_name, data_dir)

        from sqlalchemy import select

        async with factory() as session:
            stmt = (
                select(ConversationMessage)
                .where(
                    ConversationMessage.archived == False,
                    ConversationMessage.is_summary == False,
                    ConversationMessage.source == "user",
                )
                .order_by(ConversationMessage.id.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            recent = list(reversed(result.scalars().all()))

        return [{"role": msg.role, "content": msg.content} for msg in recent]

    async def _save_message(self, agent_name: str, role: str, content: str, source: str = "user") -> None:
        """Save a conversation message to the agent's SQLite."""
        settings = get_settings()
        data_dir = settings.resolve_path(settings.data_dir)
        factory = get_session_factory(agent_name, data_dir)

        async with factory() as session:
            msg = ConversationMessage(role=role, content=content, source=source)
            session.add(msg)
            await session.commit()

        # Check if compression is needed
        await self._maybe_compress(agent_name)

    async def _maybe_compress(self, agent_name: str, threshold: int = 10) -> None:
        """Summarize and archive old messages when count exceeds threshold.

        Flow:
        1. Count non-archived, non-summary messages
        2. If > threshold: load existing summary + those messages
        3. Ask LLM to produce a new rolling summary
        4. Archive the old messages, replace the summary row
        """
        settings = get_settings()
        data_dir = settings.resolve_path(settings.data_dir)
        factory = get_session_factory(agent_name, data_dir)

        from sqlalchemy import select, func, update

        async with factory() as session:
            # Count non-archived regular messages
            count_stmt = (
                select(func.count())
                .select_from(ConversationMessage)
                .where(
                    ConversationMessage.archived == False,
                    ConversationMessage.is_summary == False,
                )
            )
            count_result = await session.execute(count_stmt)
            total = count_result.scalar() or 0

            if total <= threshold:
                return

            logger.info(f"Compressing memory for '{agent_name}': {total} messages > {threshold}")

            # Load existing summary
            summary_stmt = (
                select(ConversationMessage)
                .where(ConversationMessage.is_summary == True)
                .order_by(ConversationMessage.id.desc())
                .limit(1)
            )
            summary_result = await session.execute(summary_stmt)
            old_summary = summary_result.scalar_one_or_none()

            # Load all non-archived regular messages
            msgs_stmt = (
                select(ConversationMessage)
                .where(
                    ConversationMessage.archived == False,
                    ConversationMessage.is_summary == False,
                )
                .order_by(ConversationMessage.id.asc())
            )
            msgs_result = await session.execute(msgs_stmt)
            all_messages = list(msgs_result.scalars().all())

        # Keep the last 3 messages for conversational continuity
        keep_count = 3
        to_summarize = all_messages[:-keep_count]
        # kept = all_messages[-keep_count:]  # these stay active

        if not to_summarize:
            return

        # Build the summarization prompt
        existing = old_summary.content if old_summary else "(No prior summary)"
        conversation = "\n".join(
            f"{m.role}: {m.content}" for m in to_summarize
        )

        summarize_prompt = (
            "You are a memory compression assistant. "
            "Produce a concise summary that captures key facts, preferences, "
            "and context the agent needs to remember. Keep it under 300 words.\n\n"
            f"## Existing Summary\n{existing}\n\n"
            f"## New Messages\n{conversation}\n\n"
            "## Updated Summary"
        )

        # Call LLM for summarization (use the agent's effective model config)
        model_config = self.get_effective_model_config(agent_name)
        client = create_client(model_config)
        model_name = get_model_name(model_config)

        summary_response = await client.aio.models.generate_content(
            model=model_name,
            contents=summarize_prompt,
            config=types.GenerateContentConfig(temperature=0.1, max_output_tokens=1024),
        )

        new_summary = ""
        if summary_response.candidates and summary_response.candidates[0].content.parts:
            new_summary = "".join(
                p.text for p in summary_response.candidates[0].content.parts if p.text
            )

        if not new_summary:
            logger.warning(f"Summarization failed for '{agent_name}', skipping compression")
            return

        # Archive old messages (keep last 3) and upsert the summary
        async with factory() as session:
            # Only archive the summarized messages, not the kept ones
            msg_ids = [m.id for m in to_summarize]
            await session.execute(
                update(ConversationMessage)
                .where(ConversationMessage.id.in_(msg_ids))
                .values(archived=True)
            )

            # Delete old summary and insert new one
            if old_summary:
                await session.execute(
                    update(ConversationMessage)
                    .where(ConversationMessage.id == old_summary.id)
                    .values(content=new_summary)
                )
            else:
                session.add(ConversationMessage(
                    role="system",
                    content=new_summary,
                    is_summary=True,
                ))

            await session.commit()

        logger.info(f"Memory compressed for '{agent_name}': {len(to_summarize)} messages archived, {keep_count} kept")

    def _build_tool_declarations(self, agent_name: str) -> list[types.Tool] | None:
        """Build function declarations (schemas only) for the LLM.

        We pass schemas instead of callables so the SDK doesn't try
        to auto-call them (which fails with async functions).
        We handle tool execution manually via the ToolRunner.
        """
        declarations = self.tool_runner.get_tool_declarations(agent_name)
        if not declarations:
            return None

        func_declarations = []
        for decl in declarations:
            # Build parameter schema from type hints
            properties = {}
            required = []
            if decl.function:
                hints = {
                    k: v for k, v in decl.function.__annotations__.items()
                    if k != "return"
                }
                sig = inspect.signature(decl.function)
                for param_name, hint in hints.items():
                    param_type = self._python_type_to_schema(hint)
                    properties[param_name] = param_type
                    # Required if no default value
                    param = sig.parameters.get(param_name)
                    if param and param.default is inspect.Parameter.empty:
                        required.append(param_name)

            schema = {"type": "OBJECT", "properties": properties}
            if required:
                schema["required"] = required

            func_declarations.append(
                types.FunctionDeclaration(
                    name=decl.name,
                    description=decl.description,
                    parameters=schema,
                )
            )

        return [types.Tool(function_declarations=func_declarations)]

    @staticmethod
    def _python_type_to_schema(hint: Any) -> dict:
        """Convert a Python type hint to a JSON Schema-ish dict for Gemini."""
        type_map = {
            str: "STRING",
            int: "INTEGER",
            float: "NUMBER",
            bool: "BOOLEAN",
        }
        type_name = getattr(hint, "__name__", str(hint))
        for py_type, schema_type in type_map.items():
            if hint is py_type:
                return {"type": schema_type}
        return {"type": "STRING"}

    async def execute(
        self,
        agent_name: str,
        user_message: str,
        extra_context: str = "",
        user_id: str = "",
        source: str = "user",
        _delegation_depth: int = 0,
    ) -> str:
        """Execute an agent with a user message. Returns the final response text."""
        # Set context for shared tools (delegation, memory)
        from mochi_agents.core.shared_tools import set_current_runtime, set_current_depth
        set_current_runtime(self)
        set_current_depth(_delegation_depth)

        mission = self.get_mission(agent_name)
        model_config = self.get_effective_model_config(agent_name)
        is_stateless = mission.get("stateless", False)

        # Build system instruction
        system_instruction = await self._build_system_instruction(agent_name, extra_context, user_id=user_id)

        # Load conversation memory (skip for stateless agents)
        contents = []
        if not is_stateless:
            memory = await self._load_memory(agent_name)
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

        # Save user message to memory (skip for stateless agents)
        if not is_stateless:
            await self._save_message(agent_name, "user", user_message, source=source)

        # Get tool declarations (schemas only — we handle execution manually)
        tool_declarations = self._build_tool_declarations(agent_name)

        # Create LLM client and call
        client = create_client(model_config)
        model_name = get_model_name(model_config)
        gen_config = get_generation_config(model_config)

        # Build the config with system instruction — disable automatic function calling
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=gen_config.temperature,
            max_output_tokens=gen_config.max_output_tokens,
            tools=tool_declarations,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

        logger.info(f"Executing agent '{agent_name}' with model '{model_name}'{' (stateless)' if is_stateless else ''}")

        response = await client.aio.models.generate_content(
            model=model_name,
            contents=contents,
            config=config,
        )

        # Handle tool calls in the response
        response_text = await self._handle_response(
            agent_name, response, client, model_name, contents, config,
            user_id=user_id,
        )

        # Save assistant response to memory (skip for stateless agents)
        if not is_stateless:
            await self._save_message(agent_name, "assistant", response_text, source=source)

        return response_text

    async def _handle_response(
        self,
        agent_name: str,
        response: Any,
        client: Any,
        model_name: str,
        contents: list,
        config: Any,
        max_turns: int = 10,
        user_id: str = "",
    ) -> str:
        """Process LLM response, executing tool calls in a loop if needed.

        The loop continues when:
        1. The agent's mission has tool_loop: true (default)
        2. The LLM includes <<AWAIT>> in its text, signaling it needs results

        Without <<AWAIT>>, tools are executed fire-and-forget and any
        text the LLM included is returned immediately.
        """
        mission = self.get_mission(agent_name)
        tool_loop_enabled = mission.get("tool_loop", True)

        # Runtime context variables for auto-injection into tool args
        context_vars = {"user_id": user_id}

        # Pre-load tool declarations for signature introspection
        declarations = self.tool_runner.get_tool_declarations(agent_name)
        decl_by_name = {d.name: d for d in declarations} if declarations else {}

        for turn in range(max_turns):
            if not response.candidates:
                return "I couldn't generate a response. Please try again."

            candidate = response.candidates[0]

            # Separate function calls from text parts
            function_calls = []
            text_parts = []

            for part in candidate.content.parts:
                if part.function_call:
                    function_calls.append(part.function_call)
                elif part.text:
                    text_parts.append(part.text)

            # No tool calls — return text (natural loop exit)
            if not function_calls:
                return "".join(text_parts) if text_parts else "I couldn't generate a response."

            # Execute all tool calls
            tool_results = []
            for fc in function_calls:
                tool_name = fc.name
                args = dict(fc.args) if fc.args else {}

                # Auto-inject context variables (e.g., user_id) into tool args
                # when the tool function accepts them but the LLM didn't provide them
                decl = decl_by_name.get(tool_name)
                if decl and decl.function:
                    sig = inspect.signature(decl.function)
                    for param_name, value in context_vars.items():
                        if param_name in sig.parameters and param_name not in args:
                            args[param_name] = value

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

            raw_text = "".join(text_parts)

            # Gate 1: Agent-level — loop disabled entirely
            if not tool_loop_enabled:
                clean = raw_text.replace("<<AWAIT>>", "").strip()
                return clean if clean else "Done."

            # Gate 2: LLM-level — check for <<AWAIT>> marker
            # If the LLM executed tools but provided NO conversational text, force a loop
            # so it can see the results and formulate a proper natural language reply.
            if "<<AWAIT>>" not in raw_text and raw_text.strip():
                # Fire-and-forget: tools executed, return text
                return raw_text.strip()

            # <<AWAIT>> present — send results back to LLM for next step
            logger.info(f"Agent '{agent_name}' awaiting tool results (turn {turn + 1}/{max_turns})")

            contents.append(candidate.content)
            contents.append(
                types.Content(role="user", parts=tool_results)
            )

            response = await client.aio.models.generate_content(
                model=model_name,
                contents=contents,
                config=config,
            )

        # Safety: max turns hit
        logger.warning(f"Agent '{agent_name}' hit max tool loop turns ({max_turns})")
        return "I completed several steps but hit the iteration limit. Please check the results."

    def reload_missions(self) -> None:
        """Clear the mission cache so missions are re-loaded on next access."""
        self._mission_cache.clear()
