"""Helpers for per-session working directory selection."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from code2workspace_cli.project_utils import find_project_root

if TYPE_CHECKING:
    from collections.abc import Callable

SessionWorkdirMode = Literal["isolated", "inherit"]

DEFAULT_SESSION_WORKDIR_MODE: SessionWorkdirMode = "isolated"
SESSION_WORKDIR_ROOT_NAME = "workspace"


def session_timestamp() -> str:
    """Return a local timestamp suitable for workspace directory names."""
    return datetime.now().strftime("%Y%m%d%H%M%S")


def resolve_default_session_invocation_cwd(current_cwd: str | Path) -> Path:
    """Resolve the cwd used for default session workspace creation.

    Normally this is just the process cwd. If the process happens to start in
    the user's home directory, prefer the current repo checkout root when the
    CLI itself is running from a checked-out project tree. This avoids sending
    new sessions to `~/workspace/...` when the active project context is the
    local repo/worktree.
    """
    base = Path(current_cwd).expanduser().resolve()
    if base != Path.home():
        return base
    repo_root = find_project_root(Path(__file__).resolve())
    return repo_root or base


def prepare_session_cwd(
    invocation_cwd: str | Path,
    *,
    mode: SessionWorkdirMode = DEFAULT_SESSION_WORKDIR_MODE,
    timestamp_factory: Callable[[], str] | None = None,
) -> Path:
    """Resolve the working directory for a new session.

    In isolated mode, creates `<project_root>/workspace/<timestamp>` when the
    invocation is inside a project, falling back to
    `<invocation_cwd>/workspace/<timestamp>` outside projects.
    In inherit mode, returns `invocation_cwd` unchanged.
    """
    base = Path(invocation_cwd).expanduser().resolve()
    if mode == "inherit":
        return base

    session_base = find_project_root(base) or base
    workspace_root = session_base / SESSION_WORKDIR_ROOT_NAME
    workspace_root.mkdir(parents=True, exist_ok=True)
    make_timestamp = timestamp_factory or session_timestamp

    while True:
        candidate = workspace_root / make_timestamp()
        try:
            candidate.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            continue
        return candidate.resolve()
