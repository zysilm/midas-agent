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
from midas_agent.scheduler.hiring_manager import HiringManager
from midas_agent.stdlib.actions.bash import BashAction
from midas_agent.stdlib.actions.delegate_task import DelegateTaskAction
from midas_agent.stdlib.actions.str_replace_editor import StrReplaceEditorAction
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
        StrReplaceEditorAction(),
        TaskDoneAction(),
        DelegateTaskAction(hiring_manager=None),
    ]

def _make_hiring_manager(*, call_llm=None, role="explorer", parent_system_prompt=None):
    """Build a HiringManager that always spawns with the given role."""
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

    # SystemLLM always spawns with the requested role
    system_llm = lambda req: _r(content=f'{{"action": "spawn", "role": "{role}"}}')

    hm = HiringManager(
        system_llm=system_llm,
        free_agent_manager=fam,
        spawn_callback=spawn_cb,
        call_llm=call_llm or (lambda req: _r(content="Done.")),
        parent_actions=_parent_actions(),
        parent_system_prompt=parent_system_prompt or "You are a sub-agent working on a subtask. Call report_result when done.",
    )
    return hm, fam, spawned


# ===========================================================================
# 1. Role system
# ===========================================================================


@pytest.mark.unit
class TestRoleSystem:
    """Explorer role = read-only. Worker role = full tools."""

    def test_explorer_has_str_replace_editor_bash(self):
        """Explorer sub-agent has str_replace_editor, bash."""
        captured_tools = []
        def llm(req):
            if req.tools:
                captured_tools.extend([t["function"]["name"] for t in req.tools])
            return _r(content="Found the bug in line 42.")

        hm, _, _ = _make_hiring_manager(call_llm=llm, role="explorer")
        hm.delegate("Find where function X is defined")

        for tool in ["str_replace_editor", "bash"]:
            assert tool in captured_tools, (
                f"Explorer must have '{tool}'. Got: {captured_tools}"
            )

    def test_explorer_no_use_agent(self):
        """Explorer cannot spawn sub-agents (protected)."""
        captured_tools = []
        def llm(req):
            if req.tools:
                captured_tools.extend([t["function"]["name"] for t in req.tools])
            return _r(content="Done.")

        hm, _, _ = _make_hiring_manager(call_llm=llm, role="explorer")
        hm.delegate("Search code")

        assert "use_agent" not in captured_tools

    def test_worker_has_str_replace_editor(self):
        """Worker sub-agent has str_replace_editor."""
        captured_tools = []
        def llm(req):
            if req.tools:
                captured_tools.extend([t["function"]["name"] for t in req.tools])
            return _r(content="Fixed.")

        hm, _, _ = _make_hiring_manager(call_llm=llm, role="worker")
        hm.delegate("Fix the bug in foo.py")

        assert "str_replace_editor" in captured_tools, (
            f"Worker must have str_replace_editor. Got: {captured_tools}"
        )

    def test_worker_has_bash(self):
        """Worker also has bash tools."""
        captured_tools = []
        def llm(req):
            if req.tools:
                captured_tools.extend([t["function"]["name"] for t in req.tools])
            return _r(content="Done.")

        hm, _, _ = _make_hiring_manager(call_llm=llm, role="worker")
        hm.delegate("Fix the bug")

        for tool in ["str_replace_editor", "bash"]:
            assert tool in captured_tools

    def test_default_role_is_explorer(self):
        """When SystemLLM spawns with explorer role, sub-agent gets explorer tools."""
        captured_tools = []
        def llm(req):
            if req.tools:
                captured_tools.extend([t["function"]["name"] for t in req.tools])
            return _r(content="Done.")

        hm, _, _ = _make_hiring_manager(call_llm=llm, role="explorer")
        hm.delegate("Analyze something")

        assert "str_replace_editor" in captured_tools


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
            return _r(content="This should not happen.")

        hm, _, _ = _make_hiring_manager(call_llm=llm)
        result = hm.delegate("Find the bug")

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

        hm, _, _ = _make_hiring_manager(call_llm=llm)
        result = hm.delegate("Analyze the bug")

        assert "_cstack" in result or "recursion" in result or "matrix" in result, (
            f"report_result content must be returned to caller. Got: {result}"
        )

    def test_no_report_falls_back_to_output(self):
        """If sub-agent finishes without report_result (text response),
        use the text as fallback."""
        def llm(req):
            return _r(content="I found the issue in separable.py.")

        hm, _, _ = _make_hiring_manager(call_llm=llm)
        result = hm.delegate("Find the issue")

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

        hm, _, _ = _make_hiring_manager(call_llm=llm)
        result = hm.delegate("Find the bug")

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

        hm, _, _ = _make_hiring_manager(
            call_llm=llm,
            parent_system_prompt="You are a sub-agent. Call report_result to report your findings on your assigned subtask.",
        )
        hm.delegate("Find bugs")

        assert len(captured_system_prompts) >= 1
        prompt = captured_system_prompts[0]
        assert "report_result" in prompt.lower() or "report" in prompt.lower(), (
            f"Sub-agent prompt must mention report_result. Got: {prompt[:200]}"
        )

    def test_sub_agent_prompt_mentions_subtask_focus(self):
        """Sub-agent prompt must tell it to focus on the assigned subtask."""
        captured_system_prompts = []
        def llm(req):
            for m in req.messages:
                if m.get("role") == "system":
                    captured_system_prompts.append(m["content"])
            return _r(content="Done.")

        hm, _, _ = _make_hiring_manager(
            call_llm=llm,
            parent_system_prompt="You are a sub-agent. Focus ONLY on your assigned subtask. Call report_result when done.",
        )
        hm.delegate("Find the root cause")

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

        long_prompt = (
            "You are a senior debugging agent. Focus on your assigned subtask. "
            "Call report_result with findings when done. Be thorough but focused. "
            "Read relevant code, search for patterns, and form a clear conclusion. "
            "Include file paths and line numbers in your report. " * 3
        )

        hm, _, _ = _make_hiring_manager(
            call_llm=llm,
            parent_system_prompt=long_prompt,
        )
        hm.delegate("Analyze code")

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
    """use_agent description must teach the main agent how to delegate."""

    def test_description_mentions_delegate(self):
        """Description must mention delegation or sub-task."""
        action = DelegateTaskAction(hiring_manager=None)
        desc = action.description
        assert "sub-task" in desc.lower() or "delegate" in desc.lower(), (
            "Description must mention delegation"
        )

    def test_parameter_teaches_briefing_writing(self):
        """Task parameter description must teach how to write good tasks."""
        action = DelegateTaskAction(hiring_manager=None)
        task_param = action.parameters.get("task", {})
        desc = task_param.get("description", "")

        has_guidance = (
            "file" in desc.lower() or
            "function" in desc.lower() or
            "include" in desc.lower()
        )
        assert has_guidance, (
            "Task parameter description must teach how to write concrete subtask descriptions"
        )

    def test_description_mentions_context(self):
        """Description must mention clean context."""
        action = DelegateTaskAction(hiring_manager=None)
        desc = action.description
        assert "context" in desc.lower(), (
            "Description must mention clean context for sub-agents"
        )
