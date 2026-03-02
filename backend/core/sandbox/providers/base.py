"""
Abstract base for sandbox providers.

Defines the interface that all sandbox providers (Daytona, Docker, etc.)
must implement, allowing the rest of the system to be provider-agnostic.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Any


class SandboxState(str, Enum):
    STARTED = "started"
    STOPPED = "stopped"
    ARCHIVED = "archived"
    ARCHIVING = "archiving"
    UNKNOWN = "unknown"


@dataclass
class CommandResult:
    cmd_id: str
    exit_code: int


@dataclass
class CommandLogs:
    output: str


@dataclass
class FileInfo:
    name: str
    is_dir: bool
    size: int
    mod_time: str


@dataclass
class PreviewLink:
    url: str
    token: Optional[str] = None


class SandboxProvider(ABC):
    """
    Abstract base class for sandbox lifecycle management.

    Implement this to add a new sandbox backend (Docker, E2B, Coder, etc.).
    The concrete sandbox object returned by create()/get() must expose:
        - .id -> str
        - .state -> SandboxState
        - .process  (see AbstractSandboxProcess below)
        - .fs       (see AbstractSandboxFS below)
        - await .get_preview_link(port) -> PreviewLink
    """

    @abstractmethod
    async def create(self, password: str, project_id: Optional[str] = None) -> Any:
        """Create a new sandbox. Returns the sandbox object."""
        ...

    @abstractmethod
    async def get(self, sandbox_id: str) -> Any:
        """Retrieve a sandbox by ID."""
        ...

    @abstractmethod
    async def start(self, sandbox: Any) -> None:
        """Start a stopped/archived sandbox."""
        ...

    @abstractmethod
    async def delete(self, sandbox: Any) -> None:
        """Permanently delete a sandbox."""
        ...

    @abstractmethod
    async def get_state(self, sandbox: Any) -> SandboxState:
        """Return the current state of the sandbox."""
        ...

    async def get_sandbox_id(self, sandbox: Any) -> str:
        """Return the ID of the sandbox. Default reads sandbox.id."""
        return sandbox.id

    async def get_preview_url(self, sandbox: Any, port: int) -> PreviewLink:
        """Return the preview URL for a given port. Default delegates to sandbox."""
        return await sandbox.get_preview_link(port)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_provider_instance: Optional[SandboxProvider] = None


def get_provider() -> SandboxProvider:
    """
    Return the configured sandbox provider singleton.

    Controlled by the SANDBOX_PROVIDER environment variable:
        - "daytona"  (default) – uses Daytona cloud sandboxes
        - "docker"             – uses local Docker containers
    """
    global _provider_instance
    if _provider_instance is not None:
        return _provider_instance

    from core.utils.config import config
    provider_name = (getattr(config, "SANDBOX_PROVIDER", None) or "daytona").lower()

    if provider_name == "docker":
        from core.sandbox.providers.docker_provider import DockerProvider
        _provider_instance = DockerProvider()
    else:
        from core.sandbox.providers.daytona_provider import DaytonaProvider
        _provider_instance = DaytonaProvider()

    return _provider_instance


def reset_provider() -> None:
    """Reset the provider singleton (useful for tests)."""
    global _provider_instance
    _provider_instance = None
