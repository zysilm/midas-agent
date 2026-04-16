"""Unit tests for all action implementations."""
import pytest

from midas_agent.stdlib.actions.bash import BashAction
from midas_agent.stdlib.actions.file_ops import ReadFileAction, EditFileAction, WriteFileAction
from midas_agent.stdlib.actions.search import SearchCodeAction, FindFilesAction
from midas_agent.stdlib.actions.task_done import TaskDoneAction
from midas_agent.stdlib.actions.delegate_task import DelegateTaskAction
from midas_agent.stdlib.actions.report_result import ReportResultAction


@pytest.mark.unit
class TestBashAction:
    """Tests for BashAction."""

    def test_bash_action_name(self):
        """BashAction.name returns 'bash'."""
        action = BashAction()
        assert action.name == "bash"

    def test_bash_action_execute(self):
        """BashAction.execute() returns a string result."""
        action = BashAction()
        result = action.execute(command="ls")
        assert isinstance(result, str)


@pytest.mark.unit
class TestReadFileAction:
    """Tests for ReadFileAction."""

    def test_read_file_action_name(self):
        """ReadFileAction.name returns 'read_file'."""
        action = ReadFileAction()
        assert action.name == "read_file"

    def test_read_file_action_execute(self):
        """ReadFileAction.execute() returns a string result."""
        action = ReadFileAction()
        result = action.execute(path="/tmp/test.txt")
        assert isinstance(result, str)

    def test_description_includes_cwd(self):
        """When cwd is set, tool description must include the actual working
        directory so the LLM knows where files are."""
        action = ReadFileAction(cwd="/tmp/workspace/repo")
        desc = action.description
        assert "/tmp/workspace/repo" in desc
        assert "must be an absolute path" not in desc.lower(), \
            "Description should not mandate absolute paths — this causes LLM hallucination"

    def test_description_without_cwd_no_path_leak(self):
        """When cwd is None, description must not contain a spurious working
        directory value (no empty string or None injection)."""
        action = ReadFileAction(cwd=None)
        desc = action.description
        assert "None" not in desc
        # Still should not mandate absolute paths
        assert "must be an absolute path" not in desc.lower()

    def test_file_not_found_without_cwd_no_crash(self):
        """When cwd is not set, File not found still works without crash."""
        action = ReadFileAction(cwd=None)
        result = action.execute(path="/nonexistent/path/file.py")
        assert "File not found" in result

    def test_relative_path_resolves_with_cwd(self, tmp_path):
        """Relative paths resolve against cwd and read the file successfully."""
        cwd = str(tmp_path)
        (tmp_path / "hello.py").write_text("print('hello')\n")

        action = ReadFileAction(cwd=cwd)
        result = action.execute(path="hello.py")

        assert "print('hello')" in result
        assert "File not found" not in result


