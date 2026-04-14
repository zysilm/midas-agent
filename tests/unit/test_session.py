"""Unit tests for Session."""
from unittest.mock import MagicMock

import pytest

from midas_agent.workspace.graph_emergence.session import Session
from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage


@pytest.mark.unit
class TestSession:
    """Tests for the Session class."""

    def _make_system_llm(self, content: str = "compressed summary"):
        """Create a fake system_llm callback."""
        return MagicMock(
            return_value=LLMResponse(
                content=content,
                tool_calls=None,
                usage=TokenUsage(input_tokens=10, output_tokens=5),
            )
        )

    def _make_session(self, max_context_tokens: int = 4096) -> Session:
        """Create a Session with default test parameters."""
        return Session(
            agent_id="agent-1",
            workspace_id="ws-1",
            system_llm=self._make_system_llm(),
            max_context_tokens=max_context_tokens,
        )

    def test_construction(self):
        """Session can be constructed with agent_id, workspace_id, system_llm, and max_context_tokens."""
        session = self._make_session()

        assert session is not None

    def test_add_message(self):
        """add_message() accepts a message dict without error."""
        session = self._make_session()

        session.add_message({"role": "user", "content": "hello"})

    def test_get_messages(self):
        """get_messages() returns the conversation history as a list."""
        session = self._make_session()
        session.add_message({"role": "user", "content": "hello"})

        messages = session.get_messages()

        assert isinstance(messages, list)
        assert len(messages) >= 1
        assert messages[0]["content"] == "hello"

    def test_compact_compresses_history(self):
        """compact() calls system_llm to compress the conversation history."""
        system_llm = self._make_system_llm("compressed")
        session = Session(
            agent_id="agent-1",
            workspace_id="ws-1",
            system_llm=system_llm,
            max_context_tokens=4096,
        )
        session.add_message({"role": "user", "content": "first message"})
        session.add_message({"role": "assistant", "content": "response"})

        session.compact()

        assert system_llm.call_count >= 1

    def test_auto_compact_on_threshold(self):
        """add_message() auto-triggers compact when conversation approaches the token limit."""
        system_llm = self._make_system_llm("compressed")
        session = Session(
            agent_id="agent-1",
            workspace_id="ws-1",
            system_llm=system_llm,
            max_context_tokens=100,  # Very low limit to trigger auto-compact
        )

        # Add many messages to exceed the token threshold
        for i in range(50):
            session.add_message({"role": "user", "content": f"message {i} " * 20})

        # system_llm should have been called at least once for compaction
        assert system_llm.call_count >= 1

    def test_conversation_history_property(self):
        """conversation_history property returns the full message list."""
        session = self._make_session()
        session.add_message({"role": "user", "content": "hi"})
        session.add_message({"role": "assistant", "content": "hello"})

        history = session.conversation_history

        assert isinstance(history, list)
        assert len(history) >= 2
