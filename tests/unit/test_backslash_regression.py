"""Backslash regression tests -- the exact bug from astropy-14096.

The Docker action classes routed file I/O through
`docker exec bash -c "printf '%s' '...' > file"` with manual escaping.
This corrupted backslashes: content like `r"(\\W|\\b|_)"` became
`r"(\\\\W|\\\\b|_)"`.

These tests ensure that editing a file with regex patterns preserves
all backslash sequences byte-for-byte.
"""
from __future__ import annotations

import pytest

from midas_agent.runtime.io_backend import LocalIO
from midas_agent.stdlib.actions.file_ops import EditFileAction


@pytest.mark.unit
class TestBackslashRegression:
    """Backslash content must survive edit operations unchanged."""

    def test_edit_preserves_regex_in_same_file(self, tmp_path):
        """File containing re.compile(r"(\\W|\\b|_)") and a separate function.
        Edit the function via EditFileAction. Assert the regex line is
        byte-for-byte unchanged. This is the exact bug from astropy-14096."""
        f = tmp_path / "astropy_code.py"
        original_content = (
            'import re\n'
            '\n'
            '_SIMPLE_TABLE_CHARS = re.compile(r"(\\W|\\b|_)")\n'
            '\n'
            '\n'
            'def format_table(data):\n'
            '    """Format a table."""\n'
            '    return str(data)\n'
        )
        f.write_text(original_content)

        action = EditFileAction(cwd=str(tmp_path), io=LocalIO())
        action.execute(
            path="astropy_code.py",
            old_string='def format_table(data):\n    """Format a table."""\n    return str(data)',
            new_string='def format_table(data):\n    """Format a table with header."""\n    header = "| " + " | ".join(data.columns) + " |"\n    return header + "\\n" + str(data)',
        )

        content = f.read_text()

        # The regex line must be BYTE-FOR-BYTE identical
        assert r'_SIMPLE_TABLE_CHARS = re.compile(r"(\W|\b|_)")' in content, \
            f"Regex backslashes were corrupted! Content:\n{content}"

        # The edit must have been applied
        assert "Format a table with header" in content
        assert "header" in content

    def test_edit_preserves_multiple_backslash_patterns(self, tmp_path):
        """Multiple regex patterns in the same file all survive editing."""
        f = tmp_path / "multi_regex.py"
        original_content = (
            'import re\n'
            '\n'
            'WORD_BOUNDARY = re.compile(r"\\b\\w+\\b")\n'
            'ESCAPE_CHARS = re.compile(r"[\\n\\t\\r\\\\]")\n'
            'RAW_PATTERN = r"(\\W|\\b|_)"\n'
            '\n'
            'def process(text):\n'
            '    return text.strip()\n'
        )
        f.write_text(original_content)

        action = EditFileAction(cwd=str(tmp_path), io=LocalIO())
        action.execute(
            path="multi_regex.py",
            old_string="def process(text):\n    return text.strip()",
            new_string="def process(text):\n    return text.strip().lower()",
        )

        content = f.read_text()

        # All regex patterns must be preserved
        assert r'WORD_BOUNDARY = re.compile(r"\b\w+\b")' in content
        assert r'ESCAPE_CHARS = re.compile(r"[\n\t\r\\]")' in content
        assert r'RAW_PATTERN = r"(\W|\b|_)"' in content

        # Edit must have been applied
        assert ".lower()" in content

    def test_write_and_read_backslash_content(self, tmp_path):
        """Write content with backslashes, read it back, assert identical."""
        from midas_agent.stdlib.actions.file_ops import WriteFileAction, ReadFileAction

        content = (
            'import re\n'
            'pattern = re.compile(r"(\\W|\\b|_)")\n'
            'path = r"C:\\Users\\test\\file.txt"\n'
            'escaped = "line1\\nline2\\ttab"\n'
        )

        write_action = WriteFileAction(cwd=str(tmp_path), io=LocalIO())
        write_action.execute(path="backslash_test.py", content=content)

        # Read it back and verify
        read_content = (tmp_path / "backslash_test.py").read_text()
        assert read_content == content

    def test_edit_without_io_also_preserves_backslashes(self, tmp_path):
        """Even without explicit io=, the local action preserves backslashes."""
        f = tmp_path / "code.py"
        original = (
            'pattern = re.compile(r"(\\W|\\b|_)")\n'
            'x = 1\n'
        )
        f.write_text(original)

        action = EditFileAction(cwd=str(tmp_path))
        action.execute(
            path="code.py",
            old_string="x = 1",
            new_string="x = 42",
        )

        content = f.read_text()
        assert r'pattern = re.compile(r"(\W|\b|_)")' in content
        assert "x = 42" in content
