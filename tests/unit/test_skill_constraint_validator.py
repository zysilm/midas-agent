"""Unit tests for SkillConstraintValidator — hard gates on evolved skills."""
from __future__ import annotations

import pytest

from midas_agent.workspace.graph_emergence.skill import SkillConstraintValidator


@pytest.mark.unit
class TestSkillConstraintValidator:
    """All constraints must pass for an evolved skill to be accepted."""

    def _validator(self) -> SkillConstraintValidator:
        return SkillConstraintValidator()

    # -- Size limit --

    def test_valid_content_passes(self):
        v = self._validator()
        assert v.validate(
            new_content="A useful skill." * 10,
            old_content="Old skill.",
            holdout_score_new=0.8,
            holdout_score_old=0.7,
        ) is True

    def test_exceeds_5000_chars_fails(self):
        v = self._validator()
        assert v.validate(
            new_content="x" * 5001,
            old_content="short",
            holdout_score_new=0.9,
            holdout_score_old=0.5,
        ) is False

    def test_exactly_5000_chars_passes(self):
        v = self._validator()
        assert v.validate(
            new_content="x" * 5000,
            old_content="x" * 4500,
            holdout_score_new=0.8,
            holdout_score_old=0.8,
        ) is True

    # -- Growth limit --

    def test_growth_under_20_percent_passes(self):
        v = self._validator()
        old = "a" * 1000
        new = "a" * 1190  # 19% growth
        assert v.validate(new, old, 0.8, 0.8) is True

    def test_growth_over_20_percent_fails(self):
        v = self._validator()
        old = "a" * 1000
        new = "a" * 1210  # 21% growth
        assert v.validate(new, old, 0.9, 0.5) is False

    def test_growth_skipped_when_old_is_none(self):
        """First creation (no baseline) — growth check is skipped."""
        v = self._validator()
        assert v.validate(
            new_content="a" * 3000,
            old_content=None,
            holdout_score_new=0.8,
            holdout_score_old=0.0,
        ) is True

    # -- Non-empty --

    def test_empty_content_fails(self):
        v = self._validator()
        assert v.validate("", "old", 0.8, 0.7) is False

    def test_whitespace_only_fails(self):
        v = self._validator()
        assert v.validate("   \n\t  ", "old", 0.8, 0.7) is False

    # -- No regression --

    def test_regression_fails(self):
        v = self._validator()
        assert v.validate("good content", "old", 0.5, 0.7) is False

    def test_equal_holdout_passes(self):
        v = self._validator()
        assert v.validate("good content", "old", 0.7, 0.7) is True

    # -- All-or-nothing --

    def test_one_fail_rejects_all(self):
        """Size OK, growth OK, non-empty, but regression -> reject."""
        v = self._validator()
        assert v.validate(
            new_content="decent content",
            old_content="old content",
            holdout_score_new=0.3,
            holdout_score_old=0.8,
        ) is False
