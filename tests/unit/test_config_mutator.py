"""Unit tests for config mutation utilities and GEPAConfigOptimizer."""
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage
from midas_agent.workspace.config_evolution.config_schema import (
    ConfigMeta,
    StepConfig,
    WorkflowConfig,
)
from midas_agent.workspace.config_evolution.mutator import (
    _config_to_yaml,
    _validate_mutation,
    validate_config,
)
from midas_agent.workspace.config_evolution.prompt_optimizer import (
    ConfigDatasetBuilder,
    GEPAConfigOptimizer,
    StepPromptModule,
    config_fitness_metric,
    make_judge_metric,
    _parse_judge_response,
)


def _make_config(*steps: StepConfig, name: str = "test") -> WorkflowConfig:
    return WorkflowConfig(
        meta=ConfigMeta(name=name, description="test"),
        steps=list(steps),
    )


def _make_system_llm():
    return MagicMock(
        return_value=LLMResponse(
            content="ok",
            tool_calls=None,
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        )
    )


# ===========================================================================
# validate_config
# ===========================================================================


@pytest.mark.unit
class TestValidateConfig:
    def test_valid_single_step(self):
        config = _make_config(StepConfig(id="s1", prompt="Do.", tools=["bash"]))
        assert validate_config(config) == []

    def test_empty_steps(self):
        config = _make_config()
        errors = validate_config(config)
        assert any("at least one step" in e for e in errors)

    def test_duplicate_ids(self):
        config = _make_config(
            StepConfig(id="s1", prompt="A.", tools=["bash"]),
            StepConfig(id="s1", prompt="B.", tools=["bash"]),
        )
        errors = validate_config(config)
        assert any("Duplicate" in e for e in errors)


# ===========================================================================
# _validate_mutation
# ===========================================================================


@pytest.mark.unit
class TestValidateMutation:
    def test_accepts_prompt_only_change(self):
        old = _make_config(StepConfig(id="s1", prompt="Find bug.", tools=["bash"]))
        new = _make_config(StepConfig(id="s1", prompt="Search for the bug.", tools=["bash"]))
        assert _validate_mutation(old, new)

    def test_rejects_changed_step_ids(self):
        old = _make_config(StepConfig(id="s1", prompt="A.", tools=["bash"]))
        new = _make_config(StepConfig(id="s2", prompt="A.", tools=["bash"]))
        assert not _validate_mutation(old, new)

    def test_rejects_changed_tools(self):
        old = _make_config(StepConfig(id="s1", prompt="A.", tools=["bash"]))
        new = _make_config(StepConfig(id="s1", prompt="A.", tools=["bash", "str_replace_editor"]))
        assert not _validate_mutation(old, new)

    def test_rejects_empty_prompt(self):
        old = _make_config(StepConfig(id="s1", prompt="A.", tools=["bash"]))
        new = _make_config(StepConfig(id="s1", prompt="   ", tools=["bash"]))
        assert not _validate_mutation(old, new)

    def test_rejects_excessive_growth(self):
        old = _make_config(StepConfig(id="s1", prompt="A" * 100, tools=["bash"]))
        new = _make_config(StepConfig(id="s1", prompt="B" * 200, tools=["bash"]))
        assert not _validate_mutation(old, new)

    def test_rejects_different_step_count(self):
        old = _make_config(
            StepConfig(id="s1", prompt="A.", tools=["bash"]),
            StepConfig(id="s2", prompt="B.", tools=["bash"], inputs=["s1"]),
        )
        new = _make_config(StepConfig(id="s1", prompt="A.", tools=["bash"]))
        assert not _validate_mutation(old, new)


# ===========================================================================
# StepPromptModule
# ===========================================================================


@pytest.mark.unit
class TestStepPromptModule:
    def test_construction(self):
        mod = StepPromptModule(step_prompt="Find the bug.", step_id="locate")
        assert mod.step_prompt == "Find the bug."
        assert mod.step_id == "locate"

    def test_has_predictor(self):
        mod = StepPromptModule(step_prompt="Fix it.", step_id="fix")
        assert hasattr(mod, "predictor")


# ===========================================================================
# config_fitness_metric
# ===========================================================================


@pytest.mark.unit
class TestConfigFitnessMetric:
    """Tests for the fallback word-overlap metric."""

    def test_returns_score_and_feedback(self):
        example = SimpleNamespace(expected_behavior="find the bug using grep")
        prediction = SimpleNamespace(output="find the bug using grep")
        result = config_fitness_metric(example, prediction)
        assert "score" in result
        assert "feedback" in result

    def test_empty_output_low_score(self):
        example = SimpleNamespace(expected_behavior="some expected output")
        prediction = SimpleNamespace(output="")
        result = config_fitness_metric(example, prediction)
        assert result["score"] < 0.5

    def test_perfect_overlap_high_score(self):
        example = SimpleNamespace(expected_behavior="find the bug using grep")
        prediction = SimpleNamespace(output="find the bug using grep")
        result = config_fitness_metric(example, prediction)
        assert result["score"] > 0.5


