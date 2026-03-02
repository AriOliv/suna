"""
Docker sandbox provider – 100% open-source, zero external dependency.

Uses the local Docker daemon to run containers from the same
kortix/suna image that Daytona uses, giving identical tool behaviour
without any cloud account or API key.

Requirements:
    pip install docker
    docker pull kortix/suna:0.1.3.30  (or configure SANDBOX_IMAGE_NAME)

Environment variables (all optional):
    SANDBOX_PROVIDER=docker          – activates this provider
    DOCKER_HOST                      – custom Docker socket (default: unix:///var/run/docker.sock)
    DOCKER_SANDBOX_NETWORK           – attach containers to this Docker network
    DOCKER_SANDBOX_BASE_URL          – base URL to expose containers (default: http://localhost)
"""
from __future__ import annotations

import asyncio
import io
import tarfile
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from core.utils.logger import logger
from core.utils.config import Configuration
from .base import (
    SandboxProvider,
    SandboxState,
    PreviewLink,
    CommandResult,
    CommandLogs,
    FileInfo,
)

try:
    import docker
    import docker.errors
    _DOCKER_AVAILABLE = True
except ImportError:
    _DOCKER_AVAILABLE = False
    docker = None  # type: ignore


# ---------------------------------------------------------------------------
# Internal response types (mirror Daytona SDK shapes expected by tools)
# ---------------------------------------------------------------------------

class _CommandResponse:
    __slots__ = ("cmd_id", "exit_code")

    def __init__(self, cmd_id: str, exit_code: int):
        self.cmd_id = cmd_id
        self.exit_code = exit_code


class _CommandLogs:
    __slots__ = ("output",)

    def __init__(self, output: str):
        self.output = output


class _ExecResult:
    __slots__ = ("exit_code", "stderr")

    def __init__(self, exit_code: int, stderr: str = ""):
        self.exit_code = exit_code
        self.stderr = stderr


class _PreviewLink:
    __slots__ = ("url", "token")

    def __init__(self, url: str, token: Optional[str] = None):
        self.url = url
        self.token = token


# ---------------------------------------------------------------------------
# Process layer
# ---------------------------------------------------------------------------

class DockerProcess:
    """
    Implements the same interface as Daytona's AsyncSandbox.process.

    PTY sessions are not supported – calling create_pty_session() raises
    NotImplementedError which causes SandboxShellTool to fall back to the
    _fallback_execute() path automatically.
    """

    def __init__(self, container):
        self._container = container
        # session_id -> {cmd_id -> output}
        self._sessions: Dict[str, Dict[str, str]] = {}

    # -- Session management ------------------------------------------------

    async def create_session(self, session_id: str) -> None:
        self._sessions[session_id] = {}

    async def delete_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    # -- Command execution -------------------------------------------------

    async def execute_session_command(
        self, session_id: str, req: Any, timeout: int = 300
    ) -> _CommandResponse:
        cmd_id = uuid.uuid4().hex[:8]
        command = getattr(req, "command", str(req))
        cwd = getattr(req, "cwd", "/workspace") or "/workspace"
        full_cmd = f"cd {cwd} && {command}"

        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._container.exec_run(
                    ["bash", "-c", full_cmd],
                    demux=False,
                ),
            )
            exit_code = result.exit_code if result.exit_code is not None else 0
            output = result.output.decode("utf-8", errors="replace") if result.output else ""
        except Exception as exc:
            exit_code = 1
            output = str(exc)

        if session_id in self._sessions:
            self._sessions[session_id][cmd_id] = output

        return _CommandResponse(cmd_id=cmd_id, exit_code=exit_code)

    async def get_session_command_logs(
        self, session_id: str, command_id: str
    ) -> _CommandLogs:
        output = ""
        if session_id in self._sessions:
            output = self._sessions[session_id].get(command_id, "")
        return _CommandLogs(output=output)

    # -- PTY (not supported – triggers fallback path in tools) ------------

    async def create_pty_session(self, id: str, on_data: Any = None, pty_size: Any = None):
        raise NotImplementedError(
            "Docker provider does not support PTY sessions; "
            "tool will fall back to direct session execution automatically."
        )

    # -- Convenience exec (used by sb_files_tool delete fallback) ---------

    async def exec(self, *args: str) -> _ExecResult:
        cmd = list(args)
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._container.exec_run(cmd, demux=False),
            )
            return _ExecResult(
                exit_code=result.exit_code if result.exit_code is not None else 0,
                stderr=result.output.decode("utf-8", errors="replace") if result.output else "",
            )
        except Exception as exc:
            return _ExecResult(exit_code=1, stderr=str(exc))

    async def start(self, command: str) -> _ExecResult:
        return await self.exec("bash", "-c", command)


# ---------------------------------------------------------------------------
# Filesystem layer
# ---------------------------------------------------------------------------

