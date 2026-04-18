"""Unit tests for sub-agent tool assignment.

Design doc 04-04 §3.2:
  - Protected agent (幼年): all basic actions + report_result, NO use_agent
  - Independent free agent: all basic actions + use_agent + report_result
  - Responsible agent: all basic actions + use_agent

The sub-agent's tool set is determined by its status, not by the LLM.
"""
import pytest
from unittest.mock import MagicMock

from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage, ToolCall
from midas_agent.stdlib.actions.bash import BashAction
from midas_agent.stdlib.actions.delegate_task import DelegateTaskAction
from midas_agent.stdlib.actions.str_replace_editor import StrReplaceEditorAction
from midas_agent.stdlib.actions.report_result import ReportResultAction
from midas_agent.stdlib.actions.search import SearchCodeAction, FindFilesAction
from midas_agent.stdlib.actions.task_done import TaskDoneAction
from midas_agent.workspace.graph_emergence.agent import Agent, Soul
from midas_agent.workspace.graph_emergence.free_agent_manager import FreeAgentManager
from midas_agent.workspace.graph_emergence.pricing import PricingEngine
from midas_agent.scheduler.serial_queue import SerialQueue
from midas_agent.scheduler.training_log import TrainingLog
from tests.integration.conftest import InMemoryStorageBackend, SpyHookSet


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(content=None, tool_calls=None):
    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )


def _make_log():
    return TrainingLog(
        storage=InMemoryStorageBackend(),
        hooks=SpyHookSet(),
        serial_queue=SerialQueue(),
    )


def _make_parent_actions():
    """The full action set a responsible agent would have."""
    return [
        BashAction(),
        StrReplaceEditorAction(),
        SearchCodeAction(),
        FindFilesAction(),
        TaskDoneAction(),
        DelegateTaskAction(find_candidates=lambda d: []),
    ]


# ===========================================================================
# Protected agent (幼年) tool set
# ===========================================================================


