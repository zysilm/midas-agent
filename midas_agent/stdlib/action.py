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
        self._actions: dict[str, Action] = {a.name: a for a in actions}

    def get(self, name: str) -> Action:
        try:
            return self._actions[name]
        except KeyError:
            raise ActionNotFoundError(name)

    def get_subset(self, names: list[str]) -> list[Action]:
        return [self.get(n) for n in names]
