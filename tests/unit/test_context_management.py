"""Unit tests for context management — tool output truncation and conversation compaction.

Tests define expected behavior for the two context management layers:
1. Context layer: tool output middle-elision truncation before entering conversation history
2. Compaction layer: LLM-based conversation compression when approaching context limit
"""
import pytest

from midas_agent.stdlib.react_agent import ReactAgent, AgentResult


# ===========================================================================
# Helper: truncation function (to be implemented)
# ===========================================================================

from midas_agent.context.truncation import truncate_output


# ===========================================================================
# Context Layer: Tool Output Truncation
# ===========================================================================


@pytest.mark.unit
class TestOutputTruncation:
    """Tests for tool output middle-elision truncation."""

    def test_short_output_unchanged(self):
        """Output shorter than max_chars is returned unchanged."""
        text = "hello world"
        result = truncate_output(text, max_chars=10000)
        assert result == text

    def test_exact_limit_unchanged(self):
        """Output exactly at max_chars is returned unchanged."""
        text = "x" * 10000
        result = truncate_output(text, max_chars=10000)
        assert result == text

    def test_over_limit_is_truncated(self):
        """Output exceeding max_chars is truncated."""
        text = "x" * 20000
        result = truncate_output(text, max_chars=10000)
        assert len(result) < len(text)

    def test_middle_elision_preserves_head_and_tail(self):
        """Truncated output preserves first half and last half."""
        # Create distinct head and tail content
        head = "HEAD_" * 1000  # 5000 chars
        middle = "MID_" * 5000  # 20000 chars
        tail = "TAIL_" * 1000  # 5000 chars
        text = head + middle + tail  # 30000 chars

        result = truncate_output(text, max_chars=10000)

        assert result.startswith("HEAD_")
        assert result.endswith("TAIL_")
        assert "MID_" not in result  # middle should be elided

    def test_truncation_marker_present(self):
        """Truncated output contains the elision marker with char count."""
        text = "a" * 20000
        result = truncate_output(text, max_chars=10000)

        assert "characters were elided" in result

    def test_truncation_marker_has_correct_count(self):
        """Elision marker reports approximately how many chars were removed."""
        text = "a" * 20000
        result = truncate_output(text, max_chars=10000)

        # Parse the count from "...N characters were elided..."
        import re
        m = re.search(r"(\d+) characters were elided", result)
        assert m is not None, f"No truncation marker found in: {result[:200]}"
        removed = int(m.group(1))
        # Should be approximately 10000 (20000 - 10000)
        assert removed > 5000
        assert removed < 15000

    def test_head_tail_roughly_equal_size(self):
        """Head and tail portions are roughly equal in size."""
        text = "x" * 20000
        result = truncate_output(text, max_chars=10000)

        parts = result.split("characters were elided")
        # Should have text before and after the marker
        assert len(parts) == 2
        head_part = parts[0]
        tail_part = parts[1]
        # Both should be substantial (within 2x of each other)
        assert len(head_part) > 2000
        assert len(tail_part) > 2000

    def test_unicode_safe(self):
        """Truncation does not split multi-byte unicode characters."""
        # Mix of ASCII and multi-byte chars
        text = "日本語テスト" * 3000  # ~18000 chars
        result = truncate_output(text, max_chars=5000)

        # Should not crash and should be valid unicode
        assert isinstance(result, str)
        result.encode("utf-8")  # should not raise

    def test_empty_output(self):
        """Empty string is returned unchanged."""
        assert truncate_output("", max_chars=10000) == ""

    def test_newlines_preserved_in_head_tail(self):
        """Multi-line output preserves line structure in head and tail."""
        lines = [f"line {i}: some content here\n" for i in range(1000)]
        text = "".join(lines)

        result = truncate_output(text, max_chars=5000)

        # Head should have valid lines
        assert "line 0:" in result
        # Tail should have valid lines
        assert "line 999:" in result

    def test_custom_max_chars(self):
        """Custom max_chars values work correctly."""
        text = "x" * 1000
        # Very small limit
        result = truncate_output(text, max_chars=100)
        assert len(result) < 500  # truncated + marker
        assert "characters were elided" in result


# ===========================================================================
# Integration: Truncation in ReactAgent loop
# ===========================================================================


