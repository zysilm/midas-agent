"""ReactAgent — re-exported from llm-agent-toolkit.

Also re-exports BudgetExhaustedError (used by scheduler) and provides
a helper to build StepJudge-based completion validators.
"""
from llm_agent_toolkit.stdlib.react_agent import (  # noqa: F401
    ReactAgent,
    ActionRecord,
    AgentResult,
)
from llm_agent_toolkit.types import BudgetExhaustedError  # noqa: F401


def make_step_judge_validator(system_llm, goal="Fix the issue described in the task"):
    """Build a completion_validator callback backed by StepJudge.

    Use this when creating a ReactAgent that needs LLM-based validation
    of the agent's claim to be done:

        agent = ReactAgent(
            ...,
            completion_validator=make_step_judge_validator(system_llm),
        )
    """
    from midas_agent.workspace.config_evolution.step_judge import StepJudge

    judge = StepJudge(system_llm)

    def validator(action_history, text):
        trace = StepJudge.format_trace_for_judge(action_history, len(action_history))
        verdict = judge.validate_completion(goal=goal, trace=trace, agent_message=text)
        if verdict.done:
            return None  # accept
        return (
            "You haven't completed the task yet. "
            "Continue working on the issue — use tools to make progress."
        )

    return validator
