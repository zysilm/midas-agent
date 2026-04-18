"""Integration tests for agent discovery and hiring with skills.

Tests verify LLM-driven agent selection: hiring existing skilled agents
and spawning new agents when no match exists.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

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
from midas_agent.stdlib.react_agent import ActionRecord
from midas_agent.workspace.graph_emergence.agent import Agent, Soul
from midas_agent.workspace.graph_emergence.free_agent_manager import FreeAgentManager
from midas_agent.workspace.graph_emergence.pricing import PricingEngine
from midas_agent.workspace.graph_emergence.skill import Skill
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


def _parent_actions():
    return [
        BashAction(),
        ReadFileAction(),
        EditFileAction(),
        WriteFileAction(),
        SearchCodeAction(),
        FindFilesAction(),
        TaskDoneAction(),
        DelegateTaskAction(find_candidates=lambda d: []),
    ]


# ===========================================================================
# 15. LLM hires existing agent
# ===========================================================================


@pytest.mark.integration
class TestLLMHiresExisting:
    """Set up marketplace with skilled agent. Mock LLM returns
    use_agent(agent_id='expert-1'). Verify agent runs with skill.content."""

    def test_hire_existing_agent_with_skill(self):
        log = _log()
        pe = PricingEngine(training_log=log)
        fam = FreeAgentManager(pricing_engine=pe)

        expert = Agent(
            agent_id="expert-1",
            soul=Soul(system_prompt="I am an expert debugger."),
            agent_type="free",
            skill=Skill(
                name="debug_expert",
                description="Expert at debugging Python errors",
                content="## Debugging Procedure\n1. Check error logs\n2. Reproduce\n3. Fix",
            ),
        )
        fam.register(expert)

        captured_system_prompts = []

        def sub_llm(request):
            for m in request.messages:
                if m.get("role") == "system":
                    captured_system_prompts.append(m["content"])
            return _r(tool_calls=[
                ToolCall(id="t1", name="report_result",
                         arguments={"result": "Fixed the bug."}),
            ])

        action = DelegateTaskAction(
            find_candidates=lambda desc: fam.match(desc),
            call_llm=sub_llm,
            parent_actions=_parent_actions(),
        )

        result = action.execute(
            task_description="Debug the crash in main.py",
            agent_id="expert-1",
        )

        # The system prompt should contain the skill content
        assert len(captured_system_prompts) >= 1
        sys_prompt = captured_system_prompts[0]
        assert "Debugging Procedure" in sys_prompt or "Check error logs" in sys_prompt


# ===========================================================================
# 16. LLM spawns when no match
# ===========================================================================


@pytest.mark.integration
class TestLLMSpawnsWhenNoMatch:
    """Marketplace has irrelevant agents. Mock LLM returns spawn.
    Verify new agent spawned."""

    def test_spawn_new_when_no_relevant_agents(self):
        log = _log()
        pe = PricingEngine(training_log=log)
        fam = FreeAgentManager(pricing_engine=pe)

        # Register an irrelevant agent
        irrelevant = Agent(
            agent_id="chef-1",
            soul=Soul(system_prompt="I cook food."),
            agent_type="free",
            skill=Skill(name="cooking", description="Expert chef", content="Cook pasta."),
        )
        fam.register(irrelevant)

        spawned = []

        def spawn_cb(desc):
            agent = Agent(
                agent_id=f"spawned-{len(spawned)}",
                soul=Soul(system_prompt=f"Specialist: {desc}"),
                agent_type="free",
                protected_by="lead-1",
            )
            spawned.append(agent)
            fam.register(agent)
            return agent

        def sub_llm(req):
            return _r(tool_calls=[
                ToolCall(id="t1", name="report_result",
                         arguments={"result": "Found the bug."}),
            ])

        action = DelegateTaskAction(
            find_candidates=lambda desc: fam.match(desc),
            spawn_callback=spawn_cb,
            call_llm=sub_llm,
            calling_agent_id="lead-1",
            parent_actions=_parent_actions(),
        )

        result = action.execute(
            task_description="Fix the Python import error",
            spawn=["explorer: investigate imports"],
        )

        assert len(spawned) == 1
        assert "spawned-0" in fam.free_agents


# ===========================================================================
# 17. Two-step browse then hire
# ===========================================================================


@pytest.mark.integration
class TestBrowseThenHire:
    """Mock LLM first calls use_agent without agent_id/spawn (browse),
    then calls use_agent with agent_id. Verify two separate interactions."""

    def test_browse_then_hire(self):
        log = _log()
        pe = PricingEngine(training_log=log)
        fam = FreeAgentManager(pricing_engine=pe)

        expert = Agent(
            agent_id="expert-1",
            soul=Soul(system_prompt="Expert agent."),
            agent_type="free",
            skill=Skill(name="debug", description="Debugging", content="debug"),
        )
        fam.register(expert)

        # Step 1: Browse (no agent_id, no spawn)
        action = DelegateTaskAction(
            find_candidates=lambda desc: fam.match(desc),
            call_llm=MagicMock(),
            parent_actions=_parent_actions(),
        )

        browse_result = action.execute(
            task_description="Find an agent for debugging",
        )
        assert "expert-1" in browse_result or "Candidates" in browse_result

        # Step 2: Hire the agent by ID
        def hire_llm(req):
            return _r(tool_calls=[
                ToolCall(id="t1", name="report_result",
                         arguments={"result": "Debugging complete."}),
            ])

        action2 = DelegateTaskAction(
            find_candidates=lambda desc: fam.match(desc),
            call_llm=hire_llm,
            parent_actions=_parent_actions(),
        )

        hire_result = action2.execute(
            task_description="Debug the crash",
            agent_id="expert-1",
        )

        assert "Debugging complete" in hire_result or "Result reported" in hire_result
