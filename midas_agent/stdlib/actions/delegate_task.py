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
        return (
            "Requests help from another agent in the system. The system matches "
            "your task description against available agents' skills and returns "
            "a list of candidates with pricing, along with your current token balance.\n\n"
            "Usage:\n"
            "* Provide a concise, specific task description. The system uses this "
            "to find agents with relevant skills.\n"
            "* You will receive your current balance and a candidate list with pricing. "
            "Choose one, or choose to spawn a new agent, or choose not to delegate.\n"
            "* Delegated tasks are executed in isolated sessions — the hired agent "
            "cannot see your context.\n"
            "* The hired agent will return results via `report_result`. Wait for "
            "this before proceeding."
        )

    @property
    def parameters(self) -> dict:
        return {
            "task_description": {"type": "string", "required": True},
        }

    def execute(self, **kwargs) -> str:
        task_description = kwargs["task_description"]
        candidates = self._find_candidates(task_description)
        if not candidates:
            return f"No candidates found for: {task_description}"
        lines = [f"Candidates for: {task_description}"]
        for c in candidates:
            agent_id = getattr(c, "agent_id", None) or getattr(c.agent, "agent_id", str(c))
            price = getattr(c, "price", None)
            similarity = getattr(c, "similarity", None)
            parts = [f"  - {agent_id}"]
            if price is not None:
                parts.append(f"price={price}")
            if similarity is not None:
                parts.append(f"match={similarity:.1f}")
            lines.append(", ".join(parts))
        return "\n".join(lines)
