"""Unit tests for issue protection during context compaction.

The first user message (issue description) must survive compaction.
Current behavior: compaction walks newest-first, dropping the issue.
Target behavior: the issue is always preserved in compacted history.

Tests are expected to FAIL until compaction is fixed.
"""
from __future__ import annotations

import pytest

from midas_agent.context.compaction import build_compacted_history, should_compact


ISSUE_TEXT = (
    "TimeSeries: misleading exception when required column check fails.\n\n"
    "For a TimeSeries object that has additional required columns, "
    "when codes mistakenly try to remove a required column, the exception "
    "it produces is misleading.\n\n"
    "Expected: An exception that informs the users required columns are missing.\n\n"
    "Proposal: change the message to the form of:\n"
    'ValueError: TimeSeries object is invalid - required [\'time\', \'flux\'] '
    "as the first columns but found ['time']"
)


def _build_long_conversation(n_tool_results: int = 30) -> list[dict]:
    """Build a realistic conversation: system + issue + many tool call/result pairs."""
    messages = [
        {"role": "system", "content": "You are a software engineer solving GitHub issues."},
        {"role": "user", "content": ISSUE_TEXT},
    ]
    for i in range(n_tool_results):
        messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": f"tc-{i}", "type": "function",
                            "function": {"name": "bash", "arguments": f'{{"command": "grep -r pattern_{i}"}}'}}],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": f"tc-{i}",
            "content": f"./src/module_{i}.py:42:def function_{i}(): pass\n" * 20,
        })
        messages.append({
            "role": "user",
            "content": f"Tool result {i}: found {i * 3} matches in the codebase. " * 10,
        })
    return messages


@pytest.mark.unit
class TestIssueProtectionDuringCompaction:
    """The first user message (issue) must survive build_compacted_history."""

    def test_issue_preserved_in_short_conversation(self):
        """Issue survives when conversation is short enough."""
        messages = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": ISSUE_TEXT},
            {"role": "user", "content": "Tool result: found something"},
        ]
        result = build_compacted_history(messages, summary="Agent searched code.")
        user_contents = [m["content"] for m in result if m["role"] == "user"]
        assert any(ISSUE_TEXT in c for c in user_contents), \
            "Issue text should be preserved in short conversation"

    def test_issue_preserved_in_long_conversation(self):
        """Issue survives even when conversation has 100+ tool results that exceed budget."""
        messages = _build_long_conversation(n_tool_results=100)
        # Use small budget so newer messages eat all space
        result = build_compacted_history(messages, summary="Agent made progress.",
                                         max_user_message_tokens=2000)
        user_contents = [m["content"] for m in result if m["role"] == "user"]
        # The issue proposal text must appear somewhere in the compacted history
        assert any("required ['time', 'flux']" in c or "misleading exception" in c
                    for c in user_contents), \
            "Issue text was lost during compaction of long conversation"

    def test_issue_is_first_user_message_after_compaction(self):
        """After compaction, the issue should be the first user message (before summary)."""
        messages = _build_long_conversation(n_tool_results=100)
        result = build_compacted_history(messages, summary="Progress summary.",
                                         max_user_message_tokens=2000)
        # Find all user messages
        user_messages = [m for m in result if m["role"] == "user"]
        assert len(user_messages) >= 2, "Should have at least issue + summary"
        # First user message should contain the issue, not a tool result
        first_user = user_messages[0]["content"]
        assert "misleading exception" in first_user or "required column" in first_user, \
            f"First user message should be the issue, got: {first_user[:100]}"

    def test_issue_not_truncated_when_short(self):
        """Issue text (< 1000 chars) should not be middle-elided."""
        messages = _build_long_conversation(n_tool_results=100)
        result = build_compacted_history(messages, summary="Summary.",
                                         max_user_message_tokens=2000)
        user_contents = " ".join(m["content"] for m in result if m["role"] == "user")
        # The specific proposal format from hints should be intact
        assert "required ['time', 'flux']" in user_contents, \
            "Issue hints detail was truncated"

    def test_summary_still_appended(self):
        """Compaction summary is still appended as the last user message."""
        messages = _build_long_conversation(n_tool_results=10)
        result = build_compacted_history(messages, summary="Agent found the bug in core.py.")
        last_user = [m for m in result if m["role"] == "user"][-1]
        assert "Agent found the bug" in last_user["content"]

    def test_compaction_triggered_at_32k(self):
        """With max_context_tokens=32000, compaction triggers at ~28800 tokens."""
        assert should_compact(28800, 32000) is True
        assert should_compact(20000, 32000) is False


@pytest.mark.unit
class TestIssueProtectionInReactAgent:
    """ReactAgent must preserve the issue through compaction cycles."""

    def test_issue_survives_compaction_in_agent(self):
        """Run agent with small context window; after compaction, issue is still in messages."""
        from midas_agent.llm.types import LLMResponse, TokenUsage, ToolCall
        from midas_agent.stdlib.actions.task_done import TaskDoneAction
        from midas_agent.stdlib.react_agent import ReactAgent
        from midas_agent.stdlib.action import Action

        # A fake action that returns a large result to fill up context fast
        class VerboseBashAction(Action):
            @property
            def name(self): return "bash"
            @property
            def description(self): return "Run bash"
            @property
            def parameters(self): return {"command": {"type": "string", "description": "cmd"}}
            def execute(self, **kwargs):
                # Return 3000 chars to fill context quickly
                return f"./src/module.py:42:def handle_timeseries(): pass\n" * 60

        issue_seen_after_compaction = {"value": None}
        call_count = {"n": 0}

        def fake_call_llm(request):
            call_count["n"] += 1
            all_text = " ".join(m.get("content", "") or "" for m in request.messages)

            if call_count["n"] <= 8:
                # Build up context with verbose bash results
                return LLMResponse(
                    content=None,
                    tool_calls=[ToolCall(
                        id=f"c{call_count['n']}",
                        name="bash",
                        arguments={"command": f"grep -r 'pattern_{call_count['n']}' ."},
                    )],
                    usage=TokenUsage(input_tokens=3000, output_tokens=200),
                )
            else:
                # Record whether the issue is still in messages, then finish
                issue_seen_after_compaction["value"] = "misleading exception" in all_text
                return LLMResponse(
                    content=None,
                    tool_calls=[ToolCall(
                        id=f"c{call_count['n']}",
                        name="task_done",
                        arguments={"result": "done"},
                    )],
                    usage=TokenUsage(input_tokens=3000, output_tokens=100),
                )

        def fake_system_llm(request):
            return LLMResponse(
                content="Agent searched for TimeSeries code and found relevant files in core.py.",
                tool_calls=None,
                usage=TokenUsage(input_tokens=500, output_tokens=200),
            )

        agent = ReactAgent(
            system_prompt="You are a software engineer solving GitHub issues.",
            actions=[VerboseBashAction(), TaskDoneAction()],
            call_llm=fake_call_llm,
            max_context_tokens=4000,  # Very small — forces compaction
            system_llm=fake_system_llm,
        )

        agent.run(context=ISSUE_TEXT)

        assert issue_seen_after_compaction["value"] is True, \
            "Issue was lost after compaction — agent could not see the original issue text"
