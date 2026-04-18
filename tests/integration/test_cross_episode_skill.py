"""Integration tests for cross-episode skill lifecycle.

Tests verify skills persist across episodes, accumulate for multiple agents,
and are visible in the EnvironmentContext after creation.
"""
from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock

import pytest

from midas_agent.context.environment import EnvironmentContext
from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage, ToolCall
from midas_agent.stdlib.actions.bash import BashAction
from midas_agent.stdlib.actions.delegate_task import DelegateTaskAction
from midas_agent.stdlib.actions.file_ops import (
    ReadFileAction,
    EditFileAction,
    WriteFileAction,
)
from midas_agent.stdlib.actions.search import SearchCodeAction, FindFilesAction
from midas_agent.stdlib.actions.task_done import TaskDoneAction
from midas_agent.stdlib.react_agent import ActionRecord, AgentResult
from midas_agent.workspace.graph_emergence.agent import Agent, Soul
from midas_agent.workspace.graph_emergence.free_agent_manager import FreeAgentManager
from midas_agent.workspace.graph_emergence.pricing import PricingEngine
from midas_agent.workspace.graph_emergence.skill import Skill, SkillReviewer
from midas_agent.workspace.graph_emergence.workspace import GraphEmergenceWorkspace
from midas_agent.scheduler.serial_queue import SerialQueue
from midas_agent.scheduler.training_log import TrainingLog
from tests.integration.conftest import InMemoryStorageBackend, SpyHookSet


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _r(content=None, tool_calls=None):
    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )


def _log():
    return TrainingLog(
        storage=InMemoryStorageBackend(),
        hooks=SpyHookSet(),
        serial_queue=SerialQueue(),
    )


def _skill_json(name="debug", desc="Debugging", content="Use pdb."):
    return json.dumps({"name": name, "description": desc, "content": content})


def _make_system_llm(responses=None):
    if responses is None:
        responses = [_skill_json()]
    idx = {"i": 0}

    def fake(request):
        r = responses[min(idx["i"], len(responses) - 1)]
        idx["i"] += 1
        return LLMResponse(content=r, tool_calls=None, usage=TokenUsage(30, 30))

    return fake


def _build_env_context(fam):
    agent_lines = []
    agents = fam.free_agents
    for agent_id, agent in agents.items():
        price = fam._pricing_engine.calculate_price(agent)
        skill_name = agent.skill.name if agent.skill else "general"
        agent_lines.append(f"{agent_id}: {skill_name} (price={price})")
    return EnvironmentContext(
        cwd="/testbed",
        shell="bash",
        current_date=str(date.today()),
        balance=100000,
        available_agents=agent_lines,
    )


# ===========================================================================
# 18. Full lifecycle: spawn -> skill creation -> visible in next episode
# ===========================================================================


@pytest.mark.integration
class TestFullLifecycle:
    """Episode 1: spawn agent, post_episode creates skill.
    Episode 2: verify agent shows with skill name in EnvironmentContext."""

    def test_full_lifecycle(self):
        log = _log()
        pe = PricingEngine(training_log=log)
        fam = FreeAgentManager(pricing_engine=pe)

        responsible = Agent(
            agent_id="lead-1",
            soul=Soul(system_prompt="Lead agent."),
            agent_type="workspace_bound",
        )

        # Spawn an agent
        sub_agent = Agent(
            agent_id="explorer-1",
            soul=Soul(system_prompt="Explorer agent."),
            agent_type="free",
            protected_by="lead-1",
        )
        fam.register(sub_agent)

        # Episode 1: set action history, then run post_episode to create skill
        sub_agent._last_action_history = [
            ActionRecord(action_name="find_files", arguments={"pattern": "*.py"},
                         result="Found 5 files", timestamp=1.0),
            ActionRecord(action_name="read_file", arguments={"path": "main.py"},
                         result="def main(): pass", timestamp=2.0),
            ActionRecord(action_name="report_result", arguments={"result": "Done"},
                         result="Result reported.", timestamp=3.0),
        ]

        system_llm = _make_system_llm([_skill_json("code_navigator", "Navigate codebases", "Use find and grep.")])

        reviewer = SkillReviewer(
            system_llm=system_llm,
            free_agent_manager=fam,
            skill_evolution=True,
        )

        ws = GraphEmergenceWorkspace(
            workspace_id="ws-1",
            responsible_agent=responsible,
            call_llm=MagicMock(),
            system_llm=MagicMock(),
            free_agent_manager=fam,
            skill_reviewer=reviewer,
        )

        # Give it a dummy parent result
        ws._last_result = AgentResult(
            output="Done",
            iterations=1,
            termination_reason="done",
            action_history=[],
        )

        ws.post_episode(
            eval_results={"ws-1": {"s_exec": 0.9}},
            evicted_ids=[],
        )

        # Skill should be created
        assert sub_agent.skill is not None
        assert sub_agent.skill.name == "code_navigator"

        # Episode 2: Build new EnvironmentContext, verify skill visible
        ctx = _build_env_context(fam)
        xml = ctx.serialize_to_xml()
        assert "code_navigator" in xml
        assert "explorer-1" in xml