@pytest.mark.unit
class TestEditFileAction:
    """Tests for EditFileAction."""

    def test_edit_file_action_name(self):
        """EditFileAction.name returns 'edit_file'."""
        action = EditFileAction()
        assert action.name == "edit_file"

    # -- str_replace --

    def test_replace_modifies_file_content(self, tmp_path):
        """old_string/new_string replace actually changes the file on disk."""
        f = tmp_path / "code.py"
        f.write_text("line1\nline2\nline3\nline4\n")

        action = EditFileAction(cwd=str(tmp_path))
        result = action.execute(
            path="code.py",
            old_string="line2\nline3",
            new_string="replaced2\nreplaced3",
        )

        content = f.read_text()
        assert "replaced2" in content
        assert "replaced3" in content
        assert "line1" in content
        assert "line4" in content
        # Original lines 2-3 must be gone
        assert "line2" not in content
        assert "line3" not in content

    def test_replace_single_line(self, tmp_path):
        """Replace a single line using old_string/new_string."""
        f = tmp_path / "code.py"
        f.write_text("aaa\nbbb\nccc\n")

        action = EditFileAction(cwd=str(tmp_path))
        action.execute(
            path="code.py",
            old_string="bbb",
            new_string="BBB",
        )

        lines = f.read_text().splitlines()
        assert lines == ["aaa", "BBB", "ccc"]

    def test_replace_returns_confirmation(self, tmp_path):
        """Replace returns a message confirming the edit."""
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

    # -- insert via str_replace --

    def test_insert_adds_content_after_line(self, tmp_path):
        """Insert new content after a line by including the line in old_string."""
        f = tmp_path / "code.py"
        f.write_text("line1\nline2\nline3\n")

        action = EditFileAction(cwd=str(tmp_path))
        action.execute(
            path="code.py",
            old_string="line1\n",
            new_string="line1\ninserted\n",
        )

        lines = f.read_text().splitlines()
        assert lines[0] == "line1"
        assert lines[1] == "inserted"
        assert lines[2] == "line2"

    def test_insert_at_end(self, tmp_path):
        """Insert at end by replacing last line with itself plus new content."""
        f = tmp_path / "code.py"
        f.write_text("line1\nline2\n")

        action = EditFileAction(cwd=str(tmp_path))
        action.execute(
            path="code.py",
            old_string="line2\n",
            new_string="line2\nline3\n",
        )

        lines = f.read_text().splitlines()
        assert lines == ["line1", "line2", "line3"]

    # -- delete via str_replace --

    def test_delete_removes_lines(self, tmp_path):
        """Delete lines by replacing with empty string."""
        f = tmp_path / "code.py"
        f.write_text("keep1\ndelete_me\nalso_delete\nkeep2\n")

        action = EditFileAction(cwd=str(tmp_path))
        action.execute(
            path="code.py",
            old_string="delete_me\nalso_delete\n",
            new_string="",
        )

        content = f.read_text()
        assert "keep1" in content
        assert "keep2" in content
        assert "delete_me" not in content
        assert "also_delete" not in content

    # -- syntax checking --

    def test_python_syntax_check_rejects_bad_edit(self, tmp_path):
        """Editing a .py file with invalid syntax is rejected."""
        f = tmp_path / "code.py"
        f.write_text("x = 1\ny = 2\n")

        action = EditFileAction(cwd=str(tmp_path))
        result = action.execute(
            path="code.py",
            old_string="x = 1",
            new_string="x = (",  # unclosed paren = invalid syntax
        )

        # Edit should be rejected
        assert "syntax" in result.lower() or "error" in result.lower()
        # Original file must be unchanged
        assert f.read_text() == "x = 1\ny = 2\n"

    def test_python_syntax_check_accepts_good_edit(self, tmp_path):
        """Editing a .py file with valid syntax succeeds."""
        f = tmp_path / "code.py"
        f.write_text("x = 1\ny = 2\n")

        action = EditFileAction(cwd=str(tmp_path))
        result = action.execute(
            path="code.py",
            old_string="x = 1",
            new_string="x = 42",
        )

        assert "error" not in result.lower() or "syntax" not in result.lower()
        assert "42" in f.read_text()

    def test_non_python_file_skips_syntax_check(self, tmp_path):
        """Non-.py files are not syntax-checked."""
        f = tmp_path / "config.txt"
        f.write_text("old value\n")

        action = EditFileAction(cwd=str(tmp_path))
        action.execute(
            path="config.txt",
            old_string="old value",
            new_string="this is not valid python (",
        )

        assert "this is not valid python (" in f.read_text()

    # -- edge cases --

    def test_edit_nonexistent_file_returns_error(self, tmp_path):
        """Editing a file that doesn't exist returns an error, not a crash."""
        action = EditFileAction(cwd=str(tmp_path))
        result = action.execute(
            path="does_not_exist.py",
            old_string="x",
            new_string="y",
        )

        assert "error" in result.lower() or "not found" in result.lower()

    def test_edit_resolves_relative_path_with_cwd(self, tmp_path):
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


