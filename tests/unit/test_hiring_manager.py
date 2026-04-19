"""Unit tests for HiringManager — SystemLLM-driven agent selection."""
from __future__ import annotations

import json

import pytest

from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage, ToolCall
from midas_agent.scheduler.hiring_manager import HiringManager
from midas_agent.stdlib.actions.bash import BashAction
from midas_agent.stdlib.actions.str_replace_editor import StrReplaceEditorAction
from midas_agent.stdlib.actions.task_done import TaskDoneAction
from midas_agent.stdlib.actions.delegate_task import DelegateTaskAction
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
        StrReplaceEditorAction(),
        TaskDoneAction(),
        DelegateTaskAction(hiring_manager=None),
    ]


def _make_hiring_manager(
    *,
    system_llm=None,
    sub_llm=None,
    agents=None,
    parent_actions=None,
    parent_system_prompt="You are a coding agent.",
    training_log=None,
    evicted_ws_ids=None,
):
    """Build a HiringManager with sensible defaults."""
    log = training_log or _log()
    pe = PricingEngine(training_log=log)
    fam = FreeAgentManager(pricing_engine=pe)

    if agents:
        for agent in agents:
            fam.register(agent)

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

    # Default sub_llm that just reports done
    if sub_llm is None:
        sub_llm = lambda req: _r(
            tool_calls=[ToolCall(id="t1", name="report_result",
                                 arguments={"result": "Task done."})]
        )

    # Default system_llm that always spawns
    if system_llm is None:
        system_llm = lambda req: _r(content='{"action": "spawn", "role": "explorer"}')

    hm = HiringManager(
        system_llm=system_llm,
        free_agent_manager=fam,
        spawn_callback=spawn_cb,
        call_llm=sub_llm,
        parent_actions=parent_actions or _parent_actions(),
        parent_system_prompt=parent_system_prompt,
        training_log=training_log,
        evicted_ws_ids=evicted_ws_ids,
    )
    return hm, fam, spawned


# ===========================================================================
# Tests
# ===========================================================================


@pytest.mark.unit
class TestEmptyRosterSpawnsNew:
    """When there are no free agents, SystemLLM should spawn a new one."""

    def test_empty_roster_spawns_new(self):
        hm, fam, spawned = _make_hiring_manager()
        result = hm.delegate("Find the bug in foo.py")

        assert len(spawned) == 1
        assert "spawned-0" in fam.free_agents


@pytest.mark.unit
class TestSystemLLMPicksHire:
    """SystemLLM returns hire action with a valid agent_id."""

    def test_systemllm_picks_hire(self):
        expert = Agent(
            agent_id="expert-1",
            soul=Soul(system_prompt="I am an expert."),
            agent_type="free",
            skill=Skill(name="debug", description="Debug expert", content="debug steps"),
        )

        captured_system_prompts = []

        def sub_llm(req):
            for m in req.messages:
                if m.get("role") == "system":
                    captured_system_prompts.append(m["content"])
            return _r(
                tool_calls=[ToolCall(id="t1", name="report_result",
                                     arguments={"result": "Bug found in line 42."})]
            )

        def system_llm(req):
            return _r(content='{"action": "hire", "agent_id": "expert-1"}')

        hm, fam, spawned = _make_hiring_manager(
            system_llm=system_llm,
            sub_llm=sub_llm,
            agents=[expert],
        )

        result = hm.delegate("Debug the crash")

        # Should NOT spawn a new agent
        assert len(spawned) == 0
        assert "Bug found" in result or "line 42" in result


@pytest.mark.unit
class TestSystemLLMPicksSpawn:
    """SystemLLM returns spawn action."""

    def test_systemllm_picks_spawn(self):
        def system_llm(req):
            return _r(content='{"action": "spawn", "role": "worker"}')

        hm, fam, spawned = _make_hiring_manager(system_llm=system_llm)
        result = hm.delegate("Fix the bug in bar.py")

        assert len(spawned) == 1
        assert "Task done" in result or "Sub-agent" in result


@pytest.mark.unit
class TestMalformedJsonFallback:
    """Malformed SystemLLM response falls back to spawn explorer."""

    def test_malformed_json_fallback(self):
        def system_llm(req):
            return _r(content="I think you should hire agent X, but this isn't valid JSON.")

        hm, fam, spawned = _make_hiring_manager(system_llm=system_llm)
        result = hm.delegate("Investigate the issue")

        # Should fallback to spawn
        assert len(spawned) == 1


@pytest.mark.unit
class TestHiredAgentGetsSkillContent:
    """Hired agent's system prompt includes its skill.content."""

    def test_hired_agent_gets_skill_content(self):
        expert = Agent(
            agent_id="expert-1",
            soul=Soul(system_prompt="I am an expert."),
            agent_type="free",
            skill=Skill(
                name="debug",
                description="Debug expert",
                content="## Debugging Procedure\n1. Check error logs\n2. Reproduce\n3. Fix",
            ),
        )

        captured_system_prompts = []

        def sub_llm(req):
            for m in req.messages:
                if m.get("role") == "system":
                    captured_system_prompts.append(m["content"])
            return _r(
                tool_calls=[ToolCall(id="t1", name="report_result",
                                     arguments={"result": "Fixed."})]
            )

        def system_llm(req):
            return _r(content='{"action": "hire", "agent_id": "expert-1"}')

        hm, _, _ = _make_hiring_manager(
            system_llm=system_llm,
            sub_llm=sub_llm,
            agents=[expert],
        )

        hm.delegate("Debug the crash")

        assert len(captured_system_prompts) >= 1
        sys_prompt = captured_system_prompts[0]
        assert "Debugging Procedure" in sys_prompt


@pytest.mark.unit
class TestSpawnedAgentGetsParentPrompt:
    """Spawned agent receives the parent's system prompt."""

    def test_spawned_agent_gets_parent_prompt(self):
        parent_prompt = "You are a senior debugging agent specializing in Python."
        captured_system_prompts = []

        def sub_llm(req):
            for m in req.messages:
                if m.get("role") == "system":
                    captured_system_prompts.append(m["content"])
            return _r(
                tool_calls=[ToolCall(id="t1", name="report_result",
                                     arguments={"result": "Done."})]
            )

        hm, _, _ = _make_hiring_manager(
            sub_llm=sub_llm,
            parent_system_prompt=parent_prompt,
        )

        hm.delegate("Find the root cause")

        assert len(captured_system_prompts) >= 1
        assert captured_system_prompts[0] == parent_prompt


@pytest.mark.unit
class TestProtectionStripsUseAgent:
    """Protected agents (protected_by is set) do not get use_agent or task_done."""

    def test_protection_strips_use_agent(self):
        captured_tools = []

        def sub_llm(req):
            if req.tools:
                captured_tools.extend([t["function"]["name"] for t in req.tools])
            return _r(content="Done.")

        hm, _, spawned = _make_hiring_manager(sub_llm=sub_llm)
        hm.delegate("Analyze the code")

        # Spawned agents are protected (protected_by="lead-1")
        assert len(spawned) == 1
        assert "use_agent" not in captured_tools
        assert "task_done" not in captured_tools
        assert "report_result" in captured_tools
