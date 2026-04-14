"""Unit tests for CriteriaCache."""
import json
import os
import tempfile

import pytest

from midas_agent.evaluation.criteria_cache import CriteriaCache


@pytest.mark.unit
class TestCriteriaCache:
    """Tests for the CriteriaCache per-issue criteria caching mechanism."""

    def test_construction(self):
        """CriteriaCache accepts a cache_dir parameter."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = CriteriaCache(cache_dir=tmpdir)

            assert cache is not None

    def test_get_or_extract_cache_miss(self):
        """On cache miss, get_or_extract calls the extract_fn to generate criteria."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = CriteriaCache(cache_dir=tmpdir)
            extract_fn = lambda issue_id: ["criterion_a", "criterion_b"]

            result = cache.get_or_extract("ISSUE-300", extract_fn)

            assert result == ["criterion_a", "criterion_b"]

    def test_get_or_extract_cache_hit(self):
        """On cache hit, get_or_extract returns cached result without calling extract_fn."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = CriteriaCache(cache_dir=tmpdir)
            call_count = 0

            def counting_extract(issue_id: str) -> list[str]:
                nonlocal call_count
                call_count += 1
                return ["criterion_x"]

            # First call: cache miss, extract_fn is called
            cache.get_or_extract("ISSUE-400", counting_extract)
            assert call_count == 1

            # Second call: cache hit, extract_fn must NOT be called again
            result = cache.get_or_extract("ISSUE-400", counting_extract)
            assert call_count == 1
            assert result == ["criterion_x"]

    def test_get_or_extract_returns_list(self):
        """get_or_extract returns a list[str] of criteria."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = CriteriaCache(cache_dir=tmpdir)
            criteria = ["Readable code", "No side effects", "Proper error handling"]
            extract_fn = lambda issue_id: criteria

            result = cache.get_or_extract("ISSUE-500", extract_fn)

            assert isinstance(result, list)
            assert all(isinstance(c, str) for c in result)
            assert len(result) == 3

    def test_cache_persists_to_disk(self):
        """Cache writes criteria to {cache_dir}/{issue_id}.json on disk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = CriteriaCache(cache_dir=tmpdir)
            criteria = ["Efficient", "Tested"]
            extract_fn = lambda issue_id: criteria

            cache.get_or_extract("ISSUE-600", extract_fn)

            expected_path = os.path.join(tmpdir, "ISSUE-600.json")
            assert os.path.exists(expected_path)

            with open(expected_path, "r") as f:
                persisted = json.load(f)
            assert persisted == ["Efficient", "Tested"]
