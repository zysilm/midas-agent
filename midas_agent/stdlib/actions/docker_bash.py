"""Docker-backed bash action — runs commands inside a SWE-bench container.

Subclasses BashAction so all existing code that expects BashAction
continues to work. Only execute() is overridden to route through
``docker exec`` instead of local subprocess.
"""
import subprocess

from midas_agent.stdlib.actions.bash import BashAction


class DockerBashAction(BashAction):
    """BashAction that executes commands inside a running Docker container.

    The container is expected to have the repository installed (pip install
    -e) so that ``import <package>`` works. File operations (read/edit/write)
    still happen on the host via a volume mount — only bash commands need
    the container's runtime environment.
    """

    def __init__(
        self,
        container_id: str,
        cwd: str | None = None,
        workdir: str = "/workspace",
    ) -> None:
        super().__init__(cwd=cwd)
        self._container_id = container_id
        self._workdir = workdir

    def execute(self, **kwargs) -> str:
        command = kwargs["command"]
        timeout = kwargs.get("timeout", 120)
        try:
            result = subprocess.run(
                [
                    "docker", "exec",
                    "-w", self._workdir,
                    self._container_id,
                    "bash", "-c", command,
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = result.stdout
            if result.returncode != 0 and result.stderr:
                output += result.stderr
            return output
        except subprocess.TimeoutExpired:
            return f"Command timed out after {timeout} seconds."
        except Exception as e:
            return f"Error executing command in container: {e}"
