"""
Daytona sandbox provider.

Thin wrapper around the existing Daytona SDK that adapts it to the
SandboxProvider interface, preserving all current behaviour.
"""
from typing import Optional

from daytona_sdk import (
    AsyncDaytona,
    DaytonaConfig,
    CreateSandboxFromSnapshotParams,
    AsyncSandbox,
    SandboxState as DaytonaSandboxState,
)

from core.utils.config import config, Configuration
from core.utils.logger import logger
from .base import SandboxProvider, SandboxState, PreviewLink


# Map Daytona state strings to our abstract enum
_DAYTONA_STATE_MAP = {
    "started": SandboxState.STARTED,
    "stopped": SandboxState.STOPPED,
    "archived": SandboxState.ARCHIVED,
    "archiving": SandboxState.ARCHIVING,
}


def _normalize_state(daytona_state) -> SandboxState:
    """Convert a Daytona SandboxState to our abstract SandboxState."""
    raw = str(daytona_state).lower()
    # Handles both "started" and "SandboxState.started" formats
    key = raw.split(".")[-1]
    return _DAYTONA_STATE_MAP.get(key, SandboxState.UNKNOWN)


class DaytonaProvider(SandboxProvider):
    """Sandbox provider backed by Daytona cloud (https://app.daytona.io)."""

    def __init__(self):
        daytona_cfg = DaytonaConfig(
            api_key=config.DAYTONA_API_KEY,
            api_url=config.DAYTONA_SERVER_URL,
            target=config.DAYTONA_TARGET,
        )
        self._client = AsyncDaytona(daytona_cfg)

        if daytona_cfg.api_key:
            logger.debug("[DAYTONA_PROVIDER] Configured successfully")
        else:
            logger.warning("[DAYTONA_PROVIDER] No API key – sandbox calls will fail")

    # ------------------------------------------------------------------
    # SandboxProvider interface
    # ------------------------------------------------------------------

    async def create(self, password: str, project_id: Optional[str] = None) -> AsyncSandbox:
        labels = {"id": project_id} if project_id else None
        params = CreateSandboxFromSnapshotParams(
            snapshot=Configuration.SANDBOX_SNAPSHOT_NAME,
            public=True,
            labels=labels,
            env_vars={
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
            },
            auto_stop_interval=15,
            auto_archive_interval=30,
        )
        sandbox = await self._client.create(params)
        logger.info(f"[DAYTONA_PROVIDER] Created sandbox: {sandbox.id}")
        return sandbox

    async def get(self, sandbox_id: str) -> AsyncSandbox:
        return await self._client.get(sandbox_id)

    async def start(self, sandbox: AsyncSandbox) -> None:
        await self._client.start(sandbox)

    async def delete(self, sandbox: AsyncSandbox) -> None:
        await self._client.delete(sandbox)

    async def get_state(self, sandbox: AsyncSandbox) -> SandboxState:
        return _normalize_state(sandbox.state)

    async def get_preview_url(self, sandbox: AsyncSandbox, port: int) -> PreviewLink:
        link = await sandbox.get_preview_link(port)
        url = link.url if hasattr(link, "url") else str(link).split("url='")[1].split("'")[0]
        token = link.token if hasattr(link, "token") else None
        return PreviewLink(url=url, token=token)

    # ------------------------------------------------------------------
    # Daytona-specific helpers (used internally / by pool_service)
    # ------------------------------------------------------------------

    def get_raw_client(self) -> AsyncDaytona:
        """Return the underlying AsyncDaytona client."""
        return self._client

    def is_sandbox_started(self, sandbox: AsyncSandbox) -> bool:
        """Quick check using the native Daytona enum (avoids extra async call)."""
        return sandbox.state == DaytonaSandboxState.STARTED
