"""Action ABC and ActionRegistry."""
from abc import ABC, abstractmethod


class ActionNotFoundError(Exception):
    pass


class Action(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def description(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def parameters(self) -> dict:
        raise NotImplementedError

    @abstractmethod
    def execute(self, **kwargs) -> str:
        raise NotImplementedError


class ActionRegistry:
    def __init__(self, actions: list[Action]) -> None:
        raise NotImplementedError

    def get(self, name: str) -> Action:
        raise NotImplementedError

    def get_subset(self, names: list[str]) -> list[Action]:
        raise NotImplementedError
