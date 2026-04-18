"""Unit tests for skill pipeline: action history capture in delegate_task.

Tests verify that sub-agent action histories are captured and propagated
to the skill reviewer during post_episode.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

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


def _make_delegate(**kwargs):
    """Build a DelegateTaskAction with sensible defaults."""
    log = _log()
    pe = PricingEngine(training_log=log)
    fam = FreeAgentManager(pricing_engine=pe)

    spawned = []

    def spawn_cb(desc):
        agent = Agent(
            agent_id=f"sub-{len(spawned)}",
            soul=Soul(system_prompt="Sub"),
            agent_type="free",
            protected_by="lead-1",
        )
        spawned.append(agent)
        fam.register(agent)
        return agent

    defaults = dict(
        find_candidates=lambda desc: fam.match(desc),
        spawn_callback=spawn_cb,
        parent_actions=_parent_actions(),
    )
    defaults.update(kwargs)
    return DelegateTaskAction(**defaults), fam, spawned


# ===========================================================================
# 1. Spawn captures history
# ===========================================================================


@pytest.mark.unit
class TestSpawnCapturesHistory:
    """After spawn, the spawned agent must have _last_action_history set."""

    def test_spawn_captures_action_history(self):
        """DelegateTaskAction spawns sub-agent; after execute(),
        spawned agent._last_action_history exists and is non-empty."""
        call_count = 0

        def llm(req):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _r(tool_calls=[
                    ToolCall(id="t1", name="bash",
                             arguments={"command": "find . -name '*.py'"}),
                ])
            return _r(tool_calls=[
                ToolCall(id="t2", name="report_result",
                         arguments={"result": "Found bug in main.py"}),
            ])

        action, fam, spawned = _make_delegate(call_llm=llm)
        action.execute(
            task_description="Find the bug",
            spawn=["explorer: find bugs"],
        )

        assert len(spawned) == 1
        agent = spawned[0]
        assert hasattr(agent, "_last_action_history")
        assert len(agent._last_action_history) > 0

    def test_history_has_correct_actions(self):
        """Sub-agent mock LLM returns [find_files, read_file, report_result].
        Verify _last_action_history has 3 ActionRecords with those names."""
        call_idx = 0

        def llm(req):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 1:
                return _r(tool_calls=[
                    ToolCall(id="t1", name="find_files",
                             arguments={"pattern": "*.py"}),
                ])
            if call_idx == 2:
                return _r(tool_calls=[
                    ToolCall(id="t2", name="read_file",
                             arguments={"path": "main.py"}),
                ])
            return _r(tool_calls=[
                ToolCall(id="t3", name="report_result",
                         arguments={"result": "Analysis complete."}),
            ])

        action, fam, spawned = _make_delegate(call_llm=llm)
        action.execute(
            task_description="Analyze the code",
            spawn=["explorer: analyzer"],
        )

        agent = spawned[0]
        history = agent._last_action_history
        assert len(history) == 3
        assert history[0].action_name == "find_files"
        assert history[1].action_name == "read_file"
        assert history[2].action_name == "report_result"

    def test_max_iterations_captures_partial_history(self):
        """Mock LLM never calls report_result, sub-agent hits max_iterations=20.
        Verify partial history is still captured."""
        call_count = 0

        def llm(req):
            nonlocal call_count
            call_count += 1
            # Never calls report_result -- always calls bash
            return _r(tool_calls=[
                ToolCall(id=f"t{call_count}", name="bash",
                         arguments={"command": f"echo step {call_count}"}),
            ])

        action, fam, spawned = _make_delegate(call_llm=llm)
        action.execute(
            task_description="Infinite loop task",
            spawn=["explorer: looper"],
        )

        agent = spawned[0]
        assert hasattr(agent, "_last_action_history")
        # Should have partial history (up to max_iterations=20)
        assert len(agent._last_action_history) > 0


# ===========================================================================
# 2. Hire captures history
# ===========================================================================


@pytest.mark.unit
class TestHireCapturesHistory:
    """After hire, target agent must have _last_action_history set."""

    def test_hire_captures_action_history(self):
        """Register an agent in FreeAgentManager, DelegateTaskAction hires it.
        After execute(), target._last_action_history exists."""
        log = _log()
        pe = PricingEngine(training_log=log)
        fam = FreeAgentManager(pricing_engine=pe)

        target_agent = Agent(
            agent_id="expert-1",
            soul=Soul(system_prompt="I am an expert."),
            agent_type="free",
            skill=Skill(name="debug", description="Debugging", content="debug"),
        )
        fam.register(target_agent)

        def llm(req):
            return _r(tool_calls=[
                ToolCall(id="t1", name="report_result",
                         arguments={"result": "Bug found."}),
            ])

        action = DelegateTaskAction(
            find_candidates=lambda desc: fam.match(desc),
            call_llm=llm,
            parent_actions=_parent_actions(),
        )

        action.execute(
            task_description="Fix the bug",
            agent_id="expert-1",
        )

        assert hasattr(target_agent, "_last_action_history")
        assert len(target_agent._last_action_history) > 0


# ===========================================================================
# 5. post_episode uses sub-agent history
# ===========================================================================


@pytest.mark.unit
class TestPostEpisodeUsesSubHistory:
    """post_episode must pass sub-agent's own action history to SkillReviewer,
    not the parent's."""

    def test_post_episode_passes_sub_agent_history(self):
        """Create workspace with spawned agent. Set agent._last_action_history.
        Call post_episode. Mock SkillReviewer and verify it receives the
        sub-agent's history, not the parent's."""
        log = _log()
        pe = PricingEngine(training_log=log)
        fam = FreeAgentManager(pricing_engine=pe)

        responsible = Agent(
            agent_id="lead-1",
            soul=Soul(system_prompt="Lead agent."),
            agent_type="workspace_bound",
        )

        # Create a spawned sub-agent with protected_by
        sub_agent = Agent(
            agent_id="sub-1",
            soul=Soul(system_prompt="Sub agent."),
            agent_type="free",
            protected_by="lead-1",
        )
        fam.register(sub_agent)

        # Set the sub-agent's own action history
        sub_history = [
            ActionRecord(
                action_name="search_code",
                arguments={"pattern": "bug"},
                result="Found 3 matches",
                timestamp=1.0,
            ),
            ActionRecord(
                action_name="read_file",
                arguments={"path": "fix.py"},
                result="def fix(): ...",
                timestamp=2.0,
            ),
        ]
        sub_agent._last_action_history = sub_history

        # Mock the skill reviewer to capture calls
        mock_reviewer = MagicMock(spec=SkillReviewer)

        ws = GraphEmergenceWorkspace(
            workspace_id="ws-1",
            responsible_agent=responsible,
            call_llm=MagicMock(),
            system_llm=MagicMock(),
            free_agent_manager=fam,
            skill_reviewer=mock_reviewer,
        )

        # Set a dummy last_result (parent's history)
        from midas_agent.stdlib.react_agent import AgentResult
        parent_history = [
            ActionRecord(
                action_name="task_done",
                arguments={},
                result="Done.",
                timestamp=10.0,
            ),
        ]
        ws._last_result = AgentResult(
            output="Done",
            iterations=1,
            termination_reason="done",
            action_history=parent_history,
        )

        ws.post_episode(
            eval_results={"ws-1": {"s_exec": 0.8}},
            evicted_ids=[],
        )

        # SkillReviewer.review should have been called for the sub-agent
        # with the sub-agent's own history, not parent's
        review_calls = mock_reviewer.review.call_args_list
        # At least 2 calls: one for responsible agent, one for sub-agent
        assert len(review_calls) >= 2

        # Find the call for sub-1
        sub_call = None
        for call in review_calls:
            agent_arg = call[0][0]  # first positional arg
            if getattr(agent_arg, "agent_id", None) == "sub-1":
                sub_call = call
                break

        assert sub_call is not None, "SkillReviewer.review must be called for sub-agent"
        # The third positional arg should be the sub-agent's history
        passed_history = sub_call[0][2]
        assert len(passed_history) == 2
        assert passed_history[0].action_name == "search_code"
        assert passed_history[1].action_name == "read_file"
