"""ExecutionNode — the inner layer of HTA.

Between decision points the agent runs a vanilla ReAct loop. An ExecutionNode
is a thin wrapper that constructs a fresh, isolated ReactAgent per run, so each
call — including each competing hypothesis at a decision point — gets its own
conversation context (Direction A context isolation). Losing hypotheses' agents
simply go out of scope; their context never touches the main trace.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from llm_agent_toolkit.llm.types import LLMRequest, LLMResponse
from llm_agent_toolkit.stdlib.action import Action
from llm_agent_toolkit.stdlib.react_agent import ActionRecord, ReactAgent

logger = logging.getLogger(__name__)


@dataclass
class ExecutionOutcome:
    output: str
    action_history: list[ActionRecord] = field(default_factory=list)
    termination_reason: str = "done"
    iterations: int = 0
    stuck: bool = False
    error: str | None = None
    stuck_reason: str | None = None


def hta_stuck_signals(
    action_history: list[ActionRecord],
    predicted_tokens: list[str] | None = None,
    budget_used_frac: float = 0.0,
) -> str | None:
    """Apply the three stuck signals from courseware §006.

    Returns a short reason string if the agent looks stuck, else None. This
    supplements the toolkit's verbatim-repetition check (which only catches
    identical-action loops) with three semantic signals that catch the more
    common "flailing with varied commands" failure mode (issue #44 B6).

      1. Same file read >=5 times — counts by path in any tool's arguments.
      2. Same error string in >=3 consecutive tool outputs — extracts the
         first Error/Traceback line of each.
      3. >=60% of issue budget consumed without producing predicted evidence
         — checks accumulated tool output for any of the per-class lexicon
         tokens associated with the active hypothesis winner.
    """
    # Signal 1: same file read >=5x.
    file_reads: Counter[str] = Counter()
    for a in action_history:
        args = a.arguments or {}
        path = args.get("path") or args.get("file")
        if path:
            file_reads[str(path)] += 1
    for path, n in file_reads.items():
        if n >= 5:
            return f"same_file_read_5x:{path}"

    # Signal 2: same error string in >=3 consecutive outputs.
    recent = action_history[-5:]
    error_lines: list[str | None] = []
    for a in recent:
        out = a.result or ""
        first_err: str | None = None
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            if "Error" in line or "Traceback" in line:
                first_err = line[:160]
                break
        error_lines.append(first_err)
    for i in range(len(error_lines) - 2):
        s = error_lines[i]
        if s is not None and error_lines[i + 1] == s and error_lines[i + 2] == s:
            return "same_error_3x"

    # Signal 3: >=60% budget burned without the predicted evidence appearing.
    if predicted_tokens and budget_used_frac >= 0.6:
        all_output = "\n".join((a.result or "") for a in action_history).lower()
        if not any(t.lower() in all_output for t in predicted_tokens if t):
            return "budget_60pct_no_evidence"

    return None


class ExecutionNode:
    """Runs one isolated ReAct loop and reports whether it got stuck."""

    def __init__(
        self,
        system_prompt: str,
        actions: list[Action],
        call_llm: Callable[[LLMRequest], LLMResponse],
        system_llm: Callable[[LLMRequest], LLMResponse] | None = None,
        max_tool_output_chars: int | None = None,
        max_context_tokens: int | None = None,
        balance_provider: Callable[[], int] | None = None,
        max_iterations: int | None = None,
        action_log=None,
    ) -> None:
        self._system_prompt = system_prompt
        self._actions = actions
        self._call_llm = call_llm
        self._system_llm = system_llm
        self._max_tool_output_chars = max_tool_output_chars
        self._max_context_tokens = max_context_tokens
        self._balance_provider = balance_provider
        self._max_iterations = max_iterations
        self._action_log = action_log

    def run(
        self,
        context: str,
        predicted_tokens: list[str] | None = None,
        budget_used_frac: float = 0.0,
    ) -> ExecutionOutcome:
        """Construct a fresh ReactAgent, run it, and wrap the result.

        ``predicted_tokens`` and ``budget_used_frac`` are consumed by the
        HTA-side stuck detector (signal 3 — budget burned without
        predicted evidence). The engine passes the current RCL winner's
        lexicon and the running budget fraction; either may be omitted.

        Never raises — an internal failure is reported as an ExecutionOutcome
        with ``error`` set and ``termination_reason == "error"``.
        """
        agent = ReactAgent(
            system_prompt=self._system_prompt,
            actions=self._actions,
            call_llm=self._call_llm,
            max_iterations=self._max_iterations,
            balance_provider=self._balance_provider,
            max_tool_output_chars=self._max_tool_output_chars,
            max_context_tokens=self._max_context_tokens,
            system_llm=self._system_llm,
            action_log=self._action_log,
        )
        try:
            result = agent.run(context=context)
        except Exception as e:  # noqa: BLE001 — engine must survive node failure
            logger.warning("ExecutionNode failed: %s", e)
            return ExecutionOutcome(
                output="", termination_reason="error", error=str(e), stuck=True,
                stuck_reason="exception",
            )

        toolkit_stuck = ReactAgent._check_stuck(result.action_history) is not None
        terminal_stuck = result.termination_reason in ("max_iterations", "no_action")
        hta_reason = hta_stuck_signals(
            result.action_history, predicted_tokens, budget_used_frac,
        )
        stuck = toolkit_stuck or terminal_stuck or hta_reason is not None
        if hta_reason is not None:
            reason = hta_reason
        elif toolkit_stuck:
            reason = "toolkit_repetition"
        elif terminal_stuck:
            reason = result.termination_reason
        else:
            reason = None
        return ExecutionOutcome(
            output=result.output,
            action_history=result.action_history,
            termination_reason=result.termination_reason,
            iterations=result.iterations,
            stuck=stuck,
            stuck_reason=reason,
        )
