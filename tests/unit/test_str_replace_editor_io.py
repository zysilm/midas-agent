"""Tests for StrReplaceEditorAction with IO backend integration.

Verifies that the unified editor correctly delegates file operations
to an IOBackend when one is provided (Docker training mode), and uses
direct filesystem operations when no IO is set (local/production mode).
"""
import os
import pytest
from unittest.mock import MagicMock, patch, call

from midas_agent.stdlib.actions.str_replace_editor import StrReplaceEditorAction


class FakeIO:
    """Minimal IO backend for testing delegation."""

    def __init__(self):
        self.calls = []
        self._files = {}

    def read_file(self, path):
        self.calls.append(("read_file", path))
        if path not in self._files:
            raise FileNotFoundError(f"File not found: {path}")
        return self._files[path]

    def write_file(self, path, content):
        self.calls.append(("write_file", path, content))
        self._files[path] = content

    def run_bash(self, command, cwd=None, timeout=120):
        self.calls.append(("run_bash", command))
        return ""

    def file_exists(self, path):
        return path in self._files

    def is_directory(self, path):
        return False


# ===========================================================================
# Test: IO backend is used when provided
# ===========================================================================


class TestEditorUsesIOBackend:
    """When io is provided, all file operations go through it."""

    def test_view_uses_io_read(self):
        """view command reads file via IO backend, not direct open()."""
        io = FakeIO()
        io._files["/testbed/foo.py"] = "line1\nline2\nline3\n"

        editor = StrReplaceEditorAction(cwd="/testbed", io=io)
        result = editor.execute(command="view", path="/testbed/foo.py")

        assert any(c[0] == "read_file" and c[1] == "/testbed/foo.py" for c in io.calls), \
            f"view should use io.read_file. Calls: {io.calls}"
        assert "line1" in result

    def test_view_with_range_uses_io(self):
        """view with view_range reads via IO backend."""
        io = FakeIO()
        io._files["/testbed/foo.py"] = "line1\nline2\nline3\nline4\nline5\n"

        editor = StrReplaceEditorAction(cwd="/testbed", io=io)
        result = editor.execute(command="view", path="/testbed/foo.py", view_range=[2, 4])

        assert any(c[0] == "read_file" for c in io.calls)
        assert "line2" in result
        assert "line5" not in result

    def test_create_uses_io_write(self):
        """create command writes via IO backend, not direct open()."""
        io = FakeIO()
        editor = StrReplaceEditorAction(cwd="/testbed", io=io)

        result = editor.execute(command="create", path="/testbed/new.py", file_text="print('hello')")

        assert any(c[0] == "write_file" and c[1] == "/testbed/new.py" for c in io.calls), \
            f"create should use io.write_file. Calls: {io.calls}"
        assert "created successfully" in result.lower() or "File created" in result

    def test_str_replace_uses_io_read_and_write(self):
        """str_replace reads via IO, writes via IO."""
        io = FakeIO()
        io._files["/testbed/foo.py"] = "def bug():\n    return 1\n"

        editor = StrReplaceEditorAction(cwd="/testbed", io=io)
        result = editor.execute(
            command="str_replace",
            path="/testbed/foo.py",
            old_str="return 1",
            new_str="return 42",
        )

        reads = [c for c in io.calls if c[0] == "read_file"]
        writes = [c for c in io.calls if c[0] == "write_file"]
        assert len(reads) >= 1, "str_replace should read via IO"
        assert len(writes) >= 1, "str_replace should write via IO"
        assert "return 42" in writes[-1][2], "Written content should have the replacement"

    def test_insert_uses_io_read_and_write(self):
        """insert reads via IO, writes via IO."""
        io = FakeIO()
        io._files["/testbed/foo.py"] = "line1\nline2\nline3\n"

        editor = StrReplaceEditorAction(cwd="/testbed", io=io)
        result = editor.execute(
            command="insert",
            path="/testbed/foo.py",
            insert_line=1,
            new_str="inserted_line",
        )

        writes = [c for c in io.calls if c[0] == "write_file"]
        assert len(writes) >= 1
        assert "inserted_line" in writes[-1][2]

    def test_undo_uses_io_write(self):
        """undo_edit writes previous content via IO backend."""
        io = FakeIO()
        io._files["/testbed/foo.txt"] = "original content"

        editor = StrReplaceEditorAction(cwd="/testbed", io=io)

        # First do an edit to create undo history
        editor.execute(
            command="str_replace",
            path="/testbed/foo.txt",
            old_str="original content",
            new_str="new content",
        )

        # Now undo
        io.calls.clear()
        result = editor.execute(command="undo_edit", path="/testbed/foo.txt")

        writes = [c for c in io.calls if c[0] == "write_file"]
        assert len(writes) >= 1
        assert "original content" in writes[-1][2]


