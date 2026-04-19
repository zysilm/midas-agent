"""HiringManager — SystemLLM-driven agent selection for delegation."""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Callable

from midas_agent.llm.types import LLMRequest, LLMResponse
from midas_agent.stdlib.actions.report_result import ReportResultAction

if TYPE_CHECKING:
    from midas_agent.scheduler.training_log import TrainingLog
    from midas_agent.workspace.graph_emergence.agent import Agent
    from midas_agent.workspace.graph_emergence.free_agent_manager import FreeAgentManager
    from midas_agent.workspace.graph_emergence.pricing import PricingEngineBase

logger = logging.getLogger(__name__)

# Tool sets per role
_EXPLORER_TOOLS = {"bash", "str_replace_editor", "report_result"}
_WORKER_TOOLS = {"bash", "str_replace_editor", "report_result"}


class HiringManager:
    """Uses a single SystemLLM call to pick an existing agent or spawn a new one.

    The SystemLLM receives a roster of available agents with their skill name,
    description, price, and bankruptcy rate, and returns a JSON decision:
    ``{"action": "hire", "agent_id": "..."}`` or
    ``{"action": "spawn", "role": "explorer"}``.
    """

    def __init__(
        self,
        system_llm: Callable[[LLMRequest], LLMResponse],
        free_agent_manager: FreeAgentManager,
        spawn_callback: Callable[[str], Agent],
        call_llm: Callable[[LLMRequest], LLMResponse],
        parent_actions: list,
        parent_system_prompt: str,
        training_log: TrainingLog | None = None,
        evicted_ws_ids: set[str] | None = None,
    ) -> None:
        self._system_llm = system_llm
        self._free_agent_manager = free_agent_manager
        self._spawn_callback = spawn_callback
        self._call_llm = call_llm
        self._parent_actions = parent_actions or []
        self._parent_system_prompt = parent_system_prompt
        self._training_log = training_log
        self._evicted_ws_ids = evicted_ws_ids or set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def delegate(self, task: str) -> str:
        """Pick or spawn an agent via SystemLLM, then run it on *task*."""
        from midas_agent.stdlib.react_agent import ReactAgent

        roster = self._build_roster()
        logger.info("  HiringManager: roster has %d agents", len(self._free_agent_manager.free_agents))
        logger.info("  HiringManager: roster:\n%s", roster)

        decision = self._ask_system_llm(task, roster)
        logger.info("  HiringManager: decision=%s for task=%.100s", decision, task)

        if decision["action"] == "hire":
            return self._run_hired_agent(decision["agent_id"], task)

        # spawn (default fallback)
        role = decision.get("role", "explorer")
        return self._run_spawned_agent(task, role)

    # ------------------------------------------------------------------
    # Roster building
    # ------------------------------------------------------------------

    def _build_roster(self) -> str:
        """Build a one-line-per-agent roster string."""
        from midas_agent.workspace.graph_emergence.free_agent_manager import (
            compute_bankruptcy_rate,
        )

        agents = self._free_agent_manager.free_agents
        if not agents:
            return "(no agents available)"

        lines: list[str] = []
        for agent_id, agent in agents.items():
            skill_name = agent.skill.name if agent.skill else "general"
            skill_desc = agent.skill.description if agent.skill else "no specialization"
            price = self._free_agent_manager._pricing_engine.calculate_price(agent)
            if self._training_log is not None:
                br = compute_bankruptcy_rate(
                    agent_id, self._training_log, self._evicted_ws_ids,
                )
            else:
                br = 0.0
            lines.append(
                f"{agent_id}: {skill_name} — {skill_desc} "
                f"(price={price}, bankruptcy_rate={br:.2f})"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # SystemLLM decision
    # ------------------------------------------------------------------

    def _ask_system_llm(self, task: str, roster: str) -> dict:
        """Make ONE SystemLLM call and parse the JSON response."""
        from midas_agent.prompts import HIRING_PROMPT_TEMPLATE
        prompt = HIRING_PROMPT_TEMPLATE.format(task=task, roster=roster)

        request = LLMRequest(
            messages=[{"role": "user", "content": prompt}],
            model="default",
            max_tokens=500,
        )

        try:
            response = self._system_llm(request)
            content = (response.content or "").strip()
            # Try to extract JSON from the response (handle markdown fences)
            if content.startswith("```"):
                content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            # Try to find JSON object in response
            import re
            match = re.search(r'\{[^}]+\}', content, re.DOTALL)
            if match:
                decision = json.loads(match.group())
            else:
                decision = json.loads(content)
            reason = decision.get("reason", "")
            action = decision.get("action")
            if action == "hire" and "agent_id" in decision:
                logger.info("  HiringManager: reason=%s", reason)
                return {"action": "hire", "agent_id": decision["agent_id"]}
            if action == "spawn":
                role = decision.get("role", "explorer")
                if role not in ("explorer", "worker"):
                    role = "explorer"
                logger.info("  HiringManager: reason=%s", reason)
                return {"action": "spawn", "role": role}
        except (json.JSONDecodeError, AttributeError, KeyError, TypeError) as e:
            logger.warning("  HiringManager: malformed SystemLLM response (%s): %.200s", e, content)

        # Fallback
        return {"action": "spawn", "role": "explorer"}

    # ------------------------------------------------------------------
    # Agent execution
    # ------------------------------------------------------------------

    def _run_hired_agent(self, agent_id: str, task: str) -> str:
        """Look up an existing agent, inject skill, and run."""
        from midas_agent.stdlib.react_agent import ReactAgent

        agents = self._free_agent_manager.free_agents
        agent = agents.get(agent_id)
        if agent is None:
            logger.warning("  HiringManager: agent %s not found, falling back to spawn", agent_id)
            return self._run_spawned_agent(task, "explorer")

        skill_name = agent.skill.name if agent.skill else "none"
        logger.info("  HiringManager: hiring %s (skill=%s, protected_by=%s)", agent_id, skill_name, agent.protected_by)

        # Build system prompt with skill content + sub-agent instructions
        from midas_agent.prompts import SUB_AGENT_INSTRUCTIONS
        system_prompt = agent.soul.system_prompt
        if agent.skill is not None:
            system_prompt += "\n\n## Skill Instructions\n" + agent.skill.content
        system_prompt += "\n\n" + SUB_AGENT_INSTRUCTIONS

        reported: dict = {}
        def on_report(text, _reported=reported):
            _reported["result"] = text

        sub_agent = ReactAgent(
            system_prompt=system_prompt,
            actions=self._build_sub_agent_actions(
                agent.protected_by,
                report_callback=on_report,
            ),
            call_llm=self._call_llm,
            max_iterations=20,
        )

        result = sub_agent.run(context=task)
        agent._last_action_history = result.action_history
        return reported.get("result") or result.output or "Agent completed with no output."

    def _initialize_agent(self, agent, task: str, role: str) -> None:
        """Use SystemLLM to generate the agent's identity and initial skill."""
        from midas_agent.workspace.graph_emergence.skill import Skill

        from midas_agent.prompts import AGENT_INIT_PROMPT_TEMPLATE
        prompt = AGENT_INIT_PROMPT_TEMPLATE.format(role=role, task=task)

        request = LLMRequest(
            messages=[{"role": "user", "content": prompt}],
            model="default",
        )

        try:
            response = self._system_llm(request)
            content = (response.content or "").strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

            import re
            match = re.search(r'\{[^}]+\}', content, re.DOTALL)
            if match:
                data = json.loads(match.group())
            else:
                data = json.loads(content)

            name = data.get("name", "specialist")
            description = data.get("description", task[:100])
            system_prompt = data.get("system_prompt", f"You are a specialist agent. Task: {task}")

            agent.skill = Skill(name=name, description=description, content="")
            agent.soul.system_prompt = system_prompt
            self._free_agent_manager.update_embedding(agent.agent_id)

            logger.info("  HiringManager: initialized %s as %s — %s", agent.agent_id, name, description)
        except Exception as e:
            logger.warning("  HiringManager: agent init failed (%s), using defaults", e)
            agent.skill = Skill(name=role, description=task[:100], content="")

    def _run_spawned_agent(self, task: str, role: str) -> str:
        """Spawn a new agent, initialize its identity, and run it."""
        from midas_agent.stdlib.react_agent import ReactAgent

        agent = self._spawn_callback(task)
        aid = getattr(agent, "agent_id", None) or "new agent"
        logger.info("  HiringManager: spawned %s (protected_by=%s)", aid, agent.protected_by)

        # Initialize agent identity via SystemLLM
        self._initialize_agent(agent, task, role)

        reported: dict = {}
        def on_report(text, _reported=reported):
            _reported["result"] = text

        from midas_agent.prompts import SUB_AGENT_INSTRUCTIONS
        sub_prompt = agent.soul.system_prompt + "\n\n" + SUB_AGENT_INSTRUCTIONS

        sub_agent = ReactAgent(
            system_prompt=sub_prompt,
            actions=self._build_sub_agent_actions(
                agent.protected_by,
                role=role,
                report_callback=on_report,
            ),
            call_llm=self._call_llm,
            max_iterations=20,
        )

        sub_context = f"[Spawned agent {aid}] {task}"
        result = sub_agent.run(context=sub_context)
        agent._last_action_history = result.action_history
        return reported.get("result") or result.output or "Sub-agent completed with no output."

    # ------------------------------------------------------------------
    # Sub-agent action building (moved from DelegateTaskAction)
    # ------------------------------------------------------------------

    def _build_sub_agent_actions(
        self,
        protected_by: str | None,
        role: str | None = None,
        report_callback: Callable | None = None,
    ) -> list:
        """Build action set for a sub-agent based on protection status and role.

        When *role* is provided (from spawn path role parsing):
        - explorer: bash, str_replace_editor, report_result
        - worker: bash, str_replace_editor, report_result

        When *role* is None (backward compat / hire path):
        - No role-based filtering; only protection-based filtering applies.

        Protected agents: no use_agent, no task_done.
        Independent agents: basic actions + use_agent + report_result.
        """
        from midas_agent.stdlib.actions.delegate_task import DelegateTaskAction

        if not self._parent_actions:
            from midas_agent.stdlib.actions.task_done import TaskDoneAction
            return [TaskDoneAction()]

        # Determine allowed tool names based on role
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

        # Independent agents need use_agent
        if protected_by is None and (allowed_tools is None or "use_agent" in allowed_tools):
            has_use_agent = any(a.name == "use_agent" for a in result)
            if not has_use_agent:
                # Create a thin delegate that uses a new HiringManager
                result.append(DelegateTaskAction(
                    hiring_manager=self,
                ))

        # Add report_result for sub-agents
        has_report = any(a.name == "report_result" for a in result)
        if not has_report:
            cb = report_callback if report_callback is not None else (lambda r: None)
            result.append(ReportResultAction(report=cb))

        return result
