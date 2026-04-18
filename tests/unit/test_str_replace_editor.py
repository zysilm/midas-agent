"""Unit tests for the unified str_replace_editor action.

Tests the single-tool interface that replaces read_file, edit_file, and
write_file with SWE-agent's str_replace_editor interface.
"""
import os

import pytest

from midas_agent.stdlib.actions.str_replace_editor import StrReplaceEditorAction


# ===========================================================================
# view — file
# ===========================================================================


@pytest.mark.unit
class TestViewFile:
    """view command on files."""

    def test_view_returns_content_with_line_numbers(self, tmp_path):
        f = tmp_path / "hello.py"
        f.write_text("line1\nline2\nline3\n")

        action = StrReplaceEditorAction(cwd=str(tmp_path))
        result = action.execute(command="view", path=str(f))

        assert "line1" in result
        assert "line2" in result
        assert "line3" in result
        # Should contain line numbers
        assert "1" in result
        assert "2" in result
        assert "3" in result

    def test_view_with_view_range(self, tmp_path):
        f = tmp_path / "code.py"
        lines = [f"line{i}" for i in range(1, 11)]
        f.write_text("\n".join(lines) + "\n")

        action = StrReplaceEditorAction(cwd=str(tmp_path))
        result = action.execute(command="view", path=str(f), view_range=[5, 10])

        assert "line5" in result
        assert "line10" in result
        # Lines outside the range should not appear
        assert "line1\n" not in result.split("line10")[0] or "line1" not in result.split("\n")[0]

    def test_view_with_view_range_to_end(self, tmp_path):
        f = tmp_path / "code.py"
        lines = [f"line{i}" for i in range(1, 11)]
        f.write_text("\n".join(lines) + "\n")

        action = StrReplaceEditorAction(cwd=str(tmp_path))
        result = action.execute(command="view", path=str(f), view_range=[5, -1])

        assert "line5" in result
        assert "line10" in result

    def test_view_nonexistent_file_returns_error(self, tmp_path):
        action = StrReplaceEditorAction(cwd=str(tmp_path))
        result = action.execute(command="view", path=str(tmp_path / "nope.py"))

        assert "error" in result.lower() or "does not exist" in result.lower()


# ===========================================================================
# view — directory
# ===========================================================================


@pytest.mark.unit
class TestViewDirectory:
    """view command on directories."""

    def test_view_directory_lists_files(self, tmp_path):
        (tmp_path / "a.py").write_text("x")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "b.py").write_text("y")

        action = StrReplaceEditorAction(cwd=str(tmp_path))
        result = action.execute(command="view", path=str(tmp_path))

        assert "a.py" in result
        assert "b.py" in result


# ===========================================================================
# create
# ===========================================================================


@pytest.mark.unit
class TestCreate:
    """create command."""

    def test_create_new_file(self, tmp_path):
        target = tmp_path / "new_file.py"

        action = StrReplaceEditorAction(cwd=str(tmp_path))
        result = action.execute(
            command="create",
            path=str(target),
            file_text="print('hello')\n",
        )

        assert target.exists()
        assert target.read_text() == "print('hello')\n"
        assert "created" in result.lower() or "success" in result.lower()

    def test_create_existing_file_returns_error(self, tmp_path):
        target = tmp_path / "existing.py"
        target.write_text("old content")

        action = StrReplaceEditorAction(cwd=str(tmp_path))
        result = action.execute(
            command="create",
            path=str(target),
            file_text="new content",
        )

        # Should fail because file already exists
        assert "error" in result.lower() or "already exists" in result.lower()
        # File content should be unchanged
        assert target.read_text() == "old content"


# ===========================================================================
# str_replace
# ===========================================================================