@pytest.mark.unit
class TestReactAgentTruncation:
    """Tests that ReactAgent applies truncation to tool results."""

    def test_tool_output_truncated_in_messages(self):
        """Tool results exceeding max_tool_output_chars are truncated
        before being added to the conversation history."""
        from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage, ToolCall
        from midas_agent.stdlib.action import Action

        class BigOutputAction(Action):
            @property
            def name(self): return "big_output"
            @property
            def description(self): return "Returns huge output"
            @property
            def parameters(self): return {}
            def execute(self, **kwargs):
                return "x" * 50000  # 50k chars

        class DoneAction(Action):
            @property
            def name(self): return "task_done"
            @property
            def description(self): return "Done"
            @property
            def parameters(self): return {}
            def execute(self, **kwargs):
                from midas_agent.stdlib.actions.task_done import DONE_SENTINEL
                return DONE_SENTINEL + " done"

        call_count = 0

        def fake_llm(request: LLMRequest) -> LLMResponse:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return LLMResponse(
                    content=None,
                    tool_calls=[ToolCall(id="c1", name="big_output", arguments={})],
                    usage=TokenUsage(input_tokens=100, output_tokens=50),
                )
            else:
                # Check that the tool result in messages was truncated
                tool_msgs = [m for m in request.messages if m.get("role") == "tool"]
                if tool_msgs:
                    content = tool_msgs[-1]["content"]
                    assert len(content) < 15000, (
                        f"Tool output should be truncated but was {len(content)} chars"
                    )
                    assert "characters were elided" in content
                return LLMResponse(
                    content=None,
                    tool_calls=[ToolCall(id="c2", name="task_done", arguments={})],
                    usage=TokenUsage(input_tokens=100, output_tokens=50),
                )

        agent = ReactAgent(
            system_prompt="test",
            actions=[BigOutputAction(), DoneAction()],
            call_llm=fake_llm,
            max_iterations=5,
            max_tool_output_chars=10000,
        )
        result = agent.run(context="test")
        assert result.termination_reason == "done"
        assert call_count == 2

    def test_small_output_not_truncated(self):
        """Tool results under the limit are passed through unchanged."""
        from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage, ToolCall
        from midas_agent.stdlib.action import Action

        class SmallAction(Action):
            @property
            def name(self): return "small"
            @property
            def description(self): return "Small output"
            @property
            def parameters(self): return {}
            def execute(self, **kwargs):
                return "small result"

        class DoneAction(Action):
            @property
            def name(self): return "task_done"
            @property
            def description(self): return "Done"
            @property
            def parameters(self): return {}
            def execute(self, **kwargs):
                from midas_agent.stdlib.actions.task_done import DONE_SENTINEL
                return DONE_SENTINEL + " done"

        call_count = 0

        def fake_llm(request: LLMRequest) -> LLMResponse:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return LLMResponse(
                    content=None,
                    tool_calls=[ToolCall(id="c1", name="small", arguments={})],
                    usage=TokenUsage(input_tokens=100, output_tokens=50),
                )
            else:
                tool_msgs = [m for m in request.messages if m.get("role") == "tool"]
                if tool_msgs:
                    content = tool_msgs[-1]["content"]
                    assert "small result" in content
                    assert "truncated" not in content
                return LLMResponse(
                    content=None,
                    tool_calls=[ToolCall(id="c2", name="task_done", arguments={})],
                    usage=TokenUsage(input_tokens=100, output_tokens=50),
                )

        agent = ReactAgent(
            system_prompt="test",
            actions=[SmallAction(), DoneAction()],
            call_llm=fake_llm,
            max_iterations=5,
            max_tool_output_chars=10000,
        )
        result = agent.run(context="test")
        assert result.termination_reason == "done"


# ===========================================================================
# Compaction Layer: Conversation Compression
# ===========================================================================


from midas_agent.context.compaction import (
    should_compact,
    build_compaction_prompt,
    build_compacted_history,
    COMPACTION_PROMPT,
    SUMMARY_PREFIX,
)


@pytest.mark.unit
class TestCompactionTrigger:
    """Tests for compaction trigger logic."""

    def test_below_threshold_no_compact(self):
        """When total tokens < 90% of context window, no compaction."""
        assert should_compact(
            total_tokens=100000,
            context_window=262144,
        ) is False

    def test_at_90_percent_triggers(self):
        """When total tokens >= 90% of context window, triggers compaction."""
        threshold = int(262144 * 0.9)
        assert should_compact(
            total_tokens=threshold,
            context_window=262144,
        ) is True

    def test_over_threshold_triggers(self):
        """Well over threshold triggers compaction."""
        assert should_compact(
            total_tokens=260000,
            context_window=262144,
        ) is True

    def test_zero_context_window(self):
        """Zero context window never triggers (safety)."""
        assert should_compact(total_tokens=1000, context_window=0) is False

    def test_custom_ratio(self):
        """Custom ratio (e.g., 80%) works."""
        assert should_compact(
            total_tokens=80000,
            context_window=100000,
            ratio=0.8,
        ) is True
        assert should_compact(
            total_tokens=79999,
            context_window=100000,
            ratio=0.8,
        ) is False


@pytest.mark.unit
class TestCompactionPrompt:
    """Tests for compaction prompt construction."""

    def test_compaction_prompt_exists(self):
        """The compaction prompt constant is defined and non-empty."""
        assert len(COMPACTION_PROMPT) > 50
        assert "summary" in COMPACTION_PROMPT.lower()

    def test_summary_prefix_exists(self):
        """The summary prefix for post-compaction is defined."""
        assert len(SUMMARY_PREFIX) > 50
        assert "another" in SUMMARY_PREFIX.lower() or "previous" in SUMMARY_PREFIX.lower()

    def test_build_compaction_prompt_includes_history(self):
        """build_compaction_prompt creates messages with the history context."""
        messages = [
            {"role": "user", "content": "Fix the bug in foo.py"},
            {"role": "assistant", "content": "Let me search..."},
            {"role": "tool", "content": "found foo.py:10: def broken()"},
        ]
        prompt = build_compaction_prompt(messages)
        assert isinstance(prompt, list)
        assert len(prompt) >= 1
        # Should contain the compaction instruction
        assert any("summary" in str(m).lower() for m in prompt)


