"""Shared LangGraph server management for the web frontend."""

from __future__ import annotations

from pathlib import Path

from code2workspace_cli.remote_client import RemoteAgent
from code2workspace_cli.server import ServerProcess
from code2workspace_cli.server_manager import start_server_and_get_agent


class SharedLangGraphService:
    """Own one long-lived local LangGraph server for web chat clients."""

    def __init__(
        self,
        *,
        assistant_id: str = "agent",
        cwd: str | Path | None = None,
    ) -> None:
        self._assistant_id = assistant_id
        self._cwd = Path(cwd).expanduser().resolve() if cwd is not None else Path.cwd()
        self._agent: RemoteAgent | None = None
        self._server_proc: ServerProcess | None = None

    @property
    def base_url(self) -> str:
        """Return the active upstream LangGraph base URL."""
        if self._server_proc is None:
            msg = "shared LangGraph server is not running"
            raise RuntimeError(msg)
        return self._server_proc.url

    async def start(self) -> None:
        """Start the shared LangGraph server if needed."""
        if self._server_proc is not None and self._server_proc.running:
            return
        agent, server_proc, _ = await start_server_and_get_agent(
            assistant_id=self._assistant_id,
            auto_approve=True,
            interactive=True,
            no_mcp=True,
            cwd=self._cwd,
        )
        self._agent = agent
        self._server_proc = server_proc

    async def stop(self) -> None:
        """Stop the shared LangGraph server."""
        if self._server_proc is not None:
            self._server_proc.stop()
            self._server_proc = None
        self._agent = None