@pytest.mark.unit
class TestStrReplace:
    """str_replace command."""

    def test_exact_match_replaces(self, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("x = 1\ny = 2\nz = 3\n")

        action = StrReplaceEditorAction(cwd=str(tmp_path))
        result = action.execute(
            command="str_replace",
            path=str(f),
            old_str="y = 2",
            new_str="y = 42",
        )

        content = f.read_text()
        assert "y = 42" in content
        assert "y = 2" not in content

    def test_non_unique_returns_error(self, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("x = 1\ny = 1\nz = 1\n")

        action = StrReplaceEditorAction(cwd=str(tmp_path))
        result = action.execute(
            command="str_replace",
            path=str(f),
            old_str="= 1",
            new_str="= 2",
        )

        assert "multiple" in result.lower() or "unique" in result.lower() or "occurrences" in result.lower()
        # File should be unchanged
        assert f.read_text() == "x = 1\ny = 1\nz = 1\n"

    def test_not_found_returns_error(self, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("x = 1\n")

        action = StrReplaceEditorAction(cwd=str(tmp_path))
        result = action.execute(
            command="str_replace",
            path=str(f),
            old_str="y = 2",
            new_str="y = 3",
        )

        assert "not found" in result.lower() or "did not appear" in result.lower()

    def test_preserves_backslashes(self, tmp_path):
        """Regression: backslash sequences must not be interpreted."""
        f = tmp_path / "code.txt"
        f.write_text("path = 'C:\\\\Users\\\\test'\n")

        action = StrReplaceEditorAction(cwd=str(tmp_path))
        action.execute(
            command="str_replace",
            path=str(f),
            old_str="path = 'C:\\\\Users\\\\test'",
            new_str="path = 'C:\\\\Users\\\\new_test'",
        )

        content = f.read_text()
        assert "new_test" in content

    def test_python_syntax_check_rejects_bad_syntax(self, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("x = 1\ny = 2\n")

        action = StrReplaceEditorAction(cwd=str(tmp_path))
        result = action.execute(
            command="str_replace",
            path=str(f),
            old_str="x = 1",
            new_str="x = (",  # unclosed paren
        )

        assert "syntax" in result.lower()
        # File should be unchanged
        assert f.read_text() == "x = 1\ny = 2\n"

    def test_non_python_skips_syntax_check(self, tmp_path):
        f = tmp_path / "config.txt"
        f.write_text("old value\n")

        action = StrReplaceEditorAction(cwd=str(tmp_path))
        action.execute(
            command="str_replace",
            path=str(f),
            old_str="old value",
            new_str="this is ( not valid python",
        )

        assert "this is ( not valid python" in f.read_text()


# ===========================================================================
# insert
# ===========================================================================


@pytest.mark.unit
class TestInsert:
    """insert command."""

    def test_insert_at_line(self, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("line1\nline2\nline3\n")

        action = StrReplaceEditorAction(cwd=str(tmp_path))
        result = action.execute(
            command="insert",
            path=str(f),
            insert_line=2,
            new_str="inserted",
        )

        content = f.read_text()
        lines = content.split("\n")
        assert lines[0] == "line1"
        assert lines[1] == "line2"
        assert lines[2] == "inserted"
        assert lines[3] == "line3"

    def test_insert_at_line_0(self, tmp_path):
        """insert_line=0 inserts at the beginning."""
        f = tmp_path / "code.py"
        f.write_text("line1\nline2\n")

        action = StrReplaceEditorAction(cwd=str(tmp_path))
        action.execute(
            command="insert",
            path=str(f),
            insert_line=0,
            new_str="header",
        )

        content = f.read_text()
        lines = content.split("\n")
        assert lines[0] == "header"
        assert lines[1] == "line1"


# ===========================================================================
# undo_edit
# ===========================================================================


@pytest.mark.unit
class TestUndoEdit:
    """undo_edit command."""

    def test_undo_reverts_last_edit(self, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("original\n")

        action = StrReplaceEditorAction(cwd=str(tmp_path))
        action.execute(
            command="str_replace",
            path=str(f),
            old_str="original",
            new_str="modified",
        )
        assert "modified" in f.read_text()

        action.execute(command="undo_edit", path=str(f))
        assert f.read_text() == "original\n"

    def test_undo_no_history_returns_error(self, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("content\n")

        action = StrReplaceEditorAction(cwd=str(tmp_path))
        result = action.execute(command="undo_edit", path=str(f))

        assert "error" in result.lower() or "no edit history" in result.lower()


# ===========================================================================
# Error handling
# ===========================================================================


@pytest.mark.unit
class TestErrorHandling:
    """Invalid commands and missing parameters."""

    def test_invalid_command(self, tmp_path):
        action = StrReplaceEditorAction(cwd=str(tmp_path))
        result = action.execute(command="invalid_cmd", path=str(tmp_path / "x.py"))

        assert "error" in result.lower() or "unrecognized" in result.lower()

    def test_missing_path(self, tmp_path):
        action = StrReplaceEditorAction(cwd=str(tmp_path))
        result = action.execute(command="view")

        assert "error" in result.lower() or "missing" in result.lower() or "required" in result.lower()


# ===========================================================================
# Tool interface
# ===========================================================================


@pytest.mark.unit
class TestToolInterface:
    """Verify the Action interface (name, description, parameters)."""

    def test_name_is_str_replace_editor(self):
        action = StrReplaceEditorAction()
        assert action.name == "str_replace_editor"

    def test_has_command_parameter(self):
        action = StrReplaceEditorAction()
        assert "command" in action.parameters

    def test_has_path_parameter(self):
        action = StrReplaceEditorAction()
        assert "path" in action.parameters

    def test_has_file_text_parameter(self):
        action = StrReplaceEditorAction()
        assert "file_text" in action.parameters

    def test_has_old_str_parameter(self):
        action = StrReplaceEditorAction()
        assert "old_str" in action.parameters

    def test_has_new_str_parameter(self):
        action = StrReplaceEditorAction()
        assert "new_str" in action.parameters

    def test_has_insert_line_parameter(self):
        action = StrReplaceEditorAction()
        assert "insert_line" in action.parameters

    def test_has_view_range_parameter(self):
        action = StrReplaceEditorAction()
        assert "view_range" in action.parameters
