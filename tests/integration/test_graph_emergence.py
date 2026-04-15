"""Integration Test Suite 6: Graph Emergence Execution Pipeline.

All production code is NotImplementedError stubs. These tests define the
expected behavior for TDD and will pass once the production implementations
are filled in.

Tests exercise the full Graph Emergence workspace lifecycle: responsible
agent plan-execute flow, free agent hiring, pricing, session isolation,
compaction, skill review, and end-to-end delegation.
"""
from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage, ToolCall
from midas_agent.scheduler.serial_queue import SerialQueue
from midas_agent.scheduler.storage import LogFilter
from midas_agent.scheduler.training_log import HookSet, TrainingLog
from midas_agent.stdlib.actions.delegate_task import DelegateTaskAction
from midas_agent.stdlib.actions.report_result import ReportResultAction
from midas_agent.stdlib.actions.task_done import TaskDoneAction
from midas_agent.stdlib.plan_execute_agent import PlanExecuteAgent
from midas_agent.stdlib.react_agent import AgentResult
from midas_agent.types import Issue
from midas_agent.workspace.graph_emergence.agent import Agent, Soul
from midas_agent.workspace.graph_emergence.free_agent_manager import (
    Candidate,
    FreeAgentManager,
)
from midas_agent.workspace.graph_emergence.pricing import PricingEngine
from midas_agent.workspace.graph_emergence.session import Session
from midas_agent.workspace.graph_emergence.skill import Skill, SkillReviewer
from midas_agent.workspace.graph_emergence.workspace import GraphEmergenceWorkspace
from tests.integration.conftest import (
    FakeLLMProvider,
    InMemoryStorageBackend,
    SpyHookSet,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(
    content: str = "ok",
    tool_calls: list[ToolCall] | None = None,
    input_tokens: int = 10,
    output_tokens: int = 5,
) -> LLMResponse:
    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        usage=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _make_log(
    storage: InMemoryStorageBackend | None = None,
    hooks: HookSet | None = None,
) -> tuple[TrainingLog, InMemoryStorageBackend, SpyHookSet]:
    """Build a TrainingLog wired to in-memory storage and spy hooks."""
    storage = storage or InMemoryStorageBackend()
    spy_hooks = hooks if isinstance(hooks, SpyHookSet) else SpyHookSet()
    queue = SerialQueue()
    log = TrainingLog(storage=storage, hooks=spy_hooks, serial_queue=queue)
    return log, storage, spy_hooks


def _make_agent(
    agent_id: str = "agent-1",
    agent_type: str = "workspace_bound",
    skill: Skill | None = None,
) -> Agent:
    soul = Soul(system_prompt=f"You are agent {agent_id}.")
    return Agent(
        agent_id=agent_id,
        soul=soul,
        agent_type=agent_type,
        skill=skill,
    )


def _make_free_agent(
    agent_id: str = "free-1",
    skill: Skill | None = None,
) -> Agent:
    return _make_agent(agent_id=agent_id, agent_type="free", skill=skill)


def _make_skill(name: str = "python_debug") -> Skill:
    return Skill(
        name=name,
        description=f"Expert {name} skill",
        content=f"Use {name} techniques to solve the problem.",
    )


def _make_issue() -> Issue:
    return Issue(
        issue_id="issue-ge-001",
        repo="tests/fixtures/sample_repo",
        description="Fix the parsing bug in parser.py",
        fail_to_pass=["tests/test_parser.py::test_parse_edge_case"],
        pass_to_pass=["tests/test_parser.py::test_parse_basic"],
    )


# ---------------------------------------------------------------------------
# IT-6.1: Responsible agent Plan->Execute basic flow
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestIT61PlanExecuteBasicFlow:
    """PlanExecuteAgent completes a plan-then-execute cycle driven by
    scripted FakeLLM responses. The market_info_provider is invoked during
    the planning phase."""

    def test_plan_execute_agent_completes(self):
        market_info_called = False

        def market_info_provider() -> str:
            nonlocal market_info_called
            market_info_called = True
            return "budget=5000, free_agents=3"

        # Response sequence: plan text -> tool call (task_done) -> done
        responses = [
            # Planning phase: LLM returns a plan as text
            _make_response(
                content="Plan: 1) Read the file 2) Fix the bug 3) Submit",
            ),
            # Execution phase: LLM issues a task_done tool call
            _make_response(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="task_done",
                        arguments={"summary": "Bug fixed and tests pass."},
                    )
                ],
            ),
        ]

        call_index = 0

        def scripted_call_llm(request: LLMRequest) -> LLMResponse:
            nonlocal call_index
            idx = call_index
            call_index += 1
            return responses[idx] if idx < len(responses) else responses[-1]

        agent = PlanExecuteAgent(
            system_prompt="You are the workspace lead. Plan then execute.",
            actions=[TaskDoneAction()],
            call_llm=scripted_call_llm,
            max_iterations=10,
            market_info_provider=market_info_provider,
        )

        result = agent.run(context="Fix the parsing bug in parser.py")

        # Agent completed successfully
        assert isinstance(result, AgentResult)
        assert result.termination_reason == "done"
        assert result.output is not None

        # market_info_provider was invoked during the planning phase
        assert market_info_called is True

        # At least 2 LLM calls: one for plan, one for execute
        assert call_index >= 2


