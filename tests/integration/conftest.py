"""Shared test fixtures for integration tests."""

import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable

import pytest

from midas_agent.config import MidasConfig
from midas_agent.evaluation.execution_scorer import ExecutionScorer
from midas_agent.llm.provider import LLMProvider
from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage
from midas_agent.scheduler.storage import LogFilter, StorageBackend
from midas_agent.scheduler.training_log import HookSet, LogEntry
from midas_agent.types import Issue


# ---------------------------------------------------------------------------
# FakeLLMProvider
# ---------------------------------------------------------------------------


class FakeLLMProvider(LLMProvider):
    """Scripted LLM provider for integration tests.

    Features:
    - Scripted responses returned in call order.
    - Error injection at specific call indices.
    - Optional per-call delay for concurrency tests.
    - Full call recording for assertion.
    """

    def __init__(
        self,
        responses: list[LLMResponse],
        errors: dict[int, Exception] | None = None,
        delay: float = 0.0,
    ):
        self._responses = responses
        self._errors = errors or {}
        self._delay = delay
        self._call_index = 0
        self._call_log: list[tuple[LLMRequest, LLMResponse]] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        idx = self._call_index
        self._call_index += 1

        if self._delay > 0:
            time.sleep(self._delay)

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


# ---------------------------------------------------------------------------
# InMemoryStorageBackend
# ---------------------------------------------------------------------------


class InMemoryStorageBackend(StorageBackend):
    """In-memory storage backend for integration tests."""

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


# ---------------------------------------------------------------------------
# SpyHookSet
# ---------------------------------------------------------------------------


class SpyHookSet(HookSet):
    """HookSet that records every hook invocation for assertion."""

    def __init__(self):
        self._calls: dict[str, list[dict]] = {}
        super().__init__(
            on_workspace_created=self._make_spy("on_workspace_created"),
            on_workspace_evicted=self._make_spy("on_workspace_evicted"),
            on_allocate=self._make_spy("on_allocate"),
            on_transfer=self._make_spy("on_transfer"),
            on_consume=self._make_spy("on_consume"),
            on_time_paused=self._make_spy("on_time_paused"),
            on_time_resumed=self._make_spy("on_time_resumed"),
        )

    def _make_spy(self, hook_name: str) -> Callable:
        def spy(**kwargs):
            self._calls.setdefault(hook_name, []).append(kwargs)

        return spy

    def get_calls(self, hook_name: str) -> list[dict]:
        return list(self._calls.get(hook_name, []))

    def assert_called(self, hook_name: str, times: int = 1) -> None:
        actual = len(self._calls.get(hook_name, []))
        assert actual == times, (
            f"Expected {hook_name} to be called {times} time(s), "
            f"but was called {actual} time(s)"
        )

    def assert_not_called(self, hook_name: str) -> None:
        actual = len(self._calls.get(hook_name, []))
        assert actual == 0, (
            f"Expected {hook_name} not to be called, "
            f"but was called {actual} time(s)"
        )


# ---------------------------------------------------------------------------
# FakeExecutionScorer
# ---------------------------------------------------------------------------


class FakeExecutionScorer(ExecutionScorer):
    """Replaces Docker-based test execution with configurable scores."""

    def __init__(self, scores: dict[str, float]):
        """scores: workspace_id -> S_exec"""
        self._scores = scores
        self._call_log: list[tuple[str, str]] = []

    def score(self, patch: str, issue: Issue) -> float:
        self._call_log.append((patch, issue.issue_id))
        # Use the patch content to look up the workspace (the caller must embed
        # a workspace_id marker in the patch, or we default to 0.0).
        for ws_id, s_exec in self._scores.items():
            if ws_id in patch:
                return s_exec
        return 0.0

    @property
    def call_log(self) -> list[tuple[str, str]]:
        return list(self._call_log)


# ---------------------------------------------------------------------------
# DeterministicClock
# ---------------------------------------------------------------------------


class DeterministicClock:
    """Injectable timestamp source for deterministic ordering assertions."""

    def __init__(self, start: float = 1000.0, step: float = 1.0):
        self._current = start
        self._step = step

    def now(self) -> float:
        t = self._current
        self._current += self._step
        return t


