"""Unit tests for production artifact export."""
import json
import os
import tempfile

import pytest

from midas_agent.inference.exporter import export_config_evolution, export_graph_emergence
from midas_agent.inference.schemas import GraphEmergenceArtifact
from midas_agent.workspace.config_evolution.snapshot_store import (
    ConfigSnapshot,
    ConfigSnapshotStore,
)
from midas_agent.workspace.graph_emergence.agent import Agent, Soul
from midas_agent.workspace.graph_emergence.skill import Skill
from unittest.mock import MagicMock


@pytest.mark.unit
class TestExportConfigEvolution:
    def test_exports_highest_eta_config(self):
        store = ConfigSnapshotStore(store_dir="/tmp/test_snapshots")
        store.save(ConfigSnapshot(
            episode_id="ep-1", workspace_id="ws-1",
            config_yaml="meta:\n  name: low", eta=0.5, score=0.5, cost=1000, summary="",
        ))
        store.save(ConfigSnapshot(
            episode_id="ep-2", workspace_id="ws-2",
            config_yaml="meta:\n  name: high", eta=2.0, score=0.8, cost=400, summary="",
        ))

        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as f:
            output_path = f.name

        try:
            result = export_config_evolution(store, output_path)
            assert "high" in result
            with open(output_path) as f:
                assert "high" in f.read()
        finally:
            os.unlink(output_path)

    def test_raises_on_empty_store(self):
        store = ConfigSnapshotStore(store_dir="/tmp/test_snapshots")
        with pytest.raises(ValueError, match="No snapshots"):
            export_config_evolution(store, "/tmp/out.yaml")


@pytest.mark.unit
class TestExportGraphEmergence:
    def _make_agent(self, agent_id: str, skill_name: str | None = None) -> Agent:
        soul = Soul(system_prompt=f"prompt-{agent_id}")
        skill = None
        if skill_name:
            skill = Skill(name=skill_name, description=f"desc-{skill_name}", content="content")
        return Agent(agent_id=agent_id, soul=soul, agent_type="free", skill=skill)

    def test_exports_valid_json(self):
        responsible = self._make_agent("resp-1", "coordination")
        free_agents = [
            self._make_agent("fa-1", "debugging"),
            self._make_agent("fa-2"),
        ]

        pricing_engine = MagicMock()
        pricing_engine.calculate_price.side_effect = lambda a: {"fa-1": 1000, "fa-2": 2000}[a.agent_id]

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            output_path = f.name

        try:
            artifact = export_graph_emergence(
                responsible_agent=responsible,
                free_agents=free_agents,
                pricing_engine=pricing_engine,
                hire_counts={"fa-1": 10, "fa-2": 5},
                bankruptcy_counts={"fa-1": 1, "fa-2": 3},
                budget_hint=50000,
                output_path=output_path,
            )

            assert artifact.budget_hint == 50000
            assert artifact.responsible_agent.soul.system_prompt == "prompt-resp-1"
            assert len(artifact.free_agents) == 2
            assert artifact.free_agents[0].price == 1000
            assert artifact.free_agents[0].bankruptcy_rate == pytest.approx(0.1)
            assert artifact.free_agents[1].bankruptcy_rate == pytest.approx(0.6)
            assert artifact.free_agents[1].skill is None

            # Verify file is valid JSON
            with open(output_path) as f:
                loaded = json.load(f)
            assert loaded["budget_hint"] == 50000
        finally:
            os.unlink(output_path)

    def test_zero_hires_gives_zero_bankruptcy_rate(self):
        responsible = self._make_agent("resp-1")
        free_agents = [self._make_agent("fa-1")]
        pricing_engine = MagicMock()
        pricing_engine.calculate_price.return_value = 100

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            output_path = f.name

        try:
            artifact = export_graph_emergence(
                responsible_agent=responsible,
                free_agents=free_agents,
                pricing_engine=pricing_engine,
                hire_counts={},
                bankruptcy_counts={},
                budget_hint=10000,
                output_path=output_path,
            )
            assert artifact.free_agents[0].bankruptcy_rate == 0.0
        finally:
            os.unlink(output_path)