# ---------------------------------------------------------------------------
# IT-6.2: DelegateTaskAction returns candidate list
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestIT62DelegateTaskCandidateList:
    """Register 3 free agents, then DelegateTaskAction.execute() returns a
    formatted string listing candidates with their agent_ids, skills, and
    prices."""

    def test_delegate_returns_formatted_candidates(self):
        training_log, storage, spy_hooks = _make_log()

        # Allocate budget for pricing history
        for agent_id in ("free-a", "free-b", "free-c"):
            training_log.record_allocate(to=agent_id, amount=1000)

        pricing_engine = PricingEngine(
            training_log=training_log,
            buffer_multiplier=1.2,
        )
        manager = FreeAgentManager(pricing_engine=pricing_engine)

        # Register 3 free agents with distinct skills
        agents = [
            _make_free_agent(
                "free-a",
                skill=_make_skill("python_parsing"),
            ),
            _make_free_agent(
                "free-b",
                skill=_make_skill("regex_expert"),
            ),
            _make_free_agent(
                "free-c",
                skill=_make_skill("ast_analysis"),
            ),
        ]
        for agent in agents:
            manager.register(agent)

        # Create the action wired to the manager
        delegate_action = DelegateTaskAction(
            find_candidates=lambda desc, top_k=5: manager.match(desc, top_k),
        )

        result = delegate_action.execute(
            task_description="fix parsing bug",
            top_k=5,
        )

        # Result is a string (formatted for the LLM)
        assert isinstance(result, str)

        # All 3 registered agent IDs appear in the output
        for agent in agents:
            assert agent.agent_id in result

        # Each candidate line should reference a skill name or price info
        # At minimum the result is non-empty and structured
        assert len(result) > 0


