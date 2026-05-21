"""HTAEngine — drives the decision graph for one issue.

The engine walks a worklist of steps with an explicit cursor (it never
topo-sorts, so backward edges are safe). The worklist starts as a fixed
backbone — localize, reproduce, choose fix layer, implement, validate — which
encodes the rule-triggered decision points. Decision points layer the
hypothesis mechanism on top; stuck execution nodes invoke the meta-judge, which
can splice an investigation_continuation decision (and a backward edge) into
the worklist.

Termination is bounded three ways: the global budget brake, a cap on decision
nodes, and a hard cap on total steps.
"""
from __future__ import annotations

import json
import logging
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from llm_agent_toolkit.llm.types import LLMRequest, LLMResponse
from llm_agent_toolkit.stdlib.action import Action
from llm_agent_toolkit.stdlib.react_agent import ActionRecord
from llm_agent_toolkit.types import Issue

from midas_agent.prompts import SYSTEM_PROMPT
from midas_agent.workspace.hta.advantage_memory import (
    SemanticExperienceMemory,
    SemanticMemoryEntry,
)
from midas_agent.workspace.hta.prompts import (
    MEMORY_DISTILLATION_PROMPT,
    SUBMIT_DISTILLATION_TOOL,
)
from midas_agent.workspace.hta.decision_point import (
    DecisionPoint,
    DecisionPointRegistry,
    Hypothesis,
    RuleTriggerInputs,
    evidence_tokens_for,
)
from midas_agent.workspace.hta.execution_node import ExecutionNode, ExecutionOutcome
from midas_agent.workspace.hta.graph import DecisionGraph, NodeKind, NodeStatus
from midas_agent.workspace.hta.hypothesis_gen import HypothesisGenerator
from midas_agent.workspace.hta.meta_judge import DecisionPointMetaJudge
from midas_agent.workspace.hta.sub_verifier import (
    ContinuationVerifier,
    FixLocalityVerifier,
    LLMJudgeVerifier,
    RCLVerifier,
    SpecInterpretationVerifier,
    SubVerifier,
    TestScopeVerifier,
    VerifierContext,
)

logger = logging.getLogger(__name__)


@dataclass
class HTAEngineConfig:
    epsilon: float = 1e-6           # std-collapse threshold for advantage
    max_decision_points: int = 12   # cap on decision nodes per issue
    max_steps: int = 40             # hard cap on total worklist steps
    enable_test_scope_dp: bool = False
    # Directory the engine writes per-episode analysis summaries into
    # (issue #46). When None, summary writes are skipped silently.
    run_dir: str | None = None
    # Cap on spec_interpretation re-entries per issue (issue H1 D3). The
    # original boolean latch fired escalation at most once per issue, so
    # repeated RCL collapses silently fell through to raw-score selection
    # for the rest of the episode. The counter allows escalation to fire
    # multiple times in deeply ambiguous issues, still bounded.
    max_escalations: int = 3
    # Cap on semantic-memory distillation LLM calls per issue (issue H3).
    # One call per decision point; typical issue fires 3-5 DPs so 6 leaves
    # 20% slack. Bounds cost on pathological issues that splice 10+ DPs.
    max_memory_distillations: int = 6


@dataclass
class _Step:
    kind: str                       # "decision" | "execution"
    decision_type: str | None = None
    goal: str | None = None
    backward_to: str | None = None  # node id for a re-entry backward edge


@dataclass
class _DecisionResult:
    winner: Hypothesis | None = None
    hypotheses: list[Hypothesis] = field(default_factory=list)
    escalated: bool = False
    failed: bool = False
    # Set true when fix_locality anti-gaming detected all probes firing the
    # HTA_LAYER_HIT sentinel — meaning the LLM authored trivially-satisfied
    # probes and the verifier could not discriminate (issue H1 D2).
    gaming_detected: bool = False