# ===========================================================================
# Test: Local mode (no IO) still works
# ===========================================================================


class TestEditorLocalModeBackwardCompat:
    """Without io parameter, editor uses direct filesystem (production mode)."""

    def test_view_local(self, tmp_path):
        """view works on local filesystem without IO."""
        (tmp_path / "test.py").write_text("hello world\n")
        editor = StrReplaceEditorAction(cwd=str(tmp_path))
        result = editor.execute(command="view", path=str(tmp_path / "test.py"))
        assert "hello world" in result

    def test_str_replace_local(self, tmp_path):
        """str_replace works on local filesystem without IO."""
        (tmp_path / "test.txt").write_text("old line\n")
        editor = StrReplaceEditorAction(cwd=str(tmp_path))
        result = editor.execute(
            command="str_replace",
            path=str(tmp_path / "test.txt"),
            old_str="old line",
            new_str="new line",
        )
        assert "new line" in (tmp_path / "test.txt").read_text()

    def test_create_local(self, tmp_path):
        """create works on local filesystem without IO."""
        editor = StrReplaceEditorAction(cwd=str(tmp_path))
        result = editor.execute(
            command="create",
            path=str(tmp_path / "new.py"),
            file_text="print('hi')",
        )
        assert (tmp_path / "new.py").exists()
        assert "print('hi')" in (tmp_path / "new.py").read_text()


# ===========================================================================
# Test: Backslash safety through IO backend
# ===========================================================================


class TestBackslashSafetyWithIO:
    """The exact bug from astropy-14096: backslashes must survive editing."""

    def test_str_replace_preserves_backslashes_in_other_parts(self):
        """Edit one function, regex with backslashes elsewhere stays intact."""
        io = FakeIO()
        io._files["/testbed/sky_coordinate.py"] = (
            'import re\n'
            '\n'
            'PATTERN = re.compile(r"(\\W|\\b|_)")\n'
            '\n'
            'def __getattr__(self, attr):\n'
            '    # buggy implementation\n'
            '    return self.__dict__[attr]\n'
            '\n'
            'def guess_from_table(cls, table):\n'
            '    return PATTERN.findall(str(table))\n'
        )

        editor = StrReplaceEditorAction(cwd="/testbed", io=io)
        result = editor.execute(
            command="str_replace",
            path="/testbed/sky_coordinate.py",
            old_str='def __getattr__(self, attr):\n    # buggy implementation\n    return self.__dict__[attr]',
            new_str='def __getattr__(self, attr):\n    # fixed implementation\n    for cls in type(self).__mro__:\n        if attr in cls.__dict__:\n            return cls.__dict__[attr].__get__(self, type(self))',
        )

        writes = [c for c in io.calls if c[0] == "write_file"]
        assert len(writes) == 1
        written_content = writes[0][2]

        # The regex pattern MUST be unchanged
        assert r're.compile(r"(\W|\b|_)")' in written_content, \
            f"Regex was corrupted! Content:\n{written_content}"
        # The fix MUST be applied
        assert "fixed implementation" in written_content
        assert "__mro__" in written_content

    def test_create_with_backslashes(self):
        """Creating a file with backslash content via IO preserves them."""
        io = FakeIO()
        editor = StrReplaceEditorAction(cwd="/testbed", io=io)

        content = 'REGEX = re.compile(r"(\\W|\\b|_)")\nPATH = "C:\\\\Users\\\\test"\n'
        editor.execute(command="create", path="/testbed/test.py", file_text=content)

        writes = [c for c in io.calls if c[0] == "write_file"]
        assert writes[-1][2] == content, "Content must be byte-for-byte identical"


# ===========================================================================
# Test: Docker training simulation (realistic chain)
# ===========================================================================