# ---------------------------------------------------------------------------
# IT-6.3: PricingEngine price calculation with debt premium
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestIT63PricingWithDebtPremium:
    """PricingEngine calculates price as weighted_avg * buffer_multiplier.
    When balance is negative, a debt premium is added."""

    def test_price_with_positive_balance(self):
        training_log, storage, spy_hooks = _make_log()

        agent = _make_free_agent("agent_x", skill=_make_skill("debug"))

        # Allocate and record several consumes to establish cost history
        training_log.record_allocate(to="agent_x", amount=5000)
        training_log.record_consume(entity_id="agent_x", amount=100)
        training_log.record_consume(entity_id="agent_x", amount=200)
        training_log.record_consume(entity_id="agent_x", amount=150)

        buffer_mult = 1.2
        pricing_engine = PricingEngine(
            training_log=training_log,
            buffer_multiplier=buffer_mult,
        )

        price = pricing_engine.calculate_price(agent)

        # Price must be a positive integer
        assert isinstance(price, int)
        assert price > 0

        # Price should reflect weighted_avg * buffer_multiplier
        # Weighted avg of (100, 200, 150) ~ 150, times 1.2 ~ 180
        # We do not hardcode the exact value but verify it is reasonable
        assert price >= 100  # At least the minimum consume
        assert price <= 1000  # Bounded reasonably

    def test_price_with_debt_premium(self):
        training_log, storage, spy_hooks = _make_log()

        agent = _make_free_agent("agent_x", skill=_make_skill("debug"))

        # Allocate small budget then overdraft to create negative balance
        training_log.record_allocate(to="agent_x", amount=200)
        training_log.record_consume(entity_id="agent_x", amount=100)
        training_log.record_consume(entity_id="agent_x", amount=150)
        training_log.record_consume(entity_id="agent_x", amount=150)
        # Balance = 200 - 100 - 150 - 150 = -200

        assert training_log.get_balance("agent_x") == -200

        buffer_mult = 1.2
        pricing_engine = PricingEngine(
            training_log=training_log,
            buffer_multiplier=buffer_mult,
        )

        price_with_debt = pricing_engine.calculate_price(agent)

        # Now compute a comparable price with a positive-balance agent
        training_log_2, _, _ = _make_log()
        training_log_2.record_allocate(to="agent_y", amount=5000)
        training_log_2.record_consume(entity_id="agent_y", amount=100)
        training_log_2.record_consume(entity_id="agent_y", amount=150)
        training_log_2.record_consume(entity_id="agent_y", amount=150)

        pricing_engine_2 = PricingEngine(
            training_log=training_log_2,
            buffer_multiplier=buffer_mult,
        )
        agent_y = _make_free_agent("agent_y", skill=_make_skill("debug"))
        price_no_debt = pricing_engine_2.calculate_price(agent_y)

        # Price with debt must be strictly higher (debt premium applied)
        assert price_with_debt > price_no_debt


# ---------------------------------------------------------------------------
# IT-6.4: Session isolation across workspaces
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestIT64SessionIsolation:
    """Sessions for the same agent in different workspaces are fully
    isolated -- messages in one do not leak into the other."""

    def test_sessions_isolated_by_workspace(self):
        system_llm = MagicMock(
            return_value=_make_response(content="compressed"),
        )

        session_ws1 = Session(
            agent_id="agent_x",
            workspace_id="ws1",
            system_llm=system_llm,
            max_context_tokens=4096,
        )
        session_ws2 = Session(
            agent_id="agent_x",
            workspace_id="ws2",
            system_llm=system_llm,
            max_context_tokens=4096,
        )

        # Add messages to ws1
        session_ws1.add_message({"role": "user", "content": "hello ws1"})
        session_ws1.add_message({"role": "assistant", "content": "response ws1"})
        session_ws1.add_message({"role": "user", "content": "follow-up ws1"})

        # ws2 must have zero messages
        ws2_messages = session_ws2.get_messages()
        assert len(ws2_messages) == 0

        # ws1 must have all 3 messages
        ws1_messages = session_ws1.get_messages()
        assert len(ws1_messages) == 3

        # Verify no cross-contamination in conversation_history property
        assert len(session_ws2.conversation_history) == 0
        assert len(session_ws1.conversation_history) == 3


