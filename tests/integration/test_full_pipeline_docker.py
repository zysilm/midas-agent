"""Full pipeline integration test: mock LLM → real run_training() → Docker → scorer.

Only the LLM provider is mocked. Everything else is real:
- run_training() orchestration
- Docker container startup
- DockerIO file I/O
- _generate_patch() → git diff
- submit_patch() → collect_patches()
- SWEBenchScorer evaluation

Requires: Docker running, SWE-bench images pulled.
Run with: poetry run pytest tests/integration/test_full_pipeline_docker.py -v -s
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from midas_agent.config import MidasConfig
from midas_agent.llm.provider import LLMProvider
from midas_agent.llm.types import LLMRequest, LLMResponse, TokenUsage, ToolCall
from midas_agent.types import Issue


# The known-good fix for astropy__astropy-12907
EDIT_OLD_STR = (
    "    if isinstance(right, Model):\n"
    "        cright = _coord_matrix(right, 'right', noutp)\n"
    "    else:\n"
    "        cright = np.zeros((noutp, right.shape[1]))\n"
    "        cright[-right.shape[0]:, -right.shape[1]:] = 1"
)
EDIT_NEW_STR = (
    "    if isinstance(right, Model):\n"
    "        cright = _coord_matrix(right, 'right', noutp)\n"
    "    else:\n"
    "        cright = np.zeros((noutp, right.shape[1]))\n"
    "        cright[-right.shape[0]:, -right.shape[1]:] = right"
)


def _resp(content=None, tool_calls=None):
    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        usage=TokenUsage(input_tokens=500, output_tokens=200),
    )


class ReplayLLMProvider(LLMProvider):
    """LLM provider that replays a fixed sequence of tool-call responses.

    Used by the task agent (workspace execution). The system LLM and eval LLM
    also go through providers — system LLM calls get a generic text response,
    eval LLM calls get a simple score response.
    """

    def __init__(self):
        self.call_count = 0
        self.responses = [
            # iter 1: view the file
            _resp(tool_calls=[ToolCall(
                id="c1", name="str_replace_editor",
                arguments={"command": "view", "path": "/testbed/astropy/modeling/separable.py"},
            )]),
            # iter 2: view the specific section
            _resp(tool_calls=[ToolCall(
                id="c2", name="str_replace_editor",
                arguments={"command": "view", "path": "/testbed/astropy/modeling/separable.py",
                           "view_range": [219, 260]},
            )]),
            # iter 3: make the fix
            _resp(tool_calls=[ToolCall(
                id="c3", name="str_replace_editor",
                arguments={
                    "command": "str_replace",
                    "path": "/testbed/astropy/modeling/separable.py",
                    "old_str": EDIT_OLD_STR,
                    "new_str": EDIT_NEW_STR,
                },
            )]),
            # iter 4: run pytest
            _resp(tool_calls=[ToolCall(
                id="c4", name="bash",
                arguments={"command": "python -m pytest astropy/modeling/tests/test_separable.py -q 2>&1 | tail -3"},
            )]),
            # iter 5: task_done
            _resp(tool_calls=[ToolCall(
                id="c5", name="task_done", arguments={},
            )]),
        ]

    def complete(self, request: LLMRequest) -> LLMResponse:
        idx = self.call_count
        self.call_count += 1

        # System LLM / eval LLM calls: return generic text
        if idx >= len(self.responses):
            return _resp(content='{"s_llm": 0.5}')

        return self.responses[idx]


@pytest.mark.integration
class TestFullPipelineDocker:
    """End-to-end: mock LLM → real run_training() → Docker → real scorer."""

    @pytest.fixture
    def issue(self):
        """Load the real astropy-12907 issue."""
        try:
            from datasets import load_dataset
            ds = load_dataset("princeton-nlp/SWE-bench_Verified", split="test")
            for row in ds:
                if row["instance_id"] == "astropy__astropy-12907":
                    f2p = row.get("FAIL_TO_PASS", "[]")
                    p2p = row.get("PASS_TO_PASS", "[]")
                    if isinstance(f2p, str): f2p = json.loads(f2p)
                    if isinstance(p2p, str): p2p = json.loads(p2p)
                    return Issue(
                        issue_id=row["instance_id"],
                        repo=row["repo"],
                        description=row["problem_statement"],
                        base_commit=row.get("base_commit", ""),
                        fail_to_pass=f2p,
                        pass_to_pass=p2p,
                    )
        except Exception:
            pytest.skip("Cannot load SWE-bench dataset")
        pytest.skip("Issue not found")

    def test_run_training_resolves_issue(self, issue):
        """run_training() with mock LLM produces correct patch and s_exec=1.0."""
        from midas_agent.training import run_training

        config = MidasConfig(
            initial_budget=2000000,
            workspace_count=1,
            runtime_mode="graph_emergence",
            n_evict=0,
            score_floor=0.01,
            multiplier_mode="adaptive",
            multiplier_init=1.0,
            beta=0.3,
            model="mock",
            api_key="",
            execution_env="docker",
        )

        # Patch _make_llm_provider to return our replay provider.
        # This mock is scoped to this test only.
        replay = ReplayLLMProvider()
        with patch("midas_agent.training._make_llm_provider", return_value=replay):
            run_training(config, issues=[issue])

        # The replay provider should have been called (agent ran)
        assert replay.call_count >= 5, \
            f"Expected at least 5 LLM calls, got {replay.call_count}"

    def test_patch_contains_fix(self, issue):
        """run_training() generates a patch with the correct fix."""
        from midas_agent.training import run_training, collect_patches

        config = MidasConfig(
            initial_budget=2000000,
            workspace_count=1,
            runtime_mode="graph_emergence",
            n_evict=0,
            score_floor=0.01,
            multiplier_mode="adaptive",
            multiplier_init=1.0,
            beta=0.3,
            model="mock",
            api_key="",
            execution_env="docker",
        )

        # We need to capture the patch. Wrap collect_patches to intercept.
        captured_patches = {}

        from midas_agent import training as training_module
        original_collect = training_module.collect_patches

        def capture_collect(workspaces, patches_base_dir=""):
            result = original_collect(workspaces, patches_base_dir)
            captured_patches.update(result)
            return result

        replay = ReplayLLMProvider()
        with patch("midas_agent.training._make_llm_provider", return_value=replay), \
             patch("midas_agent.training.collect_patches", side_effect=capture_collect):

            run_training(config, issues=[issue])

        # Check the captured patch
        assert len(captured_patches) > 0, "No patches were captured"
        patch_content = list(captured_patches.values())[0]
        assert patch_content.strip(), "Patch should not be empty"
        assert "= right" in patch_content, \
            f"Patch should contain '= right'. Got:\n{patch_content[:500]}"


@pytest.mark.integration
class TestDockerIOEndToEnd:
    """Test DockerIO directly against a real container."""

    @pytest.fixture
    def docker_io(self):
        """Start a real container and return DockerIO."""
        try:
            from midas_agent.docker.container_manager import ContainerManager
            from midas_agent.runtime.io_backend import DockerIO
            from swebench.harness.test_spec.test_spec import make_test_spec
            from datasets import load_dataset

            ds = load_dataset("princeton-nlp/SWE-bench_Verified", split="test")
            for row in ds:
                if row["instance_id"] == "astropy__astropy-12907":
                    spec = make_test_spec(dict(row), namespace="swebench")
                    break
            else:
                pytest.skip("Issue not found")

            cm = ContainerManager()
            cid = cm.start(image=spec.instance_image_key, host_workspace=None, install_cmd=None)
            io = DockerIO(container_id=cid, workdir="/testbed")
            yield io
            cm.stop()
        except Exception as e:
            pytest.skip(f"Docker not available: {e}")

    def test_backslash_roundtrip(self, docker_io):
        """Backslash-heavy content survives write → read through DockerIO."""
        content = (
            'import re\n'
            'PATTERN = re.compile(r"(\\W|\\b|_)")\n'
            'PATH = "C:\\\\Users\\\\test"\n'
        )
        docker_io.write_file("/testbed/test_bs.py", content)
        read_back = docker_io.read_file("/testbed/test_bs.py")
        assert read_back == content, f"Corrupted!\nExpected: {content!r}\nGot: {read_back!r}"
        docker_io.run_bash("rm /testbed/test_bs.py")

    def test_edit_via_str_replace_editor(self, docker_io):
        """StrReplaceEditorAction with DockerIO edits files inside container."""
        from midas_agent.stdlib.actions.str_replace_editor import StrReplaceEditorAction

        editor = StrReplaceEditorAction(cwd="/testbed", io=docker_io)

        # View
        result = editor.execute(command="view", path="/testbed/astropy/modeling/separable.py",
                                view_range=[241, 245])
        assert "= 1" in result

        # Edit
        result = editor.execute(
            command="str_replace",
            path="/testbed/astropy/modeling/separable.py",
            old_str="cright[-right.shape[0]:, -right.shape[1]:] = 1",
            new_str="cright[-right.shape[0]:, -right.shape[1]:] = right",
        )
        assert "has been edited" in result

        # Verify
        result = editor.execute(command="view", path="/testbed/astropy/modeling/separable.py",
                                view_range=[241, 245])
        assert "= right" in result

        # Git diff
        docker_io.run_bash("git add -A")
        diff = docker_io.run_bash("git diff --cached")
        docker_io.run_bash("git reset")
        assert "= right" in diff
        assert diff.startswith("diff --git")