class DockerFS:
    """Implements the same interface as Daytona's AsyncSandbox.fs."""

    def __init__(self, container):
        self._container = container

    # -- Helpers -----------------------------------------------------------

    @staticmethod
    def _pack_tarball(content: bytes, filename: str) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            info = tarfile.TarInfo(name=filename)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
        return buf.getvalue()

    # -- File upload / download -------------------------------------------

    async def upload_file(self, content: bytes, path: str) -> None:
        parts = path.rsplit("/", 1)
        directory = parts[0] if len(parts) > 1 else "/"
        filename = parts[1] if len(parts) > 1 else parts[0]
        tarball = self._pack_tarball(content, filename)

        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self._container.put_archive(directory, tarball),
        )

    async def download_file(self, path: str) -> bytes:
        def _get() -> bytes:
            stream, _ = self._container.get_archive(path)
            data = b"".join(stream)
            buf = io.BytesIO(data)
            with tarfile.open(fileobj=buf) as tf:
                members = tf.getmembers()
                if not members:
                    return b""
                member = members[0]
                f = tf.extractfile(member)
                return f.read() if f else b""

        return await asyncio.get_event_loop().run_in_executor(None, _get)

    # -- File metadata / listing ------------------------------------------

    async def get_file_info(self, path: str) -> FileInfo:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self._container.exec_run(
                ["stat", "-c", "%n|%F|%s|%Y", path],
                demux=False,
            ),
        )
        if result.exit_code != 0:
            raise FileNotFoundError(f"File not found: {path}")

        output = result.output.decode("utf-8", errors="replace").strip()
        parts = output.split("|")
        name = (parts[0].split("/")[-1]) if parts else path
        is_dir = "directory" in parts[1].lower() if len(parts) > 1 else False
        size = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        mod_time = parts[3] if len(parts) > 3 else ""
        return FileInfo(name=name, is_dir=is_dir, size=size, mod_time=mod_time)

    async def list_files(self, path: str) -> List[FileInfo]:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self._container.exec_run(
                [
                    "bash",
                    "-c",
                    f"find {path} -maxdepth 1 -mindepth 1 -printf '%f|%y|%s|%T@\\n' 2>/dev/null",
                ],
                demux=False,
            ),
        )
        if result.exit_code != 0 or not result.output:
            return []

        files: List[FileInfo] = []
        for line in result.output.decode("utf-8", errors="replace").strip().splitlines():
            if not line:
                continue
            parts = line.split("|")
            if len(parts) < 2:
                continue
            name = parts[0]
            is_dir = parts[1] == "d"
            size = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
            mod_time = parts[3] if len(parts) > 3 else ""
            files.append(FileInfo(name=name, is_dir=is_dir, size=size, mod_time=mod_time))
        return files

    # -- Permissions / directory / deletion --------------------------------

    async def set_file_permissions(self, path: str, permissions: str) -> None:
        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self._container.exec_run(["chmod", permissions, path]),
        )

    async def create_folder(self, path: str, permissions: str = "755") -> None:
        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self._container.exec_run(["mkdir", "-p", path]),
        )
        if permissions:
            await self.set_file_permissions(path, permissions)

    async def delete_file(self, path: str) -> None:
        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self._container.exec_run(["rm", "-f", path]),
        )


# ---------------------------------------------------------------------------
# Sandbox wrapper
# ---------------------------------------------------------------------------

