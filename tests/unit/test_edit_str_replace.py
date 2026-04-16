"""Unit tests for str_replace-based EditFileAction.

Replaces the old line-number-based edit with content-matching edit,
modeled after Claude Code's Edit tool and SWE-agent's str_replace_editor.

The LLM provides old_string (exact match) and new_string (replacement).
No line numbers needed for editing — more reliable across multiple edits.
"""
import pytest

from midas_agent.stdlib.actions.file_ops import EditFileAction


# ===========================================================================
# Basic str_replace
# ===========================================================================


@pytest.mark.unit
class TestStrReplace:
    """Core str_replace functionality."""

    def test_replace_modifies_file(self, tmp_path):
        """old_string is replaced by new_string on disk."""
        f = tmp_path / "code.py"
        f.write_text("x = 1\ny = 2\nz = 3\n")

        action = EditFileAction(cwd=str(tmp_path))
        result = action.execute(
            path="code.py",
            old_string="y = 2",
            new_string="y = 42",
        )

        content = f.read_text()
        assert "y = 42" in content
        assert "y = 2" not in content
        assert "x = 1" in content
        assert "z = 3" in content

    def test_replace_multiline(self, tmp_path):
        """old_string can span multiple lines."""
        f = tmp_path / "code.py"
        f.write_text("def foo():\n    x = 1\n    return x\n")

        action = EditFileAction(cwd=str(tmp_path))
        action.execute(
            path="code.py",
            old_string="    x = 1\n    return x",
            new_string="    x = 1\n    y = x + 1\n    return y",
        )

        content = f.read_text()
        assert "y = x + 1" in content
        assert "return y" in content

    def test_replace_preserves_indentation(self, tmp_path):
        """Whitespace in old_string must match exactly."""
        f = tmp_path / "code.py"
        f.write_text("class Foo:\n    def bar(self):\n        pass\n")

        action = EditFileAction(cwd=str(tmp_path))
        action.execute(
            path="code.py",
            old_string="        pass",
            new_string="        return 42",
        )

        assert "        return 42" in f.read_text()

    def test_replace_returns_confirmation(self, tmp_path):
        """Successful replace returns a message with the file path."""
        f = tmp_path / "code.py"
        f.write_text("old\n")

        action = EditFileAction(cwd=str(tmp_path))
        result = action.execute(
            path="code.py",
            old_string="old",
            new_string="new",
        )

        assert isinstance(result, str)
        assert "code.py" in result

    def test_replace_entire_function(self, tmp_path):
        """Replace a complete function body."""
        f = tmp_path / "code.py"
        f.write_text(
            "def broken():\n"
            "    return None\n"
            "\n"
            "def other():\n"
            "    pass\n"
        )

        action = EditFileAction(cwd=str(tmp_path))
        action.execute(
            path="code.py",
            old_string="def broken():\n    return None",
            new_string="def broken():\n    return 42",
        )

        content = f.read_text()
        assert "return 42" in content
        assert "return None" not in content
        assert "def other" in content  # untouched


# ===========================================================================
# Uniqueness requirement
# ===========================================================================


@pytest.mark.unit
class TestUniqueness:
    """old_string must be unique in the file."""

    def test_non_unique_old_string_rejected(self, tmp_path):
        """If old_string matches multiple locations, edit is rejected."""
        f = tmp_path / "code.py"
        f.write_text("x = 1\ny = 1\nz = 1\n")

        action = EditFileAction(cwd=str(tmp_path))
        result = action.execute(
            path="code.py",
            old_string="= 1",
            new_string="= 2",
        )

        # Must reject — old_string matches 3 times
        assert "unique" in result.lower() or "multiple" in result.lower() or "ambiguous" in result.lower()
        # File must be unchanged
        assert f.read_text() == "x = 1\ny = 1\nz = 1\n"

    def test_old_string_not_found_rejected(self, tmp_path):
        """If old_string doesn't exist in file, edit is rejected."""
        f = tmp_path / "code.py"
        f.write_text("x = 1\n")

        action = EditFileAction(cwd=str(tmp_path))
        result = action.execute(
            path="code.py",
            old_string="y = 2",
            new_string="y = 3",
        )

        assert "not found" in result.lower() or "no match" in result.lower()
        assert f.read_text() == "x = 1\n"

    def test_unique_with_more_context(self, tmp_path):
        """Adding surrounding context makes a non-unique match unique."""
        f = tmp_path / "code.py"
        f.write_text("x = 1\ny = 1\n")

        action = EditFileAction(cwd=str(tmp_path))
        # "x = 1" is unique (only on first line)
        action.execute(
            path="code.py",
            old_string="x = 1",
            new_string="x = 99",
        )

        content = f.read_text()
        assert "x = 99" in content
        assert "y = 1" in content  # untouched


# ===========================================================================
# Syntax checking (Python files)
# ===========================================================================


@pytest.mark.unit
class TestSyntaxCheck:
    """Python files are syntax-checked before committing the edit."""

    def test_bad_syntax_rejected(self, tmp_path):
        """Edit that produces invalid Python is rejected."""
        f = tmp_path / "code.py"
        f.write_text("x = 1\ny = 2\n")

        action = EditFileAction(cwd=str(tmp_path))
        result = action.execute(
            path="code.py",
            old_string="x = 1",
            new_string="x = (",  # unclosed paren
        )

        assert "syntax" in result.lower() or "error" in result.lower()
        assert f.read_text() == "x = 1\ny = 2\n"  # unchanged

    def test_good_syntax_accepted(self, tmp_path):
        """Edit that produces valid Python succeeds."""
        f = tmp_path / "code.py"
        f.write_text("x = 1\ny = 2\n")

        action = EditFileAction(cwd=str(tmp_path))
        action.execute(
            path="code.py",
            old_string="x = 1",
            new_string="x = 42",
        )

        assert "x = 42" in f.read_text()

    def test_non_python_skips_syntax_check(self, tmp_path):
        """Non-.py files are not syntax-checked."""
        f = tmp_path / "config.txt"
        f.write_text("old value\n")

        action = EditFileAction(cwd=str(tmp_path))
        action.execute(
            path="config.txt",
            old_string="old value",
            new_string="this is ( not valid python",
        )

        assert "this is ( not valid python" in f.read_text()


