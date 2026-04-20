"""DelegateTask action — thin wrapper around HiringManager."""
from __future__ import annotations

from typing import TYPE_CHECKING

from midas_agent.stdlib.action import Action

if TYPE_CHECKING:
    from midas_agent.scheduler.hiring_manager import HiringManager

from midas_agent.prompts import SUB_AGENT_INSTRUCTIONS


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
        parent_context = kwargs.get("parent_context", "")
        if not task.strip():
            return "Error: task must be a non-empty string."
        if self._hiring_manager is None:
            return "Error: no hiring manager configured."
        return self._hiring_manager.delegate(task, parent_context=parent_context)