class DockerSandbox:
    """
    A Docker container wrapped to expose the same interface as
    Daytona's AsyncSandbox so that all existing tool code works unchanged.
    """

    def __init__(self, container, host_ports: Dict[int, int], base_url: str = "http://localhost"):
        self._container = container
        self._host_ports = host_ports  # {container_port: host_port}
        self._base_url = base_url
        self.process = DockerProcess(container)
        self.fs = DockerFS(container)

    @property
    def id(self) -> str:
        return self._container.id[:12]

    @property
    def state(self) -> SandboxState:
        try:
            self._container.reload()
            status = self._container.status
        except Exception:
            return SandboxState.UNKNOWN

        if status == "running":
            return SandboxState.STARTED
        if status in ("exited", "dead"):
            return SandboxState.STOPPED
        if status == "paused":
            return SandboxState.ARCHIVED
        return SandboxState.UNKNOWN

    async def get_preview_link(self, port: int) -> _PreviewLink:
        host_port = self._host_ports.get(port)
        url = f"{self._base_url}:{host_port}" if host_port else f"{self._base_url}:{port}"
        return _PreviewLink(url=url, token=None)


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class DockerProvider(SandboxProvider):
    """
    Sandbox provider backed by local Docker.

    Each sandbox is an isolated Docker container running the same
    kortix/suna image that Daytona uses.  No cloud account needed.
    """

    def __init__(self):
        if not _DOCKER_AVAILABLE:
            raise ImportError(
                "The 'docker' package is required for SANDBOX_PROVIDER=docker. "
                "Install it with: pip install docker"
            )
        import os
        self._client = docker.from_env()
        self._base_url = os.getenv("DOCKER_SANDBOX_BASE_URL", "http://localhost")
        self._network = os.getenv("DOCKER_SANDBOX_NETWORK")
        # In-memory registry so get() can resolve short IDs
        self._registry: Dict[str, DockerSandbox] = {}
        logger.info("[DOCKER_PROVIDER] Initialized (local Docker daemon)")

    # ------------------------------------------------------------------
    # SandboxProvider interface
    # ------------------------------------------------------------------

    async def create(self, password: str, project_id: Optional[str] = None) -> DockerSandbox:
        suffix = uuid.uuid4().hex[:4]
        prefix = project_id[:8] if project_id else "suna"
        container_name = f"suna-{prefix}-{suffix}"

        env = {
            "CHROME_PERSISTENT_SESSION": "true",
            "RESOLUTION": "1048x768x24",
            "RESOLUTION_WIDTH": "1048",
            "RESOLUTION_HEIGHT": "768",
            "VNC_PASSWORD": password,
            "ANONYMIZED_TELEMETRY": "false",
            "CHROME_PATH": "",
            "CHROME_USER_DATA": "",
            "CHROME_DEBUGGING_PORT": "9222",
            "CHROME_DEBUGGING_HOST": "localhost",
            "CHROME_CDP": "",
        }

        labels = {
            "suna.managed": "true",
            "suna.project_id": project_id or "",
        }

        run_kwargs: dict = dict(
            image=Configuration.SANDBOX_IMAGE_NAME,
            command=Configuration.SANDBOX_ENTRYPOINT,
            name=container_name,
            detach=True,
            ports={"6080/tcp": None, "8080/tcp": None},
            environment=env,
            labels=labels,
            shm_size="512m",
        )
        if self._network:
            run_kwargs["network"] = self._network

        logger.info(f"[DOCKER_PROVIDER] Creating container: {container_name}")
        try:
            container = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._client.containers.run(**run_kwargs),
            )
        except docker.errors.ImageNotFound:
            msg = (
                f"[DOCKER_PROVIDER] Image {Configuration.SANDBOX_IMAGE_NAME} not found. "
                f"Pull it with: docker pull {Configuration.SANDBOX_IMAGE_NAME}"
            )
            logger.error(msg)
            raise RuntimeError(msg)

        # Reload to get assigned host ports
        await asyncio.get_event_loop().run_in_executor(None, container.reload)
        host_ports = self._extract_host_ports(container)

        sandbox = DockerSandbox(container, host_ports, self._base_url)
        self._registry[sandbox.id] = sandbox

        logger.info(
            f"[DOCKER_PROVIDER] Container ready: {sandbox.id}  ports={host_ports}"
        )
        # Give supervisord a moment to start all services
        await asyncio.sleep(3)
        return sandbox

    async def get(self, sandbox_id: str) -> DockerSandbox:
        if sandbox_id in self._registry:
            return self._registry[sandbox_id]

        try:
            container = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._client.containers.get(sandbox_id),
            )
        except docker.errors.NotFound:
            raise ValueError(f"[DOCKER_PROVIDER] Container not found: {sandbox_id}")

        await asyncio.get_event_loop().run_in_executor(None, container.reload)
        host_ports = self._extract_host_ports(container)
        sandbox = DockerSandbox(container, host_ports, self._base_url)
        self._registry[sandbox_id] = sandbox
        return sandbox

    async def start(self, sandbox: DockerSandbox) -> None:
        await asyncio.get_event_loop().run_in_executor(
            None, sandbox._container.start
        )
        await asyncio.sleep(2)

    async def delete(self, sandbox: DockerSandbox) -> None:
        sid = sandbox.id
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: sandbox._container.remove(force=True),
            )
            self._registry.pop(sid, None)
            logger.info(f"[DOCKER_PROVIDER] Removed container: {sid}")
        except Exception as exc:
            logger.warning(f"[DOCKER_PROVIDER] Error removing {sid}: {exc}")

    async def get_state(self, sandbox: DockerSandbox) -> SandboxState:
        return sandbox.state  # DockerSandbox.state already returns SandboxState

    async def get_preview_url(self, sandbox: DockerSandbox, port: int) -> PreviewLink:
        link = await sandbox.get_preview_link(port)
        return PreviewLink(url=link.url, token=link.token)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_host_ports(container) -> Dict[int, int]:
        host_ports: Dict[int, int] = {}
        for binding, mappings in (container.ports or {}).items():
            if mappings:
                container_port = int(binding.split("/")[0])
                host_ports[container_port] = int(mappings[0]["HostPort"])
        return host_ports
