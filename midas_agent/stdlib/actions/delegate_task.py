"""DelegateTask action — query hireable agents (Graph Emergence only)."""
from __future__ import annotations

from typing import Callable

from midas_agent.stdlib.action import Action


class DelegateTaskAction(Action):
    def __init__(
        self,
        find_candidates: Callable,
        spawn_callback: Callable | None = None,
        balance_provider: Callable[[], int] | None = None,
        calling_agent_id: str | None = None,
    ) -> None:
        self._find_candidates = find_candidates
        self._spawn_callback = spawn_callback
        self._balance_provider = balance_provider
        self._calling_agent_id = calling_agent_id

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
            "spawn": {"type": "boolean", "required": False},
        }

    def _is_caller_protected(self) -> bool:
        """Check if the calling agent is protected by looking it up via candidates."""
        if self._calling_agent_id is None:
            return False
        # Use find_candidates with empty query to discover all agents,
        # then check if the calling agent has protected_by set.
        try:
            candidates = self._find_candidates("")
            for c in candidates:
                agent = getattr(c, "agent", None)
                if agent is not None and getattr(agent, "agent_id", None) == self._calling_agent_id:
                    return bool(getattr(agent, "protected_by", None))
        except Exception:
            pass
        return False

    def execute(self, **kwargs) -> str:
        task_description = kwargs["task_description"]
        spawn = kwargs.get("spawn", False)

        # Handle spawn request
        if spawn and self._spawn_callback is not None:
            # Protected agents cannot spawn new agents
            if self._is_caller_protected():
                return "Protected agent cannot spawn new agents. Not allowed."
            agent = self._spawn_callback(task_description)
            agent_id = getattr(agent, "agent_id", None) or "new agent"
            return f"Spawned agent {agent_id} for: {task_description}"

        candidates = self._find_candidates(task_description)
        lines: list[str] = []
        if not candidates:
            lines.append(f"No candidates found for: {task_description}")
        else:
            lines.append(f"Candidates for: {task_description}")
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

        # Always offer spawn option when spawn_callback is available
        if self._spawn_callback is not None:
            lines.append("Option: spawn a new agent for this task.")

        # Append balance information if provider is set
        if self._balance_provider is not None:
            balance = self._balance_provider()
            lines.append(f"[你的余额: {balance}]")

        return "\n".join(lines)
