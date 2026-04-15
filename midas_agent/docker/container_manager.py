"""Container manager — SWE-bench Docker container lifecycle.

Manages pulling images, starting containers with volume mounts, installing
the repo, and cleanup. Used only when ``execution_env="docker"`` in config.
"""
from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger(__name__)


class ContainerManager:
    """Manages a SWE-bench Docker container for agent workspace execution.

    Lifecycle:
      1. ``start(image, host_workspace, container_workspace)`` — pull image
         if needed, start container with volume mount, install repo.
      2. Agent uses ``DockerBashAction`` with the returned ``container_id``.
      3. ``stop()`` — stop and remove the container.
    """

    def __init__(self) -> None:
        self._container_id: str | None = None

    @property
    def container_id(self) -> str | None:
        return self._container_id

    def start(
        self,
        image: str,
        host_workspace: str,
        container_workspace: str = "/workspace",
        install_cmd: str | None = "pip install -e .",
    ) -> str:
        """Start a Docker container with the workspace mounted.

        Args:
            image: Docker image name (e.g. ``swebench/sweb.eval.x86_64.astropy...``).
            host_workspace: Local path to the cloned repo.
            container_workspace: Mount point inside the container.
            install_cmd: Command to install the repo inside the container.
                Pass None to skip installation.

        Returns:
            The container ID.
        """
        # Pull image if not present locally
        self._pull_if_needed(image)

        # Start container in background with workspace mounted
        mount = f"{host_workspace}:{container_workspace}"
        result = subprocess.run(
            [
                "docker", "run", "-d",
                "--platform", "linux/amd64",
                "-v", mount,
                "-w", container_workspace,
                image,
                "sleep", "infinity",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to start container from {image}: {result.stderr}"
            )
        self._container_id = result.stdout.strip()[:12]
        logger.info("Started container %s from %s", self._container_id, image)

        # Install the repo inside the container
        if install_cmd:
            self._exec(install_cmd, timeout=300)
            logger.info("Installed repo in container %s", self._container_id)

        return self._container_id

    def stop(self) -> None:
        """Stop and remove the container."""
        if self._container_id is None:
            return
        try:
            subprocess.run(
                ["docker", "rm", "-f", self._container_id],
                capture_output=True,
                timeout=30,
            )
            logger.info("Removed container %s", self._container_id)
        except Exception as e:
            logger.warning("Failed to remove container %s: %s", self._container_id, e)
        finally:
            self._container_id = None

    def _pull_if_needed(self, image: str) -> None:
        """Pull Docker image if not available locally."""
        check = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            timeout=10,
        )
        if check.returncode != 0:
            logger.info("Pulling image %s ...", image)
            pull = subprocess.run(
                ["docker", "pull", "--platform", "linux/amd64", image],
                capture_output=True,
                text=True,
                timeout=600,
            )
            if pull.returncode != 0:
                raise RuntimeError(f"Failed to pull {image}: {pull.stderr}")
            logger.info("Pulled image %s", image)

    def _exec(self, cmd: str, timeout: int = 120) -> str:
        """Execute a command inside the running container."""
        if self._container_id is None:
            raise RuntimeError("Container not started")
        result = subprocess.run(
            [
                "docker", "exec",
                self._container_id,
                "bash", "-c", cmd,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            logger.warning("Container exec failed: %s", result.stderr[:500])
        return result.stdout
