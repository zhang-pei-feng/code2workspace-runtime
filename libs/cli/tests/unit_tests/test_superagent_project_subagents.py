"""Smoke tests for project-level subagent cleanup on this branch."""

from pathlib import Path

from code2workspace_cli.subagents import list_subagents


def _repo_root() -> Path:
    path = Path(__file__).resolve()
    for parent in path.parents:
        if (parent / ".code2workspace").exists():
            return parent
    msg = "Could not locate repo root from test path"
    raise RuntimeError(msg)


def test_current_branch_has_no_checked_in_project_subagents() -> None:
    project_agents_dir = _repo_root() / ".code2workspace" / "agents"

    subagents = list_subagents(project_agents_dir=project_agents_dir)
    assert subagents == []
