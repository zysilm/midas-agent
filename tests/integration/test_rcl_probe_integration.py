"""Integration tests for the H4 execution-grounded RCL probe wired
into HTAEngine._resolve.

Two scenarios:
1. Flag-on engine run: verify the probe actually engages on RCL,
   payload carries the `probe` block, and the probe-derived winner
   differs from the grep winner when the probe finds an on-path
   hypothesis the grep verifier did not favour.
2. Flag-off regression: identical engine setup with both H4 flags off
   must produce the same payload shape as the existing test_hta_engine
   suite (no `probe` block, RCL winner driven by text-grep).
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
from midas_agent.workspace.hta.graph import NodeKind


_SEED_RCL = ["framework_default_value", "operator_overload_path",
             "serialization_roundtrip"]


def _hyp_dict(cls):
    # Each hypothesis predicts its own unique path; the probe will
    # accept whichever path the mocked sandbox decides exists.
    return {
        "hypothesis_class": cls,
        "rationale": f"rationale {cls}",
        "predicted_path": f"pkg/{cls}.py",
        "test_payload": "print('p')",
    }


def _hyp_response(classes):
    return LLMResponse(
        content=None,
        tool_calls=[ToolCall(id="t1", name="submit_hypotheses",
                             arguments={"hypotheses": [_hyp_dict(c)
                                                       for c in classes]})],
        usage=TokenUsage(input_tokens=1, output_tokens=1),
    )


def _distill_response():
    return LLMResponse(
        content=None,
        tool_calls=[ToolCall(id="d1", name="submit_distillation",
                             arguments={"winner_summary": "w",
                                        "counterfactual_summary": "l"})],
        usage=TokenUsage(input_tokens=1, output_tokens=1),
    )


def _text_response():
    return LLMResponse(content="done", tool_calls=None,
                       usage=TokenUsage(input_tokens=1, output_tokens=1))


def _system_llm(req):
    tn = {t["function"]["name"] for t in (req.tools or [])}
    user = req.messages[-1]["content"] if req.messages else ""
    if "submit_hypotheses" in tn:
        m = re.search(r"type:\s*(\S+)", user)
        dt = m.group(1) if m else "root_cause_localization"
        if dt == "root_cause_localization":
            return _hyp_response(_SEED_RCL)
        return _hyp_response(["a", "b", "c"])
    if "submit_distillation" in tn:
        return _distill_response()
    return _text_response()


def _build_engine(run_bash, config, work_dir="/testbed"):
    # Description includes "__init__" so the legacy RCLVerifier lexicon
    # for `framework_default_value` scores positive on at least one
    # hypothesis — otherwise all three score 0.0, std collapses, and
    # the engine escalates without ever distilling a memory entry.
    issue = Issue(issue_id="i1", repo="o/r",
                  description="bug in __init__",
                  fail_to_pass=[])
    d = tempfile.mkdtemp(prefix="h4_int_")
    memory = SemanticExperienceMemory(os.path.join(d, "mem.json"))
    engine = HTAEngine(
        issue=issue,
        call_llm=MagicMock(return_value=_text_response()),
        system_llm=_system_llm,
        actions=[],
        advantage_memory=memory,
        registry=DecisionPointRegistry(),
        run_bash=run_bash,
        write_file=MagicMock(return_value="/tmp/p"),
        remove_file=MagicMock(),
        config=config,
        work_dir=work_dir,
        balance_provider=lambda: 1_000_000,
    )
    return engine, memory


def _rcl_node(graph):
    return next((n for n in graph.nodes.values()
                 if n.kind == NodeKind.DECISION
                 and n.decision_type == "root_cause_localization"), None)


@pytest.mark.integration
class TestProbeOnIntegration:
    def test_probe_runs_and_changes_winner(self):
        """Mock sandbox so only `operator_overload_path.py` exists; that
        hypothesis should win even though the grep verifier wouldn't
        decisively favour it. Payload must carry the `probe` block."""
        def run_bash(cmd):
            if "test -f" in cmd:
                if "operator_overload_path.py" in cmd:
                    return "HTA_PROBE_EXISTS"
                return "HTA_PROBE_MISSING"
            return ""
        cfg = HTAEngineConfig(rcl_execution_probe=True, max_steps=1)
        engine, _ = _build_engine(run_bash, cfg)
        graph = engine.run()
        rcl = _rcl_node(graph)
        assert rcl is not None
        assert "probe" in rcl.payload
        # Only one hypothesis had its path exist → its probe score is
        # SCORE_EXISTS_UNCONFIRMED, others got SCORE_PATH_ABSENT.
        # Group-relative advantage picks the only positive-score one.
        assert rcl.winner_hypothesis == "operator_overload_path"
        per_hyp = rcl.payload["probe"]["per_hypothesis"]
        scored = {h["name"]: h["probe_score"] for h in per_hyp}
        assert scored["operator_overload_path"] >= 0.5
        assert scored["framework_default_value"] == 0.0
        assert scored["serialization_roundtrip"] == 0.0

    def test_phase2_writes_probe_label_to_memory(self):
        def run_bash(cmd):
            if "test -f" in cmd and "framework_default_value.py" in cmd:
                return "HTA_PROBE_EXISTS"
            return "HTA_PROBE_MISSING"
        cfg = HTAEngineConfig(
            rcl_execution_probe=True,
            rcl_probe_memory_label=True,
            max_steps=1,
        )
        engine, mem = _build_engine(run_bash, cfg)
        engine.run()
        # The RCL entry should carry probe_label set from the winner's
        # probe score (0.7 — exists_unconfirmed).
        rcl_entries = [e for e in mem._pending
                       if e.decision_type == "root_cause_localization"]
        assert len(rcl_entries) == 1
        assert rcl_entries[0].probe_label == pytest.approx(0.7, abs=0.01)
        # Commit with episode failure: the probe label should override.
        mem.commit_pending(outcome_score=0.0)
        rcl = mem._entries[0]
        assert rcl.outcome_score == pytest.approx(0.7, abs=0.01)
        assert rcl.episode_outcome_for_reference == pytest.approx(0.0)


@pytest.mark.integration
class TestFlagOffRegression:
    def test_flag_off_payload_has_no_probe_block(self):
        """Acceptance criterion (§5): flag-off behaviour is byte-identical
        to current. The decision payload must NOT carry the `probe` key."""
        run_bash = MagicMock(return_value="")
        cfg = HTAEngineConfig(max_steps=1)
        assert cfg.rcl_execution_probe is False
        assert cfg.rcl_probe_memory_label is False
        engine, mem = _build_engine(run_bash, cfg)
        graph = engine.run()
        rcl = _rcl_node(graph)
        assert rcl is not None
        assert "probe" not in rcl.payload, (
            "flag-off RCL payload must not carry the probe block; got "
            f"keys: {list(rcl.payload)}"
        )
        # And no RCL entry should carry probe_label.
        rcl_entries = [e for e in mem._pending
                       if e.decision_type == "root_cause_localization"]
        assert rcl_entries
        assert all(e.probe_label is None for e in rcl_entries)

    def test_flag_off_does_not_touch_sandbox_for_rcl(self):
        """The probe never runs → run_bash gets zero calls from RCL
        scoring."""
        run_bash = MagicMock(return_value="")
        cfg = HTAEngineConfig(max_steps=1)
        engine, _ = _build_engine(run_bash, cfg)
        engine.run()
        # The legacy RCLVerifier is text-grep only; it never touches
        # run_bash. The fix_locality verifier (which CAN run probes) and
        # any execution nodes are bounded by max_steps=1 — so the very
        # first DP is RCL, and the run terminates immediately after it
        # finishes without entering an execution node. Zero sandbox
        # calls is the expected count.
        assert run_bash.call_count == 0
