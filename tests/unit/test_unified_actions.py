"""Unit tests for unified actions with IO backend.

Tests define the target behavior of:
- Actions accept optional io: IOBackend parameter
- Same action class works with both LocalIO and mock IO backends
- Backward compatible: actions work without io parameter
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from midas_agent.runtime.io_backend import IOBackend, LocalIO
from midas_agent.stdlib.actions.bash import BashAction
from midas_agent.stdlib.actions.file_ops import EditFileAction, ReadFileAction, WriteFileAction
from midas_agent.stdlib.actions.search import FindFilesAction, SearchCodeAction


# ===================================================================
# BashAction with IO backend
# ===================================================================


@pytest.mark.unit
class TestBashActionWithIO:
    """BashAction uses io.run_bash() when io is provided."""

    def test_bash_with_local_io(self):
        """BashAction(io=LocalIO()) runs commands locally."""
        action = BashAction(io=LocalIO())
        result = action.execute(command="echo hello")
        assert "hello" in result

    def test_bash_with_mock_io(self):
        """BashAction(io=mock_io) delegates to mock."""
        mock_io = MagicMock(spec=IOBackend)
        mock_io.run_bash.return_value = "mocked output"
        action = BashAction(io=mock_io)
        result = action.execute(command="echo hello")

        mock_io.run_bash.assert_called_once()
        assert result == "mocked output"

    def test_bash_backward_compatible(self):
        """BashAction without io parameter still works."""
        action = BashAction()
        result = action.execute(command="echo backward_compat")
        assert "backward_compat" in result


# ===================================================================
# EditFileAction with IO backend
# ===================================================================


@pytest.mark.unit
class TestEditFileActionWithIO:
    """EditFileAction uses io.read_file/write_file when io is provided."""

    def test_edit_with_local_io(self, tmp_path):
        """EditFileAction(io=LocalIO()) edits files locally."""
        f = tmp_path / "code.py"
        f.write_text("x = 1\ny = 2\n")

        action = EditFileAction(cwd=str(tmp_path), io=LocalIO())
        result = action.execute(
            path="code.py",
            old_string="x = 1",
            new_string="x = 42",
        )

        assert "x = 42" in f.read_text()
        assert "code.py" in result

    def test_edit_with_mock_io(self):
        """EditFileAction(io=mock_io) uses mock for file I/O."""
        mock_io = MagicMock(spec=IOBackend)
        mock_io.read_file.return_value = "x = 1\ny = 2\n"

        action = EditFileAction(io=mock_io)
        result = action.execute(
            path="/testbed/code.py",
            old_string="x = 1",
            new_string="x = 42",
        )

        mock_io.read_file.assert_called_once()
        mock_io.write_file.assert_called_once()
        # Verify the written content has the replacement
        written_content = mock_io.write_file.call_args[0][1]
        assert "x = 42" in written_content

    def test_edit_backward_compatible(self, tmp_path):
        """EditFileAction without io parameter still works."""
        f = tmp_path / "code.py"
        f.write_text("old\n")

        action = EditFileAction(cwd=str(tmp_path))
        action.execute(path="code.py", old_string="old", new_string="new")

        assert f.read_text().strip() == "new"

    def test_edit_preserves_backslashes_with_io(self, tmp_path):
        """Editing a file with backslash content preserves them."""
        f = tmp_path / "regex.py"
        f.write_text('import re\npattern = re.compile(r"(\\W|\\b|_)")\ndef foo():\n    return 1\n')

        action = EditFileAction(cwd=str(tmp_path), io=LocalIO())
        action.execute(
            path="regex.py",
            old_string="def foo():\n    return 1",
            new_string="def foo():\n    return 42",
        )

        content = f.read_text()
        assert r're.compile(r"(\W|\b|_)")' in content
        assert "return 42" in content


# ===================================================================
# ReadFileAction with IO backend
# ===================================================================


@pytest.mark.unit
class TestReadFileActionWithIO:
    """ReadFileAction uses io.read_file when io is provided."""

    def test_read_with_local_io(self, tmp_path):
        """ReadFileAction(io=LocalIO()) reads files locally."""
        f = tmp_path / "test.txt"
        f.write_text("line 1\nline 2\nline 3\n")

        action = ReadFileAction(cwd=str(tmp_path), io=LocalIO())
        result = action.execute(path="test.txt")

        assert "line 1" in result
        assert "line 2" in result

    def test_read_with_mock_io(self):
        """ReadFileAction(io=mock_io) delegates to mock."""
        mock_io = MagicMock(spec=IOBackend)
        mock_io.read_file.return_value = "line 1\nline 2\nline 3\n"

        action = ReadFileAction(io=mock_io)
        result = action.execute(path="/testbed/test.txt")

        mock_io.read_file.assert_called_once()
        assert "line 1" in result

    def test_read_with_offset_and_limit(self, tmp_path):
        """ReadFileAction with io respects offset and limit."""
        f = tmp_path / "test.txt"
        lines = "\n".join(f"line {i}" for i in range(1, 11)) + "\n"
        f.write_text(lines)

        action = ReadFileAction(cwd=str(tmp_path), io=LocalIO())
        result = action.execute(path="test.txt", offset=2, limit=3)

        assert "line 3" in result
        assert "line 4" in result
        assert "line 5" in result
        # line 1 and line 2 should not be shown (offset=2 skips first 2)
        # line 6+ should not be shown (limit=3)

    def test_read_backward_compatible(self, tmp_path):
        """ReadFileAction without io parameter still works."""
        f = tmp_path / "test.txt"
        f.write_text("hello\n")

        action = ReadFileAction(cwd=str(tmp_path))
        result = action.execute(path="test.txt")

        assert "hello" in result


# ===================================================================
# WriteFileAction with IO backend
# ===================================================================


@pytest.mark.unit
class TestWriteFileActionWithIO:
    """WriteFileAction uses io.write_file when io is provided."""

    def test_write_with_local_io(self, tmp_path):
        """WriteFileAction(io=LocalIO()) writes files locally."""
        action = WriteFileAction(cwd=str(tmp_path), io=LocalIO())
        result = action.execute(path="new.txt", content="hello world")

        assert (tmp_path / "new.txt").read_text() == "hello world"
        assert "Written" in result

    def test_write_with_mock_io(self):
        """WriteFileAction(io=mock_io) delegates to mock."""
        mock_io = MagicMock(spec=IOBackend)

        action = WriteFileAction(io=mock_io)
        result = action.execute(path="/testbed/new.txt", content="hello")

        mock_io.write_file.assert_called_once_with("/testbed/new.txt", "hello")

    def test_write_backward_compatible(self, tmp_path):
        """WriteFileAction without io parameter still works."""
        action = WriteFileAction(cwd=str(tmp_path))
        action.execute(path="test.txt", content="test")

        assert (tmp_path / "test.txt").read_text() == "test"


# ===================================================================
# SearchCodeAction with IO backend
# ===================================================================


@pytest.mark.unit
class TestSearchCodeActionWithIO:
    """SearchCodeAction uses io.run_bash when io is provided."""

    def test_search_with_mock_io(self):
        """SearchCodeAction(io=mock_io) delegates grep to mock."""
        mock_io = MagicMock(spec=IOBackend)
        mock_io.run_bash.return_value = "file.py:1:def hello():\n"

        action = SearchCodeAction(io=mock_io)
        result = action.execute(pattern="def hello")

        mock_io.run_bash.assert_called_once()
        assert "file.py" in result

    def test_search_backward_compatible(self, tmp_path):
        """SearchCodeAction without io parameter still works."""
        (tmp_path / "a.py").write_text("def hello():\n    pass\n")
        action = SearchCodeAction(cwd=str(tmp_path))
        result = action.execute(pattern="def hello")

        assert "def hello" in result


# ===================================================================
# FindFilesAction with IO backend
# ===================================================================


@pytest.mark.unit
class TestFindFilesActionWithIO:
    """FindFilesAction uses io.run_bash when io is provided."""

    def test_find_with_mock_io(self):
        """FindFilesAction(io=mock_io) delegates find to mock."""
        mock_io = MagicMock(spec=IOBackend)
        mock_io.run_bash.return_value = "./src/main.py\n./tests/test_main.py\n"

        action = FindFilesAction(io=mock_io)
        result = action.execute(pattern="*.py")

        mock_io.run_bash.assert_called_once()
        assert "main.py" in result

    def test_find_backward_compatible(self, tmp_path):
        """FindFilesAction without io parameter still works."""
        (tmp_path / "test.py").write_text("")
        action = FindFilesAction(cwd=str(tmp_path))
        result = action.execute(pattern="*.py")

        assert "test.py" in result


# ===================================================================
# Same action class, different backend
# ===================================================================


@pytest.mark.unit
class TestSameActionDifferentBackend:
    """Verify that the same action class works with both LocalIO and mock IO."""

    def test_edit_same_class_both_backends(self, tmp_path):
        """EditFileAction works with both LocalIO and mock IO."""
        # LocalIO
        f = tmp_path / "code.py"
        f.write_text("x = 1\n")
        action_local = EditFileAction(cwd=str(tmp_path), io=LocalIO())
        action_local.execute(path="code.py", old_string="x = 1", new_string="x = 2")
        assert "x = 2" in f.read_text()

        # Mock IO
        mock_io = MagicMock(spec=IOBackend)
        mock_io.read_file.return_value = "x = 1\n"
        action_mock = EditFileAction(io=mock_io)
        action_mock.execute(path="/testbed/code.py", old_string="x = 1", new_string="x = 2")
        mock_io.write_file.assert_called_once()

    def test_bash_same_class_both_backends(self):
        """BashAction works with both LocalIO and mock IO."""
        # LocalIO
        action_local = BashAction(io=LocalIO())
        assert "hello" in action_local.execute(command="echo hello")

        # Mock IO
        mock_io = MagicMock(spec=IOBackend)
        mock_io.run_bash.return_value = "hello\n"
        action_mock = BashAction(io=mock_io)
        assert "hello" in action_mock.execute(command="echo hello")