@pytest.mark.unit
class TestWriteFileAction:
    """Tests for WriteFileAction."""

    def test_write_file_action_name(self):
        """WriteFileAction.name returns 'write_file'."""
        action = WriteFileAction()
        assert action.name == "write_file"

    def test_write_creates_file(self, tmp_path):
        """write_file creates a new file with the given content."""
        action = WriteFileAction(cwd=str(tmp_path))
        result = action.execute(path="new_file.txt", content="hello world")

        f = tmp_path / "new_file.txt"
        assert f.exists()
        assert f.read_text() == "hello world"
        assert "Written" in result

    def test_write_overwrites_existing(self, tmp_path):
        """write_file overwrites an existing file completely."""
        f = tmp_path / "existing.txt"
        f.write_text("old content")

        action = WriteFileAction(cwd=str(tmp_path))
        action.execute(path="existing.txt", content="new content")

        assert f.read_text() == "new content"

    def test_write_creates_parent_dirs(self, tmp_path):
        """write_file creates parent directories if they don't exist."""
        action = WriteFileAction(cwd=str(tmp_path))
        action.execute(path="deep/nested/dir/file.txt", content="nested")

        f = tmp_path / "deep" / "nested" / "dir" / "file.txt"
        assert f.exists()
        assert f.read_text() == "nested"


@pytest.mark.unit
class TestSearchCodeAction:
    """Tests for SearchCodeAction."""

    def test_search_code_action_name(self):
        """SearchCodeAction.name returns 'search_code'."""
        action = SearchCodeAction()
        assert action.name == "search_code"

    def test_search_finds_matching_content(self, tmp_path):
        """search_code returns file paths and matching lines for a pattern."""
        (tmp_path / "a.py").write_text("def hello():\n    pass\n")
        (tmp_path / "b.py").write_text("x = 1\n")

        action = SearchCodeAction(cwd=str(tmp_path))
        result = action.execute(pattern="def hello")

        assert "a.py" in result
        assert "def hello" in result

    def test_search_no_match_returns_empty_or_message(self, tmp_path):
        """search_code with no matches returns an informative result."""
        (tmp_path / "a.py").write_text("x = 1\n")

        action = SearchCodeAction(cwd=str(tmp_path))
        result = action.execute(pattern="zzz_nonexistent_zzz")

        # Should not contain file matches
        assert "a.py" not in result

    def test_search_with_include_filter(self, tmp_path):
        """search_code with include filters to specific file types."""
        (tmp_path / "a.py").write_text("target_string\n")
        (tmp_path / "b.js").write_text("target_string\n")

        action = SearchCodeAction(cwd=str(tmp_path))
        result = action.execute(pattern="target_string", include="*.py")

        assert "a.py" in result
        # b.js should be excluded
        assert "b.js" not in result

    def test_search_regex_support(self, tmp_path):
        """search_code supports regex patterns."""
        (tmp_path / "code.py").write_text(
            "def calculate_sum():\n"
            "def calculate_product():\n"
            "def other():\n"
        )

        action = SearchCodeAction(cwd=str(tmp_path))
        result = action.execute(pattern=r"def calculate_\w+")

        assert "calculate_sum" in result
        assert "calculate_product" in result

    def test_search_respects_cwd(self, tmp_path):
        """search_code searches within cwd, not the system root."""
        sub = tmp_path / "project"
        sub.mkdir()
        (sub / "mod.py").write_text("unique_marker_xyz\n")

        action = SearchCodeAction(cwd=str(sub))
        result = action.execute(pattern="unique_marker_xyz")

        assert "mod.py" in result

    def test_search_with_path_narrows_scope(self, tmp_path):
        """search_code with path parameter searches only that subdirectory."""
        src = tmp_path / "src"
        tests = tmp_path / "tests"
        src.mkdir()
        tests.mkdir()
        (src / "a.py").write_text("marker\n")
        (tests / "b.py").write_text("marker\n")

        action = SearchCodeAction(cwd=str(tmp_path))
        result = action.execute(pattern="marker", path="src")

        assert "a.py" in result
        assert "b.py" not in result


