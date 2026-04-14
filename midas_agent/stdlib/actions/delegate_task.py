"""DelegateTask action — query hireable agents (Graph Emergence only)."""
from __future__ import annotations

from typing import Callable

from midas_agent.stdlib.action import Action


class DelegateTaskAction(Action):
    def __init__(self, find_candidates: Callable) -> None:
        self._find_candidates = find_candidates

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
        task_description = kwargs["task_description"]
        top_k = kwargs.get("top_k", 5)
        candidates = self._find_candidates(task_description, top_k=top_k)
        if not candidates:
            return f"No candidates found for: {task_description}"
        lines = [f"Candidates for: {task_description}"]
        for c in candidates:
            agent_id = getattr(c, "agent_id", str(c))
            skill = getattr(c, "skill", None)
            price = getattr(c, "price", None)
            parts = [f"  - {agent_id}"]
            if skill is not None:
                skill_name = getattr(skill, "name", str(skill))
                parts.append(f"skill={skill_name}")
            if price is not None:
                parts.append(f"price={price}")
            lines.append(", ".join(parts))
        return "\n".join(lines)
