"""Shared test fixtures for unit tests."""
import pytest

from midas_agent.llm.provider import LLMProvider
from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage
from midas_agent.scheduler.storage import StorageBackend, LogFilter
from midas_agent.scheduler.training_log import LogEntry


class FakeLLMProvider(LLMProvider):
    """Test double for LLMProvider. Returns canned responses in order."""

    def __init__(
        self,
        responses: list[LLMResponse],
        errors: dict[int, Exception] | None = None,
    ):
        self._responses = responses
        self._errors = errors or {}
        self._call_index = 0
        self._call_log: list[tuple[LLMRequest, LLMResponse]] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        idx = self._call_index
        self._call_index += 1
        if idx in self._errors:
            raise self._errors[idx]
        response = (
            self._responses[idx]
            if idx < len(self._responses)
            else self._responses[-1]
        )
        self._call_log.append((request, response))
        return response

    @property
    def call_count(self) -> int:
        return self._call_index

    @property
    def call_log(self) -> list[tuple[LLMRequest, LLMResponse]]:
        return list(self._call_log)


class InMemoryStorageBackend(StorageBackend):
    """Test double for StorageBackend. Backed by a plain list."""

    def __init__(self):
        self._entries: list[LogEntry] = []

    def append(self, entry: LogEntry) -> None:
        self._entries.append(entry)

    def query(self, filter: LogFilter) -> list[LogEntry]:
        results = list(self._entries)
        if filter.entity_id is not None:
            results = [
                e
                for e in results
                if e.to == filter.entity_id or e.from_entity == filter.entity_id
            ]
        if filter.type is not None:
            results = [e for e in results if e.type == filter.type]
        if filter.workspace_id is not None:
            results = [e for e in results if e.workspace_id == filter.workspace_id]
        if filter.since is not None:
            results = [e for e in results if e.timestamp >= filter.since]
        if filter.until is not None:
            results = [e for e in results if e.timestamp <= filter.until]
        return sorted(results, key=lambda e: e.timestamp)

    @property
    def entries(self) -> list[LogEntry]:
        return list(self._entries)


# -- Fixtures --


@pytest.fixture
def fake_llm_response():
    """A single canned LLM response."""
    return LLMResponse(
        content="test response",
        tool_calls=None,
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )


@pytest.fixture
def fake_llm_provider(fake_llm_response):
    """A FakeLLMProvider pre-loaded with one canned response."""
    return FakeLLMProvider(responses=[fake_llm_response])


@pytest.fixture
def in_memory_storage():
    """An empty InMemoryStorageBackend."""
    return InMemoryStorageBackend()
