"""Unit tests for Workspace abstract base class."""
import pytest

from midas_agent.workspace.base import Workspace
from midas_agent.types import Issue


@pytest.mark.unit
class TestWorkspaceABC:
    """Tests that Workspace enforces its abstract interface contract."""

    def test_workspace_is_abstract(self):
        """Workspace cannot be instantiated directly because it is abstract."""
        with pytest.raises(TypeError):
            Workspace(  # type: ignore[abstract]
                workspace_id="ws-1",
                call_llm=lambda req: None,
                system_llm=lambda req: None,
            )

    def test_subclass_must_implement_receive_budget(self):
        """A subclass that omits receive_budget raises TypeError on instantiation."""

        class Incomplete(Workspace):
            def execute(self, issue):
                pass

            def submit_patch(self):
                pass

            def post_episode(self, eval_results, evicted_ids):
                pass

        with pytest.raises(TypeError):
            Incomplete(
                workspace_id="ws-1",
                call_llm=lambda req: None,
                system_llm=lambda req: None,
            )

    def test_subclass_must_implement_execute(self):
        """A subclass that omits execute raises TypeError on instantiation."""

        class Incomplete(Workspace):
            def receive_budget(self, amount):
                pass

            def submit_patch(self):
                pass

            def post_episode(self, eval_results, evicted_ids):
                pass

        with pytest.raises(TypeError):
            Incomplete(
                workspace_id="ws-1",
                call_llm=lambda req: None,
                system_llm=lambda req: None,
            )

    def test_subclass_must_implement_submit_patch(self):
        """A subclass that omits submit_patch raises TypeError on instantiation."""

        class Incomplete(Workspace):
            def receive_budget(self, amount):
                pass

            def execute(self, issue):
                pass

            def post_episode(self, eval_results, evicted_ids):
                pass

        with pytest.raises(TypeError):
            Incomplete(
                workspace_id="ws-1",
                call_llm=lambda req: None,
                system_llm=lambda req: None,
            )

    def test_subclass_must_implement_post_episode(self):
        """A subclass that omits post_episode raises TypeError on instantiation."""

        class Incomplete(Workspace):
            def receive_budget(self, amount):
                pass

            def execute(self, issue):
                pass

            def submit_patch(self):
                pass

        with pytest.raises(TypeError):
            Incomplete(
                workspace_id="ws-1",
                call_llm=lambda req: None,
                system_llm=lambda req: None,
            )
