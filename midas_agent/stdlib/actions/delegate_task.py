"""DelegateTask action — query hireable agents (Graph Emergence only)."""
from __future__ import annotations

from typing import Callable

from midas_agent.stdlib.action import Action

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

# Tool sets per role
_EXPLORER_TOOLS = {"bash", "read_file", "search_code", "find_files", "report_result"}
_WORKER_TOOLS = {"bash", "read_file", "edit_file", "write_file", "search_code", "find_files", "report_result"}


class DelegateTaskAction(Action):
    def __init__(
        self,
        find_candidates: Callable,
        spawn_callback: Callable | None = None,
        balance_provider: Callable[[], int] | None = None,
        calling_agent_id: str | None = None,
        call_llm: Callable | None = None,
        parent_actions: list | None = None,
    ) -> None:
        self._find_candidates = find_candidates
        self._spawn_callback = spawn_callback
        self._balance_provider = balance_provider
        self._calling_agent_id = calling_agent_id
        self._call_llm = call_llm
        self._parent_actions = parent_actions or []

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
            "# Roles: explorer vs worker\n"
            "Prefix the spawn description with 'explorer:' or 'worker:' to "
            "set the sub-agent's role.\n"
            " - **explorer** (default): read-only access. Can search code, "
            "read files, and run bash, but cannot edit or write files. Use "
            "for analysis, investigation, and code search tasks.\n"
            " - **worker**: full access. Can also edit and write files. Use "
            "when the sub-agent needs to make changes (fix a bug, write a "
            "test, refactor code).\n"
            "Examples: `spawn=[\"explorer: find where X is defined\"]`, "
            "`spawn=[\"worker: fix the off-by-one in foo.py\"]`.\n\n"
            "# Writing effective briefings\n"
            "The task_description is the only context your sub-agent receives. "
            "Make it concrete, specific, and self-contained:\n"
            " - State exactly what to do and what to report back.\n"
            " - Include file paths, function names, or error messages when "
            "relevant — the sub-agent does not share your context.\n"
            " - Scope the task narrowly. A well-defined subtask gets better "
            "results than a vague 'look into this'.\n"
            " - Sub-agents report their findings back to you via "
            "report_result. You will receive the report_result content "
            "directly.\n\n"
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
            "charged to your balance. They report results back to you via "
            "report_result."
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

    def _build_sub_agent_actions(
        self,
        protected_by: str | None,
        role: str | None = None,
        report_callback: Callable | None = None,
    ) -> list:
        """Build action set for a sub-agent based on protection status and role.

        When *role* is provided (from spawn path role parsing):
        - explorer: bash, read_file, search_code, find_files, report_result
        - worker: bash, read_file, edit_file, write_file, search_code, find_files, report_result

        When *role* is None (backward compat / hire path):
        - No role-based filtering; only protection-based filtering applies.

        Protected agents (幼年): no use_agent, no task_done.
        Independent agents: basic actions + use_agent + report_result.
        """
        from midas_agent.stdlib.actions.report_result import ReportResultAction

        if not self._parent_actions:
            # Fallback: minimal actions (backward compat)
            from midas_agent.stdlib.actions.task_done import TaskDoneAction
            return [TaskDoneAction()]

        # Determine allowed tool names based on role (None = no filtering)
        if role == "worker":
            allowed_tools = _WORKER_TOOLS
        elif role == "explorer":
            allowed_tools = _EXPLORER_TOOLS
        else:
            allowed_tools = None  # no role-based filtering

        result = []
        for action in self._parent_actions:
            # Role-based filtering (only when role is explicitly set)
            if allowed_tools is not None and action.name not in allowed_tools:
                continue
            if protected_by is not None:
                # Protected agent: no use_agent, no task_done
                if action.name == "use_agent":
                    continue
                if action.name == "task_done":
                    continue
            result.append(action)

        # Independent agents need use_agent (a new DelegateTaskAction instance)
        if protected_by is None and (allowed_tools is None or "use_agent" in allowed_tools):
            has_use_agent = any(a.name == "use_agent" for a in result)
            if not has_use_agent:
                result.append(DelegateTaskAction(
                    find_candidates=self._find_candidates,
                    spawn_callback=self._spawn_callback,
                    call_llm=self._call_llm,
                    parent_actions=self._parent_actions,
                ))

        # Add report_result for sub-agents (if not already present)
        has_report = any(a.name == "report_result" for a in result)
        if not has_report:
            cb = report_callback if report_callback is not None else (lambda r: None)
            result.append(ReportResultAction(report=cb))

        return result

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
                # Parse role prefix from description
                role = "explorer"  # default
                clean_desc = desc
                if desc.lower().startswith("explorer:"):
                    role = "explorer"
                    clean_desc = desc[len("explorer:"):].strip()
                elif desc.lower().startswith("worker:"):
                    role = "worker"
                    clean_desc = desc[len("worker:"):].strip()

                agent = self._spawn_callback(clean_desc)
                aid = getattr(agent, "agent_id", None) or "new agent"
                if self._call_llm is not None:
                    from midas_agent.stdlib.react_agent import ReactAgent

                    # Mutable container to capture report_result content
                    reported: dict = {}
                    def on_report(text, _reported=reported):
                        _reported["result"] = text

                    system_prompt = SUB_AGENT_INSTRUCTIONS + "\nYour assigned role: " + role + "\n"

                    sub_agent = ReactAgent(
                        system_prompt=system_prompt,
                        actions=self._build_sub_agent_actions(
                            agent.protected_by,
                            role=role,
                            report_callback=on_report,
                        ),
                        call_llm=self._call_llm,
                        max_iterations=9999,
                    )
                    sub_context = f"[Spawned agent {aid}] {task_description}"
                    result = sub_agent.run(context=sub_context)
                    output = reported.get("result") or result.output or "Sub-agent completed with no output."
                    lines.append(f"Spawned agent {aid} result: {output}")
                else:
                    lines.append(f"Spawned agent {aid} for: {clean_desc}")
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

                # Mutable container to capture report_result content
                reported: dict = {}
                def on_report(text, _reported=reported):
                    _reported["result"] = text

                sub_agent = ReactAgent(
                    system_prompt=target.soul.system_prompt,
                    actions=self._build_sub_agent_actions(
                        target.protected_by,
                        report_callback=on_report,
                    ),
                    call_llm=self._call_llm,
                    max_iterations=9999,
                )
                result = sub_agent.run(context=task_description)
                output = reported.get("result") or result.output or "Agent completed with no output."
                return output
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
