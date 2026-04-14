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


@pytest.mark.unit
class TestEditFileAction:
    """Tests for EditFileAction."""

    def test_edit_file_action_name(self):
        """EditFileAction.name returns 'edit_file'."""
        action = EditFileAction()
        assert action.name == "edit_file"

    def test_edit_file_action_execute(self):
        """EditFileAction.execute() returns a string result."""
        action = EditFileAction()
        result = action.execute(
            path="/tmp/test.txt",
            command="replace",
            start_line=1,
        )
        assert isinstance(result, str)


@pytest.mark.unit
class TestWriteFileAction:
    """Tests for WriteFileAction."""

    def test_write_file_action_name(self):
        """WriteFileAction.name returns 'write_file'."""
        action = WriteFileAction()
        assert action.name == "write_file"

    def test_write_file_action_execute(self):
        """WriteFileAction.execute() returns a string result."""
        action = WriteFileAction()
        result = action.execute(path="/tmp/test.txt", content="hello")
        assert isinstance(result, str)


@pytest.mark.unit
class TestSearchCodeAction:
    """Tests for SearchCodeAction."""

    def test_search_code_action_name(self):
        """SearchCodeAction.name returns 'search_code'."""
        action = SearchCodeAction()
        assert action.name == "search_code"

    def test_search_code_action_execute(self):
        """SearchCodeAction.execute() returns a string result."""
        action = SearchCodeAction()
        result = action.execute(pattern="def test_")
        assert isinstance(result, str)


@pytest.mark.unit
class TestFindFilesAction:
    """Tests for FindFilesAction."""

    def test_find_files_action_name(self):
        """FindFilesAction.name returns 'find_files'."""
        action = FindFilesAction()
        assert action.name == "find_files"

    def test_find_files_action_execute(self):
        """FindFilesAction.execute() returns a string result."""
        action = FindFilesAction()
        result = action.execute(pattern="*.py")
        assert isinstance(result, str)


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
        assert action.name == "delegate_task"


@pytest.mark.unit
class TestReportResultAction:
    """Tests for ReportResultAction."""

    def test_report_result_construction(self):
        """ReportResultAction can be constructed with a report callback."""
        action = ReportResultAction(report=lambda result: None)
        assert action.name == "report_result"
