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
        parent_system_prompt: str | None = None,
        env_context_xml: str | None = None,
    ) -> None:
        self._find_candidates = find_candidates
        self._spawn_callback = spawn_callback
        self._balance_provider = balance_provider
        self._calling_agent_id = calling_agent_id
        self._call_llm = call_llm
        self._parent_actions = parent_actions or []
        self._parent_system_prompt = parent_system_prompt
        self._env_context_xml = env_context_xml

    @property
    def name(self) -> str:
        return "use_agent"

    @property
    def description(self) -> str:
        return (
            "Spawn a sub-agent for a well-scoped sub-task.\n\n"
            "# Why delegate\n"
            "Every LLM call you make costs tokens proportional to your full "
            "conversation history. Sub-agents start with a clean context, so "
            "the same work costs them far fewer tokens (e.g. 1000 vs 5000). "
            "Their costs are charged to your balance.\n\n"
            "# Roles\n"
            " - `explorer:` (default) — read-only. Can search, read, bash. "
            "Use for investigation.\n"
            " - `worker:` — full access. Can also edit/write files. Use for "
            "code changes.\n"
            "Example: `spawn=[\"explorer: find where _cstack is defined\"]`\n\n"
            "# Designing subtasks\n"
            " - Subtasks must be concrete, self-contained, and advance the "
            "main task.\n"
            " - Include file paths, function names, or error messages — the "
            "sub-agent cannot see your context.\n"
            " - Prefer narrow, well-defined asks over vague 'look into this'.\n"
            " - For code-edit tasks, ensure each sub-agent works on disjoint "
            "files.\n\n"
            "# When to delegate\n"
            " - The sub-task is independent (e.g. search, test discovery, "
            "isolated code fix).\n"
            " - Your context is long — a fresh agent is cheaper.\n"
            " - You have multiple independent directions — spawn in parallel.\n\n"
            "# When NOT to delegate\n"
            " - The next step depends on what you just learned.\n"
            " - The task is simple and your context is still short.\n"
            " - You are very low on budget.\n\n"
            "# How to use\n"
            " - **Spawn:** `spawn=[\"role: description\"]` — fresh specialist.\n"
            " - **Hire:** `agent_id=\"<id>\"` — hire from marketplace.\n"
            " - **Browse:** omit both to see available agents and prices.\n\n"
            "After delegating, focus on non-overlapping work. Sub-agents "
            "report findings via report_result."
        )

    @property
    def parameters(self) -> dict:
        return {
            "task_description": {"type": "string", "required": True, "description": "Concrete, self-contained description of the sub-task. This is the only context the sub-agent receives."},
            "spawn": {"type": "array", "items": {"type": "string"}, "required": False, "description": "List of specialist descriptions to spawn. Prefix with 'explorer:' or 'worker:' to set the role."},
            "agent_id": {"type": "string", "required": False, "description": "ID of an existing agent to hire from the marketplace."},
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
        task_description = kwargs.get("task_description") or kwargs.get("task") or ""
        spawn = kwargs.get("spawn", False)

        # Validate spawn parameter type
        if spawn is True:
            spawn = [task_description]
        elif spawn and not isinstance(spawn, list):
            return (
                f"Error: 'spawn' must be an array of strings, got {type(spawn).__name__}. "
                f"Example: spawn=[\"worker: analyze the bug\"]"
            )

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

                    if self._parent_system_prompt is not None:
                        system_prompt = self._parent_system_prompt
                    else:
                        system_prompt = SUB_AGENT_INSTRUCTIONS + "\nYour assigned role: " + role + "\n"

                    sub_agent = ReactAgent(
                        system_prompt=system_prompt,
                        actions=self._build_sub_agent_actions(
                            agent.protected_by,
                            role=role,
                            report_callback=on_report,
                        ),
                        call_llm=self._call_llm,
                        max_iterations=20,
                    )
                    sub_context = f"[Spawned agent {aid}] {task_description}"
                    if self._env_context_xml:
                        sub_context = self._env_context_xml + "\n\n" + sub_context
                    result = sub_agent.run(context=sub_context)
                    agent._last_action_history = result.action_history
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

                # Inject skill content into system prompt if available
                system_prompt = target.soul.system_prompt
                skill = getattr(target, "skill", None)
                if skill is not None:
                    system_prompt += (
                        "\n\n## Skill Instructions\n" + skill.content
                    )

                sub_agent = ReactAgent(
                    system_prompt=system_prompt,
                    actions=self._build_sub_agent_actions(
                        target.protected_by,
                        report_callback=on_report,
                    ),
                    call_llm=self._call_llm,
                    max_iterations=20,
                )
                hire_context = task_description
                if self._env_context_xml:
                    hire_context = self._env_context_xml + "\n\n" + hire_context
                result = sub_agent.run(context=hire_context)
                target._last_action_history = result.action_history
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
