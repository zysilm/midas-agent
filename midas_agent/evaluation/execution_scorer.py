"""Execution scorer — Docker-based deterministic scoring."""
from midas_agent.types import Issue


class ExecutionScorer:
    def __init__(self, docker_image: str, timeout: int) -> None:
        raise NotImplementedError

    def score(self, patch: str, issue: Issue) -> float:
        raise NotImplementedError
