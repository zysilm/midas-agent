"""Unit tests for HypothesisGenerator.

The system LLM is a MagicMock returning real LLMResponse objects — no fakes.
"""
import os
import tempfile
from unittest.mock import MagicMock

import pytest

from llm_agent_toolkit.llm.types import LLMResponse, ToolCall, TokenUsage

from midas_agent.workspace.hta.advantage_memory import SemanticExperienceMemory
from midas_agent.workspace.hta.decision_point import DecisionPointRegistry
from midas_agent.workspace.hta.hypothesis_gen import HypothesisGenerator


def _resp(tool_args=None, content=None):
    tool_calls = None
    if tool_args is not None:
        tool_calls = [ToolCall(id="c1", name="submit_hypotheses", arguments=tool_args)]
    return LLMResponse(
        content=content, tool_calls=tool_calls,
        usage=TokenUsage(input_tokens=1, output_tokens=1),
    )


def _hyp(cls, **kw):
    return {
        "hypothesis_class": cls,
        "rationale": kw.get("rationale", "because"),
        "predicted_path": kw.get("predicted_path", "pkg/mod.py"),
        "test_payload": kw.get("test_payload", "print(1)"),
    }


@pytest.fixture
def memory():
    d = tempfile.mkdtemp(prefix="hta_gen_")
    return SemanticExperienceMemory(os.path.join(d, "mem.json"))


@pytest.fixture
def rcl():
    return DecisionPointRegistry().get("root_cause_localization")


@pytest.mark.unit
class TestHypothesisGenerator:
    def test_generates_requested_count(self, memory, rcl):
        llm = MagicMock(return_value=_resp({"hypotheses": [
            _hyp("framework_default_value"),
            _hyp("serialization_roundtrip"),
            _hyp("inheritance_dispatch"),
        ]}))
        gen = HypothesisGenerator(system_llm=llm)
        hyps = gen.generate(rcl, "an issue", "", g=3, memory=memory)
        assert len(hyps) == 3
        assert hyps[0].name == "framework_default_value"
        assert hyps[0].predicted_path == "pkg/mod.py"

    def test_truncates_to_g(self, memory, rcl):
        llm = MagicMock(return_value=_resp({"hypotheses": [
            _hyp("framework_default_value"),
            _hyp("serialization_roundtrip"),
            _hyp("inheritance_dispatch"),
        ]}))
        gen = HypothesisGenerator(system_llm=llm)
        hyps = gen.generate(rcl, "an issue", "", g=2, memory=memory)
        assert len(hyps) == 2

    def test_seed_class_kept_novel_class_tagged(self, memory, rcl):
        llm = MagicMock(return_value=_resp({"hypotheses": [
            _hyp("framework_default_value"),
            _hyp("Some Brand New Cause"),
        ]}))
        gen = HypothesisGenerator(system_llm=llm)
        hyps = gen.generate(rcl, "an issue", "", g=2, memory=memory)
        assert hyps[0].name == "framework_default_value"
        assert hyps[1].is_novel
        assert hyps[1].novel_slug == "some_brand_new_cause"

    def test_explicit_novel_prefix_preserved(self, memory, rcl):
        llm = MagicMock(return_value=_resp({"hypotheses": [
            _hyp("__novel__:async_race"),
        ]}))
        gen = HypothesisGenerator(system_llm=llm)
        hyps = gen.generate(rcl, "an issue", "", g=1, memory=memory)
        assert hyps[0].name == "__novel__:async_race"

    def test_retries_when_no_tool_call(self, memory, rcl):
        llm = MagicMock(side_effect=[
            _resp(content="here is some prose instead of a tool call"),
            _resp({"hypotheses": [_hyp("framework_default_value")]}),
        ])
        gen = HypothesisGenerator(system_llm=llm)
        hyps = gen.generate(rcl, "an issue", "", g=1, memory=memory)
        assert len(hyps) == 1
        assert llm.call_count == 2

    def test_returns_empty_after_exhausting_attempts(self, memory, rcl):
        llm = MagicMock(return_value=_resp(content="never calls the tool"))
        gen = HypothesisGenerator(system_llm=llm)
        hyps = gen.generate(rcl, "an issue", "", g=2, memory=memory)
        assert hyps == []
        assert llm.call_count == 3

    def test_survives_api_exception(self, memory, rcl):
        llm = MagicMock(side_effect=[
            RuntimeError("api down"),
            _resp({"hypotheses": [_hyp("framework_default_value")]}),
        ])
        gen = HypothesisGenerator(system_llm=llm)
        hyps = gen.generate(rcl, "an issue", "", g=1, memory=memory)
        assert len(hyps) == 1

    def test_g_clamped_to_valid_range(self, memory, rcl):
        llm = MagicMock(return_value=_resp({"hypotheses": [
            _hyp("framework_default_value"),
        ]}))
        gen = HypothesisGenerator(system_llm=llm)
        # g=9 is clamped to 3; only one hypothesis is returned by the LLM anyway.
        hyps = gen.generate(rcl, "an issue", "", g=9, memory=memory)
        assert len(hyps) == 1
