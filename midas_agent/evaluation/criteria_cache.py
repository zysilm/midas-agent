"""Criteria cache — per-issue caching of extracted criteria."""
from __future__ import annotations

from typing import Callable


class CriteriaCache:
    def __init__(self, cache_dir: str) -> None:
        raise NotImplementedError

    def get_or_extract(
        self,
        issue_id: str,
        extract_fn: Callable[[str], list[str]],
    ) -> list[str]:
        raise NotImplementedError
