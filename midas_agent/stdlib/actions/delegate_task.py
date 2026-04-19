"""DelegateTask action — thin wrapper around HiringManager."""
from __future__ import annotations

from typing import TYPE_CHECKING

from midas_agent.stdlib.action import Action

if TYPE_CHECKING:
    from midas_agent.scheduler.hiring_manager import HiringManager

SUB_AGENT_INSTRUCTIONS = """You are a spawned sub-agent working on a specific subtask assigned by your parent agent.

Your responsibilities:
- Focus ONLY on your assigned subtask. Do not try to solve the entire problem.
- When you have completed your analysis or work, call report_result with a clear, concise summary of your findings.
- Your report_result content will be delivered directly to your parent agent.

Guidelines:
- Be thorough but focused. Read relevant code, search for patterns, and form a clear conclusion.
- If you are an explorer, you can search and read code but cannot edit files.
- If you are a worker, you can also edit and write files.
- Always call report_result when done. Do not just stop — explicitly report your findings.
"""


class DelegateTaskAction(Action):
    def __init__(
        self,
        hiring_manager: HiringManager | None = None,
        **kwargs,
    ) -> None:
        self._hiring_manager = hiring_manager

    @property
    def name(self) -> str:
        return "use_agent"

    @property
    def description(self) -> str:
        return (
            "Delegate a sub-task to a sub-agent in a clean context."
        )

    @property
    def parameters(self) -> dict:
        return {
            "task": {
                "type": "string",
                "required": True,
                "description": (
                    "The sub-task to perform. Include file paths, function names, "
                    "and what you need back."
                ),
            },
        }

    def execute(self, **kwargs) -> str:
        task = kwargs.get("task") or kwargs.get("task_description") or ""
        if not task.strip():
            return "Error: task must be a non-empty string."
        if self._hiring_manager is None:
            return "Error: no hiring manager configured."
        return self._hiring_manager.delegate(task)
