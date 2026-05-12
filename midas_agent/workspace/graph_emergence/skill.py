"""Skill and SkillReviewer for Graph Emergence."""
from __future__ import annotations

from typing import Callable, TYPE_CHECKING

from pydantic import BaseModel

from llm_agent_toolkit.llm.types import LLMRequest, LLMResponse
from llm_agent_toolkit.stdlib.react_agent import ActionRecord

try:
    import dspy
except ImportError:
    dspy = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from midas_agent.workspace.graph_emergence.agent import Agent
    from midas_agent.workspace.graph_emergence.free_agent_manager import FreeAgentManager


class Skill(BaseModel):
    name: str
    description: str
    content: str  # hard limit 5000 chars


class SkillReviewer:
    def __init__(
        self,
        system_llm: Callable[[LLMRequest], LLMResponse],
        free_agent_manager: FreeAgentManager,
        skill_evolution: bool = True,
    ) -> None:
        self._system_llm = system_llm
        self._free_agent_manager = free_agent_manager
        self._skill_evolution = skill_evolution

    def review(self, agent: Agent, eval_results: dict, action_history: list[ActionRecord]) -> None:
        # 1. If disabled, do nothing
        if not self._skill_evolution:
            return

        s_exec = eval_results.get("s_exec", 0.0)

        # 2. Path A: skill is None + S_exec > 0 -> create initial skill
        if agent.skill is None:
            if s_exec > 0:
                from midas_agent.workspace.graph_emergence.skill_evolution import create_initial_skill
                new_skill = create_initial_skill(self._system_llm, action_history, eval_results)
                if new_skill is not None:
                    agent.skill = new_skill
                    self._free_agent_manager.update_embedding(agent.agent_id)
            return

        # 3. Path B: skill exists -> GEPA evolution
        if dspy is None:
            return
        try:
            from midas_agent.workspace.graph_emergence.skill_evolution import (
                SkillDatasetBuilder,
                SkillModule,
                skill_fitness_metric,
            )

            # Build dataset (minimal for now -- just the current episode)
            builder = SkillDatasetBuilder()
            action_summary = "; ".join(
                f"{a.action_name}: {a.result[:50]}" for a in action_history
            )
            builder.add_episode(
                task_input=eval_results.get("issue_description", "task"),
                action_summary=action_summary,
                score=s_exec,
            )
            train, val, holdout = builder.build()

            # Wrap skill as DSPy module
            skill_module = SkillModule(skill_text=agent.skill.content)

            # Run GEPA
            optimizer = dspy.GEPA(metric=skill_fitness_metric, max_steps=10)
            optimized = optimizer.compile(skill_module, trainset=train, valset=val)
            new_content = optimized.skill_text

            # Constraint gating
            validator = SkillConstraintValidator()
            holdout_score_old = s_exec
            holdout_score_new = s_exec

            if validator.validate(new_content, agent.skill.content, holdout_score_new, holdout_score_old):
                agent.skill = Skill(
                    name=agent.skill.name,
                    description=agent.skill.description,
                    content=new_content,
                )
                self._free_agent_manager.update_embedding(agent.agent_id)
        except Exception:
            # GEPA failure -> keep original skill, don't crash
            pass


class SkillConstraintValidator:
    """Hard gates that every evolved skill must pass before acceptance."""

    def validate(
        self,
        new_content: str,
        old_content: str | None,
        holdout_score_new: float,
        holdout_score_old: float,
    ) -> bool:
        # Size limit
        if len(new_content) > 5000:
            return False
        # Growth limit (skip on first creation and on trivially short baselines)
        if (
            old_content is not None
            and len(old_content) >= 100
            and len(new_content) > len(old_content) * 1.2
        ):
            return False
        # Non-empty
        if new_content.strip() == "":
            return False
        # No regression
        if holdout_score_new < holdout_score_old:
            return False
        return True