# ===========================================================================
# Multiple edits on same file (the key advantage over line-number)
# ===========================================================================


@pytest.mark.unit
class TestMultipleEdits:
    """Multiple edits on the same file without re-reading.
    This is the key advantage: no line-number drift."""

    def test_two_edits_same_file(self, tmp_path):
        """Two consecutive edits on the same file both succeed."""
        f = tmp_path / "code.py"
        f.write_text("x = 1\ny = 2\nz = 3\n")

        action = EditFileAction(cwd=str(tmp_path))

        # First edit
        action.execute(
            path="code.py",
            old_string="x = 1",
            new_string="x = 10",
        )

        # Second edit — no need to re-read, old_string still matches
        action.execute(
            path="code.py",
            old_string="z = 3",
            new_string="z = 30",
        )

        content = f.read_text()
        assert "x = 10" in content
        assert "y = 2" in content
        assert "z = 30" in content

    def test_three_edits_different_functions(self, tmp_path):
        """Edit three different functions without re-reading."""
        f = tmp_path / "code.py"
        f.write_text(
            "def foo():\n    return 1\n\n"
            "def bar():\n    return 2\n\n"
            "def baz():\n    return 3\n"
        )

        action = EditFileAction(cwd=str(tmp_path))

        action.execute(path="code.py", old_string="return 1", new_string="return 10")
        action.execute(path="code.py", old_string="return 2", new_string="return 20")
        action.execute(path="code.py", old_string="return 3", new_string="return 30")

        content = f.read_text()
        assert "return 10" in content
        assert "return 20" in content
        assert "return 30" in content

    def test_edit_after_insertion_no_drift(self, tmp_path):
        """After inserting lines (changing file length), next edit
        still works because it uses content matching, not line numbers."""
        f = tmp_path / "code.py"
        f.write_text("a = 1\nb = 2\n")

        action = EditFileAction(cwd=str(tmp_path))

        # Insert extra lines by replacing a with a + new stuff
        action.execute(
            path="code.py",
            old_string="a = 1",
            new_string="a = 1\nextra1 = 10\nextra2 = 20",
        )

        # Now b = 2 has shifted down, but str_replace still finds it
        action.execute(
            path="code.py",
            old_string="b = 2",
            new_string="b = 200",
        )

        content = f.read_text()
        assert "a = 1" in content
        assert "extra1 = 10" in content
        assert "b = 200" in content


# ===========================================================================
# Edge cases
# ===========================================================================


@pytest.mark.unit
class TestEdgeCases:
    """Edge cases and error handling."""

    def test_file_not_found(self, tmp_path):
        """Editing a nonexistent file returns an error."""
        action = EditFileAction(cwd=str(tmp_path))
        result = action.execute(
            path="nonexistent.py",
            old_string="x",
            new_string="y",
        )

        assert "error" in result.lower() or "not found" in result.lower()

    def test_relative_path_resolved(self, tmp_path):
        """Relative paths resolve against cwd."""
        sub = tmp_path / "src"
        sub.mkdir()
        f = sub / "mod.py"
        f.write_text("old\n")

        action = EditFileAction(cwd=str(tmp_path))
        action.execute(
            path="src/mod.py",
            old_string="old",
            new_string="new",
        )

        assert f.read_text().strip() == "new"

    def test_empty_new_string_deletes(self, tmp_path):
        """Replacing with empty string effectively deletes the matched text."""
        f = tmp_path / "code.py"
        f.write_text("keep\ndelete_me\nkeep_too\n")

        action = EditFileAction(cwd=str(tmp_path))
        action.execute(
            path="code.py",
            old_string="delete_me\n",
            new_string="",
        )

        content = f.read_text()
        assert "keep\n" in content
        assert "keep_too" in content
        assert "delete_me" not in content

    def test_old_string_same_as_new_string(self, tmp_path):
        """Replacing with identical string is a no-op but should not error."""
        f = tmp_path / "code.py"
        f.write_text("x = 1\n")

        action = EditFileAction(cwd=str(tmp_path))
        result = action.execute(
            path="code.py",
            old_string="x = 1",
            new_string="x = 1",
        )

        assert f.read_text() == "x = 1\n"


# ===========================================================================
# Parameters schema
# ===========================================================================


@pytest.mark.unit
class TestParameters:
    """The parameters property should expose old_string/new_string,
    NOT the old line-number parameters."""

    def test_has_old_string_param(self):
        action = EditFileAction()
        assert "old_string" in action.parameters

    def test_has_new_string_param(self):
        action = EditFileAction()
        assert "new_string" in action.parameters

    def test_has_path_param(self):
        action = EditFileAction()
        assert "path" in action.parameters

    def test_no_start_line_param(self):
        """Line-number parameters must be removed."""
        action = EditFileAction()
        assert "start_line" not in action.parameters
        assert "end_line" not in action.parameters
        assert "insert_line" not in action.parameters
        assert "command" not in action.parameters
