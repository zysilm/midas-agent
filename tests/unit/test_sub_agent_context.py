"""Tests for sub-agent context inheritance."""
import pytest

from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage, ToolCall
from midas_agent.scheduler.hiring_manager import HiringManager
from midas_agent.stdlib.actions.delegate_task import DelegateTaskAction
from midas_agent.stdlib.actions.bash import BashAction
from midas_agent.stdlib.actions.str_replace_editor import StrReplaceEditorAction
from midas_agent.stdlib.actions.task_done import TaskDoneAction
from midas_agent.stdlib.actions.report_result import ReportResultAction
from midas_agent.workspace.graph_emergence.agent import Agent, Soul
from midas_agent.workspace.graph_emergence.free_agent_manager import FreeAgentManager
from midas_agent.workspace.graph_emergence.pricing import PricingEngine
from midas_agent.scheduler.serial_queue import SerialQueue
from midas_agent.scheduler.training_log import TrainingLog
from tests.integration.conftest import InMemoryStorageBackend, SpyHookSet


def _make_response(content=None, tool_calls=None):
    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        usage=TokenUsage(input_tokens=100, output_tokens=50),
    )


def _make_log():
    return TrainingLog(
        storage=InMemoryStorageBackend(),
        hooks=SpyHookSet(),
        serial_queue=SerialQueue(),
    )


def _make_hiring_manager(*, fake_llm, parent_system_prompt="test",
                          parent_actions=None, spawn_factory=None):
    """Build a HiringManager for testing."""
    log = _make_log()
    pe = PricingEngine(training_log=log)
    fam = FreeAgentManager(pricing_engine=pe)

    if spawn_factory is None:
        def spawn_factory(desc):
            return Agent(
                agent_id="spawned-test",
                soul=Soul(system_prompt=parent_system_prompt),
                agent_type="free",
                protected_by="parent-1",
            )

    system_llm = lambda req: _make_response(content='{"action": "spawn", "role": "explorer"}')

    if parent_actions is None:
        parent_actions = [
            BashAction(cwd="/testbed"),
            StrReplaceEditorAction(cwd="/testbed"),
            TaskDoneAction(),
        ]

    hm = HiringManager(
        system_llm=system_llm,
        free_agent_manager=fam,
        spawn_callback=spawn_factory,
        call_llm=fake_llm,
        parent_actions=parent_actions,
        parent_system_prompt=parent_system_prompt,
    )
    return hm


class TestSubAgentGetsParentSystemPrompt:
    """Sub-agents must receive the parent's system prompt, not a generic one."""

    def test_sub_agent_gets_parent_system_prompt(self):
        """When HiringManager spawns a sub-agent, the ReactAgent uses
        the parent's system prompt."""
        captured = {}

        def fake_llm(request: LLMRequest) -> LLMResponse:
            if request.messages and request.messages[0]["role"] == "system":
                captured["system_prompt"] = request.messages[0]["content"]
            return _make_response(
                tool_calls=[ToolCall(
                    id="c1", name="report_result",
                    arguments={"result": "found it at /testbed/foo.py"},
                )],
            )

        parent_system_prompt = "You are a coding agent that solves issues..."

        hm = _make_hiring_manager(
            fake_llm=fake_llm,
            parent_system_prompt=parent_system_prompt,
        )

        action = DelegateTaskAction(hiring_manager=hm)
        action.execute(task="Find where foo is defined")

        assert "system_prompt" in captured, "Sub-agent LLM should have been called"
        assert captured["system_prompt"] == parent_system_prompt, (
            f"Sub-agent should get parent's system prompt, got: {captured['system_prompt'][:100]}"
        )


class TestSubAgentMaxIterations:
    """Sub-agents must be capped at 20 iterations."""

    def test_sub_agent_max_iterations_capped(self):
        """Sub-agent ReactAgent is created with max_iterations=20."""
        call_count = {"n": 0}

        def fake_llm(request: LLMRequest) -> LLMResponse:
            call_count["n"] += 1
            if call_count["n"] >= 25:
                return _make_response(
                    tool_calls=[ToolCall(
                        id="done", name="report_result",
                        arguments={"result": "done"},
                    )],
                )
            return _make_response(
                tool_calls=[ToolCall(
                    id=f"c{call_count['n']}", name="bash",
                    arguments={"command": "echo hello"},
                )],
            )

        hm = _make_hiring_manager(
            fake_llm=fake_llm,
            parent_system_prompt="test",
        )

        action = DelegateTaskAction(hiring_manager=hm)
        action.execute(task="Investigate the bug")

        # Sub-agent should stop at 20 iterations, not run to 9999
        assert call_count["n"] <= 20, (
            f"Sub-agent ran {call_count['n']} iterations, should be capped at 20"
        )
