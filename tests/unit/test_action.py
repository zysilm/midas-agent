"""Unit tests for Action ABC and ActionRegistry."""
import pytest

from midas_agent.stdlib.action import Action, ActionRegistry, ActionNotFoundError


# -- Helpers --


class ConcreteAction(Action):
    """Minimal concrete Action subclass for testing the registry."""

    def __init__(self, action_name: str):
        self._name = action_name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"Description for {self._name}"

    @property
    def parameters(self) -> dict:
        return {}

    def execute(self, **kwargs) -> str:
        return "ok"


@pytest.mark.unit
class TestActionABC:
    """Tests that Action enforces its abstract interface contract."""

    def test_action_is_abstract(self):
        """Action cannot be instantiated directly because it is abstract."""
        with pytest.raises(TypeError):
            Action()  # type: ignore[abstract]

    def test_action_requires_name(self):
        """A subclass that omits the name property raises TypeError."""

        class MissingName(Action):
            @property
            def description(self) -> str:
                return "desc"

            @property
            def parameters(self) -> dict:
                return {}

            def execute(self, **kwargs) -> str:
                return ""

        with pytest.raises(TypeError):
            MissingName()

    def test_action_requires_description(self):
        """A subclass that omits the description property raises TypeError."""

        class MissingDescription(Action):
            @property
            def name(self) -> str:
                return "test"

            @property
            def parameters(self) -> dict:
                return {}

            def execute(self, **kwargs) -> str:
                return ""

        with pytest.raises(TypeError):
            MissingDescription()

    def test_action_requires_parameters(self):
        """A subclass that omits the parameters property raises TypeError."""

        class MissingParameters(Action):
            @property
            def name(self) -> str:
                return "test"

            @property
            def description(self) -> str:
                return "desc"

            def execute(self, **kwargs) -> str:
                return ""

        with pytest.raises(TypeError):
            MissingParameters()

    def test_action_requires_execute(self):
        """A subclass that omits the execute method raises TypeError."""

        class MissingExecute(Action):
            @property
            def name(self) -> str:
                return "test"

            @property
            def description(self) -> str:
                return "desc"

            @property
            def parameters(self) -> dict:
                return {}

        with pytest.raises(TypeError):
            MissingExecute()


@pytest.mark.unit
class TestActionRegistry:
    """Tests for ActionRegistry construction and lookup."""

    def test_registry_construction(self):
        """ActionRegistry can be constructed from a list of actions."""
        actions = [ConcreteAction("alpha"), ConcreteAction("beta")]
        registry = ActionRegistry(actions)
        assert registry is not None

    def test_registry_get_existing(self):
        """get() returns the action matching the given name."""
        actions = [ConcreteAction("alpha"), ConcreteAction("beta")]
        registry = ActionRegistry(actions)
        result = registry.get("alpha")
        assert result.name == "alpha"

    def test_registry_get_missing_raises(self):
        """get() raises ActionNotFoundError for an unknown action name."""
        actions = [ConcreteAction("alpha")]
        registry = ActionRegistry(actions)
        with pytest.raises(ActionNotFoundError):
            registry.get("nonexistent")

    def test_registry_get_subset(self):
        """get_subset() returns only the actions with matching names."""
        actions = [
            ConcreteAction("a"),
            ConcreteAction("b"),
            ConcreteAction("c"),
        ]
        registry = ActionRegistry(actions)
        subset = registry.get_subset(["a", "b"])
        assert len(subset) == 2

    def test_registry_get_subset_missing_raises(self):
        """get_subset() raises ActionNotFoundError when a requested name is unknown."""
        actions = [ConcreteAction("a")]
        registry = ActionRegistry(actions)
        with pytest.raises(ActionNotFoundError):
            registry.get_subset(["a", "nonexistent"])
