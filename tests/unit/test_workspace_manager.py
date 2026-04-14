"""Unit tests for WorkspaceManager lifecycle operations."""
import pytest

from midas_agent.workspace.manager import WorkspaceManager
from midas_agent.workspace.base import Workspace
from midas_agent.config import MidasConfig


def _make_config(**overrides) -> MidasConfig:
    """Helper to build a MidasConfig with sensible defaults."""
    defaults = dict(
        initial_budget=10000,
        workspace_count=4,
        runtime_mode="config_evolution",
    )
    defaults.update(overrides)
    return MidasConfig(**defaults)


def _dummy_call_llm_factory(workspace_id: str):
    """Stub call_llm_factory that returns a no-op callable."""
    return lambda req: None


def _dummy_system_llm(req):
    """Stub system_llm_callback."""
    return None


@pytest.mark.unit
class TestWorkspaceManager:
    """Tests for WorkspaceManager construction and workspace lifecycle."""

    def test_construction(self):
        """WorkspaceManager can be constructed with config, call_llm_factory, and system_llm_callback."""
        config = _make_config()
        mgr = WorkspaceManager(
            config=config,
            call_llm_factory=_dummy_call_llm_factory,
            system_llm_callback=_dummy_system_llm,
        )
        assert mgr is not None

    def test_create_workspace(self):
        """create() returns a Workspace instance and stores it internally."""
        config = _make_config()
        mgr = WorkspaceManager(
            config=config,
            call_llm_factory=_dummy_call_llm_factory,
            system_llm_callback=_dummy_system_llm,
        )
        ws = mgr.create(workspace_id="ws-1")
        assert isinstance(ws, Workspace)

    def test_destroy_workspace(self):
        """destroy() removes a workspace from the internal dict."""
        config = _make_config()
        mgr = WorkspaceManager(
            config=config,
            call_llm_factory=_dummy_call_llm_factory,
            system_llm_callback=_dummy_system_llm,
        )
        mgr.create(workspace_id="ws-1")
        mgr.destroy(workspace_id="ws-1")
        assert "ws-1" not in mgr.workspaces

    def test_list_workspaces(self):
        """list_workspaces() returns all active workspaces."""
        config = _make_config()
        mgr = WorkspaceManager(
            config=config,
            call_llm_factory=_dummy_call_llm_factory,
            system_llm_callback=_dummy_system_llm,
        )
        result = mgr.list_workspaces()
        assert isinstance(result, list)

    def test_replace_workspace(self):
        """replace() destroys the old workspace and creates a new one."""
        config = _make_config()
        mgr = WorkspaceManager(
            config=config,
            call_llm_factory=_dummy_call_llm_factory,
            system_llm_callback=_dummy_system_llm,
        )
        mgr.create(workspace_id="ws-old")
        new_ws = mgr.replace(
            old_workspace_id="ws-old",
            new_workspace_id="ws-new",
        )
        assert isinstance(new_ws, Workspace)
        assert "ws-old" not in mgr.workspaces

    def test_workspaces_property(self):
        """workspaces property returns a dict of workspace_id to Workspace."""
        config = _make_config()
        mgr = WorkspaceManager(
            config=config,
            call_llm_factory=_dummy_call_llm_factory,
            system_llm_callback=_dummy_system_llm,
        )
        result = mgr.workspaces
        assert isinstance(result, dict)

    def test_create_with_config(self):
        """create() accepts initial_config and passes it to the new workspace."""
        config = _make_config()
        mgr = WorkspaceManager(
            config=config,
            call_llm_factory=_dummy_call_llm_factory,
            system_llm_callback=_dummy_system_llm,
        )
        ws = mgr.create(
            workspace_id="ws-cfg",
            initial_config={"temperature": 0.7, "max_tokens": 2048},
        )
        assert isinstance(ws, Workspace)