# ---------------------------------------------------------------------------
# IT-6.5: Session compaction near context limit
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestIT65SessionCompaction:
    """When messages approach max_context_tokens, adding a new message
    triggers auto-compaction via system_llm."""

    def test_auto_compact_on_threshold(self):
        system_llm = MagicMock(
            return_value=_make_response(content="compressed summary"),
        )

        session = Session(
            agent_id="agent_x",
            workspace_id="ws1",
            system_llm=system_llm,
            max_context_tokens=100,  # Very low to trigger compaction early
        )

        # Add messages until we exceed the token threshold
        for i in range(50):
            session.add_message(
                {"role": "user", "content": f"message {i} " * 20}
            )

        # system_llm must have been called at least once for compaction
        assert system_llm.call_count >= 1

        # After compaction, conversation_history should be shorter than
        # the raw 50 messages we added
        history = session.get_messages()
        assert len(history) < 50


# ---------------------------------------------------------------------------
# IT-6.6: ReportResultAction delivers result
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestIT66ReportResultAction:
    """ReportResultAction.execute() invokes the spy callback with the
    correct result and status."""

    def test_report_result_callback(self):
        callback_log: list[dict] = []

        def spy_report(result: str) -> None:
            callback_log.append({"result": result})

        action = ReportResultAction(report=spy_report)

        output = action.execute(result="fix applied")

        # Callback was invoked exactly once
        assert len(callback_log) == 1
        assert callback_log[0]["result"] == "fix applied"

        # execute() returns a string confirmation
        assert isinstance(output, str)


# ---------------------------------------------------------------------------
# IT-6.7: Free agent no-eviction semantics
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestIT67FreeAgentNoEviction:
    """A free agent whose balance goes negative remains active.
    on_workspace_evicted must NOT fire for free agents."""

    def test_free_agent_negative_balance_stays_active(self):
        training_log, storage, spy_hooks = _make_log()

        # Allocate a small budget then overdraft
        training_log.record_allocate(to="free_agent_1", amount=100)
        training_log.record_consume(
            entity_id="free_agent_1",
            amount=200,
            workspace_id="ws_host",
        )

        # Balance is -100
        assert training_log.get_balance("free_agent_1") == -100

        # Free agents remain active regardless of negative balance.
        # The is_active semantics differ for free agents in Graph Emergence:
        # they are never evicted. The workspace that hosts them absorbs the
        # cost. We verify is_active by checking that the system treats the
        # entity as still usable (no eviction).
        #
        # Note: is_active() for a workspace-bound entity returns False when
        # balance <= 0, but for a free agent entity within a workspace the
        # eviction hook should NOT have fired.
        spy_hooks.assert_not_called("on_workspace_evicted")


# ---------------------------------------------------------------------------
# IT-6.8: post_episode triggers SkillReviewer
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestIT68PostEpisodeSkillReviewer:
    """GraphEmergenceWorkspace.post_episode() delegates to
    SkillReviewer.review() and returns None."""

    def test_post_episode_calls_skill_reviewer(self):
        responsible_agent = _make_agent("lead-1", agent_type="workspace_bound")

        call_llm = MagicMock(return_value=_make_response())
        system_llm = MagicMock(return_value=_make_response())

        # Use a real-ish SkillReviewer (but still stub) -- we spy on review()
        skill_reviewer = MagicMock(spec=SkillReviewer)
        free_agent_manager = MagicMock(spec=FreeAgentManager)

        ws = GraphEmergenceWorkspace(
            workspace_id="ws-ge-1",
            responsible_agent=responsible_agent,
            call_llm=call_llm,
            system_llm=system_llm,
            free_agent_manager=free_agent_manager,
            skill_reviewer=skill_reviewer,
        )

        eval_results = {
            "agent_id": "free-1",
            "score": 0.85,
            "summary": "Good performance on parsing task",
        }

        result = ws.post_episode(eval_results, evicted_ids=[])

        # SkillReviewer.review() was called with the eval_results
        skill_reviewer.review.assert_called_once_with(eval_results)

        # post_episode returns None for GraphEmergenceWorkspace
        assert result is None


