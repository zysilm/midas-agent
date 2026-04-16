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
        call_llm: Callable | None = None,
    ) -> None:
        self._find_candidates = find_candidates
        self._spawn_callback = spawn_callback
        self._balance_provider = balance_provider
        self._calling_agent_id = calling_agent_id
        self._call_llm = call_llm

    @property
    def name(self) -> str:
        return "use_agent"

    @property
    def description(self) -> str:
        return (
            "Launch a sub-agent to handle an independent sub-task.\n\n"
            "Sub-agents start with a clean context window — fewer input "
            "tokens per LLM call means the same work costs less budget. "
            "This is especially valuable when your own context is already "
            "long from many file reads and tool results.\n\n"
            "# When to use this tool\n"
            " - The sub-task is independent and self-contained (e.g. "
            "'search for where function X is defined', 'write a test for "
            "module Y').\n"
            " - Your context is already long. A fresh agent with a clean "
            "context window is more token-efficient for the remaining work.\n"
            " - The sub-task requires a different focus (e.g. debugging a "
            "specific function while you continue fixing another part).\n\n"
            "# When NOT to use this tool\n"
            " - The next step depends tightly on what you just learned — "
            "the context transfer overhead is not worth it.\n"
            " - The task is simple and your context is still short.\n"
            " - You are low on budget — spawning costs tokens for the "
            "sub-agent's own LLM calls, charged to your balance.\n\n"
            "# How to use\n"
            " - **Spawn new agents:** set `spawn=[\"specialist description\"]` "
            "to create fresh specialists. Each spawned agent runs your task "
            "description in its own clean context.\n"
            " - **Hire existing agents:** set `agent_id` to hire a known "
            "agent from the marketplace (shown in your planning info).\n"
            " - **Browse candidates:** omit both `spawn` and `agent_id` to "
            "see available agents and their prices.\n\n"
            "Spawned agents are under your protection — their LLM costs are "
            "charged to your balance. They report results back to you."
        )

    @property
    def parameters(self) -> dict:
        return {
            "task_description": {"type": "string", "required": True},
            "spawn": {"type": "array", "items": {"type": "string"}, "required": False},
            "agent_id": {"type": "string", "required": False},
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

        # Backward compat: spawn=True treated as spawn=[task_description]
        if spawn is True:
            spawn = [task_description]

        # Handle spawn request (list of specialist descriptions)
        if isinstance(spawn, list) and spawn and self._spawn_callback is not None:
            # Protected agents cannot spawn new agents
            if self._is_caller_protected():
                return "Protected agent cannot spawn new agents. Not allowed."
            lines: list[str] = []
            for desc in spawn:
                agent = self._spawn_callback(desc)
                aid = getattr(agent, "agent_id", None) or "new agent"
                if self._call_llm is not None:
                    from midas_agent.stdlib.react_agent import ReactAgent
                    from midas_agent.stdlib.actions.task_done import TaskDoneAction

                    sub_agent = ReactAgent(
                        system_prompt=agent.soul.system_prompt,
                        actions=[TaskDoneAction()],
                        call_llm=self._call_llm,
                        max_iterations=10,
                    )
                    sub_context = f"[Spawned agent {aid}] {task_description}"
                    result = sub_agent.run(context=sub_context)
                    output = result.output if result.output else "Sub-agent completed with no output."
                    lines.append(f"Spawned agent {aid} result: {output}")
                else:
                    lines.append(f"Spawned agent {aid} for: {desc}")
            return "\n".join(lines)

        # Handle hire request (agent_id specified)
        agent_id_param = kwargs.get("agent_id")
        if agent_id_param:
            if self._call_llm is not None:
                candidates = self._find_candidates(task_description)
                target = None
                for c in candidates:
                    a = getattr(c, "agent", c)
                    if getattr(a, "agent_id", None) == agent_id_param:
                        target = a
                        break
                if target is None:
                    return f"Agent not found: {agent_id_param}"

                from midas_agent.stdlib.react_agent import ReactAgent
                from midas_agent.stdlib.actions.task_done import TaskDoneAction

                sub_agent = ReactAgent(
                    system_prompt=target.soul.system_prompt,
                    actions=[TaskDoneAction()],
                    call_llm=self._call_llm,
                    max_iterations=10,
                )
                result = sub_agent.run(context=task_description)
                return result.output if result.output else "Agent completed with no output."
            else:
                return f"Agent not found: {agent_id_param}"

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
                # Label agents spawned by the caller as young agents
                if self._calling_agent_id is not None:
                    agent_obj = getattr(c, "agent", None)
                    if agent_obj is not None and getattr(agent_obj, "protected_by", None) == self._calling_agent_id:
                        parts.append("[幼年agent]")
                lines.append(", ".join(parts))

        # Always offer spawn option when spawn_callback is available
        if self._spawn_callback is not None:
            lines.append("Option: spawn a new agent for this task.")

        # Append balance information if provider is set
        if self._balance_provider is not None:
            balance = self._balance_provider()
            lines.append(f"[你的余额: {balance}]")

        return "\n".join(lines)
