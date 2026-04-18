"""Integration tests for the full inference pipeline.

Tests exercise the complete flow:
- Default artifact → single agent inference
- Trained artifact → multi-agent inference
- Train export → infer import → works end-to-end
- LLM config resolution in pipeline context
- Action injection into running agent

Tests are expected to FAIL until the CLI/resolver/TUI are implemented.
"""
from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from midas_agent.inference.schemas import GraphEmergenceArtifact
from midas_agent.llm.types import LLMResponse, TokenUsage, ToolCall
from midas_agent.workspace.graph_emergence.agent import Agent, Soul
from midas_agent.workspace.graph_emergence.skill import Skill


# ===================================================================
# Helpers
# ===================================================================


def _make_llm_response(content="ok", tool_calls=None, tokens=15):
    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        usage=TokenUsage(input_tokens=tokens, output_tokens=tokens),
    )


def _task_done_response(result="done"):
    return _make_llm_response(
        content=None,
        tool_calls=[ToolCall(id="tc-1", name="task_done", arguments={"result": result})],
    )


def _make_trained_artifact() -> GraphEmergenceArtifact:
    """Build a realistic trained artifact with multiple agents."""
    responsible = Agent(
        agent_id="resp-1",
        soul=Soul(system_prompt="You are a senior engineer. Coordinate the team."),
        agent_type="workspace_bound",
        skill=Skill(name="coordinate", description="Coordination", content="Plan and delegate."),
        protecting=["fa-debug", "fa-search"],
    )
    free_agents = [
        Agent(
            agent_id="fa-debug",
            soul=Soul(system_prompt="You are a debugging specialist."),
            agent_type="free",
            skill=Skill(name="debug", description="Debugging expert", content="Use pdb and logging."),
            protected_by="resp-1",
        ),
        Agent(
            agent_id="fa-search",
            soul=Soul(system_prompt="You are a code search specialist."),
            agent_type="free",
            skill=Skill(name="search", description="Code search", content="Use grep and find."),
            protected_by="resp-1",
        ),
    ]
    return GraphEmergenceArtifact(
        responsible_agent=responsible,
        free_agents=free_agents,
        agent_prices={"fa-debug": 1200, "fa-search": 800},
        agent_bankruptcy_rates={"fa-debug": 0.05, "fa-search": 0.0},
        last_etas={"ws-1": 1.5},
        adaptive_multiplier_value=1.2,
        total_episodes=10,
        budget_hint=50000,
    )