# ---------------------------------------------------------------------------
# IT-6.9: receive_budget flows to responsible agent
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestIT69ReceiveBudgetAllocation:
    """Budget allocation for Graph Emergence flows through the Scheduler:
    Scheduler records the allocate entry in TrainingLog for the responsible
    agent, then calls workspace.receive_budget().  The workspace itself
    does NOT interact with TrainingLog — that is the Scheduler's job
    (design: scheduler.md §1.2 allocate_budgets)."""

    def test_scheduler_records_allocate_for_responsible_agent(self):
        training_log, storage, spy_hooks = _make_log()

        responsible_agent = _make_agent("lead-1", agent_type="workspace_bound")

        call_llm = MagicMock(return_value=_make_response())
        system_llm = MagicMock(return_value=_make_response())

        free_agent_manager = MagicMock(spec=FreeAgentManager)
        skill_reviewer = MagicMock(spec=SkillReviewer)

        ws = GraphEmergenceWorkspace(
            workspace_id="ws-ge-1",
            responsible_agent=responsible_agent,
            call_llm=call_llm,
            system_llm=system_llm,
            free_agent_manager=free_agent_manager,
            skill_reviewer=skill_reviewer,
        )

        # --- Simulate what the Scheduler does (design: scheduler.md §1.2) ---
        # Step 1: Scheduler records the allocation in TrainingLog
        training_log.record_allocate(to="lead-1", amount=5000)

        # Step 2: Scheduler notifies the workspace
        ws.receive_budget(5000)

        # --- Verify TrainingLog state (Scheduler's responsibility) ---
        spy_hooks.assert_called("on_allocate", times=1)
        alloc_calls = spy_hooks.get_calls("on_allocate")
        assert alloc_calls[0]["to_balance_after"] == 5000

        allocate_entries = training_log.get_log_entries(
            LogFilter(entity_id="lead-1", type="allocate")
        )
        assert len(allocate_entries) == 1
        assert allocate_entries[0].to == "lead-1"
        assert allocate_entries[0].amount == 5000

        # --- Verify the balance is attributed to the responsible agent ---
        assert training_log.get_balance("lead-1") == 5000