@pytest.mark.unit
class TestProtectedAgentTools:
    """Spawned (protected) agents get basic tools + report_result,
    but NOT use_agent. Design doc 04-04 §3.2."""

    def test_protected_worker_has_basic_actions(self):
        """Protected worker sub-agent must have basic actions:
        bash, str_replace_editor, search_code, find_files."""
        training_log = _make_log()
        pricing_engine = PricingEngine(training_log=training_log)
        free_agent_manager = FreeAgentManager(pricing_engine=pricing_engine)

        spawned = []
        def spawn_callback(desc):
            agent = Agent(
                agent_id="sub-1",
                soul=Soul(system_prompt="Sub"),
                agent_type="free",
                protected_by="lead-1",
            )
            spawned.append(agent)
            free_agent_manager.register(agent)
            return agent

        captured_tools = []
        def capturing_llm(request: LLMRequest) -> LLMResponse:
            # Extract tool names from the request
            if request.tools:
                captured_tools.extend([t["function"]["name"] for t in request.tools])
            return _make_response(content="Analysis complete.")

        action = DelegateTaskAction(
            find_candidates=lambda desc: free_agent_manager.match(desc),
            spawn_callback=spawn_callback,
            call_llm=capturing_llm,
            parent_actions=_make_parent_actions(),
        )

        action.execute(
            task_description="Analyze the bug",
            spawn=["worker: analyzer"],
        )

        basic_tools = {"bash", "str_replace_editor", "search_code", "find_files"}
        for tool in basic_tools:
            assert tool in captured_tools, (
                f"Protected sub-agent must have '{tool}' tool. "
                f"Got: {captured_tools}"
            )

    def test_protected_agent_has_report_result(self):
        """Protected sub-agent must have report_result to communicate
        findings back to the caller."""
        training_log = _make_log()
        pricing_engine = PricingEngine(training_log=training_log)
        free_agent_manager = FreeAgentManager(pricing_engine=pricing_engine)

        spawned = []
        def spawn_callback(desc):
            agent = Agent(
                agent_id="sub-1",
                soul=Soul(system_prompt="Sub"),
                agent_type="free",
                protected_by="lead-1",
            )
            spawned.append(agent)
            free_agent_manager.register(agent)
            return agent

        captured_tools = []
        def capturing_llm(request: LLMRequest) -> LLMResponse:
            if request.tools:
                captured_tools.extend([t["function"]["name"] for t in request.tools])
            return _make_response(content="Done.")

        action = DelegateTaskAction(
            find_candidates=lambda desc: free_agent_manager.match(desc),
            spawn_callback=spawn_callback,
            call_llm=capturing_llm,
            parent_actions=_make_parent_actions(),
        )

        action.execute(
            task_description="Find the bug",
            spawn=["searcher"],
        )

        assert "report_result" in captured_tools, (
            f"Protected agent must have report_result. Got: {captured_tools}"
        )

    def test_protected_agent_does_not_have_use_agent(self):
        """Protected sub-agent must NOT have use_agent tool.
        Design doc: '被保护agent不可再spawn'."""
        training_log = _make_log()
        pricing_engine = PricingEngine(training_log=training_log)
        free_agent_manager = FreeAgentManager(pricing_engine=pricing_engine)

        spawned = []
        def spawn_callback(desc):
            agent = Agent(
                agent_id="sub-1",
                soul=Soul(system_prompt="Sub"),
                agent_type="free",
                protected_by="lead-1",
            )
            spawned.append(agent)
            free_agent_manager.register(agent)
            return agent

        captured_tools = []
        def capturing_llm(request: LLMRequest) -> LLMResponse:
            if request.tools:
                captured_tools.extend([t["function"]["name"] for t in request.tools])
            return _make_response(content="Done.")

        action = DelegateTaskAction(
            find_candidates=lambda desc: free_agent_manager.match(desc),
            spawn_callback=spawn_callback,
            call_llm=capturing_llm,
            parent_actions=_make_parent_actions(),
        )

        action.execute(
            task_description="Analyze code",
            spawn=["analyzer"],
        )

        assert "use_agent" not in captured_tools, (
            f"Protected agent must NOT have use_agent. Got: {captured_tools}"
        )

    def test_protected_agent_does_not_have_task_done(self):
        """Protected sub-agent should not have task_done — only the
        responsible agent ends the workspace. Sub-agents use report_result."""
        training_log = _make_log()
        pricing_engine = PricingEngine(training_log=training_log)
        free_agent_manager = FreeAgentManager(pricing_engine=pricing_engine)

        spawned = []
        def spawn_callback(desc):
            agent = Agent(
                agent_id="sub-1",
                soul=Soul(system_prompt="Sub"),
                agent_type="free",
                protected_by="lead-1",
            )
            spawned.append(agent)
            free_agent_manager.register(agent)
            return agent

        captured_tools = []
        def capturing_llm(request: LLMRequest) -> LLMResponse:
            if request.tools:
                captured_tools.extend([t["function"]["name"] for t in request.tools])
            return _make_response(content="Done.")

        action = DelegateTaskAction(
            find_candidates=lambda desc: free_agent_manager.match(desc),
            spawn_callback=spawn_callback,
            call_llm=capturing_llm,
            parent_actions=_make_parent_actions(),
        )

        action.execute(
            task_description="Search code",
            spawn=["searcher"],
        )

        assert "task_done" not in captured_tools, (
            f"Protected agent must NOT have task_done. Got: {captured_tools}"
        )


# ===========================================================================
# Sub-agent actually uses tools
# ===========================================================================


