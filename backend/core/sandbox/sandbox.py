"""
Sandbox lifecycle functions.

All sandbox operations go through the configured SandboxProvider
(Daytona by default, Docker for self-hosted deployments).

Set SANDBOX_PROVIDER=docker in your .env to use local Docker instead.
"""
import asyncio
from typing import Any, Optional

from dotenv import load_dotenv

from core.utils.logger import logger
from core.utils.config import Configuration
from core.sandbox.providers.base import SandboxState, get_provider

load_dotenv()


# ---------------------------------------------------------------------------
# Provider singleton helpers
# ---------------------------------------------------------------------------

def _provider():
    """Lazy accessor so the provider is only instantiated on first use."""
    return get_provider()


# ---------------------------------------------------------------------------
# Public API (same signatures as before – fully backward-compatible)
# ---------------------------------------------------------------------------

async def get_or_start_sandbox(sandbox_id: str) -> Any:
    """
    Retrieve a sandbox by ID.  If it is stopped/archived, start it first.
    """
    provider = _provider()
    logger.info(f"[SANDBOX] Getting sandbox: {sandbox_id}")

    sandbox = await provider.get(sandbox_id)
    state = await provider.get_state(sandbox)

    if state in (SandboxState.ARCHIVED, SandboxState.STOPPED, SandboxState.ARCHIVING):
        logger.info(f"[SANDBOX] Sandbox {sandbox_id} is {state.value} – starting…")
        try:
            await provider.start(sandbox)

            # Wait up to 30 s for STARTED state
            for _ in range(30):
                await asyncio.sleep(1)
                sandbox = await provider.get(sandbox_id)
                state = await provider.get_state(sandbox)
                if state == SandboxState.STARTED:
                    break

            await start_supervisord_session(sandbox)
        except Exception as exc:
            logger.error(f"[SANDBOX] Failed to start {sandbox_id}: {exc}")
            raise

    logger.info(f"[SANDBOX] Sandbox {sandbox_id} is ready")
    return sandbox


async def get_sandbox(sandbox_id: str) -> Any:
    """
    Retrieve a sandbox by ID without auto-starting.
    Useful when you need to inspect state without side effects.
    """
    return await _provider().get(sandbox_id)


async def get_sandbox_state(sandbox: Any) -> SandboxState:
    """Return the abstract state of any sandbox object."""
    return await _provider().get_state(sandbox)


async def start_supervisord_session(sandbox: Any) -> None:
    """
    Ensure supervisord is running inside the sandbox.

    For Daytona sandboxes this is necessary after a restart.
    For Docker, supervisord is already the container entrypoint – the call
    is a no-op that may fail silently (which is expected).
    """
    session_id = "supervisord-session"
    try:
        await sandbox.process.create_session(session_id)

        # Use a duck-typed request that works with both Daytona SDK and Docker
        class _Req:
            command = (
                "exec /usr/bin/supervisord -n -c "
                "/etc/supervisor/conf.d/supervisord.conf"
            )
            var_async = True
            cwd = None

        await sandbox.process.execute_session_command(session_id, _Req())
        logger.info("[SANDBOX] Supervisord session started")
    except Exception as exc:
        # Not fatal – supervisord may already be running
        logger.warning(f"[SANDBOX] Could not start supervisord session: {exc}")


async def create_sandbox(password: str, project_id: Optional[str] = None) -> Any:
    """Create a new sandbox with all required services configured."""
    provider = _provider()
    logger.info("[SANDBOX] Creating new sandbox environment")

    sandbox = await provider.create(password, project_id)
    logger.info(f"[SANDBOX] Sandbox created: {sandbox.id}")

    await start_supervisord_session(sandbox)
    logger.info("[SANDBOX] Sandbox environment initialised")
    return sandbox


async def delete_sandbox(sandbox_id: str) -> bool:
    """Permanently delete a sandbox by ID."""
    provider = _provider()
    logger.info(f"[SANDBOX] Deleting sandbox: {sandbox_id}")
    try:
        sandbox = await provider.get(sandbox_id)
        await provider.delete(sandbox)
        logger.info(f"[SANDBOX] Deleted sandbox: {sandbox_id}")
        return True
    except Exception as exc:
        logger.error(f"[SANDBOX] Error deleting {sandbox_id}: {exc}")
        raise


async def ping_sandbox(sandbox_id: str) -> bool:
    """
    Send a keepalive command to a running sandbox.
    Returns True on success, False if the sandbox is not running.
    """
    provider = _provider()
    try:
        sandbox = await provider.get(sandbox_id)
        state = await provider.get_state(sandbox)

        if state != SandboxState.STARTED:
            logger.debug(f"[SANDBOX] Ping skipped – {sandbox_id} is {state.value}")
            return False

        session_id = f"keepalive_{sandbox_id[:8]}"
        await sandbox.process.create_session(session_id)

        class _Req:
            command = "echo keepalive"
            var_async = False
            cwd = None

        await sandbox.process.execute_session_command(session_id, _Req())
        return True
    except Exception as exc:
        logger.warning(f"[SANDBOX] Ping failed for {sandbox_id}: {exc}")
        return False
