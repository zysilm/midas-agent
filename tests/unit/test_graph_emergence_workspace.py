"""Unit tests for GraphEmergenceWorkspace."""
from unittest.mock import MagicMock, patch

import pytest

from midas_agent.workspace.graph_emergence.workspace import GraphEmergenceWorkspace
from midas_agent.workspace.base import Workspace
from midas_agent.workspace.graph_emergence.agent import Agent, Soul
from midas_agent.workspace.graph_emergence.free_agent_manager import FreeAgentManager
from midas_agent.workspace.graph_emergence.pricing import PricingEngine
from midas_agent.workspace.graph_emergence.skill import Skill, SkillReviewer
from midas_agent.scheduler.serial_queue import SerialQueue
from midas_agent.scheduler.training_log import HookSet, TrainingLog
from midas_agent.stdlib.plan_execute_agent import PlanExecuteAgent
from midas_agent.scheduler.hiring_manager import HiringManager
from midas_agent.stdlib.actions.delegate_task import DelegateTaskAction
from midas_agent.types import Issue
from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage

from tests.unit.conftest import InMemoryStorageBackend


@pytest.mark.unit
class TestGraphEmergenceWorkspace:
    """Tests for the GraphEmergenceWorkspace class."""

    def _make_call_llm(self):
        """Create a scripted call_llm that returns plan -> task_done."""
        from midas_agent.llm.types import ToolCall

        responses = [
            LLMResponse(content="Plan: fix the bug.", tool_calls=None, usage=TokenUsage(input_tokens=10, output_tokens=5)),
            LLMResponse(content=None, tool_calls=[ToolCall(id="c1", name="search_code", arguments={"pattern": "bug"})], usage=TokenUsage(input_tokens=10, output_tokens=5)),
            LLMResponse(content=None, tool_calls=[ToolCall(id="c2", name="task_done", arguments={})], usage=TokenUsage(input_tokens=10, output_tokens=5)),
        ]
        idx = {"i": 0}

        def call_llm(request):
            i = idx["i"]
            idx["i"] += 1
            return responses[i] if i < len(responses) else responses[-1]

        return call_llm

    def _make_agent(self) -> Agent:
        """Create a test responsible Agent."""
        soul = Soul(system_prompt="You are the workspace lead.")
        return Agent(agent_id="lead-1", soul=soul, agent_type="workspace_bound")

    def _make_workspace(self) -> GraphEmergenceWorkspace:
        """Create a GraphEmergenceWorkspace with mocked dependencies."""
        return GraphEmergenceWorkspace(
            workspace_id="ws-ge-1",
            responsible_agent=self._make_agent(),
            call_llm=self._make_call_llm(),
            system_llm=self._make_call_llm(),
            free_agent_manager=MagicMock(spec=FreeAgentManager),
            skill_reviewer=MagicMock(spec=SkillReviewer),
        )

    def test_is_workspace_subclass(self):
        """GraphEmergenceWorkspace is a subclass of Workspace."""
        assert issubclass(GraphEmergenceWorkspace, Workspace)

    def test_construction(self):
        """GraphEmergenceWorkspace can be constructed with all required arguments."""
        ws = self._make_workspace()

        assert ws is not None

    def test_receive_budget(self):
        """receive_budget() accepts a token budget amount."""
        ws = self._make_workspace()

        ws.receive_budget(1000)  # Should not raise

    def test_execute_starts_plan_execute(self):
        """execute() starts the PlanExecuteAgent to handle the issue."""
        ws = self._make_workspace()
        issue = Issue(
            issue_id="issue-1",
            repo="test/repo",
            description="Implement feature X",
        )

        ws.execute(issue)  # Should not raise; internally starts PlanExecuteAgent

    def test_submit_patch(self):
        """submit_patch() sets _last_patch on the workspace."""
        ws = self._make_workspace()

        ws.submit_patch()  # Should not raise
        assert hasattr(ws, "_last_patch")

    def test_submit_patch_writes_git_diff(self, tmp_path):
        """submit_patch() stores git diff in _last_patch and writes to disk."""
        import subprocess

        # Set up a git repo with a change
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=repo, capture_output=True)
        (repo / "file.py").write_text("original\n")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)
        (repo / "file.py").write_text("modified\n")

        patches_dir = tmp_path / "patches"
        ws = self._make_workspace()
        ws.work_dir = str(repo)
        ws._patches_dir = str(patches_dir)

        ws.submit_patch()

        # _last_patch must contain the diff content
        assert "original" in ws._last_patch
        assert "modified" in ws._last_patch

        # Audit file should also be written
        ws_patches = patches_dir / "ws-ge-1"
        assert ws_patches.is_dir()
        patch_files = list(ws_patches.glob("*.patch"))
        assert len(patch_files) == 1
        content = patch_files[0].read_text()
        assert "original" in content
        assert "modified" in content

    def test_submit_patch_empty_diff(self, tmp_path):
        """submit_patch() sets _last_patch to empty string when no changes."""
        import subprocess

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=repo, capture_output=True)
        (repo / "file.py").write_text("unchanged\n")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)

        patches_dir = tmp_path / "patches"
        ws = self._make_workspace()
        ws.work_dir = str(repo)
        ws._patches_dir = str(patches_dir)

        ws.submit_patch()

        assert ws._last_patch == ""

    def test_post_episode_returns_none(self):
        """post_episode() always returns None for GraphEmergenceWorkspace (no config evolution)."""
        ws = self._make_workspace()

        result = ws.post_episode({"ws-ge-1": {"s_exec": 0.8, "s_w": 0.8}}, evicted_ids=[])

        assert result is None

    def test_execute_passes_balance_provider_to_plan_execute_agent(self):
        """execute() must pass a balance_provider to PlanExecuteAgent so the
        agent sees its token balance after every tool result."""
        ws = self._make_workspace()
        ws.receive_budget(8000)

        issue = Issue(
            issue_id="issue-1",
            repo="test/repo",
            description="Fix bug",
        )

        captured_kwargs: dict = {}
        original_init = PlanExecuteAgent.__init__

        def spy_init(self_agent, *args, **kwargs):
            captured_kwargs.update(kwargs)
            original_init(self_agent, *args, **kwargs)

        with patch.object(PlanExecuteAgent, "__init__", spy_init):
            ws.execute(issue)

        assert "balance_provider" in captured_kwargs, \
            "PlanExecuteAgent must receive balance_provider"
        assert captured_kwargs["balance_provider"] is not None
        assert callable(captured_kwargs["balance_provider"])

    def test_execute_passes_hiring_manager_to_delegate_task(self):
        """execute() must pass a HiringManager to DelegateTaskAction."""
        ws = self._make_workspace()
        ws.receive_budget(6000)

        issue = Issue(
            issue_id="issue-1",
            repo="test/repo",
            description="Fix bug",
        )

        captured_kwargs: dict = {}
        original_init = DelegateTaskAction.__init__

        def spy_init(self_action, *args, **kwargs):
            captured_kwargs.update(kwargs)
            original_init(self_action, *args, **kwargs)

        with patch.object(DelegateTaskAction, "__init__", spy_init):
            ws.execute(issue)

        assert "hiring_manager" in captured_kwargs, \
            "DelegateTaskAction must receive hiring_manager"
        assert isinstance(captured_kwargs["hiring_manager"], HiringManager)

    def test_balance_provider_returns_current_budget(self):
        """The balance_provider callback must return the workspace's current budget."""
        ws = self._make_workspace()
        ws.receive_budget(5000)

        issue = Issue(
            issue_id="issue-1",
            repo="test/repo",
            description="Fix bug",
        )

        captured_provider = None
        original_init = PlanExecuteAgent.__init__

        def spy_init(self_agent, *args, **kwargs):
            nonlocal captured_provider
            captured_provider = kwargs.get("balance_provider")
            original_init(self_agent, *args, **kwargs)

        with patch.object(PlanExecuteAgent, "__init__", spy_init):
            ws.execute(issue)

        assert captured_provider is not None
        assert captured_provider() == 5000

    def test_env_context_xml_contains_real_info(self):
        """env_context_xml must contain real budget and environment info,
        not hardcoded placeholders."""
        ws = self._make_workspace()
        ws.receive_budget(7500)

        issue = Issue(
            issue_id="issue-1",
            repo="test/repo",
            description="Fix bug",
        )

        captured_xml = None
        original_init = PlanExecuteAgent.__init__

        def spy_init(self_agent, *args, **kwargs):
            nonlocal captured_xml
            captured_xml = kwargs.get("env_context_xml")
            original_init(self_agent, *args, **kwargs)

        with patch.object(PlanExecuteAgent, "__init__", spy_init):
            ws.execute(issue)

        assert captured_xml is not None
        assert isinstance(captured_xml, str)
        assert "environment_context" in captured_xml

    def test_execute_hiring_manager_has_correct_config(self):
        """execute() must create a HiringManager with the responsible agent's
        system_llm and parent_system_prompt."""
        ws = self._make_workspace()

        issue = Issue(
            issue_id="issue-1",
            repo="test/repo",
            description="Fix bug",
        )

        captured_kwargs: dict = {}
        original_init = DelegateTaskAction.__init__

        def spy_init(self_action, *args, **kwargs):
            captured_kwargs.update(kwargs)
            original_init(self_action, *args, **kwargs)

        with patch.object(DelegateTaskAction, "__init__", spy_init):
            ws.execute(issue)

        assert "hiring_manager" in captured_kwargs, \
            "DelegateTaskAction must receive hiring_manager"
        hm = captured_kwargs["hiring_manager"]
        assert hm._parent_system_prompt == "You are the workspace lead."

    def test_env_context_lists_agents_with_prices(self):
        """env_context_xml must list available free agents with their
        prices, so the LLM can plan delegation during the planning phase."""
        storage = InMemoryStorageBackend()
        training_log = TrainingLog(
            storage=storage, hooks=HookSet(), serial_queue=SerialQueue(),
        )
        pricing_engine = PricingEngine(training_log=training_log)
        free_agent_manager = FreeAgentManager(pricing_engine=pricing_engine)

        # Register two agents with skills
        agent_a = Agent(
            agent_id="expert-a",
            soul=Soul(system_prompt="expert"),
            agent_type="free",
            skill=Skill(name="debugging", description="Debug expert", content="..."),
        )
        agent_b = Agent(
            agent_id="expert-b",
            soul=Soul(system_prompt="expert"),
            agent_type="free",
            skill=Skill(name="testing", description="Test writer", content="..."),
        )
        free_agent_manager.register(agent_a)
        free_agent_manager.register(agent_b)

        ws = GraphEmergenceWorkspace(
            workspace_id="ws-ge-1",
            responsible_agent=self._make_agent(),
            call_llm=self._make_call_llm(),
            system_llm=self._make_call_llm(),
            free_agent_manager=free_agent_manager,
            skill_reviewer=MagicMock(spec=SkillReviewer),
        )
        ws.receive_budget(10000)

        issue = Issue(issue_id="issue-1", repo="test/repo", description="Fix bug")

        captured_xml = None
        original_init = PlanExecuteAgent.__init__

        def spy_init(self_agent, *args, **kwargs):
            nonlocal captured_xml
            captured_xml = kwargs.get("env_context_xml")
            original_init(self_agent, *args, **kwargs)

        with patch.object(PlanExecuteAgent, "__init__", spy_init):
            ws.execute(issue)

        assert captured_xml is not None
        # Must list both agents by ID
        assert "expert-a" in captured_xml, f"env_context must list agent IDs: {captured_xml}"
        assert "expert-b" in captured_xml, f"env_context must list agent IDs: {captured_xml}"
        # Must include prices (integers from PricingEngine)
        price_a = pricing_engine.calculate_price(agent_a)
        price_b = pricing_engine.calculate_price(agent_b)
        assert str(price_a) in captured_xml, f"env_context must include price {price_a}: {captured_xml}"
        assert str(price_b) in captured_xml, f"env_context must include price {price_b}: {captured_xml}"

    def test_env_context_includes_balance(self):
        """env_context_xml must include the workspace's current token balance."""
        ws = self._make_workspace()
        ws.receive_budget(42000)

        issue = Issue(issue_id="issue-1", repo="test/repo", description="Fix bug")

        captured_xml = None
        original_init = PlanExecuteAgent.__init__

        def spy_init(self_agent, *args, **kwargs):
            nonlocal captured_xml
            captured_xml = kwargs.get("env_context_xml")
            original_init(self_agent, *args, **kwargs)

        with patch.object(PlanExecuteAgent, "__init__", spy_init):
            ws.execute(issue)

        assert captured_xml is not None
        assert "42000" in captured_xml, f"env_context must include balance: {captured_xml}"