@pytest.mark.unit
class TestFindFilesAction:
    """Tests for FindFilesAction."""

    def test_find_files_action_name(self):
        """FindFilesAction.name returns 'find_files'."""
        action = FindFilesAction()
        assert action.name == "find_files"

    def test_find_files_returns_matching_paths(self, tmp_path):
        """find_files returns file paths matching the glob pattern."""
        (tmp_path / "foo.py").write_text("")
        (tmp_path / "bar.py").write_text("")
        (tmp_path / "baz.txt").write_text("")

        action = FindFilesAction(cwd=str(tmp_path))
        result = action.execute(pattern="*.py")

        assert "foo.py" in result
        assert "bar.py" in result
        assert "baz.txt" not in result

    def test_find_files_recursive_glob(self, tmp_path):
        """find_files supports recursive **/ glob patterns."""
        sub = tmp_path / "src" / "deep"
        sub.mkdir(parents=True)
        (sub / "nested.py").write_text("")
        (tmp_path / "top.py").write_text("")

        action = FindFilesAction(cwd=str(tmp_path))
        result = action.execute(pattern="**/*.py")

        assert "nested.py" in result
        assert "top.py" in result

    def test_find_files_no_match(self, tmp_path):
        """find_files with no matches returns informative result."""
        (tmp_path / "a.txt").write_text("")

        action = FindFilesAction(cwd=str(tmp_path))
        result = action.execute(pattern="*.rs")

        assert "a.txt" not in result

    def test_find_files_with_path_narrows_scope(self, tmp_path):
        """find_files with path searches only that subdirectory."""
        src = tmp_path / "src"
        tests = tmp_path / "tests"
        src.mkdir()
        tests.mkdir()
        (src / "mod.py").write_text("")
        (tests / "test_mod.py").write_text("")

        action = FindFilesAction(cwd=str(tmp_path))
        result = action.execute(pattern="*.py", path="src")

        assert "mod.py" in result
        assert "test_mod.py" not in result

    def test_find_files_respects_cwd(self, tmp_path):
        """find_files searches within cwd."""
        sub = tmp_path / "project"
        sub.mkdir()
        (sub / "app.py").write_text("")

        action = FindFilesAction(cwd=str(sub))
        result = action.execute(pattern="*.py")

        assert "app.py" in result


@pytest.mark.unit
class TestTaskDoneAction:
    """Tests for TaskDoneAction."""

    def test_task_done_action_name(self):
        """TaskDoneAction.name returns 'task_done'."""
        action = TaskDoneAction()
        assert action.name == "task_done"

    def test_task_done_action_execute(self):
        """TaskDoneAction.execute() returns a string result."""
        action = TaskDoneAction()
        result = action.execute(summary="Task completed successfully.")
        assert isinstance(result, str)


