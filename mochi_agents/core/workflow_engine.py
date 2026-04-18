"""Workflow Engine — executes YAML workflow definitions.

Workflows are sequences of steps that combine tool calls and LLM prompts
into reproducible, deterministic pipelines. They're created by the Learner
Agent or Admin Agent and stored in the workflows/ directory.

Step types:
- tool_call: Execute a tool with arguments (supports variable substitution)
- llm_prompt: Send a prompt to an agent's LLM (supports variable substitution)
- respond: Return content as the final response
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import yaml

from mochi_agents.config import get_settings

logger = logging.getLogger(__name__)


class WorkflowEngine:
    """Loads and executes YAML workflow definitions."""

    def __init__(self, runtime: Any, tool_runner: Any) -> None:
        self.runtime = runtime
        self.tool_runner = tool_runner
        self._workflows: dict[str, dict] = {}

    def load(self, workflows_dir: Path | None = None) -> None:
        """Load all .yaml workflow files from the workflows/ directory."""
        if workflows_dir is None:
            settings = get_settings()
            workflows_dir = settings.resolve_path(".") / "workflows"

        self._workflows.clear()

        if not workflows_dir.exists():
            return

        for path in workflows_dir.glob("*.yaml"):
            try:
                with open(path) as f:
                    workflow = yaml.safe_load(f)
                if workflow and "name" in workflow and "steps" in workflow:
                    self._workflows[workflow["name"]] = workflow
                    logger.info(f"Loaded workflow: {workflow['name']}")
            except Exception as e:
                logger.error(f"Failed to load workflow {path}: {e}")

        logger.info(f"Loaded {len(self._workflows)} workflows")

    def get_workflow(self, name: str) -> dict | None:
        """Return a workflow definition by name."""
        return self._workflows.get(name)

    def list_workflows(self) -> list[dict[str, str]]:
        """Return a list of workflow names and descriptions."""
        return [
            {"name": w["name"], "description": w.get("description", "")}
            for w in self._workflows.values()
        ]

    def get_workflow_summary(self) -> str:
        """Return a formatted summary for the Manager's context."""
        if not self._workflows:
            return ""

        lines = ["## Available Workflows"]
        for w in self._workflows.values():
            lines.append(f"- **{w['name']}**: {w.get('description', '(no description)')}")
        return "\n".join(lines)

    async def execute(
        self,
        workflow_name: str,
        variables: dict[str, str] | None = None,
        user_id: str = "",
    ) -> str:
        """Execute a workflow by name, returning the final response text.

        Args:
            workflow_name: The workflow to execute.
            variables: Initial variables (e.g., {"user_id": "123"}).
            user_id: User ID for context tool injection.
        """
        workflow = self._workflows.get(workflow_name)
        if not workflow:
            return f"Workflow '{workflow_name}' not found."

        agent = workflow.get("agent", "manager")
        steps = workflow.get("steps", [])
        context: dict[str, str] = dict(variables or {})
        final_output = ""

        logger.info(f"Executing workflow '{workflow_name}' ({len(steps)} steps)")

        for i, step in enumerate(steps):
            action = step.get("action")
            output_var = step.get("output")

            try:
                if action == "tool_call":
                    result = await self._execute_tool_step(agent, step, context)
                    if output_var:
                        context[output_var] = json.dumps(result) if isinstance(result, dict) else str(result)

                elif action == "llm_prompt":
                    prompt = self._substitute(step.get("prompt", ""), context)
                    result = await self.runtime.execute(agent, prompt, user_id=user_id)
                    if output_var:
                        context[output_var] = result
                    final_output = result

                elif action == "respond":
                    content = self._substitute(step.get("content", ""), context)
                    final_output = content

                else:
                    logger.warning(f"Unknown step action '{action}' in workflow '{workflow_name}'")

            except Exception as e:
                logger.error(f"Workflow '{workflow_name}' step {i} failed: {e}")
                return f"Workflow error at step {i + 1}: {e}"

        return final_output or "Workflow completed."

    async def _execute_tool_step(
        self, agent: str, step: dict, context: dict[str, str],
    ) -> Any:
        """Execute a tool_call step."""
        tool_name = step.get("tool", "")
        raw_args = step.get("args", {})

        # Substitute variables in args
        args = {}
        for key, value in raw_args.items():
            if isinstance(value, str):
                args[key] = self._substitute(value, context)
            else:
                args[key] = value

        result = await self.tool_runner.execute(agent, tool_name, args)

        if result.success:
            return result.data
        else:
            raise RuntimeError(f"Tool '{tool_name}' failed: {result.error}")

    @staticmethod
    def _substitute(template: str, context: dict[str, str]) -> str:
        """Replace $variable references in a string with values from context."""
        def replacer(match: re.Match) -> str:
            var_name = match.group(1)
            return context.get(var_name, match.group(0))

        return re.sub(r"\$(\w+)", replacer, template)
