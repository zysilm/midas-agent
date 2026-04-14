"""Criteria cache — per-issue caching of extracted criteria."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable


class CriteriaCache:
    """Per-issue cache for extracted evaluation criteria.

    Provides a two-level cache (in-memory dict + on-disk JSON files) so that
    criteria extraction via LLM is performed at most once per issue, even
    across CriteriaCache instances that share the same ``cache_dir``.
    """

    def __init__(self, cache_dir: str) -> None:
        self._cache_dir = cache_dir
        self._memory: dict[str, list[str]] = {}

        # Ensure the cache directory exists.
        Path(cache_dir).mkdir(parents=True, exist_ok=True)

        # Pre-load any existing .json files into the in-memory cache so that
        # a freshly-constructed instance picks up criteria persisted by a
        # previous instance (IT-7.5).
        for filename in os.listdir(cache_dir):
            if filename.endswith(".json"):
                issue_id = filename[: -len(".json")]
                filepath = os.path.join(cache_dir, filename)
                with open(filepath, "r") as fh:
                    self._memory[issue_id] = json.load(fh)

    def get_or_extract(
        self,
        issue_id: str,
        extract_fn: Callable[[str], list[str]],
    ) -> list[str]:
        """Return cached criteria for *issue_id*, or extract and cache them.

        Lookup order:
        1. In-memory dict (fastest).
        2. On-disk JSON file ``{cache_dir}/{issue_id}.json``.
        3. Call ``extract_fn(issue_id)`` and persist the result to both
           in-memory cache and disk.
        """
        # 1. In-memory hit.
        if issue_id in self._memory:
            return self._memory[issue_id]

        # 2. On-disk hit (guards against files written after __init__).
        disk_path = os.path.join(self._cache_dir, f"{issue_id}.json")
        if os.path.exists(disk_path):
            with open(disk_path, "r") as fh:
                criteria = json.load(fh)
            self._memory[issue_id] = criteria
            return criteria

        # 3. Cache miss — extract, persist, and return.
        criteria = extract_fn(issue_id)
        self._memory[issue_id] = criteria

        with open(disk_path, "w") as fh:
            json.dump(criteria, fh)

        return criteria