@pytest.mark.unit
class TestDelegateTaskAction:
    """Tests for DelegateTaskAction."""

    def test_delegate_task_construction(self):
        """DelegateTaskAction can be constructed with a find_candidates callback."""
        action = DelegateTaskAction(find_candidates=lambda desc: [])
        assert action.name == "use_agent"

    def test_description_references_planning_context(self):
        """delegate_task description should reference the planning context
        for agent/pricing info, not duplicate it in the tool description."""
        action = DelegateTaskAction(find_candidates=lambda desc: [])
        desc = action.description
        # Should reference planning phase / market info
        assert "plan" in desc.lower() or "market" in desc.lower(), \
            f"Description should reference planning context: {desc}"
        # Should NOT contain the full delegation strategy guide —
        # that belongs in market_info during planning phase
        assert "when to delegate" not in desc.lower(), \
            f"Delegation strategy should be in market_info, not tool desc: {desc}"

    def test_delegate_task_output_includes_balance(self):
        """When balance_provider is set and querying candidates (no agent_id,
        no spawn), output includes current balance."""
        action = DelegateTaskAction(
            find_candidates=lambda desc: [],
            spawn_callback=lambda desc: None,
            balance_provider=lambda: 35000,
        )
        output = action.execute(task_description="fix bug")
        assert "35000" in output

    def test_delegate_task_no_balance_without_provider(self):
        """When balance_provider is None, output does not include balance."""
        action = DelegateTaskAction(
            find_candidates=lambda desc: [],
        )
        output = action.execute(task_description="fix bug")
        assert "余额" not in output and "balance" not in output.lower()

    def test_spawn_accepts_list_of_descriptions(self):
        """spawn parameter accepts a list of specialist descriptions and
        creates one agent per description (batch spawn)."""
        from midas_agent.workspace.graph_emergence.agent import Agent, Soul

        spawned: list[Agent] = []

        def spawn_callback(task_description: str) -> Agent:
            agent = Agent(
                agent_id=f"spawned-{len(spawned)}",
                soul=Soul(system_prompt=f"Specialist: {task_description}"),
                agent_type="free",
                protected_by="lead-1",
            )
            spawned.append(agent)
            return agent

        action = DelegateTaskAction(
            find_candidates=lambda desc: [],
            spawn_callback=spawn_callback,
        )
        output = action.execute(
            task_description="fix bugs",
            spawn=["debugger specialist", "test writer"],
        )

        assert len(spawned) == 2
        assert "spawned-0" in output
        assert "spawned-1" in output

    def test_spawn_single_item_list(self):
        """spawn with a single-item list creates exactly one agent."""
        from midas_agent.workspace.graph_emergence.agent import Agent, Soul

        spawned: list[Agent] = []

        def spawn_callback(task_description: str) -> Agent:
            agent = Agent(
                agent_id=f"spawned-{len(spawned)}",
                soul=Soul(system_prompt=f"Specialist: {task_description}"),
                agent_type="free",
                protected_by="lead-1",
            )
            spawned.append(agent)
            return agent

        action = DelegateTaskAction(
            find_candidates=lambda desc: [],
            spawn_callback=spawn_callback,
        )
        output = action.execute(
            task_description="fix bug",
            spawn=["parser specialist"],
        )

        assert len(spawned) == 1
        assert "spawned-0" in output

    def test_hire_with_agent_id(self):
        """When agent_id is specified without call_llm, delegate_task
        returns an error about agent not found."""
        action = DelegateTaskAction(
            find_candidates=lambda desc: [],
        )
        output = action.execute(
            task_description="fix parsing bug",
            agent_id="expert-1",
        )
        assert isinstance(output, str)
        assert "not found" in output.lower()

    def test_candidate_output_labels_young_agents(self):
        """When candidates include agents protected by the caller, they must
        be labeled as 幼年agent so the LLM understands they are its own
        spawned agents (cheap, clean context)."""
        from midas_agent.workspace.graph_emergence.agent import Agent, Soul
        from midas_agent.workspace.graph_emergence.free_agent_manager import Candidate

        young_agent = Agent(
            agent_id="spawned-1",
            soul=Soul(system_prompt="specialist"),
            agent_type="free",
            protected_by="lead-1",
        )
        independent_agent = Agent(
            agent_id="expert-2",
            soul=Soul(system_prompt="expert"),
            agent_type="free",
            protected_by=None,
        )

        candidates = [
            Candidate(agent=young_agent, similarity=1.0, price=100),
            Candidate(agent=independent_agent, similarity=0.8, price=500),
        ]

        action = DelegateTaskAction(
            find_candidates=lambda desc: candidates,
            calling_agent_id="lead-1",
        )
        output = action.execute(task_description="fix parser")

        # Young agent (protected_by == calling_agent_id) should be labeled
        assert "幼年" in output, \
            f"Protected agent should be labeled as 幼年agent: {output}"
        assert "spawned-1" in output
        assert "expert-2" in output

    def test_candidate_output_no_young_label_for_independent(self):
        """Independent agents (not protected by caller) must NOT be labeled
        as 幼年agent."""
        from midas_agent.workspace.graph_emergence.agent import Agent, Soul
        from midas_agent.workspace.graph_emergence.free_agent_manager import Candidate

        independent = Agent(
            agent_id="expert-1",
            soul=Soul(system_prompt="expert"),
            agent_type="free",
            protected_by=None,
        )

        candidates = [
            Candidate(agent=independent, similarity=1.0, price=300),
        ]

        action = DelegateTaskAction(
            find_candidates=lambda desc: candidates,
            calling_agent_id="lead-1",
        )
        output = action.execute(task_description="fix bug")

        assert "expert-1" in output
        # No young label for independent agents
        lines_with_expert = [l for l in output.split("\n") if "expert-1" in l]
        for line in lines_with_expert:
            assert "幼年" not in line


@pytest.mark.unit
class TestReportResultAction:
    """Tests for ReportResultAction."""

    def test_report_result_construction(self):
        """ReportResultAction can be constructed with a report callback."""
        action = ReportResultAction(report=lambda result: None)
        assert action.name == "report_result"