def _write_artifact(artifact: GraphEmergenceArtifact, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(artifact.model_dump_json(indent=2))


# ===================================================================
# 1. Inference with default artifact (no training)
# ===================================================================


@pytest.mark.integration
class TestInferWithDefaultArtifact:
    """When no trained artifact exists, inference uses the package default."""

    def test_default_artifact_loads_and_runs(self, tmp_path):
        """Single-agent inference works with the default artifact."""
        from midas_agent.resolver import resolve_artifact_path

        # No .midas/agents/ in tmp_path -> falls back to package default
        artifact_path = resolve_artifact_path(cwd=str(tmp_path))

        with open(artifact_path) as f:
            artifact = GraphEmergenceArtifact.model_validate_json(f.read())

        assert artifact.responsible_agent is not None
        assert artifact.responsible_agent.agent_type == "workspace_bound"

    def test_default_agent_produces_result_with_actions(self, tmp_path):
        """Default agent, given full actions, can execute and return a result."""
        from midas_agent.cli import build_action_set
        from midas_agent.resolver import resolve_artifact_path
        from midas_agent.stdlib.plan_execute_agent import PlanExecuteAgent

        artifact_path = resolve_artifact_path(cwd=str(tmp_path))
        with open(artifact_path) as f:
            artifact = GraphEmergenceArtifact.model_validate_json(f.read())

        actions = build_action_set(cwd=str(tmp_path), env="local")

        call_llm = MagicMock(return_value=_task_done_response("patch applied"))

        agent = PlanExecuteAgent(
            system_prompt=artifact.responsible_agent.soul.system_prompt,
            actions=actions,
            call_llm=call_llm,
        )
        result = agent.run(context="Fix the bug")
        assert result.termination_reason == "done"


# ===================================================================
# 2. Inference with trained artifact
# ===================================================================


@pytest.mark.integration
class TestInferWithTrainedArtifact:
    """Trained artifact is loaded and its full agent graph is usable."""

    def test_project_level_artifact_used(self, tmp_path):
        """Artifact in .midas/agents/ is found and loaded."""
        from midas_agent.resolver import resolve_artifact_path

        artifact = _make_trained_artifact()
        artifact_path = tmp_path / ".midas" / "agents" / "graph_emergence_artifact.json"
        _write_artifact(artifact, str(artifact_path))

        resolved = resolve_artifact_path(cwd=str(tmp_path))
        assert resolved == str(artifact_path)

        with open(resolved) as f:
            loaded = GraphEmergenceArtifact.model_validate_json(f.read())
        assert len(loaded.free_agents) == 2
        assert loaded.responsible_agent.agent_id == "resp-1"

    def test_explicit_artifact_overrides_project(self, tmp_path):
        """--artifact flag overrides project-level .midas/agents/."""
        from midas_agent.resolver import resolve_artifact_path

        # Project-level
        project_artifact = _make_trained_artifact()
        project_path = tmp_path / ".midas" / "agents" / "graph_emergence_artifact.json"
        _write_artifact(project_artifact, str(project_path))

        # Explicit — different artifact (no free agents)
        explicit_artifact = GraphEmergenceArtifact(
            responsible_agent=Agent(
                agent_id="explicit",
                soul=Soul(system_prompt="Explicit agent"),
                agent_type="workspace_bound",
            ),
            budget_hint=9999,
        )
        explicit_path = str(tmp_path / "custom.json")
        _write_artifact(explicit_artifact, explicit_path)

        resolved = resolve_artifact_path(explicit=explicit_path, cwd=str(tmp_path))
        with open(resolved) as f:
            loaded = GraphEmergenceArtifact.model_validate_json(f.read())
        assert loaded.responsible_agent.agent_id == "explicit"
        assert loaded.budget_hint == 9999

    def test_trained_artifact_agent_graph_intact(self, tmp_path):
        """Multi-agent team from training preserves all fields after load."""
        artifact = _make_trained_artifact()
        artifact_path = str(tmp_path / "artifact.json")
        _write_artifact(artifact, artifact_path)

        with open(artifact_path) as f:
            loaded = GraphEmergenceArtifact.model_validate_json(f.read())

        # Protection chain intact
        assert loaded.responsible_agent.protecting == ["fa-debug", "fa-search"]
        fa_map = {a.agent_id: a for a in loaded.free_agents}
        assert fa_map["fa-debug"].protected_by == "resp-1"
        assert fa_map["fa-debug"].skill.name == "debug"

        # Economic state intact
        assert loaded.agent_prices["fa-debug"] == 1200
        assert loaded.agent_bankruptcy_rates["fa-debug"] == pytest.approx(0.05)

    def test_free_agent_manager_rebuilt_from_artifact(self, tmp_path):
        """FreeAgentManager can be populated from loaded artifact and match works."""
        from midas_agent.inference.frozen_pricing import FrozenPricingEngine
        from midas_agent.workspace.graph_emergence.free_agent_manager import FreeAgentManager

        artifact = _make_trained_artifact()
        artifact_path = str(tmp_path / "artifact.json")
        _write_artifact(artifact, artifact_path)

        with open(artifact_path) as f:
            loaded = GraphEmergenceArtifact.model_validate_json(f.read())

        manager = FreeAgentManager(pricing_engine=FrozenPricingEngine(loaded.agent_prices))
        for fa in loaded.free_agents:
            manager.register(fa)

        candidates = manager.match("debug the error", top_k=2)
        assert len(candidates) == 2
        # Debug agent should match "debug" keyword
        ids = [c.agent.agent_id for c in candidates]
        assert "fa-debug" in ids


# ===================================================================
# 3. Train → export → infer pipeline
# ===================================================================


@pytest.mark.integration
class TestTrainExportToInfer:
    """Training artifacts land in .midas/agents/ and are loadable by inference."""

    def test_export_creates_midas_agents_dir(self, tmp_path):
        """Exporter creates .midas/agents/ directory if it doesn't exist."""
        from midas_agent.inference.exporter import export_graph_emergence

        responsible = Agent(
            agent_id="r", soul=Soul(system_prompt="coord"),
            agent_type="workspace_bound",
        )
        free_agents = [
            Agent(agent_id="fa-1", soul=Soul(system_prompt="helper"),
                  agent_type="free", skill=Skill(name="s", description="d", content="c")),
        ]
        pricing = MagicMock()
        pricing.calculate_price.return_value = 500

        output_path = str(tmp_path / ".midas" / "agents" / "graph_emergence_artifact.json")
        export_graph_emergence(
            responsible_agent=responsible,
            free_agents=free_agents,
            pricing_engine=pricing,
            hire_counts={},
            bankruptcy_counts={},
            budget_hint=20000,
            output_path=output_path,
        )

        assert os.path.isfile(output_path)

    def test_exported_artifact_loadable_by_resolver(self, tmp_path):
        """Artifact exported to .midas/agents/ is found by resolve_artifact_path."""
        from midas_agent.inference.exporter import export_graph_emergence
        from midas_agent.resolver import resolve_artifact_path

        responsible = Agent(
            agent_id="r", soul=Soul(system_prompt="coord"),
            agent_type="workspace_bound",
        )
        pricing = MagicMock()
        pricing.calculate_price.return_value = 100

        output_path = str(tmp_path / ".midas" / "agents" / "graph_emergence_artifact.json")
        export_graph_emergence(
            responsible_agent=responsible,
            free_agents=[],
            pricing_engine=pricing,
            hire_counts={},
            bankruptcy_counts={},
            budget_hint=10000,
            output_path=output_path,
        )

        resolved = resolve_artifact_path(cwd=str(tmp_path))
        assert resolved == output_path

        with open(resolved) as f:
            loaded = GraphEmergenceArtifact.model_validate_json(f.read())
        assert loaded.responsible_agent.agent_id == "r"


# ===================================================================
# 4. LLM config in pipeline context
# ===================================================================


@pytest.mark.integration
class TestLLMConfigInPipeline:
    """LLM configuration flows correctly through the inference pipeline."""

    def test_env_var_model_creates_provider(self, monkeypatch, tmp_path):
        """MIDAS_MODEL + MIDAS_API_KEY env vars produce a usable config."""
        from midas_agent.resolver import resolve_llm_config

        monkeypatch.setenv("MIDAS_MODEL", "gpt-4o")
        monkeypatch.setenv("MIDAS_API_KEY", "sk-test-key")

        config = resolve_llm_config(cwd=str(tmp_path))
        assert config.model == "gpt-4o"
        assert config.api_key == "sk-test-key"

    def test_missing_config_shows_guidance(self, monkeypatch, tmp_path):
        """No LLM config at all -> error message teaches user to configure."""
        from midas_agent.resolver import ConfigurationError, resolve_llm_config

        monkeypatch.delenv("MIDAS_MODEL", raising=False)
        monkeypatch.delenv("MIDAS_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        with pytest.raises(ConfigurationError) as exc_info:
            resolve_llm_config(cwd=str(tmp_path))

        guidance = str(exc_info.value)
        # Must contain actionable instructions about what to configure
        assert "MIDAS_MODEL" in guidance or "--model" in guidance or "model" in guidance

    def test_project_config_yaml_works(self, monkeypatch, tmp_path):
        """LLM config from .midas/config.yaml is usable end-to-end."""
        from midas_agent.resolver import resolve_llm_config

        monkeypatch.delenv("MIDAS_MODEL", raising=False)
        monkeypatch.delenv("MIDAS_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        config_dir = tmp_path / ".midas"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text(
            "model: claude-sonnet-4-20250514\napi_key: test-fake-key-not-real\napi_base: https://api.anthropic.com\n"
        )

        config = resolve_llm_config(cwd=str(tmp_path))
        assert config.model == "claude-sonnet-4-20250514"
        assert config.api_key == "test-fake-key-not-real"
        assert config.api_base == "https://api.anthropic.com"


# ===================================================================
# 5. Action injection — agent has real tools
# ===================================================================


@pytest.mark.integration
class TestActionInjection:
    """The inference agent must receive a complete action set, not just TaskDoneAction."""

    def test_infer_local_has_bash_action(self, tmp_path):
        """Local inference provides BashAction."""
        from midas_agent.cli import build_action_set

        actions = build_action_set(cwd=str(tmp_path), env="local")
        names = {a.name for a in actions}
        assert "bash" in names

    def test_infer_local_has_file_actions(self, tmp_path):
        """Local inference provides the unified str_replace_editor action."""
        from midas_agent.cli import build_action_set

        actions = build_action_set(cwd=str(tmp_path), env="local")
        names = {a.name for a in actions}
        assert "str_replace_editor" in names

    def test_infer_local_has_search_actions(self, tmp_path):
        """Local inference provides search_code and find_files."""
        from midas_agent.cli import build_action_set

        actions = build_action_set(cwd=str(tmp_path), env="local")
        names = {a.name for a in actions}
        assert "search_code" in names
        assert "find_files" in names

    def test_agent_can_execute_bash_in_cwd(self, tmp_path):
        """BashAction from build_action_set actually executes in the right cwd."""
        from midas_agent.cli import build_action_set

        # Create a file in tmp_path
        (tmp_path / "marker.txt").write_text("hello")

        actions = build_action_set(cwd=str(tmp_path), env="local")
        bash = next(a for a in actions if a.name == "bash")
        result = bash.execute(command="cat marker.txt")
        assert "hello" in result

    def test_agent_can_view_file_in_cwd(self, tmp_path):
        """StrReplaceEditorAction views files relative to cwd."""
        from midas_agent.cli import build_action_set

        (tmp_path / "test.py").write_text("print('hi')")

        actions = build_action_set(cwd=str(tmp_path), env="local")
        editor = next(a for a in actions if a.name == "str_replace_editor")
        result = editor.execute(command="view", path=str(tmp_path / "test.py"))
        assert "print" in result

    def test_full_infer_agent_has_all_tools(self, tmp_path):
        """PlanExecuteAgent built for inference has all expected tools available."""
        from midas_agent.cli import build_action_set
        from midas_agent.stdlib.plan_execute_agent import PlanExecuteAgent

        actions = build_action_set(cwd=str(tmp_path), env="local")
        call_llm = MagicMock(return_value=_task_done_response("done"))

        agent = PlanExecuteAgent(
            system_prompt="Test agent",
            actions=actions,
            call_llm=call_llm,
        )

        # Agent should have tools in its tool list
        tools = agent._build_tools()
        tool_names = {t["function"]["name"] for t in tools}
        assert "bash" in tool_names
        assert "str_replace_editor" in tool_names
        assert "task_done" in tool_names


# ===================================================================
# 6. End-to-end: TUI with real artifact
# ===================================================================


@pytest.mark.integration
class TestTUIEndToEnd:
    """TUI loads artifact, resolves config, and runs agent."""

    def test_tui_with_trained_artifact(self, tmp_path):
        """TUI loads a trained artifact and runs the responsible agent."""
        from midas_agent.tui import TUI
        from midas_agent.cli import build_action_set

        artifact = _make_trained_artifact()
        artifact_path = str(tmp_path / "artifact.json")
        _write_artifact(artifact, artifact_path)

        with open(artifact_path) as f:
            loaded = GraphEmergenceArtifact.model_validate_json(f.read())

        actions = build_action_set(cwd=str(tmp_path), env="local")
        call_llm = MagicMock(return_value=_task_done_response("fixed"))

        tui = TUI(
            call_llm=call_llm,
            actions=actions,
            system_prompt=loaded.responsible_agent.soul.system_prompt,
        )

        with patch("builtins.input", side_effect=["Fix the bug", "/quit"]):
            tui.run()

        assert call_llm.call_count >= 1

    def test_tui_with_default_artifact_no_crash(self, tmp_path):
        """TUI works with the default (untrained) artifact."""
        from midas_agent.resolver import resolve_artifact_path
        from midas_agent.tui import TUI
        from midas_agent.cli import build_action_set

        artifact_path = resolve_artifact_path(cwd=str(tmp_path))
        with open(artifact_path) as f:
            artifact = GraphEmergenceArtifact.model_validate_json(f.read())

        actions = build_action_set(cwd=str(tmp_path), env="local")
        call_llm = MagicMock(return_value=_task_done_response("ok"))

        tui = TUI(
            call_llm=call_llm,
            actions=actions,
            system_prompt=artifact.responsible_agent.soul.system_prompt,
        )

        with patch("builtins.input", side_effect=["Hello", "/quit"]):
            tui.run()

    def test_on_action_fires_during_tui_session(self, tmp_path):
        """Action events are emitted to TUI's on_action during execution."""
        from midas_agent.tui import TUI
        from midas_agent.cli import build_action_set

        events = []
        actions = build_action_set(cwd=str(tmp_path), env="local")
        call_llm = MagicMock(return_value=_task_done_response("done"))

        tui = TUI(
            call_llm=call_llm,
            actions=actions,
            system_prompt="test",
            on_action=lambda e: events.append(e),
        )

        with patch("builtins.input", side_effect=["Do something", "/quit"]):
            tui.run()

        # At least task_done was executed
        assert len(events) >= 1
        assert events[0].action_name == "task_done"