@pytest.mark.unit
class TestSubAgentUsesTools:
    """Sub-agent must be able to actually invoke tools, not just have
    them listed."""

    def test_sub_agent_calls_search_code(self):
        """Sub-agent can call search_code and the result is included
        in its findings returned to the caller."""
        training_log = _make_log()
        pricing_engine = PricingEngine(training_log=training_log)
        free_agent_manager = FreeAgentManager(pricing_engine=pricing_engine)

        spawned = []
        def spawn_callback(desc):
            agent = Agent(
                agent_id="sub-1",
                soul=Soul(system_prompt="You are a code searcher."),
                agent_type="free",
                protected_by="lead-1",
            )
            spawned.append(agent)
            free_agent_manager.register(agent)
            return agent

        call_count = 0
        def sub_llm(request: LLMRequest) -> LLMResponse:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Sub-agent's first action: search for code
                return _make_response(
                    tool_calls=[ToolCall(
                        id="sc1",
                        name="search_code",
                        arguments={"pattern": "def separability_matrix"},
                    )],
                )
            else:
                # After seeing search results, report finding
                return _make_response(
                    content="Found separability_matrix in separable.py"
                )

        action = DelegateTaskAction(
            find_candidates=lambda desc: free_agent_manager.match(desc),
            spawn_callback=spawn_callback,
            call_llm=sub_llm,
            parent_actions=_make_parent_actions(),
        )

        result = action.execute(
            task_description="Find where separability_matrix is defined",
            spawn=["code searcher"],
        )

        assert call_count >= 2, "Sub-agent should have made multiple LLM calls"
        assert "separability_matrix" in result or "separable.py" in result, (
            f"Sub-agent's search findings should be in result. Got: {result}"
        )

    def test_sub_agent_calls_view_file(self, tmp_path):
        """Sub-agent can call str_replace_editor view on actual files."""
        (tmp_path / "target.py").write_text("def bug():\n    return None\n")

        training_log = _make_log()
        pricing_engine = PricingEngine(training_log=training_log)
        free_agent_manager = FreeAgentManager(pricing_engine=pricing_engine)

        spawned = []
        def spawn_callback(desc):
            agent = Agent(
                agent_id="sub-1",
                soul=Soul(system_prompt="You are a file reader."),
                agent_type="free",
                protected_by="lead-1",
            )
            spawned.append(agent)
            free_agent_manager.register(agent)
            return agent

        call_count = 0
        def sub_llm(request: LLMRequest) -> LLMResponse:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_response(
                    tool_calls=[ToolCall(
                        id="rf1",
                        name="str_replace_editor",
                        arguments={"command": "view", "path": str(tmp_path / "target.py")},
                    )],
                )
            else:
                return _make_response(content="File contains a bug function.")

        # Parent actions with cwd set
        parent_actions = [
            BashAction(cwd=str(tmp_path)),
            StrReplaceEditorAction(cwd=str(tmp_path)),
            SearchCodeAction(cwd=str(tmp_path)),
            FindFilesAction(cwd=str(tmp_path)),
            TaskDoneAction(),
            DelegateTaskAction(find_candidates=lambda d: []),
        ]

        action = DelegateTaskAction(
            find_candidates=lambda desc: free_agent_manager.match(desc),
            spawn_callback=spawn_callback,
            call_llm=sub_llm,
            parent_actions=parent_actions,
        )

        result = action.execute(
            task_description="Read target.py and describe it",
            spawn=["file reader"],
        )

        assert call_count >= 2
        assert "bug" in result.lower() or "function" in result.lower(), (
            f"Sub-agent should have read the file. Got: {result}"
        )


# ===========================================================================
# Independent (free) agent tool set
# ===========================================================================


@pytest.mark.unit
class TestIndependentAgentTools:
    """Independent free agents (not protected) can spawn sub-agents.
    Design doc 04-04 §3.4: once promoted, can spawn."""

    def test_independent_agent_has_use_agent(self):
        """An independent free agent (protected_by=None) hired via
        agent_id should have use_agent in its tool set."""
        training_log = _make_log()
        pricing_engine = PricingEngine(training_log=training_log)
        free_agent_manager = FreeAgentManager(pricing_engine=pricing_engine)

        # Register an independent agent (not protected)
        independent = Agent(
            agent_id="indie-1",
            soul=Soul(system_prompt="Independent agent"),
            agent_type="free",
            protected_by=None,
        )
        free_agent_manager.register(independent)

        captured_tools = []
        def capturing_llm(request: LLMRequest) -> LLMResponse:
            if request.tools:
                captured_tools.extend([t["function"]["name"] for t in request.tools])
            return _make_response(content="Done.")

        action = DelegateTaskAction(
            find_candidates=lambda desc: free_agent_manager.match(desc),
            call_llm=capturing_llm,
            parent_actions=_make_parent_actions(),
        )

        action.execute(
            task_description="Do work",
            agent_id="indie-1",
        )

        assert "use_agent" in captured_tools, (
            f"Independent agent must have use_agent. Got: {captured_tools}"
        )
