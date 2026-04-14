"""Unit tests for Configuration Evolution config schema data classes."""
import pytest

from midas_agent.workspace.config_evolution.config_schema import (
    ConfigMeta,
    StepConfig,
    WorkflowConfig,
)


@pytest.mark.unit
class TestConfigMeta:
    """Tests for the ConfigMeta data class."""

    def test_config_meta_fields(self):
        """ConfigMeta stores name and description correctly."""
        meta = ConfigMeta(name="test", description="desc")

        assert meta.name == "test"
        assert meta.description == "desc"


@pytest.mark.unit
class TestStepConfig:
    """Tests for the StepConfig data class."""

    def test_step_config_fields(self):
        """StepConfig stores id, prompt, tools, and inputs correctly."""
        step = StepConfig(id="s1", prompt="do thing", tools=["bash"], inputs=[])

        assert step.id == "s1"
        assert step.prompt == "do thing"
        assert step.tools == ["bash"]
        assert step.inputs == []

    def test_step_config_defaults(self):
        """StepConfig defaults tools and inputs to empty lists."""
        step = StepConfig(id="s2", prompt="another step")

        assert step.tools == []
        assert step.inputs == []


@pytest.mark.unit
class TestWorkflowConfig:
    """Tests for the WorkflowConfig data class."""

    def test_workflow_config_fields(self):
        """WorkflowConfig stores meta and steps correctly."""
        meta = ConfigMeta(name="wf1", description="workflow one")
        step = StepConfig(id="s1", prompt="step one", tools=["bash"])
        config = WorkflowConfig(meta=meta, steps=[step])

        assert config.meta is meta
        assert config.meta.name == "wf1"
        assert len(config.steps) == 1
        assert config.steps[0].id == "s1"

    def test_workflow_config_multiple_steps_with_dependencies(self):
        """WorkflowConfig supports multiple steps where later steps reference earlier step ids via inputs."""
        meta = ConfigMeta(name="pipeline", description="multi-step pipeline")
        step_a = StepConfig(id="analyze", prompt="analyze the code", tools=["bash"])
        step_b = StepConfig(
            id="patch",
            prompt="generate a patch",
            tools=["bash", "file_ops"],
            inputs=["analyze"],
        )
        step_c = StepConfig(
            id="verify",
            prompt="verify the patch",
            tools=["bash"],
            inputs=["analyze", "patch"],
        )
        config = WorkflowConfig(meta=meta, steps=[step_a, step_b, step_c])

        assert len(config.steps) == 3
        assert config.steps[1].inputs == ["analyze"]
        assert config.steps[2].inputs == ["analyze", "patch"]