@pytest.mark.unit
class TestJudgeMetric:
    """Tests for LLM-as-judge metric and helpers."""

    def test_parse_judge_response_valid(self):
        text = "SCORE: 0.8\nFEEDBACK: Good strategy alignment."
        score, feedback = _parse_judge_response(text)
        assert score == 0.8
        assert "Good strategy" in feedback

    def test_parse_judge_response_missing_score(self):
        score, feedback = _parse_judge_response("no score here")
        assert score == 0.5  # default

    def test_parse_judge_response_clamped(self):
        score, _ = _parse_judge_response("SCORE: 1.5")
        assert score == 1.0

    def test_make_judge_metric_calls_system_llm(self):
        system_llm = MagicMock(return_value=LLMResponse(
            content="SCORE: 0.7\nFEEDBACK: Decent approach.",
            tool_calls=None,
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        ))
        metric = make_judge_metric(system_llm)
        example = SimpleNamespace(expected_behavior="[iter 1] bash(grep bug) → found")
        prediction = SimpleNamespace(output="Search for the bug using grep")
        result = metric(example, prediction)
        assert system_llm.call_count == 1
        # Result should have score and feedback
        if hasattr(result, "score"):
            assert result.score == 0.7
        else:
            assert result["score"] == 0.7


# ===========================================================================
# ConfigDatasetBuilder
# ===========================================================================


@pytest.mark.unit
class TestConfigDatasetBuilder:
    def test_empty_build(self):
        builder = ConfigDatasetBuilder()
        train, val, holdout = builder.build()
        assert train == [] and val == [] and holdout == []

    def test_size_tracking(self):
        builder = ConfigDatasetBuilder()
        builder.add_episode("task", "summary", 1.0)
        assert builder.size == 1

    def test_split_ratios(self):
        builder = ConfigDatasetBuilder()
        for i in range(20):
            builder.add_episode(f"task_{i}", f"summary_{i}", 1.0)
        train, val, holdout = builder.build()
        assert len(train) == 10  # 50%
        assert len(val) == 5     # 25%
        assert len(holdout) == 5  # 25%

    def test_minimum_one_train(self):
        builder = ConfigDatasetBuilder()
        builder.add_episode("task", "summary", 1.0)
        train, val, holdout = builder.build()
        assert len(train) == 1

    def test_sliding_window_drops_oldest(self):
        builder = ConfigDatasetBuilder(max_window=5)
        for i in range(8):
            builder.add_episode(f"task_{i}", f"trace_{i}", 1.0)
        assert builder.size == 5
        # Oldest 3 dropped, remaining are task_3 through task_7
        train, val, holdout = builder.build()
        all_examples = train + val + holdout
        task_inputs = [e.task_input for e in all_examples]
        assert "task_0" not in task_inputs
        assert "task_7" in task_inputs

    def test_sliding_window_default_is_20(self):
        builder = ConfigDatasetBuilder()
        for i in range(25):
            builder.add_episode(f"task_{i}", f"trace_{i}", 1.0)
        assert builder.size == 20


# ===========================================================================
# GEPAConfigOptimizer
# ===========================================================================


@pytest.mark.unit
class TestGEPAConfigOptimizer:
    def test_construction(self):
        opt = GEPAConfigOptimizer(system_llm=_make_system_llm())
        assert opt is not None

    def test_construction_with_data_dir(self, tmp_path):
        data_dir = str(tmp_path / "data")
        opt = GEPAConfigOptimizer(system_llm=_make_system_llm(), data_dir=data_dir)
        assert os.path.isdir(data_dir)

    def test_record_episode(self):
        opt = GEPAConfigOptimizer(system_llm=_make_system_llm())
        opt.record_episode("task", "trace text", 1.0)
        assert opt.dataset.size == 1

    def test_record_episode_persists_to_disk(self, tmp_path):
        data_dir = str(tmp_path / "data")
        opt = GEPAConfigOptimizer(system_llm=_make_system_llm(), data_dir=data_dir)
        opt.record_episode("Fix the bug", "[iter 1] bash(...)", 1.0, issue_id="astropy-123")
        files = os.listdir(data_dir)
        assert len(files) == 1
        assert "astropy-123" in files[0]
        import json
        with open(os.path.join(data_dir, files[0])) as f:
            data = json.load(f)
        assert data["issue_id"] == "astropy-123"
        assert data["trace"] == "[iter 1] bash(...)"
        assert data["score"] == 1.0

    def test_should_not_optimize_before_interval(self):
        opt = GEPAConfigOptimizer(
            system_llm=_make_system_llm(),
            gepa_interval=5,
            min_dataset_size=5,
        )
        for i in range(4):
            opt.record_episode(f"task_{i}", f"trace_{i}", 1.0)
        assert not opt.should_optimize()

    def test_should_optimize_after_interval(self):
        opt = GEPAConfigOptimizer(
            system_llm=_make_system_llm(),
            gepa_interval=5,
            min_dataset_size=5,
        )
        for i in range(5):
            opt.record_episode(f"task_{i}", f"trace_{i}", 1.0)
        assert opt.should_optimize()

    def test_maybe_optimize_returns_original_before_interval(self):
        opt = GEPAConfigOptimizer(
            system_llm=_make_system_llm(),
            gepa_interval=5,
            min_dataset_size=5,
        )
        config = _make_config(StepConfig(id="s1", prompt="Do.", tools=["bash"]))
        result, changed = opt.maybe_optimize(config)
        assert result is config  # unchanged
        assert changed is False

    def test_condense_prompt(self):
        """Condensation asks system_llm to shorten a prompt."""
        condensed = "Find relevant files using grep."
        system_llm = MagicMock(return_value=LLMResponse(
            content=condensed,
            tool_calls=None,
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        ))
        opt = GEPAConfigOptimizer(system_llm=system_llm)
        result = opt._condense_prompt("A" * 3000, max_chars=2000)
        assert result == condensed
        assert system_llm.call_count == 1

    def test_sliding_window_parameter(self):
        """GEPAConfigOptimizer passes window_size to dataset builder."""
        opt = GEPAConfigOptimizer(system_llm=_make_system_llm(), window_size=3)
        for i in range(5):
            opt.record_episode(f"task_{i}", f"trace_{i}", 1.0)
        assert opt.dataset.size == 3
