"""Unit + Integration tests for delegation v2: roles, fixed instructions,
report_result wiring, and briefing guidance.

Four features tested:
  1. Role system: explorer (read-only) vs worker (full tools)
  2. report_result wiring: stores result, terminates loop, returns to caller
  3. Fixed prefix instructions: sub-agent knows to report back
  4. use_agent description: teaches main agent how to write briefings
"""
import pytest
from unittest.mock import MagicMock

from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage, ToolCall
from midas_agent.stdlib.actions.bash import BashAction
from midas_agent.stdlib.actions.delegate_task import DelegateTaskAction
from midas_agent.stdlib.actions.file_ops import (
    ReadFileAction, EditFileAction, WriteFileAction,
)
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
# 1. Role system
# ===========================================================================


@pytest.mark.unit
class TestRoleSystem:
    """Explorer role = read-only. Worker role = full tools."""

    def test_explorer_has_search_read_bash(self):
        """Explorer sub-agent has search_code, find_files, read_file, bash."""
        captured_tools = []
        def llm(req):
            if req.tools:
                captured_tools.extend([t["function"]["name"] for t in req.tools])
            return _r(content="Found the bug in line 42.")

        action, _, _ = _make_delegate(call_llm=llm)
        action.execute(
            task_description="Find where function X is defined",
            spawn=["explorer: code searcher"],
        )

        for tool in ["search_code", "find_files", "read_file", "bash"]:
            assert tool in captured_tools, (
                f"Explorer must have '{tool}'. Got: {captured_tools}"
            )

    def test_explorer_no_edit_write(self):
        """Explorer sub-agent does NOT have edit_file or write_file."""
        captured_tools = []
        def llm(req):
            if req.tools:
                captured_tools.extend([t["function"]["name"] for t in req.tools])
            return _r(content="Done.")

        action, _, _ = _make_delegate(call_llm=llm)
        action.execute(
            task_description="Analyze the code",
            spawn=["explorer: analyzer"],
        )

        assert "edit_file" not in captured_tools, (
            "Explorer must NOT have edit_file"
        )
        assert "write_file" not in captured_tools, (
            "Explorer must NOT have write_file"
        )

    def test_explorer_no_use_agent(self):
        """Explorer cannot spawn sub-agents."""
        captured_tools = []
        def llm(req):
            if req.tools:
                captured_tools.extend([t["function"]["name"] for t in req.tools])
            return _r(content="Done.")

        action, _, _ = _make_delegate(call_llm=llm)
        action.execute(
            task_description="Search code",
            spawn=["explorer: searcher"],
        )

        assert "use_agent" not in captured_tools

    def test_worker_has_edit_write(self):
        """Worker sub-agent has edit_file and write_file."""
        captured_tools = []
        def llm(req):
            if req.tools:
                captured_tools.extend([t["function"]["name"] for t in req.tools])
            return _r(content="Fixed.")

        action, _, _ = _make_delegate(call_llm=llm)
        action.execute(
            task_description="Fix the bug in foo.py",
            spawn=["worker: bug fixer"],
        )

        assert "edit_file" in captured_tools, (
            f"Worker must have edit_file. Got: {captured_tools}"
        )
        assert "write_file" in captured_tools

    def test_worker_has_search_read(self):
        """Worker also has search/read tools."""
        captured_tools = []
        def llm(req):
            if req.tools:
                captured_tools.extend([t["function"]["name"] for t in req.tools])
            return _r(content="Done.")

        action, _, _ = _make_delegate(call_llm=llm)
        action.execute(
            task_description="Fix the bug",
            spawn=["worker: fixer"],
        )

        for tool in ["search_code", "read_file", "bash"]:
            assert tool in captured_tools

    def test_default_role_is_explorer(self):
        """When no role prefix, default to explorer (safer, read-only)."""
        captured_tools = []
        def llm(req):
            if req.tools:
                captured_tools.extend([t["function"]["name"] for t in req.tools])
            return _r(content="Done.")

        action, _, _ = _make_delegate(call_llm=llm)
        action.execute(
            task_description="Analyze something",
            spawn=["code analyzer"],  # no "explorer:" or "worker:" prefix
        )

        # Default = explorer = no edit_file
        assert "edit_file" not in captured_tools, (
            "Default role should be explorer (no edit). Got: {captured_tools}"
        )
        assert "read_file" in captured_tools


# ===========================================================================
# 2. report_result wiring
# ===========================================================================


