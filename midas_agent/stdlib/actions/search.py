"""Search actions — code search and file find."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from midas_agent.stdlib.action import Action

if TYPE_CHECKING:
    from midas_agent.runtime.io_backend import IOBackend

DEFAULT_SEARCH_LIMIT = 30


class SearchCodeAction(Action):
    def __init__(self, cwd: str | None = None, io: IOBackend | None = None) -> None:
        self.cwd = cwd
        self._io = io

    @property
    def name(self) -> str:
        return "search_code"

    @property
    def description(self) -> str:
        return "Searches file contents using regex (ripgrep). Returns matching lines with file paths and line numbers."

    @property
    def parameters(self) -> dict:
        return {
            "pattern": {"type": "string", "required": True, "description": "Regex pattern to search for in file contents."},
            "path": {"type": "string", "required": False, "description": "Directory to search in. Defaults to cwd."},
            "include": {"type": "string", "required": False, "description": "Glob pattern to filter files, e.g. '*.py'."},
            "max_results": {"type": "integer", "required": False, "description": "Maximum number of matching lines to return. Defaults to 30."},
        }

    def _strip_cwd_prefix(self, output: str) -> str:
        """Strip the cwd prefix from all paths in search output."""
        cwd = self.cwd or os.getcwd()
        # Ensure cwd ends with separator for clean stripping
        if not cwd.endswith(os.sep):
            cwd += os.sep
        return output.replace(cwd, "")

    def _apply_limit(self, output: str, limit: int) -> str:
        """Cap output to limit lines and append truncation indicator."""
        lines = output.split("\n")
        if len(lines) <= limit:
            return output
        kept = "\n".join(lines[:limit])
        remaining = len(lines) - limit
        return f"{kept}\n... and {remaining} more matches (use max_results to see more)"

    def execute(self, **kwargs) -> str:
        pattern = kwargs["pattern"]
        include = kwargs.get("include")
        path = kwargs.get("path")
        max_results = kwargs.get("max_results", DEFAULT_SEARCH_LIMIT)

        # IO backend mode: delegate search to io.run_bash()
        if self._io is not None:
            import shlex
            search_path = path or "."
            cmd_parts = ["grep", "-rnE", shlex.quote(pattern)]
            if include:
                cmd_parts.extend(["--include", shlex.quote(include)])
            cmd_parts.append(shlex.quote(search_path))
            cmd_str = " ".join(cmd_parts)
            try:
                output = self._io.run_bash(cmd_str, cwd=self.cwd, timeout=30)
                output = output.strip()
                if not output:
                    return "No matches found"
                return self._apply_limit(output, max_results)
            except Exception as e:
                return f"Search error: {e}"

        search_dir = self.cwd or os.getcwd()
        if path:
            search_dir = os.path.join(search_dir, path)

        cmd: list[str]
        try:
            # Try ripgrep first
            cmd = ["rg", "-n", pattern]
            if include:
                cmd.extend(["--glob", include])
            cmd.append(search_dir)
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0 and result.stdout.strip():
                output = self._strip_cwd_prefix(result.stdout.strip())
                return self._apply_limit(output, max_results)
            if result.returncode == 1:
                # rg returns 1 when no matches
                return "No matches found"
            if result.returncode == 2:
                # rg error (e.g. bad regex) -- still return something useful
                return f"Search error: {result.stderr.strip()}"
            # returncode 0 but empty output
            return "No matches found"
        except FileNotFoundError:
            # rg not installed, fall back to grep
            cmd = ["grep", "-rnE", pattern]
            if include:
                cmd.extend(["--include", include])
            cmd.append(search_dir)
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=30
                )
                if result.returncode == 0 and result.stdout.strip():
                    output = self._strip_cwd_prefix(result.stdout.strip())
                    return self._apply_limit(output, max_results)
                return "No matches found"
            except Exception as e:
                return f"Search error: {e}"
        except Exception as e:
            return f"Search error: {e}"


DEFAULT_FIND_LIMIT = 50


class FindFilesAction(Action):
    def __init__(self, cwd: str | None = None, io: IOBackend | None = None) -> None:
        self.cwd = cwd
        self._io = io

    @property
    def name(self) -> str:
        return "find_files"

    @property
    def description(self) -> str:
        return "Finds files by glob pattern. Returns matching file paths."

    @property
    def parameters(self) -> dict:
        return {
            "pattern": {"type": "string", "required": True, "description": "Glob pattern to match file names, e.g. '*.py' or '**/*.yaml'."},
            "path": {"type": "string", "required": False, "description": "Directory to search in. Defaults to cwd."},
            "max_results": {"type": "integer", "required": False, "description": "Maximum number of file paths to return. Defaults to 50."},
        }

    def execute(self, **kwargs) -> str:
        pattern = kwargs["pattern"]
        path = kwargs.get("path")
        max_results = kwargs.get("max_results", DEFAULT_FIND_LIMIT)

        # IO backend mode: delegate find to io.run_bash()
        if self._io is not None:
            import shlex
            search_path = path or "."
            # Convert glob pattern to find -name pattern
            name_pattern = pattern.replace("**/", "")
            cmd = f"find {shlex.quote(search_path)} -name {shlex.quote(name_pattern)} -type f"
            try:
                output = self._io.run_bash(cmd, cwd=self.cwd, timeout=30)
                output = output.strip()
                if not output:
                    return f"No files found matching: {pattern}"
                lines = [line.lstrip("./") for line in output.split("\n") if line.strip()]
                lines = sorted(lines)
                total = len(lines)
                if total <= max_results:
                    return "\n".join(lines)
                show_count = max(max_results - 1, 1)
                kept = lines[:show_count]
                remaining = total - show_count
                return "\n".join(kept) + f"\n... and {remaining} more files (use max_results to see more)"
            except Exception as e:
                return f"Error finding files: {e}"

        base_dir = Path(self.cwd) if self.cwd else Path.cwd()
        if path:
            base_dir = base_dir / path

        matches = sorted(base_dir.glob(pattern))

        if not matches:
            return f"No files found matching: {pattern}"

        # Return paths relative to cwd (or base_dir if no cwd)
        cwd_path = Path(self.cwd) if self.cwd else Path.cwd()
        rel_paths: list[str] = []
        for m in matches:
            if m.is_file():
                try:
                    rel_paths.append(str(m.relative_to(cwd_path)))
                except ValueError:
                    rel_paths.append(str(m))

        if not rel_paths:
            return f"No files found matching: {pattern}"

        total = len(rel_paths)
        if total <= max_results:
            return "\n".join(rel_paths)

        # Keep max_results - 1 data lines + 1 indicator line = max_results total
        show_count = max(max_results - 1, 1)
        kept = rel_paths[:show_count]
        remaining = total - show_count
        return "\n".join(kept) + f"\n... and {remaining} more files (use max_results to see more)"