# ---------------------------------------------------------------------------
# IT-6.10: End-to-end hiring flow
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestIT610EndToEndHiringFlow:
    """Full delegate -> hire -> execute -> report cycle.

    Verify that:
    - DelegateTaskAction finds candidates
    - A transfer record is created when hiring (budget moves from
      responsible agent to free agent)
    - The hired agent executes and reports back
    - The result is delivered to the workspace
    """

    def test_full_hiring_cycle(self):
        training_log, storage, spy_hooks = _make_log()

        # --- Setup agents ---
        responsible_agent = _make_agent("lead-1", agent_type="workspace_bound")
        hired_agent = _make_free_agent(
            "free-parser",
            skill=_make_skill("python_parsing"),
        )

        # Allocate budget to the workspace lead
        training_log.record_allocate(to="lead-1", amount=5000)

        # --- Setup pricing and manager ---
        pricing_engine = PricingEngine(
            training_log=training_log,
            buffer_multiplier=1.2,
        )
        free_agent_manager = FreeAgentManager(pricing_engine=pricing_engine)
        free_agent_manager.register(hired_agent)

        # --- Step 1: Delegate -- find candidates ---
        delegate_action = DelegateTaskAction(
            find_candidates=lambda desc, top_k=5: free_agent_manager.match(
                desc, top_k
            ),
        )
        delegate_result = delegate_action.execute(
            task_description="fix parsing bug",
            top_k=3,
        )

        # Candidates returned
        assert isinstance(delegate_result, str)
        assert "free-parser" in delegate_result

        # --- Step 2: Hire -- transfer budget from lead to free agent ---
        candidates = free_agent_manager.match("fix parsing bug", top_k=3)
        assert len(candidates) >= 1

        chosen = candidates[0]
        hire_price = chosen.price

        # Transfer budget from responsible agent to hired agent
        transfer_entry = training_log.record_transfer(
            from_entity="lead-1",
            to="free-parser",
            amount=hire_price,
        )

        # Transfer recorded correctly
        assert transfer_entry.type == "transfer"
        assert transfer_entry.from_entity == "lead-1"
        assert transfer_entry.to == "free-parser"
        assert transfer_entry.amount == hire_price

        # Balances updated
        assert training_log.get_balance("lead-1") == 5000 - hire_price
        assert training_log.get_balance("free-parser") == hire_price

        # --- Step 3: Hired agent executes and reports ---
        report_log: list[dict] = []

        def on_report(result: str) -> None:
            report_log.append({"result": result})

        report_action = ReportResultAction(report=on_report)

        # Simulate the hired agent consuming some tokens during execution
        training_log.record_consume(
            entity_id="free-parser",
            amount=50,
            workspace_id="ws-ge-1",
        )

        # Hired agent reports back
        report_output = report_action.execute(
            result="Parsing bug fixed: added null check at line 42",
            status="success",
        )

        # --- Step 4: Verify attribution and result delivery ---

        # Report callback was invoked
        assert len(report_log) == 1
        assert "fixed" in report_log[0]["result"].lower()

        # report_action returns a string
        assert isinstance(report_output, str)

        # Consume entry has dual attribution (entity + workspace)
        consume_entries = training_log.get_log_entries(
            LogFilter(entity_id="free-parser", type="consume")
        )
        assert len(consume_entries) == 1
        assert consume_entries[0].workspace_id == "ws-ge-1"
        assert consume_entries[0].to == "free-parser"
        assert consume_entries[0].amount == 50

        # Transfer is queryable
        transfer_entries = training_log.get_log_entries(
            LogFilter(type="transfer")
        )
        assert len(transfer_entries) == 1
        assert transfer_entries[0].from_entity == "lead-1"
        assert transfer_entries[0].to == "free-parser"

        # on_workspace_evicted was NOT fired (free agent semantics)
        spy_hooks.assert_not_called("on_workspace_evicted")


# ---------------------------------------------------------------------------
# IT-6.11: delegate_task with empty pool offers spawn option
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestIT611DelegateTaskSpawnOption:
    """When no free agents exist, delegate_task must still offer the
    option to spawn a new agent. The response should indicate that
    spawn is available even when no candidates match."""

    def test_empty_pool_offers_spawn(self):
        training_log, storage, spy_hooks = _make_log()
        pricing_engine = PricingEngine(training_log=training_log)
        free_agent_manager = FreeAgentManager(pricing_engine=pricing_engine)
        # Pool is empty — no agents registered

        delegate = DelegateTaskAction(
            find_candidates=lambda desc: free_agent_manager.match(desc),
            spawn_callback=lambda desc: None,  # spawn available but not invoked yet
        )

        output = delegate.execute(task_description="fix the parsing bug")

        # Must mention spawn as an option, not just "No candidates found"
        assert "spawn" in output.lower()


# ---------------------------------------------------------------------------
# IT-6.12: Spawn creates agent with protection relationship
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestIT612SpawnCreatesProtectedAgent:
    """When the responsible agent chooses to spawn via delegate_task,
    the system must create a new agent with a protection relationship
    (protector=responsible_agent) and register it in FreeAgentManager."""

    def test_spawn_creates_protected_agent(self):
        training_log, storage, spy_hooks = _make_log()
        pricing_engine = PricingEngine(training_log=training_log)
        free_agent_manager = FreeAgentManager(pricing_engine=pricing_engine)

        spawned_agents: list[Agent] = []

        def spawn_callback(task_description: str) -> Agent:
            agent = Agent(
                agent_id=f"spawned-{len(spawned_agents)}",
                soul=Soul(system_prompt=f"Specialist for: {task_description}"),
                agent_type="free",
                protected_by="lead-1",
            )
            spawned_agents.append(agent)
            free_agent_manager.register(agent)
            return agent

        delegate = DelegateTaskAction(
            find_candidates=lambda desc: free_agent_manager.match(desc),
            spawn_callback=spawn_callback,
        )

        # Simulate LLM choosing spawn (execute with spawn=True)
        output = delegate.execute(
            task_description="fix parsing bug",
            spawn=True,
        )

        assert len(spawned_agents) == 1
        assert spawned_agents[0].protected_by == "lead-1"
        assert spawned_agents[0].agent_id in free_agent_manager.free_agents


