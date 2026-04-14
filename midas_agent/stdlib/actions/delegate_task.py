"""DelegateTask action — query hireable agents (Graph Emergence only)."""
from __future__ import annotations

from typing import Callable

from midas_agent.stdlib.action import Action


class DelegateTaskAction(Action):
    def __init__(self, find_candidates: Callable) -> None:
        raise NotImplementedError

    @property
    def name(self) -> str:
        return "delegate_task"

    @property
    def description(self) -> str:
        return "Find hireable agents for a sub-task."

    @property
    def parameters(self) -> dict:
        return {
            "task_description": {"type": "string", "required": True},
            "top_k": {"type": "integer", "required": False, "default": 5},
        }

    def execute(self, **kwargs) -> str:
        raise NotImplementedError