# ===========================================================================
# 19. Failure then success creates skill
# ===========================================================================


@pytest.mark.integration
class TestFailureThenSuccess:
    """Episode 1: s_exec=0 -> no skill.
    Episode 2: s_exec=1.0 -> skill created."""

    def test_failure_then_success(self):
        log = _log()
        pe = PricingEngine(training_log=log)
        fam = FreeAgentManager(pricing_engine=pe)

        sub_agent = Agent(
            agent_id="worker-1",
            soul=Soul(system_prompt="Worker."),
            agent_type="free",
            protected_by="lead-1",
        )
        fam.register(sub_agent)

        system_llm = _make_system_llm([_skill_json("fixer", "Fix bugs", "Debug and fix.")])
        reviewer = SkillReviewer(
            system_llm=system_llm,
            free_agent_manager=fam,
            skill_evolution=True,
        )

        history = [
            ActionRecord(action_name="bash", arguments={"command": "ls"},
                         result="main.py", timestamp=1.0),
        ]

        # Episode 1: s_exec=0 -> no skill
        reviewer.review(sub_agent, {"s_exec": 0.0}, history)
        assert sub_agent.skill is None

        # Episode 2: s_exec=1.0 -> skill created
        reviewer.review(sub_agent, {"s_exec": 1.0}, history)
        assert sub_agent.skill is not None
        assert sub_agent.skill.name == "fixer"


# ===========================================================================
# 20. Multiple agents accumulate
# ===========================================================================


@pytest.mark.integration
class TestMultipleAgentsAccumulate:
    """Spawn agent A (ep1), spawn agent B (ep2) -> marketplace shows both."""

    def test_multiple_agents_both_have_skills(self):
        log = _log()
        pe = PricingEngine(training_log=log)
        fam = FreeAgentManager(pricing_engine=pe)

        # Agent A
        agent_a = Agent(
            agent_id="agent-a",
            soul=Soul(system_prompt="Agent A."),
            agent_type="free",
            protected_by="lead-1",
        )
        fam.register(agent_a)

        # Agent B
        agent_b = Agent(
            agent_id="agent-b",
            soul=Soul(system_prompt="Agent B."),
            agent_type="free",
            protected_by="lead-1",
        )
        fam.register(agent_b)

        history = [
            ActionRecord(action_name="search_code", arguments={"pattern": "err"},
                         result="Found 1 match", timestamp=1.0),
        ]

        system_llm_a = _make_system_llm([_skill_json("searcher", "Search expert", "grep...")])
        reviewer_a = SkillReviewer(
            system_llm=system_llm_a,
            free_agent_manager=fam,
            skill_evolution=True,
        )
        reviewer_a.review(agent_a, {"s_exec": 0.7}, history)

        system_llm_b = _make_system_llm([_skill_json("editor", "Edit expert", "vim...")])
        reviewer_b = SkillReviewer(
            system_llm=system_llm_b,
            free_agent_manager=fam,
            skill_evolution=True,
        )
        reviewer_b.review(agent_b, {"s_exec": 0.8}, history)

        # Both should have skills
        assert agent_a.skill is not None
        assert agent_b.skill is not None
        assert agent_a.skill.name == "searcher"
        assert agent_b.skill.name == "editor"

        # Marketplace should show both
        ctx = _build_env_context(fam)
        xml = ctx.serialize_to_xml()
        assert "searcher" in xml
        assert "editor" in xml