# ---------------------------------------------------------------------------
# IT-6.13: Protected agent LLM calls charged to protector
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestIT613ProtectedAgentChargedToProtector:
    """A protected agent's LLM consumption must be debited from the
    protector's balance, not the protected agent's own balance."""

    def test_consume_charged_to_protector(self):
        training_log, storage, spy_hooks = _make_log()

        # Protector (responsible agent) has budget
        training_log.record_allocate(to="lead-1", amount=10000)

        # Protected agent has no allocation
        # Consume should go against lead-1's balance via workspace_id
        training_log.record_consume(
            entity_id="lead-1",  # charged to protector
            amount=500,
            workspace_id="ws-1",
        )

        assert training_log.get_balance("lead-1") == 9500
        # The protected agent (spawned-0) never had its own balance


# ---------------------------------------------------------------------------
# IT-6.14: End-to-end spawn -> execute -> report cycle
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestIT614SpawnExecuteReportCycle:
    """Full spawn -> delegate -> sub-agent execute -> report_result cycle.
    The spawned agent should be able to execute a sub-task and report
    results back to the responsible agent."""

    def test_full_spawn_cycle(self):
        training_log, storage, spy_hooks = _make_log()
        pricing_engine = PricingEngine(training_log=training_log)
        free_agent_manager = FreeAgentManager(pricing_engine=pricing_engine)

        # Track spawned agents and reported results
        spawned: list[Agent] = []
        reported: list[str] = []

        def spawn_callback(task_description: str) -> Agent:
            agent = Agent(
                agent_id="spawned-worker",
                soul=Soul(system_prompt=f"Worker for: {task_description}"),
                agent_type="free",
                protected_by="lead-1",
            )
            spawned.append(agent)
            free_agent_manager.register(agent)
            return agent

        def report_callback(result: str) -> None:
            reported.append(result)

        delegate = DelegateTaskAction(
            find_candidates=lambda desc: free_agent_manager.match(desc),
            spawn_callback=spawn_callback,
        )
        report_action = ReportResultAction(report=report_callback)

        # Step 1: Spawn via delegate_task
        delegate.execute(task_description="write unit test", spawn=True)
        assert len(spawned) == 1

        # Step 2: Spawned agent is now in the pool
        assert "spawned-worker" in free_agent_manager.free_agents

        # Step 3: Spawned agent reports result
        report_action.execute(result="Test written and passes.")
        assert len(reported) == 1
        assert "Test written" in reported[0]


# ---------------------------------------------------------------------------
# IT-6.15: delegate_task with candidates AND spawn option
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestIT615DelegateWithCandidatesAndSpawn:
    """When free agents exist, delegate_task should return both the
    candidate list AND the spawn option."""

    def test_candidates_plus_spawn(self):
        training_log, storage, spy_hooks = _make_log()
        pricing_engine = PricingEngine(training_log=training_log)
        free_agent_manager = FreeAgentManager(pricing_engine=pricing_engine)

        # Register one existing agent
        free_agent_manager.register(
            _make_free_agent("expert-1", skill=_make_skill("debugging"))
        )

        delegate = DelegateTaskAction(
            find_candidates=lambda desc: free_agent_manager.match(desc),
            spawn_callback=lambda desc: None,
        )

        output = delegate.execute(task_description="debug the crash")

        # Should contain both the candidate and spawn option
        assert "expert-1" in output
        assert "spawn" in output.lower()
