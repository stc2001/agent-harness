"""Docker sandbox backend — optional alternative to Bubblewrap.

Requires docker SDK. Register via:
    from harness.sandbox.docker_impl import DockerSandbox
    from harness.sandbox.backends import register_backend
    register_backend("docker", DockerSandbox)
"""

from __future__ import annotations

import docker
from docker.errors import DockerException, NotFound

from harness.interfaces import SandboxConfig, SandboxSpawnError


class DockerSandbox:
    """Docker-based sandbox implementation.

    Implements the SandboxInterface Protocol defined in harness.interfaces.
    """

    def __init__(self) -> None:
        self._client: docker.DockerClient | None = None
        self._by_uuid: dict[str, str] = {}  # I-26: sandbox_id(uuid) → container id

    def _get_client(self) -> docker.DockerClient:
        if self._client is None:
            try:
                self._client = docker.from_env()
            except DockerException as exc:
                raise SandboxSpawnError(
                    f"Docker daemon is not available: {exc}"
                ) from exc
        return self._client

    def spawn(self, config: SandboxConfig,
              sandbox_id: str | None = None) -> str:
        client = self._get_client()
        env = dict(config.env_vars)
        try:
            container = client.containers.run(
                image=config.image or "python:3.11-slim",
                detach=True,
                environment=env,
                network=config.network if config.network != "none" else None,
            )
        except DockerException as exc:
            raise SandboxSpawnError(f"Failed to spawn: {exc}") from exc
        if sandbox_id is not None:
            self._by_uuid[sandbox_id] = container.id
        return container.id

    def stop(self, sandbox_id: str, timeout: int = 5) -> None:
        """Gracefully stop a Docker container."""
        container_id = self._by_uuid.get(sandbox_id, sandbox_id)
        client = self._get_client()
        try:
            container = client.containers.get(container_id)
            container.stop(timeout=timeout)
            container.remove(force=True)
        except NotFound:
            return
        except DockerException as exc:
            raise SandboxSpawnError(f"Failed to stop {container_id}: {exc}") from exc

    def kill(self, sandbox_id: str, keep_workspace: bool = False) -> None:
        container_id = self._by_uuid.get(sandbox_id, sandbox_id)
        # keep_workspace is accepted for SandboxInterface parity; Docker
        # containers are ephemeral by design, so workspace is never kept.
        client = self._get_client()
        try:
            container = client.containers.get(container_id)
            container.stop(timeout=0)
            container.remove(force=True)
        except NotFound:
            self._by_uuid.pop(sandbox_id, None)
            return
        except DockerException as exc:
            raise SandboxSpawnError(f"Failed to kill {container_id}: {exc}") from exc

    def is_alive(self, sandbox_id: str) -> bool:
        container_id = self._by_uuid.get(sandbox_id, sandbox_id)
        client = self._get_client()
        try:
            container = client.containers.get(container_id)
            return container.status == "running"
        except NotFound:
            return False
        except DockerException:
            return False

    def get_logs(self, sandbox_id: str) -> str:
        container_id = self._by_uuid.get(sandbox_id, sandbox_id)
        client = self._get_client()
        try:
            container = client.containers.get(container_id)
            logs = container.logs(stdout=True, stderr=True, timestamps=False)
            return logs.decode("utf-8", errors="replace") if isinstance(logs, bytes) else logs
        except NotFound:
            return ""
        except DockerException:
            return ""

    def execute(
        self, sandbox_id: str, command: str,
        restrict=None,
    ) -> tuple[str, str, int]:
        """Run a command inside the Docker container.

        I-34: exec-stage restrictions are not supported on the docker
        backend yet — requesting one fails closed.
        """
        if restrict is not None:
            from harness.interfaces import SandboxRestrictionError
            raise SandboxRestrictionError(
                "docker backend: exec-stage restrict unsupported (fail-closed)")
        container_id = self._by_uuid.get(sandbox_id, sandbox_id)
        client = self._get_client()
        try:
            container = client.containers.get(container_id)
            exit_code, output = container.exec_run(
                cmd=["/bin/sh", "-c", command],
                stdout=True, stderr=True,
            )
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            return (output, "", exit_code)
        except NotFound:
            return ("", "Container not found", -1)
        except DockerException as exc:
            return ("", str(exc), -1)
