"""Unified production inference entry point."""
from __future__ import annotations

import json
from datetime import date

from midas_agent.context.environment import EnvironmentContext
from midas_agent.inference.frozen_pricing import FrozenPricingEngine
from midas_agent.inference.production_meter import ProductionResourceMeter
from midas_agent.inference.schemas import GraphEmergenceArtifact
from midas_agent.llm.provider import LLMProvider
from midas_agent.llm.types import LLMRequest, LLMResponse
from midas_agent.stdlib.action import ActionRegistry
from midas_agent.stdlib.actions.task_done import TaskDoneAction
from midas_agent.stdlib.plan_execute_agent import PlanExecuteAgent
from midas_agent.stdlib.react_agent import AgentResult
from midas_agent.types import Issue
from midas_agent.workspace.config_evolution.config_schema import (
    ConfigMeta,
    StepConfig,
    WorkflowConfig,
)
from midas_agent.workspace.config_evolution.executor import DAGExecutor
from midas_agent.workspace.graph_emergence.free_agent_manager import FreeAgentManager


def run_inference(
    config_path: str,
    issue: Issue,
    llm_provider: LLMProvider,
    action_registry: ActionRegistry,
    budget: int | None = None,
) -> str | None:
    """Unified production entry point.

    Routes to config evolution or graph emergence based on file extension.
    Returns the patch string, or None if execution was aborted.
    """
    if config_path.endswith(".yaml") or config_path.endswith(".yml"):
        return _run_config_evolution(config_path, issue, llm_provider, action_registry, budget)
    elif config_path.endswith(".json"):
        return _run_graph_emergence(config_path, issue, llm_provider, action_registry, budget)
    else:
        raise ValueError(f"Unsupported config file type: {config_path}")


def _run_config_evolution(
    config_path: str,
    issue: Issue,
    llm_provider: LLMProvider,
    action_registry: ActionRegistry,
    budget: int | None,
) -> str | None:
    import yaml

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    meta = ConfigMeta(
        name=raw.get("meta", {}).get("name", ""),
        description=raw.get("meta", {}).get("description", ""),
    )
    steps = [
        StepConfig(
            id=s["id"],
            prompt=s.get("prompt", ""),
            tools=s.get("tools", []),
            inputs=s.get("inputs", []),
        )
        for s in raw.get("steps", [])
    ]
    config = WorkflowConfig(meta=meta, steps=steps)

    if budget is not None:
        meter = ProductionResourceMeter(llm_provider, budget)
        call_llm = lambda req: meter.process(req)
    else:
        call_llm = llm_provider.complete

    executor = DAGExecutor(action_registry)
    result = executor.execute(config, issue, call_llm)
    return result.patch


def _run_graph_emergence(
    config_path: str,
    issue: Issue,
    llm_provider: LLMProvider,
    action_registry: ActionRegistry,
    budget: int | None,
) -> str | None:
    with open(config_path) as f:
        raw = json.load(f)

    artifact = GraphEmergenceArtifact.model_validate(raw)

    # Build frozen pricing from artifact
    frozen_pricing = FrozenPricingEngine(artifact.agent_prices)

    # Build free agent manager with frozen prices
    free_agent_manager = FreeAgentManager(pricing_engine=frozen_pricing)
    for agent in artifact.free_agents:
        free_agent_manager.register(agent)

    # Set up metered LLM callback
    effective_budget = budget if budget is not None else artifact.budget_hint
    meter = ProductionResourceMeter(llm_provider, effective_budget)
    call_llm = lambda req: meter.process(req)

    # Build responsible agent
    system_prompt = artifact.responsible_agent.soul.system_prompt

    # Build environment context
    agent_lines = []
    for fa in artifact.free_agents:
        skill_desc = fa.skill.description if fa.skill else "no skill"
        price = artifact.agent_prices.get(fa.agent_id, 0)
        bankruptcy_rate = artifact.agent_bankruptcy_rates.get(fa.agent_id, 0.0)
        agent_lines.append(
            f"{fa.agent_id}: {skill_desc} "
            f"(price={price}, bankruptcy_rate={bankruptcy_rate:.0%})"
        )

    env_context = EnvironmentContext(
        cwd="/testbed",
        shell="bash",
        current_date=str(date.today()),
        balance=effective_budget,
        available_agents=agent_lines,
    )
    env_context_xml = env_context.serialize_to_xml()

    # Run PlanExecuteAgent
    agent = PlanExecuteAgent(
        system_prompt=system_prompt,
        actions=[TaskDoneAction()],
        call_llm=call_llm,
        env_context_xml=env_context_xml,
    )

    result = agent.run(context=issue.description)

    if result.termination_reason == "done":
        return result.output
    return None
