"""Unit tests for the H4 execution-grounded RCL probe.

These tests cover:
- the parser/sandbox helpers (pure functions, easy)
- the probe class's tier dispatch (path-existence, symbol grounding,
  reproduction-trace intersection, lexicon fallback, budget skip)
- per-issue caching of the reproduction trace

No HTAEngine integration tests live here — Phase 1d's flag-off
regression is exercised by the existing test_hta_engine.py suite.
Probe-on engine integration is exercised separately in
test_rcl_probe_integration.py.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from llm_agent_toolkit.types import Issue

from midas_agent.workspace.hta.decision_point import Hypothesis
from midas_agent.workspace.hta.sub_verifier import (
    ExecutionGroundedRCLProbe,
    RCLProbeResult,
    RCLVerifier,
    VerifierContext,
    _check_path_exists,
    _check_symbol_in_file,
    _parse_predicted_path,
    _path_on_trace,
    _sandbox_path,
)


def _ctx(run_bash, issue=None, work_dir="/testbed") -> VerifierContext:
    return VerifierContext(
        issue=issue or Issue(issue_id="i1", repo="o/r", description="bug",
                             fail_to_pass=[]),
        work_dir=work_dir,
        run_bash=run_bash,
        write_file=MagicMock(),
        remove_file=MagicMock(),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestParsePredictedPath:
    def test_bare_file(self):
        assert _parse_predicted_path("astropy/table/table.py") == (
            "astropy/table/table.py", None)

    def test_file_plus_symbol(self):
        assert _parse_predicted_path(
            "astropy/table/table.py:_check_required_columns") == (
            "astropy/table/table.py", "_check_required_columns")

    def test_file_plus_dotted_symbol(self):
        assert _parse_predicted_path(
            "astropy/table/table.py:Table.__init__") == (
            "astropy/table/table.py", "Table.__init__")

    def test_hash_separator(self):
        assert _parse_predicted_path(
            "django/db/models/sql/query.py#build_filter") == (
            "django/db/models/sql/query.py", "build_filter")

    def test_absolute_path_preserved(self):
        assert _parse_predicted_path("/testbed/astropy/core.py") == (
            "/testbed/astropy/core.py", None)

    def test_prose_suffix_after_py(self):
        # Mixed-language path observed on H3 cold run ep3.
        out = _parse_predicted_path(
            "astropy/table/table.py中设置列数据的代码路径(可能在Table.__init__或add_row之类的函数)")
        assert out == ("astropy/table/table.py", None)

    def test_unparseable(self):
        assert _parse_predicted_path("no path here") == (None, None)
        assert _parse_predicted_path("") == (None, None)
        assert _parse_predicted_path(None) == (None, None)


@pytest.mark.unit
class TestSandboxPath:
    def test_relative_prefixed(self):
        ctx = _ctx(MagicMock(), work_dir="/testbed")
        assert _sandbox_path(ctx, "astropy/core.py") == "/testbed/astropy/core.py"

    def test_absolute_passes_through(self):
        ctx = _ctx(MagicMock(), work_dir="/testbed")
        assert _sandbox_path(ctx, "/elsewhere/file.py") == "/elsewhere/file.py"


@pytest.mark.unit
class TestSandboxChecks:
    def test_path_exists_yes(self):
        rb = MagicMock(return_value="HTA_PROBE_EXISTS\n")
        ctx = _ctx(rb)
        assert _check_path_exists(ctx, "x.py") is True
        rb.assert_called_once()
        assert "test -f" in rb.call_args[0][0]

    def test_path_exists_no(self):
        rb = MagicMock(return_value="HTA_PROBE_MISSING\n")
        ctx = _ctx(rb)
        assert _check_path_exists(ctx, "x.py") is False

    def test_path_exists_garbage(self):
        rb = MagicMock(return_value="")
        ctx = _ctx(rb)
        assert _check_path_exists(ctx, "x.py") is False

    def test_symbol_present(self):
        rb = MagicMock(return_value="HTA_SYMBOL_FOUND\n")
        ctx = _ctx(rb)
        assert _check_symbol_in_file(ctx, "x.py", "my_func") is True
        # uses grep -F (fixed string), passes -- before path
        assert "grep -F" in rb.call_args[0][0]

    def test_symbol_absent(self):
        rb = MagicMock(return_value="HTA_SYMBOL_ABSENT\n")
        ctx = _ctx(rb)
        assert _check_symbol_in_file(ctx, "x.py", "my_func") is False


@pytest.mark.unit
class TestPathOnTrace:
    _TRACE = (
        "Traceback (most recent call last):\n"
        '  File "/testbed/astropy/table/table.py", line 1243, in _convert_data_to_col\n'
        "    data = data.view(NdarrayMixin)\n"
        "AssertionError\n"
    )

    def test_file_match(self):
        assert _path_on_trace("astropy/table/table.py", None, self._TRACE) is True

    def test_off_path(self):
        assert _path_on_trace("django/db/models/sql/query.py", None,
                              self._TRACE) is False

    def test_symbol_fallback(self):
        assert _path_on_trace("nope/x.py", "_convert_data_to_col",
                              self._TRACE) is True

    def test_empty_trace(self):
        assert _path_on_trace("astropy/x.py", None, "") is False


# ---------------------------------------------------------------------------
# Probe class
# ---------------------------------------------------------------------------

def _probe(run_bash_responses, *, fail_to_pass=None, max_iters=8,
           balance=None) -> ExecutionGroundedRCLProbe:
    """Build a probe + a ctx whose run_bash returns the given response
    sequence (one per call). Returns (probe, ctx, run_bash_mock)."""
    rb = MagicMock(side_effect=list(run_bash_responses))
    issue = Issue(issue_id="i1", repo="o/r", description="bug",
                  fail_to_pass=fail_to_pass or [])
    ctx = _ctx(rb, issue=issue)
    p = ExecutionGroundedRCLProbe(
        fallback_verifier=RCLVerifier(),
        max_iters=max_iters,
        balance_provider=balance,
    )
    return p, ctx, rb


@pytest.mark.unit
class TestProbeTierDispatch:
    def test_path_absent_scores_zero(self):
        p, ctx, _ = _probe(["HTA_PROBE_MISSING"])
        h = Hypothesis(name="framework_default_value",
                       predicted_path="pkg/missing.py")
        r = p.probe(h, ctx)
        assert r.score == ExecutionGroundedRCLProbe.SCORE_PATH_ABSENT
        assert r.method_used == "path_absent"
        assert r.iters_used == 1

    def test_path_exists_no_symbol_no_repro_returns_exists_unconfirmed(self):
        # No fail_to_pass → no reproduction tier → exists_unconfirmed
        p, ctx, _ = _probe(["HTA_PROBE_EXISTS"], fail_to_pass=[])
        h = Hypothesis(name="framework_default_value",
                       predicted_path="pkg/found.py")  # no symbol
        r = p.probe(h, ctx)
        assert r.score >= ExecutionGroundedRCLProbe.SCORE_EXISTS_UNCONFIRMED
        assert r.method_used == "exists_unconfirmed"
        assert r.iters_used == 1

    def test_path_exists_symbol_absent_floors_low(self):
        # path-check ✓, symbol-check ✗ → SCORE_PATH_ABSENT_SYMBOL
        p, ctx, _ = _probe(["HTA_PROBE_EXISTS", "HTA_SYMBOL_ABSENT"])
        h = Hypothesis(name="framework_default_value",
                       predicted_path="pkg/found.py:absent_symbol")
        r = p.probe(h, ctx)
        assert r.score == ExecutionGroundedRCLProbe.SCORE_PATH_ABSENT_SYMBOL
        assert r.method_used == "path_absent"
        assert r.iters_used == 2

    def test_on_path_promotes_to_full_score(self):
        # path ✓, symbol ✓, reproduction trace mentions file → on_path
        trace = ("Traceback...\n"
                 '  File "/testbed/pkg/found.py", line 5, in my_func\n'
                 "AssertionError")
        p, ctx, _ = _probe(
            ["HTA_PROBE_EXISTS", "HTA_SYMBOL_FOUND", trace],
            fail_to_pass=["pkg/tests/test_x.py::test_x"],
        )
        h = Hypothesis(name="framework_default_value",
                       predicted_path="pkg/found.py:my_func")
        r = p.probe(h, ctx)
        assert r.score == ExecutionGroundedRCLProbe.SCORE_ON_PATH
        assert r.method_used == "on_path"

    def test_off_path_keeps_exists_unconfirmed(self):
        trace = ("Traceback...\n"
                 '  File "/testbed/other/file.py", line 5, in other_func\n'
                 "AssertionError")
        p, ctx, _ = _probe(
            ["HTA_PROBE_EXISTS", trace],   # path-exists, repro-runs
            fail_to_pass=["pkg/tests/test_x.py::test_x"],
        )
        h = Hypothesis(name="framework_default_value",
                       predicted_path="pkg/found.py")  # no symbol named
        r = p.probe(h, ctx)
        assert r.method_used == "exists_unconfirmed"
        assert r.score == ExecutionGroundedRCLProbe.SCORE_EXISTS_UNCONFIRMED

    def test_unparseable_path_falls_back_to_lexicon(self):
        # No file → straight to fallback, never touches sandbox.
        p, ctx, rb = _probe([], fail_to_pass=[])
        h = Hypothesis(name="framework_default_value",
                       predicted_path="this isn't a path at all")
        r = p.probe(h, ctx)
        assert r.method_used == "lexicon_fallback"
        assert r.iters_used == 0
        rb.assert_not_called()

    def test_budget_skip(self):
        # balance_provider returns 0 → probe skipped entirely.
        p, ctx, rb = _probe(
            ["HTA_PROBE_EXISTS"],
            balance=lambda: 0,
        )
        h = Hypothesis(name="framework_default_value",
                       predicted_path="pkg/found.py")
        r = p.probe(h, ctx)
        assert r.method_used == "skipped_budget"
        assert r.iters_used == 0
        rb.assert_not_called()


@pytest.mark.unit
class TestReproductionTraceCache:
    def test_trace_cached_across_hypotheses(self):
        """Two hypotheses on the same issue share one reproduction run."""
        trace = ("Traceback...\n"
                 '  File "/testbed/pkg/a.py", line 1, in foo\n'
                 "AssertionError")
        # path check (hyp1) + repro (hyp1) + path check (hyp2). Second
        # hypothesis must NOT re-run pytest.
        p, ctx, rb = _probe(
            ["HTA_PROBE_EXISTS", trace, "HTA_PROBE_EXISTS"],
            fail_to_pass=["pkg/tests/test_x.py::test_foo"],
        )
        h1 = Hypothesis(name="framework_default_value",
                        predicted_path="pkg/a.py")
        h2 = Hypothesis(name="operator_overload_path",
                        predicted_path="pkg/b.py")
        r1 = p.probe(h1, ctx)
        r2 = p.probe(h2, ctx)
        # Only 3 sandbox calls total despite two hypotheses.
        assert rb.call_count == 3
        # First gets on_path (file matches trace), second gets
        # exists_unconfirmed (file doesn't match trace) — but it still
        # got the cached trace.
        assert r1.method_used == "on_path"
        assert r2.method_used == "exists_unconfirmed"


# ---------------------------------------------------------------------------
# Phase 2 memory-label flow
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestPhase2MemoryLabel:
    def test_probe_label_overrides_episode_outcome(self):
        import os
        import tempfile
        import time
        from midas_agent.workspace.hta.advantage_memory import (
            SemanticExperienceMemory, SemanticMemoryEntry,
        )
        d = tempfile.mkdtemp(prefix="h4_2_")
        m = SemanticExperienceMemory(os.path.join(d, "mem.json"))
        # RCL entry with probe_label
        m.buffer(SemanticMemoryEntry(
            decision_type="root_cause_localization",
            winner_class="A", winner_summary="w", counterfactual_summary="l",
            outcome_score=0.0, issue_id="i1", timestamp=time.time(),
            is_novel_winner=False, probe_label=0.7,
        ))
        # fix_locality entry without probe_label
        m.buffer(SemanticMemoryEntry(
            decision_type="fix_locality_scope",
            winner_class="surface_patch", winner_summary="w",
            counterfactual_summary="l", outcome_score=0.0, issue_id="i1",
            timestamp=time.time(), is_novel_winner=False,
        ))
        # Episode failed.
        m.commit_pending(outcome_score=0.0)
        rcl = m._entries[0]
        fl = m._entries[1]
        assert rcl.outcome_score == pytest.approx(0.7)
        assert rcl.episode_outcome_for_reference == pytest.approx(0.0)
        assert fl.outcome_score == pytest.approx(0.0)
        assert fl.episode_outcome_for_reference is None

    def test_no_probe_label_keeps_episode_outcome(self):
        import os
        import tempfile
        import time
        from midas_agent.workspace.hta.advantage_memory import (
            SemanticExperienceMemory, SemanticMemoryEntry,
        )
        d = tempfile.mkdtemp(prefix="h4_2b_")
        m = SemanticExperienceMemory(os.path.join(d, "mem.json"))
        m.buffer(SemanticMemoryEntry(
            decision_type="root_cause_localization",
            winner_class="A", winner_summary="w", counterfactual_summary="l",
            outcome_score=0.0, issue_id="i1", timestamp=time.time(),
            is_novel_winner=False,  # no probe_label
        ))
        m.commit_pending(outcome_score=1.0)
        assert m._entries[0].outcome_score == pytest.approx(1.0)
        assert m._entries[0].episode_outcome_for_reference is None
