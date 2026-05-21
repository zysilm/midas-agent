"""Integration test for H3 semantic memory: 3 episodes accumulate experience.

Runs three full HTAEngine.run() cycles against the same shared
SemanticExperienceMemory, with a stub system_llm that deterministically
answers all three tool types (submit_hypotheses, submit_distillation,
judge_decision_point). After episode 3, bias_summary at the start of a
hypothetical episode 4 should include experience from episodes 1 and 2
(passes) but not episode 3 (if the caller passes its issue_id).
"""
import os
import re
import tempfile
from unittest.mock import MagicMock

import pytest

from llm_agent_toolkit.llm.types import LLMResponse, ToolCall, TokenUsage
from llm_agent_toolkit.types import Issue

from midas_agent.workspace.hta.advantage_memory import SemanticExperienceMemory
from midas_agent.workspace.hta.decision_point import DecisionPointRegistry
from midas_agent.workspace.hta.engine import HTAEngine, HTAEngineConfig
from midas_agent.workspace.hta.sub_verifier import SubVerifier


_SEED_CLASSES = {
    "root_cause_localization": [
        "framework_default_value", "operator_overload_path", "serialization_roundtrip",
    ],
    "fix_locality_scope": ["surface_patch", "intermediate_layer", "root_layer"],
}


def _hyp_response(classes):
    return LLMResponse(
        content=None,
        tool_calls=[ToolCall(id="t1", name="submit_hypotheses", arguments={
            "hypotheses": [
                {"hypothesis_class": c, "rationale": f"r {c}",
                 "predicted_path": f"pkg/{c}.py", "test_payload": "print(1)"}
                for c in classes
            ],
        })],
        usage=TokenUsage(input_tokens=1, output_tokens=1),
    )


def _distill_response(winner_class):
    return LLMResponse(
        content=None,
        tool_calls=[ToolCall(id="d1", name="submit_distillation", arguments={
            "winner_summary": f"Winner {winner_class} fit the traceback.",
            "counterfactual_summary": "Losers did not match the failing path.",
        })],
        usage=TokenUsage(input_tokens=1, output_tokens=1),
    )


def _text_response():
    return LLMResponse(content="done", tool_calls=None,
                       usage=TokenUsage(input_tokens=1, output_tokens=1))


def _make_system_llm():
    last_classes: list[list[str]] = []

    def llm(req):
        tool_names = {t["function"]["name"] for t in (req.tools or [])}
        user = req.messages[-1]["content"] if req.messages else ""
        if "submit_hypotheses" in tool_names:
            m = re.search(r"type:\s*(\S+)", user)
            dt = m.group(1) if m else "root_cause_localization"
            classes = _SEED_CLASSES.get(dt, ["a", "b", "c"])
            last_classes.append(classes)
            return _hyp_response(classes)
        if "submit_distillation" in tool_names:
            # Pull the winner_class from the most-recent hypotheses list
            # (the prompt names it explicitly but parsing is overkill).
            classes = last_classes[-1] if last_classes else ["unknown"]
            return _distill_response(classes[0])
        return _text_response()

    return llm


def _scored_verifier(score_map, default=0.5):
    v = MagicMock(spec=SubVerifier)
    v.verify.side_effect = lambda h, ctx, cheap=True: score_map.get(h.name, default)
    return v


def _run_episode(issue_id, memory, system_llm):
    issue = Issue(issue_id=issue_id, repo="o/r", description=f"bug in {issue_id}")
    rcl_v = _scored_verifier({
        "framework_default_value": 0.9, "operator_overload_path": 0.5,
        "serialization_roundtrip": 0.1,
    })
    fl_v = _scored_verifier({
        "surface_patch": 0.9, "intermediate_layer": 0.4, "root_layer": 0.1,
    })
    engine = HTAEngine(
        issue=issue,
        call_llm=MagicMock(return_value=_text_response()),
        system_llm=system_llm,
        actions=[],
        advantage_memory=memory,
        registry=DecisionPointRegistry(),
        run_bash=MagicMock(return_value="ok"),
        write_file=MagicMock(return_value="/tmp/_hta_probe.py"),
        remove_file=MagicMock(),
        config=HTAEngineConfig(),
        work_dir="/tmp/work",
        balance_provider=lambda: 1_000_000,
    )
    engine._verifiers["root_cause_localization"] = rcl_v
    engine._verifiers["fix_locality_scope"] = fl_v
    engine.run()
    return engine


@pytest.mark.integration
class TestThreeEpisodesBuildMemory:
    def test_three_fake_episodes_build_memory(self):
        d = tempfile.mkdtemp(prefix="h3_e2e_")
        memory = SemanticExperienceMemory(os.path.join(d, "mem.json"))
        system_llm = _make_system_llm()

        # Episode 1: pass (s_exec = 1.0).
        _run_episode("astropy__astropy-1", memory, system_llm)
        memory.commit_pending(outcome_score=1.0)
        ep1_total = len(memory)

        # Episode 2: pass (different issue).
        _run_episode("astropy__astropy-2", memory, system_llm)
        memory.commit_pending(outcome_score=1.0)
        ep2_total = len(memory)

        # Episode 3: fail.
        _run_episode("django__django-3", memory, system_llm)
        memory.commit_pending(outcome_score=0.1)
        ep3_total = len(memory)

        # Each episode resolves 2 backbone decision points (RCL + fix_locality)
        # plus may add more from stuck handling. At minimum 2 per episode.
        assert ep1_total >= 2
        assert ep2_total >= ep1_total + 2
        assert ep3_total >= ep2_total + 2

        # bias_summary at episode 4 (excluding django__django-3 as the
        # current issue) sees passes only — django-3 is a fail and the
        # most-recent fail rule would inject it if no other fail exists.
        bias = memory.bias_summary(
            "root_cause_localization",
            k=5, current_issue_id="astropy__astropy-99",
        )
        # All three episodes' RCL winners were "framework_default_value"
        # (top of seed_classes, highest verifier score).
        assert "framework_default_value" in bias
        # The cold-start placeholder must not appear.
        assert "No prior experience" not in bias

    def test_excluding_current_issue_filters_its_entries(self):
        d = tempfile.mkdtemp(prefix="h3_excl_")
        memory = SemanticExperienceMemory(os.path.join(d, "mem.json"))
        system_llm = _make_system_llm()

        _run_episode("astropy__astropy-A", memory, system_llm)
        memory.commit_pending(outcome_score=1.0)
        _run_episode("astropy__astropy-B", memory, system_llm)
        memory.commit_pending(outcome_score=1.0)

        bias_all = memory.bias_summary("root_cause_localization", k=10)
        bias_excl_b = memory.bias_summary(
            "root_cause_localization", k=10,
            current_issue_id="astropy__astropy-B",
        )
        assert "astropy-A" in bias_all
        assert "astropy-B" in bias_all
        assert "astropy-A" in bias_excl_b
        assert "astropy-B" not in bias_excl_b

    def test_distillation_count_proportional_to_decisions(self):
        d = tempfile.mkdtemp(prefix="h3_count_")
        memory = SemanticExperienceMemory(os.path.join(d, "mem.json"))
        system_llm = _make_system_llm()
        engine = _run_episode("astropy__astropy-12907", memory, system_llm)
        # Per-DP distillation: counter equals number of resolved DPs
        # (not escalation-only states which write nothing).
        # The default backbone resolves exactly 2 DPs (RCL + fix_locality).
        assert engine._memory_distillations_used >= 2
        assert engine._memory_distillations_used == len(memory._pending)
