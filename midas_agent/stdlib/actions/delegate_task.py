"""use_agent action — spawn a sub-agent for an independent sub-task."""
from __future__ import annotations

from typing import Callable

from midas_agent.stdlib.action import Action

SUB_AGENT_PROMPT = """\
You are a sub-agent working on a specific subtask assigned by your parent agent.

Focus ONLY on your assigned subtask. Do not try to solve the entire problem.
When done, call report_result with a clear, concise summary of your findings.
If you are an explorer, you can search and read but cannot edit files.
If you are a worker, you can also edit and write files.
Always call report_result when done — do not just stop.\
"""

_EXPLORER_TOOLS = {"bash", "str_replace_editor", "report_result"}
_WORKER_TOOLS = {"bash", "str_replace_editor", "report_result"}


class DelegateTaskAction(Action):
    def __init__(
        self,
        call_llm: Callable | None = None,
        parent_actions: list | None = None,
        parent_system_prompt: str | None = None,
        **kwargs,
    ) -> None:
        self._call_llm = call_llm
        self._parent_actions = parent_actions or []
        self._parent_system_prompt = parent_system_prompt

    @property
    def name(self) -> str:
        return "use_agent"

    @property
    def description(self) -> str:
        return (
            "Spawn a sub-agent to handle an independent sub-task in a clean context.\n"
            "Roles: `explorer` (default) — read-only; `worker` — can also edit files.\n"
            "The sub-agent receives only the `task` text — include all needed details."
        )

    @property
    def parameters(self) -> dict:
        return {
            "task": {
                "type": "string",
                "required": True,
                "description": "The sub-task to perform. Include file paths, function names, and what you need back.",
            },
            "role": {
                "type": "string",
                "required": False,
                "enum": ["explorer", "worker"],
                "description": "Agent role. explorer (default) = read-only, worker = can edit files.",
            },
        }

    def _build_sub_agent_actions(self, role: str, report_callback: Callable) -> list:
        """Build action set for a sub-agent based on role."""
        from midas_agent.stdlib.actions.report_result import ReportResultAction

        allowed = _WORKER_TOOLS if role == "worker" else _EXPLORER_TOOLS
        actions = [a for a in self._parent_actions if a.name in allowed]
        actions.append(ReportResultAction(report=report_callback))
        return actions

    def execute(self, **kwargs) -> str:
        task = kwargs.get("task", "")
        role = kwargs.get("role", "explorer")

        if not task:
            return "Error: 'task' is required."

        if role not in ("explorer", "worker"):
            role = "explorer"

        if self._call_llm is None:
            return "Error: no LLM available for sub-agent."

        from midas_agent.stdlib.react_agent import ReactAgent

        reported: dict = {}
        def on_report(text, _reported=reported):
            _reported["result"] = text

        system_prompt = self._parent_system_prompt or SUB_AGENT_PROMPT
        # Append role instruction
        system_prompt += f"\n\nYour role: {role}."

        sub_agent = ReactAgent(
            system_prompt=system_prompt,
            actions=self._build_sub_agent_actions(role, on_report),
            call_llm=self._call_llm,
            max_iterations=20,
        )
        result = sub_agent.run(context=task)
        output = reported.get("result") or result.output or "Sub-agent completed with no output."
        return output