@pytest.mark.unit
class TestReportResultWiring:
    """report_result must store result, terminate loop, return to caller."""

    def test_report_result_terminates_sub_agent(self):
        """When sub-agent calls report_result, its loop stops immediately."""
        call_count = 0
        def llm(req):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _r(tool_calls=[ToolCall(
                    id="r1", name="report_result",
                    arguments={"result": "Bug is in line 42."},
                )])
            # Should never reach here — report_result should stop the loop
            return _r(content="This should not happen.")

        action, _, _ = _make_delegate(call_llm=llm)
        result = action.execute(
            task_description="Find the bug",
            spawn=["explorer: searcher"],
        )

        assert call_count == 1, (
            f"report_result should terminate loop after 1 call, "
            f"but LLM was called {call_count} times"
        )

    def test_report_result_content_returned_to_caller(self):
        """The content passed to report_result is what the caller sees."""
        def llm(req):
            return _r(tool_calls=[ToolCall(
                id="r1", name="report_result",
                arguments={"result": "Root cause: _cstack recursion breaks matrix dims."},
            )])

        action, _, _ = _make_delegate(call_llm=llm)
        result = action.execute(
            task_description="Analyze the bug",
            spawn=["explorer: analyzer"],
        )

        assert "_cstack" in result or "recursion" in result or "matrix" in result, (
            f"report_result content must be returned to caller. Got: {result}"
        )

    def test_no_report_falls_back_to_output(self):
        """If sub-agent finishes without report_result (text response),
        use the text as fallback."""
        def llm(req):
            return _r(content="I found the issue in separable.py.")

        action, _, _ = _make_delegate(call_llm=llm)
        result = action.execute(
            task_description="Find the issue",
            spawn=["explorer: finder"],
        )

        assert "separable.py" in result or "issue" in result.lower(), (
            f"Fallback to text output should work. Got: {result}"
        )

    def test_report_result_has_priority_over_text(self):
        """If sub-agent calls report_result, that takes priority over
        any text content in the LLM response."""
        def llm(req):
            return _r(
                content="Some thinking text...",
                tool_calls=[ToolCall(
                    id="r1", name="report_result",
                    arguments={"result": "DEFINITIVE ANSWER: bug in line 42"},
                )],
            )

        action, _, _ = _make_delegate(call_llm=llm)
        result = action.execute(
            task_description="Find the bug",
            spawn=["explorer: searcher"],
        )

        assert "DEFINITIVE ANSWER" in result or "line 42" in result, (
            f"report_result should take priority. Got: {result}"
        )


# ===========================================================================
# 3. Fixed prefix instructions
# ===========================================================================


@pytest.mark.unit
class TestFixedPrefixInstructions:
    """Sub-agent system prompt must include fixed instructions about
    its role and how to return results."""

    def test_sub_agent_prompt_mentions_report_result(self):
        """Sub-agent's system prompt must mention report_result so it
        knows how to return findings."""
        captured_system_prompts = []
        def llm(req):
            for m in req.messages:
                if m.get("role") == "system":
                    captured_system_prompts.append(m["content"])
            return _r(content="Done.")

        action, _, _ = _make_delegate(call_llm=llm)
        action.execute(
            task_description="Find bugs",
            spawn=["explorer: searcher"],
        )

        assert len(captured_system_prompts) >= 1
        prompt = captured_system_prompts[0]
        assert "report_result" in prompt.lower() or "report" in prompt.lower(), (
            f"Sub-agent prompt must mention report_result. Got: {prompt[:200]}"
        )

    def test_sub_agent_prompt_mentions_subtask_focus(self):
        """Sub-agent prompt must tell it to focus on the assigned subtask,
        not try to solve the entire problem."""
        captured_system_prompts = []
        def llm(req):
            for m in req.messages:
                if m.get("role") == "system":
                    captured_system_prompts.append(m["content"])
            return _r(content="Done.")

        action, _, _ = _make_delegate(call_llm=llm)
        action.execute(
            task_description="Find the root cause",
            spawn=["explorer: analyzer"],
        )

        prompt = captured_system_prompts[0]
        assert "subtask" in prompt.lower() or "assigned task" in prompt.lower() or "focus" in prompt.lower(), (
            f"Sub-agent prompt must mention subtask focus. Got: {prompt[:200]}"
        )

    def test_sub_agent_prompt_not_just_one_line(self):
        """Sub-agent prompt must be substantial, not just
        'You are a specialist agent. Task: ...'"""
        captured_system_prompts = []
        def llm(req):
            for m in req.messages:
                if m.get("role") == "system":
                    captured_system_prompts.append(m["content"])
            return _r(content="Done.")

        action, _, _ = _make_delegate(call_llm=llm)
        action.execute(
            task_description="Analyze code",
            spawn=["explorer: analyzer"],
        )

        prompt = captured_system_prompts[0]
        assert len(prompt) > 200, (
            f"Sub-agent prompt should be substantial (>200 chars), "
            f"got {len(prompt)} chars: {prompt[:100]}"
        )


# ===========================================================================
# 4. use_agent description guidance
# ===========================================================================


@pytest.mark.unit
class TestDescriptionGuidance:
    """use_agent description must teach the main agent how to write
    effective sub-agent briefings."""

    def test_description_mentions_roles(self):
        """Description must mention explorer and worker roles."""
        action, _, _ = _make_delegate()
        desc = action.description
        assert "explorer" in desc.lower(), "Description must mention explorer role"
        assert "worker" in desc.lower(), "Description must mention worker role"

    def test_description_teaches_briefing_writing(self):
        """Description must teach how to write good task_description."""
        action, _, _ = _make_delegate()
        desc = action.description

        # Should mention being specific/concrete
        has_guidance = (
            "specific" in desc.lower() or
            "concrete" in desc.lower() or
            "well-defined" in desc.lower() or
            "self-contained" in desc.lower()
        )
        assert has_guidance, (
            "Description must teach how to write concrete subtask descriptions"
        )

    def test_description_mentions_scope_control(self):
        """Description must teach scope control (e.g., 'report in N words')."""
        action, _, _ = _make_delegate()
        desc = action.description

        has_scope = (
            "report" in desc.lower() or
            "scope" in desc.lower() or
            "focus" in desc.lower() or
            "do not" in desc.lower()
        )
        assert has_scope, (
            "Description must teach scope control for sub-agents"
        )

    def test_description_mentions_report_result(self):
        """Description must mention report_result as the way sub-agents
        return findings."""
        action, _, _ = _make_delegate()
        desc = action.description
        assert "report_result" in desc or "report" in desc.lower(), (
            "Description must mention how sub-agents report back"
        )