class TestDockerTrainingChain:
    """Simulate the training pipeline: workspace creates editor with IO,
    agent uses it to view/edit/create files inside Docker container."""

    def test_full_edit_workflow_through_io(self):
        """Simulate: view file → str_replace → view again → verify."""
        io = FakeIO()
        io._files["/testbed/astropy/modeling/separable.py"] = (
            "def _cstack(left, right):\n"
            "    noutp = left.shape[0] + right.shape[0]\n"
            "    # ... code ...\n"
            "    cright[-right.shape[0]:, -right.shape[1]:] = 1\n"
            "    return np.hstack([cleft, cright])\n"
        )

        editor = StrReplaceEditorAction(cwd="/testbed", io=io)

        # Step 1: view the file
        result = editor.execute(
            command="view",
            path="/testbed/astropy/modeling/separable.py",
        )
        assert "_cstack" in result

        # Step 2: make the fix
        result = editor.execute(
            command="str_replace",
            path="/testbed/astropy/modeling/separable.py",
            old_str="    cright[-right.shape[0]:, -right.shape[1]:] = 1",
            new_str="    cright[-right.shape[0]:, -right.shape[1]:] = right",
        )
        assert "has been edited" in result

        # Step 3: view again to verify
        result = editor.execute(
            command="view",
            path="/testbed/astropy/modeling/separable.py",
        )
        assert "= right" in result
        assert "= 1" not in result

    def test_create_reproduce_script_then_cleanup(self):
        """Agent creates reproduce script, then would delete it via bash."""
        io = FakeIO()
        editor = StrReplaceEditorAction(cwd="/testbed", io=io)

        # Create reproduce script
        result = editor.execute(
            command="create",
            path="/testbed/reproduce.py",
            file_text="from astropy import test\ntest()\n",
        )
        assert "created successfully" in result.lower() or "File created" in result

        # Verify it's in IO's files
        assert "/testbed/reproduce.py" in io._files
        assert "from astropy import test" in io._files["/testbed/reproduce.py"]

    def test_multiple_edits_with_undo(self):
        """Multiple edits to same file, then undo reverts correctly."""
        io = FakeIO()
        io._files["/testbed/core.py"] = "version = 1\nname = 'old'\n"

        editor = StrReplaceEditorAction(cwd="/testbed", io=io)

        # Edit 1
        editor.execute(
            command="str_replace",
            path="/testbed/core.py",
            old_str="version = 1",
            new_str="version = 2",
        )
        assert "version = 2" in io._files["/testbed/core.py"]

        # Edit 2
        editor.execute(
            command="str_replace",
            path="/testbed/core.py",
            old_str="name = 'old'",
            new_str="name = 'new'",
        )
        assert "name = 'new'" in io._files["/testbed/core.py"]

        # Undo last edit
        editor.execute(command="undo_edit", path="/testbed/core.py")
        assert "name = 'old'" in io._files["/testbed/core.py"]
        assert "version = 2" in io._files["/testbed/core.py"]


# ===========================================================================
# Test: Workspace wiring
# ===========================================================================


class TestWorkspaceWiresIO:
    """Workspace must pass IO backend to StrReplaceEditorAction."""

    def test_workspace_passes_io_to_editor(self):
        """When workspace has _io set, StrReplaceEditorAction receives it."""
        # This tests the wiring in workspace.py execute() method
        from midas_agent.workspace.graph_emergence.workspace import GraphEmergenceWorkspace

        # Check that the workspace code creates StrReplaceEditorAction with io
        import inspect
        source = inspect.getsource(GraphEmergenceWorkspace.execute)

        # The workspace should pass io= to StrReplaceEditorAction
        assert "StrReplaceEditorAction" in source, \
            "Workspace must use StrReplaceEditorAction"
        # This will fail until we add io= parameter
        assert "io=" in source or "io=io" in source or "io=self._io" in source, \
            "Workspace must pass io backend to StrReplaceEditorAction"


class TestTrainingPipelineDockerIO:
    """Training pipeline must wire DockerIO into actions."""

    def test_training_creates_docker_io(self):
        """training.py creates DockerIO when execution_env=docker."""
        import inspect
        from midas_agent import training
        source = inspect.getsource(training.run_training)

        # Must create DockerIO from container_id
        assert "DockerIO" in source or "docker_io" in source, \
            "training.py must create DockerIO for Docker execution mode"

    def test_training_passes_io_to_workspace(self):
        """training.py passes IO backend to workspace."""
        import inspect
        from midas_agent import training
        source = inspect.getsource(training.run_training)

        # Must set io on workspace
        assert "_io" in source or "io=" in source, \
            "training.py must pass IO backend to workspace"