# Backbone goals for execution nodes (the rule-driven main flow).
_REPRODUCE_GOAL = (
    "Reproduce the bug described in the issue and confirm the selected root "
    "cause. Write or run a minimal reproduction."
)
_IMPLEMENT_GOAL = (
    "Implement the fix at the chosen code layer. Make the smallest change that "
    "resolves the issue without breaking existing behaviour."
)
_VALIDATE_TARGETED_GOAL = (
    "Run the failing tests named in the issue and confirm they now pass."
)
_VALIDATE_BROAD_GOAL = (
    "Run the broader set of tests around your change and check for regressions."
)


class HTAEngine:
    def __init__(
        self,
        issue: Issue,
        call_llm: Callable[[LLMRequest], LLMResponse],
        system_llm: Callable[[LLMRequest], LLMResponse],
        actions: list[Action],
        advantage_memory: SemanticExperienceMemory,
        registry: DecisionPointRegistry,
        run_bash: Callable[[str], str],
        write_file: Callable[[str, str], str],
        remove_file: Callable[[str], None],
        config: HTAEngineConfig,
        work_dir: str = "",
        balance_provider: Callable[[], int] | None = None,
        max_tool_output_chars: int | None = None,
        max_context_tokens: int | None = None,
        action_log=None,
    ) -> None:
        self._issue = issue
        self._call_llm = call_llm
        self._system_llm = system_llm
        self._actions = actions
        self._memory = advantage_memory
        self._registry = registry
        self._run_bash = run_bash
        self._write_file = write_file
        self._remove_file = remove_file
        self._config = config
        self._work_dir = work_dir
        self._balance_provider = balance_provider
        self._max_tool_output_chars = max_tool_output_chars
        self._max_context_tokens = max_context_tokens
        self._action_log = action_log

        self._hypothesis_gen = HypothesisGenerator(system_llm=system_llm)
        self._meta_judge = DecisionPointMetaJudge(system_llm=system_llm, registry=registry)
        # SpecInterpretationVerifier escalates to Tier-2 (LLM judge) when
        # lexical is inconclusive; wire system_llm so the escalation can
        # actually happen (issue #44 B5).
        self._verifiers: dict[str, SubVerifier] = {
            "root_cause_localization": RCLVerifier(),
            "fix_locality_scope": FixLocalityVerifier(),
            "spec_interpretation": SpecInterpretationVerifier(system_llm=system_llm),
            "investigation_continuation": ContinuationVerifier(),
            "test_scope_strategy": TestScopeVerifier(),
        }
        # Shared Tier-2 judge used to force LLM-grade verification on
        # novel-class hypotheses (courseware §003 novel-class exception,
        # issue #44 C2). Without this, novels would always be scored by
        # the cheap default verifier — whose lexicon does not include
        # them — and would be silently pruned before they could prove
        # themselves.
        self._novel_class_judge = LLMJudgeVerifier(system_llm)
        self._decision_count = 0
        # Counter of spec_interpretation escalations triggered this issue
        # (issue H1 D3, replaces the boolean self._escalated latch).
        self._escalation_count = 0
        # Per-issue Tier-2 budget: at most 3 LLM-judge calls per issue.
        self._tier2_calls_used = 0
        self._tier2_cap = 3
        # Per-issue semantic-memory distillation budget (issue H3).
        self._memory_distillations_used = 0
        # Stuck-signal context (per issue): RCL winner's evidence tokens and
        # the initial budget snapshot so signal 3 can compute the fraction
        # of budget burned without seeing predicted evidence.
        self._predicted_tokens: list[str] = []
        self._initial_budget: int | None = None

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> DecisionGraph:
        # Reset per-issue Tier-2 budget — the cap is per-issue, not per-process.
        self._tier2_calls_used = 0
        # Reset per-issue escalation counter (issue H1 D3).
        self._escalation_count = 0
        # Reset per-issue distillation counter (issue H3).
        self._memory_distillations_used = 0
        # Snapshot the starting budget for the stuck-signal 3 calculation.
        self._initial_budget = (
            self._balance_provider() if self._balance_provider is not None else None
        )
        self._predicted_tokens = []
        graph = DecisionGraph()
        root = graph.add_node(NodeKind.EXECUTION, "bootstrap", status=NodeStatus.DONE)
        cursor_id = root.node_id

        worklist: deque[_Step] = deque([
            _Step("decision", decision_type="root_cause_localization"),
            _Step("execution", goal=_REPRODUCE_GOAL),
            _Step("decision", decision_type="fix_locality_scope"),
            _Step("execution", goal=_IMPLEMENT_GOAL),
            _Step("execution", goal=_VALIDATE_TARGETED_GOAL),
            _Step("execution", goal=_VALIDATE_BROAD_GOAL),
        ])
        last_action_history: list[ActionRecord] = []
        steps_run = 0

        while worklist and steps_run < self._config.max_steps:
            if self._balance_provider is not None and self._balance_provider() <= 0:
                logger.info("HTAEngine: budget exhausted, stopping")
                break
            step = worklist.popleft()
            steps_run += 1

            if step.kind == "decision":
                cursor_id = self._run_decision_step(step, graph, cursor_id, worklist,
                                                    last_action_history)
            else:
                cursor_id, outcome = self._run_execution_step(step, graph, cursor_id)
                last_action_history = outcome.action_history
                if outcome.termination_reason == "budget_exhausted":
                    break
                if outcome.stuck:
                    cursor_id = self._handle_stuck(step, graph, cursor_id, outcome, worklist)

        # Issue #46: write a per-episode analysis summary for the aggregator.
        # Wrapped so a summary-write failure never propagates to the engine.
        self._write_episode_summary(graph)
        return graph

    def _write_episode_summary(self, graph: DecisionGraph) -> None:
        if not self._config.run_dir:
            return
        try:
            from midas_agent.workspace.hta.analysis.episode_summary import (
                build_summary, write_summary,
            )
            import os as _os
            final_budget = (
                self._balance_provider() if self._balance_provider is not None else None
            )
            summary = build_summary(
                issue_id=self._issue.issue_id,
                branch=_os.environ.get("MIDAS_HTA_BRANCH", "unknown"),
                graph=graph,
                initial_budget=self._initial_budget,
                final_budget=final_budget,
                tier2_calls_used=self._tier2_calls_used,
                memory_distillations_emitted=self._memory_distillations_used,
                memory_distillation_cap=self._config.max_memory_distillations,
            )
            path = write_summary(summary, self._config.run_dir)
            logger.info("Wrote HTA episode summary -> %s", path)
        except Exception as e:  # noqa: BLE001 — must never fail the episode
            logger.warning("HTA episode-summary write failed: %s", e)

    # ------------------------------------------------------------------
    # Decision steps
    # ------------------------------------------------------------------

    def _run_decision_step(
        self,
        step: _Step,
        graph: DecisionGraph,
        cursor_id: str,
        worklist: deque[_Step],
        action_history: list[ActionRecord],
    ) -> str:
        if self._decision_count >= self._config.max_decision_points:
            logger.info("HTAEngine: decision-point cap reached, skipping %s", step.decision_type)
            return cursor_id

        dp = self._registry.get(step.decision_type)
        if dp is None:
            return cursor_id

        node = graph.add_node(
            NodeKind.DECISION, step.decision_type, decision_type=step.decision_type,
            status=NodeStatus.RUNNING,
        )
        graph.add_edge(cursor_id, node.node_id, reason="advance")
        if step.backward_to:
            graph.add_edge(node.node_id, step.backward_to, kind="backward", reason="re-entry")
        self._decision_count += 1

        result = self._resolve(dp, graph, node.node_id, action_history)

        if result.escalated and self._escalation_count < self._config.max_escalations:
            self._escalation_count += 1
            node.status = NodeStatus.DONE
            node.distilled_evidence = (
                "[root_cause_localization] hypotheses were indistinguishable "
                "(advantage collapse) — escalating to spec_interpretation."
            )
            # Preserve the collapsed hypotheses on the node so the analyzer
            # can see the std=0 group that triggered escalation.
            node.payload = self._decision_payload(result)
            # Re-read the spec, then re-enter RCL with a backward edge.
            worklist.appendleft(_Step("decision", decision_type="root_cause_localization",
                                      backward_to=node.node_id))
            worklist.appendleft(_Step("decision", decision_type="spec_interpretation"))
            return node.node_id

        if result.failed or result.winner is None:
            node.status = NodeStatus.ABANDONED
            return node.node_id

        node.status = NodeStatus.DONE
        node.winner_hypothesis = result.winner.name
        node.distilled_evidence = self._distill_decision(step.decision_type, result.winner)
        node.payload = self._decision_payload(result)
        return node.node_id

    # G is fixed at 3 per the courseware spec §003 — adaptive_g was an
    # implementation-only optimisation that produced a permanent G=1 absorbing
    # state (see issue #44 B1/C1). Holding G at 3 maximally exercises the
    # mechanism and matches the spec.
    _G = 3

    def _resolve(
        self,
        dp: DecisionPoint,
        graph: DecisionGraph,
        node_id: str,
        action_history: list[ActionRecord],
        stuck_reason: str | None = None,
    ) -> _DecisionResult:
        """Generate G=3 hypotheses, verify, score by group-relative advantage."""
        evidence = graph.trace_evidence(node_id)
        hypotheses = self._hypothesis_gen.generate(
            dp, self._issue.description, evidence, self._G, self._memory,
        )
        if not hypotheses:
            return _DecisionResult(failed=True)

        # Record any novel classes the generator emitted.
        for h in hypotheses:
            if h.is_novel:
                self._memory.maybe_register_novel(h.novel_slug or "unspecified")

        default_verifier = self._verifiers.get(dp.decision_type) or ContinuationVerifier()
        ctx = VerifierContext(
            issue=self._issue,
            work_dir=self._work_dir,
            run_bash=self._run_bash,
            write_file=self._write_file,
            remove_file=self._remove_file,
            action_history=action_history,
            stuck_reason=stuck_reason,
        )
        for h in hypotheses:
            verifier = self._select_verifier_for(h, default_verifier)
            try:
                h.score = verifier.verify(h, ctx)
            except Exception as e:  # noqa: BLE001 — a broken probe must not crash the engine
                logger.warning("Sub-verifier failed for %s: %s", h.name, e)
                h.score = 0.0

        # Anti-gaming for fix_locality_scope (issue H1 D2): if every probe
        # fired the sentinel and scored 1.0, the LLM has gamed the verifier
        # with trivially-satisfied conditions (e.g. `assert True,
        # 'HTA_LAYER_HIT'`). The verifier failed to discriminate. Two
        # effects: (a) demote all scores to a low neutral value so any
        # downstream consumer of `hyp.score` sees "low confidence" rather
        # than "everyone hit 1.0 = strong signal everywhere"; (b) flag the
        # gaming in the decision payload so the analyzer can count it and
        # any future memory layer can record "the verifier couldn't tell
        # these apart" as the actual lesson. Regime-independent: works the
        # same way whether the memory layer is numerical or semantic.
        gaming_detected = False
        if (dp.decision_type == "fix_locality_scope"
                and len(hypotheses) >= 2
                and all(abs(h.score - 1.0) < 1e-6 for h in hypotheses)):
            logger.warning(
                "FixLocality gaming detected: all %d probes scored 1.0 "
                "(sentinel fired indiscriminately). Demoting to 0.4.",
                len(hypotheses),
            )
            for h in hypotheses:
                h.score = 0.4
            gaming_detected = True

        # Defensive: if the LLM (or retries) yielded fewer than 2 hypotheses,
        # group-relative advantage is undefined. Per Q1, buffer a soft signal
        # centred on a neutral 0.5 so the class can still drift down on
        # repeated failures — i.e. don't let a single-hypothesis path freeze
        # the memory the way the old G=1 absorbing path did.
        if len(hypotheses) == 1:
            winner = hypotheses[0]
            winner.advantage = winner.score - 0.5
            # Issue H3: distill one semantic memory entry per decision
            # point instead of buffering numerical advantages per hypothesis.
            entry = self._distill_memory_entry(dp, hypotheses, winner)
            if entry is not None:
                self._memory.buffer(entry)
            return _DecisionResult(
                winner=winner, hypotheses=hypotheses,
                gaming_detected=gaming_detected,
            )

        scores = [h.score for h in hypotheses]
        mean = statistics.mean(scores)
        std = statistics.pstdev(scores)

        # Dynamic-sampling collapse: the hypotheses cannot be told apart.
        if std < self._config.epsilon:
            if (dp.decision_type == "root_cause_localization"
                    and self._escalation_count < self._config.max_escalations):
                # Per spec §008: do NOT update memory when there is no signal.
                return _DecisionResult(hypotheses=hypotheses, escalated=True)
            # Non-RCL collapse (or already escalated): fall back to raw
            # score for selection. Issue H3: under semantic memory the
            # "zero advantage buffer" loop has no analog — we still distill
            # one entry so the LLM learns "verifier could not discriminate
            # at this decision type".
            for h in hypotheses:
                h.advantage = 0.0
            winner = max(hypotheses, key=lambda h: h.score)
        else:
            for h in hypotheses:
                h.advantage = (h.score - mean) / std
            winner = max(hypotheses, key=lambda h: h.advantage)

        # Issue H3: one semantic distillation per decision point.
        entry = self._distill_memory_entry(dp, hypotheses, winner)
        if entry is not None:
            self._memory.buffer(entry)

        # After an RCL decision: snapshot the winning class's evidence
        # lexicon so the next execution node's stuck-signal 3 can check
        # whether the predicted evidence ever appears.
        if dp.decision_type == "root_cause_localization":
            self._predicted_tokens = evidence_tokens_for(winner.name)

        return _DecisionResult(
            winner=winner, hypotheses=hypotheses,
            gaming_detected=gaming_detected,
        )

    # ------------------------------------------------------------------
    # Semantic memory distillation (issue H3)
    # ------------------------------------------------------------------

    def _distill_memory_entry(
        self,
        dp: DecisionPoint,
        hypotheses: list[Hypothesis],
        winner: Hypothesis,
    ) -> SemanticMemoryEntry | None:
        """Issue one ``system_llm`` call to turn a resolved decision point
        into a :class:`SemanticMemoryEntry`. Returns None if the cap is
        hit, the LLM declines to use the tool, or the API call fails.

        The entry's ``outcome_score`` is left at 0.0 here; it is filled in
        by :meth:`SemanticExperienceMemory.commit_pending` using the
        episode's s_exec so failed-episode entries are stamped as failures.
        """
        if self._memory_distillations_used >= self._config.max_memory_distillations:
            return None

        prompt = MEMORY_DISTILLATION_PROMPT.format(
            issue_id=self._issue.issue_id,
            issue_excerpt=(self._issue.description or "")[:600],
            decision_type=dp.decision_type,
            hypotheses_block=self._format_hypotheses_for_distillation(hypotheses),
            winner_class=winner.name,
        )
        messages = [
            {"role": "system", "content":
             "You distill one HTA decision into reusable lessons. "
             "Always call submit_distillation."},
            {"role": "user", "content": prompt},
        ]
        try:
            resp = self._system_llm(
                LLMRequest(messages=messages, model="default",
                           tools=[SUBMIT_DISTILLATION_TOOL]),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Memory distillation API error: %s", e)
            return None

        if not resp.tool_calls:
            return None

        for tc in resp.tool_calls:
            if tc.name != "submit_distillation":
                continue
            args = tc.arguments
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    return None
            if not isinstance(args, dict):
                return None
            winner_summary = str(args.get("winner_summary", "")).strip()[:400]
            counterfactual_summary = (
                str(args.get("counterfactual_summary", "")).strip()[:400]
            )
            if not winner_summary or not counterfactual_summary:
                return None

            self._memory_distillations_used += 1
            return SemanticMemoryEntry(
                decision_type=dp.decision_type,
                winner_class=winner.name,
                winner_summary=winner_summary,
                counterfactual_summary=counterfactual_summary,
                outcome_score=0.0,   # filled in at commit_pending
                issue_id=self._issue.issue_id,
                timestamp=time.time(),
                is_novel_winner=winner.is_novel,
            )
        return None

    @staticmethod
    def _format_hypotheses_for_distillation(hyps: list[Hypothesis]) -> str:
        lines = []
        for i, h in enumerate(hyps, 1):
            lines.append(
                f"Hypothesis {i}: class={h.name}\n"
                f"  rationale: {(h.rationale or '')[:200]}\n"
                f"  verifier score: {h.score:.2f}, advantage: {h.advantage:+.2f}"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Execution steps
    # ------------------------------------------------------------------

    def _run_execution_step(
        self, step: _Step, graph: DecisionGraph, cursor_id: str,
    ) -> tuple[str, ExecutionOutcome]:
        node = graph.add_node(
            NodeKind.EXECUTION, step.goal or "execute", status=NodeStatus.RUNNING,
        )
        graph.add_edge(cursor_id, node.node_id, reason="advance")

        context = self._build_execution_context(graph, node.node_id, step.goal or "")
        exec_node = ExecutionNode(
            system_prompt=SYSTEM_PROMPT,
            actions=self._actions,
            call_llm=self._call_llm,
            system_llm=self._system_llm,
            max_tool_output_chars=self._max_tool_output_chars,
            max_context_tokens=self._max_context_tokens,
            balance_provider=self._balance_provider,
            action_log=self._action_log,
        )
        outcome = exec_node.run(
            context,
            predicted_tokens=list(self._predicted_tokens) or None,
            budget_used_frac=self._budget_used_frac(),
        )

        if outcome.termination_reason in ("error", "budget_exhausted"):
            node.status = NodeStatus.ABANDONED
        else:
            node.status = NodeStatus.DONE
        node.distilled_evidence = self._distill_execution(outcome)
        node.payload = {
            "termination_reason": outcome.termination_reason,
            "iterations": outcome.iterations,
            "stuck": outcome.stuck,
            "stuck_reason": outcome.stuck_reason,
        }
        return node.node_id, outcome

    def _handle_stuck(
        self,
        step: _Step,
        graph: DecisionGraph,
        cursor_id: str,
        outcome: ExecutionOutcome,
        worklist: deque[_Step],
    ) -> str:
        """A stuck execution node — consult the meta-judge for a continuation DP,
        or bypass straight to IC for unambiguous rule-based signals."""
        if self._decision_count >= self._config.max_decision_points:
            return cursor_id

        # Signal 1 (same_file_read_5x) is a hard rule-based observation: no
        # semantic judgement is needed to confirm the agent is stuck. Bypass
        # the conservative meta-judge gate, which routinely suppresses IC even
        # when activation is obviously appropriate (issue #45). Signals 2 and
        # 3 remain gated by the meta-judge because they are softer (a repeated
        # error could be a legitimate pattern; budget-burn without evidence
        # could be a hard issue where evidence takes time to surface).
        bypass_meta_judge = (
            outcome.stuck_reason is not None
            and outcome.stuck_reason.startswith("same_file_read_5x")
        )
        if bypass_meta_judge:
            dp = self._registry.get("investigation_continuation")
        else:
            dp = self._meta_judge.classify(
                rule_inputs=_EMPTY_RULE_INPUTS,
                is_stuck=True,
                issue_summary=self._issue.description,
                recent_trace=_format_trace(outcome.action_history),
            )
        if dp is None:
            return cursor_id

        if dp.decision_type.startswith("__novel__"):
            self._memory.maybe_register_novel(dp.decision_type)

        node = graph.add_node(
            NodeKind.DECISION, dp.decision_type, decision_type=dp.decision_type,
            status=NodeStatus.RUNNING,
        )
        graph.add_edge(cursor_id, node.node_id, reason="stuck")
        self._decision_count += 1

        result = self._resolve(
            dp, graph, node.node_id, outcome.action_history,
            stuck_reason=outcome.stuck_reason,
        )
        if result.failed or result.winner is None:
            node.status = NodeStatus.ABANDONED
            return node.node_id

        node.status = NodeStatus.DONE
        node.winner_hypothesis = result.winner.name
        node.distilled_evidence = self._distill_decision(dp.decision_type, result.winner)
        node.payload = self._decision_payload(result)

        # Act on the chosen continuation strategy.
        cls = result.winner.name
        if cls == "persist_same_path":
            worklist.appendleft(_Step("execution", goal=step.goal, backward_to=cursor_id))
        elif cls in ("pivot_evidence_type", "pivot_target"):
            worklist.appendleft(_Step(
                "execution",
                goal=(
                    f"Previous approach stalled. Change tack ({cls}): "
                    f"{result.winner.rationale} Original goal: {step.goal}"
                ),
                backward_to=cursor_id,
            ))
        # "abandon" -> add nothing; the worklist moves on.
        return node.node_id

    def _budget_used_frac(self) -> float:
        """Fraction of the per-issue budget consumed so far, in [0.0, 1.0]."""
        if self._balance_provider is None or not self._initial_budget:
            return 0.0
        remaining = max(0, self._balance_provider())
        used = max(0, self._initial_budget - remaining)
        return min(1.0, used / self._initial_budget)

    def _select_verifier_for(self, hyp: Hypothesis, default: SubVerifier) -> SubVerifier:
        """Force Tier-2 (LLM judge) on novel-class hypotheses, capped per issue.

        Per courseware §003 novel-class exception (issue #44 C2): a
        novel-class hypothesis (one whose class is __novel__:<slug>) is
        verified at Tier-2 regardless of the decision point's default
        tier. Without this, novels would be scored by the default verifier
        whose lexicon doesn't include them — they would score badly
        across their probation window and be silently pruned without ever
        being given a fair hearing. We spend the cheap, recoverable
        resource (an LLM call) to protect the expensive, unrecoverable
        one (learning a new failure pattern).

        The cap is per-issue and applies only to this engine-driven
        forcing path; SpecInterpretationVerifier's internal escalation
        manages its own LLM budget by virtue of how rarely lexical
        scores land in the inconclusive band.
        """
        if hyp.is_novel and self._tier2_calls_used < self._tier2_cap:
            self._tier2_calls_used += 1
            return self._novel_class_judge
        return default

    # ------------------------------------------------------------------
    # Context + distillation helpers
    # ------------------------------------------------------------------

    def _build_execution_context(self, graph: DecisionGraph, node_id: str, goal: str) -> str:
        evidence = graph.trace_evidence(node_id)
        return (
            f"## GitHub issue\n{self._issue.description.strip()}\n\n"
            f"## Progress and decisions so far\n{evidence or '(none yet)'}\n\n"
            f"## Your current task\n{goal}"
        )

    @staticmethod
    def _distill_decision(decision_type: str, winner: Hypothesis) -> str:
        return (
            f"[{decision_type}] selected hypothesis '{winner.name}' "
            f"(advantage {winner.advantage:+.2f}). {winner.rationale} "
            f"Predicted location: {winner.predicted_path or 'n/a'}"
        ).strip()

    @staticmethod
    def _distill_execution(outcome: ExecutionOutcome) -> str:
        if outcome.error:
            return f"(execution failed: {outcome.error})"
        return (outcome.output or "").strip()[:1500]

    @staticmethod
    def _decision_payload(result: _DecisionResult) -> dict:
        return {
            "hypotheses": [
                {
                    "name": h.name,
                    "rationale": h.rationale,
                    "predicted_path": h.predicted_path,
                    "test_payload": h.test_payload,
                    "score": h.score,
                    "advantage": h.advantage,
                }
                for h in result.hypotheses
            ],
            "winner": result.winner.name if result.winner else None,
            "escalated": result.escalated,
            "gaming_detected": result.gaming_detected,
        }


def _format_trace(action_history: list[ActionRecord], last_n: int = 8) -> str:
    recent = action_history[-last_n:]
    lines = []
    for rec in recent:
        result = (rec.result or "")[:200]
        lines.append(f"- {rec.action_name}({rec.arguments}) -> {result}")
    return "\n".join(lines)


_EMPTY_RULE_INPUTS = RuleTriggerInputs()