@pytest.mark.unit
class TestCompactedHistoryBuilding:
    """Tests for building the new history after LLM compaction."""

    def test_compacted_history_contains_summary(self):
        """After compaction, the new history contains the LLM-generated summary."""
        old_messages = [
            {"role": "user", "content": "task description " * 100},
            {"role": "assistant", "content": "analysis " * 100},
            {"role": "tool", "content": "result " * 100},
            {"role": "user", "content": "recent question"},
        ]
        summary = "Previously: analyzed the code and found the bug in foo.py line 10."

        new_history = build_compacted_history(
            old_messages=old_messages,
            summary=summary,
            max_user_message_tokens=20000,
        )

        # Summary should be in the new history
        all_content = " ".join(m.get("content", "") for m in new_history)
        assert "found the bug in foo.py" in all_content

    def test_compacted_history_has_summary_prefix(self):
        """The summary message includes the handoff prefix."""
        old_messages = [
            {"role": "user", "content": "fix bug"},
        ]
        summary = "Fixed the import error."

        new_history = build_compacted_history(
            old_messages=old_messages,
            summary=summary,
            max_user_message_tokens=20000,
        )

        all_content = " ".join(m.get("content", "") for m in new_history)
        assert SUMMARY_PREFIX[:30] in all_content

    def test_compacted_history_keeps_recent_user_messages(self):
        """Recent user messages are preserved in the compacted history."""
        old_messages = [
            {"role": "user", "content": "old task from long ago"},
            {"role": "assistant", "content": "old response"},
            {"role": "user", "content": "recent important instruction"},
        ]
        summary = "Summary of work so far."

        new_history = build_compacted_history(
            old_messages=old_messages,
            summary=summary,
            max_user_message_tokens=20000,
        )

        all_content = " ".join(m.get("content", "") for m in new_history)
        assert "recent important instruction" in all_content

    def test_compacted_history_drops_old_tool_results(self):
        """Tool results from old messages are not preserved (only user messages)."""
        old_messages = [
            {"role": "user", "content": "fix bug"},
            {"role": "tool", "content": "grep output: " + "x" * 10000},
            {"role": "user", "content": "now edit the file"},
        ]
        summary = "Searched and found the bug."

        new_history = build_compacted_history(
            old_messages=old_messages,
            summary=summary,
            max_user_message_tokens=20000,
        )

        all_content = " ".join(m.get("content", "") for m in new_history)
        assert "grep output" not in all_content

    def test_compacted_history_respects_token_budget(self):
        """User messages are trimmed to fit within max_user_message_tokens."""
        # Create many large user messages
        old_messages = []
        for i in range(50):
            old_messages.append({"role": "user", "content": f"message {i} " * 500})
            old_messages.append({"role": "assistant", "content": f"response {i}"})

        summary = "Summary."

        new_history = build_compacted_history(
            old_messages=old_messages,
            summary=summary,
            max_user_message_tokens=5000,  # very tight budget
        )

        # Total user message content should be bounded
        total_user_chars = sum(
            len(m["content"]) for m in new_history
            if m.get("role") == "user" and SUMMARY_PREFIX[:10] not in m.get("content", "")
        )
        # 5000 tokens ≈ 20000 chars. Give generous margin.
        assert total_user_chars < 30000

    def test_compacted_history_preserves_newest_first(self):
        """When budget is tight, newest user messages are kept over oldest."""
        old_messages = [
            {"role": "user", "content": "OLD_MESSAGE " * 2000},  # big
            {"role": "user", "content": "NEW_MESSAGE"},  # small
        ]
        summary = "Summary."

        new_history = build_compacted_history(
            old_messages=old_messages,
            summary=summary,
            max_user_message_tokens=100,  # very tight
        )

        all_content = " ".join(m.get("content", "") for m in new_history)
        assert "NEW_MESSAGE" in all_content

    def test_compacted_history_is_smaller_than_original(self):
        """The compacted history should be substantially smaller."""
        old_messages = []
        for i in range(20):
            old_messages.append({"role": "user", "content": f"question {i} " * 100})
            old_messages.append({"role": "assistant", "content": f"answer {i} " * 200})
            old_messages.append({"role": "tool", "content": f"output {i} " * 300})

        summary = "Summarized 20 rounds of work."

        new_history = build_compacted_history(
            old_messages=old_messages,
            summary=summary,
            max_user_message_tokens=5000,
        )

        old_size = sum(len(m.get("content", "")) for m in old_messages)
        new_size = sum(len(m.get("content", "")) for m in new_history)
        assert new_size < old_size / 2, (
            f"Compacted history ({new_size}) should be <50% of original ({old_size})"
        )
