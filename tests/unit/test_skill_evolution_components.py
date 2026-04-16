"""Unit tests for skill evolution components: SkillModule, fitness metric,
dataset builder, and initial skill creation.

Tests are expected to FAIL until skill_evolution.py is implemented.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from midas_agent.llm.types import LLMResponse, TokenUsage
from midas_agent.stdlib.react_agent import ActionRecord
from midas_agent.workspace.graph_emergence.skill import Skill
from midas_agent.workspace.graph_emergence.skill_evolution import (
    SkillDatasetBuilder,
    SkillModule,
    create_initial_skill,
    skill_fitness_metric,
)


# ===================================================================
# SkillModule (DSPy wrapper)
# ===================================================================


@pytest.mark.unit
class TestSkillModule:
    """SkillModule wraps skill.content as a DSPy-evolvable parameter."""

    def test_stores_skill_text(self):
        module = SkillModule(skill_text="Debug by reading logs first.")
        assert module.skill_text == "Debug by reading logs first."

    def test_forward_calls_predictor(self):
        module = SkillModule(skill_text="Use grep to find bugs.")
        with patch.object(module, "predictor") as mock_pred:
            mock_pred.return_value = MagicMock(output="done")
            result = module.forward(task_input="Fix the bug")
            mock_pred.assert_called_once()
            # skill_instructions should contain the skill text
            call_kwargs = mock_pred.call_args
            assert "Use grep to find bugs." in str(call_kwargs)

    def test_skill_text_is_string(self):
        module = SkillModule(skill_text="content")
        assert isinstance(module.skill_text, str)


# ===================================================================
# Fitness metric
# ===================================================================


@pytest.mark.unit
class TestSkillFitnessMetric:
    """Multi-objective fitness: accuracy (word overlap) + brevity."""

    def _example(self, expected: str):
        ex = MagicMock()
        ex.expected_behavior = expected
        return ex

    def _prediction(self, output: str):
        pred = MagicMock()
        pred.output = output
        return pred

    def test_full_overlap_high_accuracy(self):
        result = skill_fitness_metric(
            self._example("fix the divide by zero error"),
            self._prediction("fix the divide by zero error"),
        )
        scores = result["scores"]
        assert scores["accuracy"] >= 0.9

    def test_no_overlap_low_accuracy(self):
        result = skill_fitness_metric(
            self._example("fix the divide by zero error"),
            self._prediction("hello world completely unrelated"),
        )
        scores = result["scores"]
        assert scores["accuracy"] <= 0.5

    def test_partial_overlap(self):
        result = skill_fitness_metric(
            self._example("fix the divide by zero error in calculator"),
            self._prediction("fix the error in the system"),
        )
        scores = result["scores"]
        assert 0.3 < scores["accuracy"] < 0.9

    def test_brevity_short_text(self):
        result = skill_fitness_metric(
            self._example("anything"),
            self._prediction("short"),
        )
        scores = result["scores"]
        assert scores["brevity"] > 0.9

    def test_brevity_at_limit(self):
        result = skill_fitness_metric(
            self._example("anything"),
            self._prediction("x" * 5000),
        )
        scores = result["scores"]
        assert scores["brevity"] <= 0.01

    def test_returns_scores_dict(self):
        result = skill_fitness_metric(
            self._example("expected"),
            self._prediction("output"),
        )
        assert "scores" in result
        assert "accuracy" in result["scores"]
        assert "brevity" in result["scores"]

    def test_empty_output_zero(self):
        result = skill_fitness_metric(
            self._example("expected behavior"),
            self._prediction(""),
        )
        scores = result["scores"]
        assert scores["accuracy"] == 0.0

    def test_empty_expected(self):
        """Empty expected_behavior should not crash."""
        result = skill_fitness_metric(
            self._example(""),
            self._prediction("some output"),
        )
        assert "scores" in result


# ===================================================================
# Initial skill creation (Path A)
# ===================================================================


def _make_action_record(name: str, args: dict, result: str) -> ActionRecord:
    return ActionRecord(
        action_name=name,
        arguments=args,
        result=result,
        timestamp=1000.0,
    )


@pytest.mark.unit
class TestCreateInitialSkill:
    """SystemLLM extracts first skill from action history."""

    def test_extracts_from_action_history(self):
        system_llm = MagicMock(return_value=LLMResponse(
            content=json.dumps({
                "name": "debug-django",
                "description": "Debug Django ORM issues",
                "content": "## Procedure\n1. Search for QuerySet...\n2. Check loops...",
            }),
            tool_calls=None,
            usage=TokenUsage(50, 50),
        ))
        action_history = [
            _make_action_record("search_code", {"pattern": "QuerySet"}, "Found 3 matches"),
            _make_action_record("read_file", {"path": "models.py"}, "class MyModel..."),
            _make_action_record("edit_file", {"path": "views.py"}, "OK"),
        ]
        skill = create_initial_skill(
            system_llm=system_llm,
            action_history=action_history,
            eval_results={"s_exec": 0.8, "issue_description": "Fix N+1 query"},
        )
        assert skill is not None
        assert isinstance(skill, Skill)

    def test_produces_all_fields(self):
        system_llm = MagicMock(return_value=LLMResponse(
            content=json.dumps({
                "name": "search-expert",
                "description": "Code search specialist",
                "content": "Use grep to find patterns.",
            }),
            tool_calls=None,
            usage=TokenUsage(30, 30),
        ))
        skill = create_initial_skill(
            system_llm=system_llm,
            action_history=[_make_action_record("bash", {"command": "grep foo"}, "result")],
            eval_results={"s_exec": 0.6},
        )
        assert skill.name
        assert skill.description
        assert skill.content

    def test_content_within_limit(self):
        system_llm = MagicMock(return_value=LLMResponse(
            content=json.dumps({
                "name": "s",
                "description": "d",
                "content": "x" * 4000,
            }),
            tool_calls=None,
            usage=TokenUsage(30, 30),
        ))
        skill = create_initial_skill(
            system_llm=system_llm,
            action_history=[_make_action_record("bash", {}, "ok")],
            eval_results={"s_exec": 0.5},
        )
        assert len(skill.content) <= 5000

    def test_empty_action_history_returns_none(self):
        system_llm = MagicMock()
        skill = create_initial_skill(
            system_llm=system_llm,
            action_history=[],
            eval_results={"s_exec": 0.5},
        )
        assert skill is None
        system_llm.assert_not_called()

    def test_prompt_contains_actions(self):
        system_llm = MagicMock(return_value=LLMResponse(
            content=json.dumps({"name": "n", "description": "d", "content": "c"}),
            tool_calls=None,
            usage=TokenUsage(20, 20),
        ))
        action_history = [
            _make_action_record("search_code", {"pattern": "buggy_func"}, "Found it"),
        ]
        create_initial_skill(
            system_llm=system_llm,
            action_history=action_history,
            eval_results={"s_exec": 0.9},
        )
        call_args = system_llm.call_args[0][0]
        messages_text = " ".join(m.get("content", "") for m in call_args.messages)
        assert "search_code" in messages_text or "buggy_func" in messages_text


# ===================================================================
# Dataset builder
# ===================================================================


@pytest.mark.unit
class TestSkillDatasetBuilder:
    """Builds evaluation dataset from real training history."""

    def test_builds_from_single_episode(self):
        builder = SkillDatasetBuilder()
        builder.add_episode(
            task_input="Fix the bug",
            action_summary="Searched code, edited file, ran tests",
            score=0.8,
        )
        train, val, holdout = builder.build()
        assert len(train) + len(val) + len(holdout) >= 1

    def test_builds_from_multiple_episodes(self):
        builder = SkillDatasetBuilder()
        for i in range(10):
            builder.add_episode(
                task_input=f"Fix bug #{i}",
                action_summary=f"Action summary for bug {i}",
                score=0.5 + i * 0.05,
            )
        train, val, holdout = builder.build()
        total = len(train) + len(val) + len(holdout)
        assert total == 10

    def test_split_ratios(self):
        builder = SkillDatasetBuilder()
        for i in range(20):
            builder.add_episode(f"task {i}", f"summary {i}", 0.7)
        train, val, holdout = builder.build()
        # 50/25/25 split: 10/5/5
        assert len(train) == 10
        assert len(val) == 5
        assert len(holdout) == 5

    def test_fewer_than_4_records_no_crash(self):
        builder = SkillDatasetBuilder()
        builder.add_episode("task", "summary", 0.8)
        train, val, holdout = builder.build()
        # Should not crash, even with 1 record
        assert len(train) + len(val) + len(holdout) == 1

    def test_examples_have_required_fields(self):
        builder = SkillDatasetBuilder()
        builder.add_episode("Fix the NPE", "Found null check missing, added guard", 0.9)
        train, val, holdout = builder.build()
        all_examples = train + val + holdout
        for ex in all_examples:
            assert hasattr(ex, "task_input")
            assert hasattr(ex, "expected_behavior")

    def test_accumulates_across_calls(self):
        builder = SkillDatasetBuilder()
        builder.add_episode("task 1", "summary 1", 0.6)
        builder.add_episode("task 2", "summary 2", 0.7)
        builder.add_episode("task 3", "summary 3", 0.8)
        train, val, holdout = builder.build()
        assert len(train) + len(val) + len(holdout) == 3