# ---------------------------------------------------------------------------
# FakeIssue
# ---------------------------------------------------------------------------


FAKE_ISSUE = Issue(
    issue_id="issue-001",
    repo="tests/fixtures/sample_repo",
    description="Fix the bug in calculator.py where divide_by_zero is not handled.",
    fail_to_pass=["tests/test_calculator.py::test_divide_by_zero"],
    pass_to_pass=["tests/test_calculator.py::test_add"],
)

FAKE_ISSUE_2 = Issue(
    issue_id="issue-002",
    repo="tests/fixtures/sample_repo",
    description="Fix the off-by-one error in range_sum.",
    fail_to_pass=["tests/test_calculator.py::test_range_sum"],
    pass_to_pass=["tests/test_calculator.py::test_add"],
)


# ---------------------------------------------------------------------------
# StubWorkspace (minimal Workspace ABC impl for Scheduler tests)
# ---------------------------------------------------------------------------


from midas_agent.workspace.base import Workspace


class StubWorkspace(Workspace):
    """Minimal Workspace implementation that records method calls."""

    def __init__(
        self,
        workspace_id: str,
        call_llm: Callable[[LLMRequest], LLMResponse] | None = None,
        system_llm: Callable[[LLMRequest], LLMResponse] | None = None,
        patch_content: str = "",
    ):
        super().__init__(
            workspace_id=workspace_id,
            call_llm=call_llm or (lambda r: None),
            system_llm=system_llm or (lambda r: None),
        )
        self._patch_content = patch_content
        self.calls: list[tuple[str, dict]] = []
        self.budget_received: int = 0

    def receive_budget(self, amount: int) -> None:
        self.budget_received += amount
        self.calls.append(("receive_budget", {"amount": amount}))

    def execute(self, issue: Issue) -> None:
        self.calls.append(("execute", {"issue_id": issue.issue_id}))

    def submit_patch(self) -> None:
        self.calls.append(("submit_patch", {}))

    def post_episode(self, eval_results: dict, evicted_ids: list[str]) -> dict | None:
        self.calls.append(("post_episode", {"eval_results": eval_results, "evicted_ids": evicted_ids}))
        return None


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_llm_response() -> LLMResponse:
    return LLMResponse(
        content="test response",
        tool_calls=None,
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )


@pytest.fixture
def fake_llm_provider(fake_llm_response) -> FakeLLMProvider:
    return FakeLLMProvider(responses=[fake_llm_response])


@pytest.fixture
def in_memory_storage() -> InMemoryStorageBackend:
    return InMemoryStorageBackend()


@pytest.fixture
def spy_hooks() -> SpyHookSet:
    return SpyHookSet()


@pytest.fixture
def deterministic_clock() -> DeterministicClock:
    return DeterministicClock()


@pytest.fixture
def fake_issue() -> Issue:
    return FAKE_ISSUE


@pytest.fixture
def fake_issue_2() -> Issue:
    return FAKE_ISSUE_2


@pytest.fixture
def temp_dir():
    """Temporary directory tree with standard subdirectories."""
    d = tempfile.mkdtemp(prefix="midas_test_")
    os.makedirs(os.path.join(d, "patches"), exist_ok=True)
    os.makedirs(os.path.join(d, "snapshots"), exist_ok=True)
    os.makedirs(os.path.join(d, "criteria_cache"), exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def config_evolution_config() -> MidasConfig:
    return MidasConfig(
        initial_budget=10000,
        workspace_count=3,
        runtime_mode="config_evolution",
        n_evict=1,
        score_floor=0.01,
        multiplier_mode="static",
        multiplier_init=1.0,
        beta=0.3,
    )


@pytest.fixture
def graph_emergence_config() -> MidasConfig:
    return MidasConfig(
        initial_budget=10000,
        workspace_count=2,
        runtime_mode="graph_emergence",
        n_evict=1,
        score_floor=0.01,
        multiplier_mode="static",
        multiplier_init=1.0,
        beta=0.3,
    )
