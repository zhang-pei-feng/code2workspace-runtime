from pathlib import Path

from code2workspace_cli.session_workspace import (
    prepare_session_cwd,
    resolve_default_session_invocation_cwd,
)


def test_prepare_session_cwd_returns_invocation_dir_in_inherit_mode(tmp_path: Path) -> None:
    resolved = prepare_session_cwd(tmp_path, mode="inherit")
    assert resolved == tmp_path.resolve()


def test_prepare_session_cwd_creates_timestamped_workspace_dir(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "code2workspace_cli.session_workspace.find_project_root",
        lambda _path=None: None,
    )
    resolved = prepare_session_cwd(
        tmp_path,
        mode="isolated",
        timestamp_factory=lambda: "20260421010203",
    )

    assert resolved == (tmp_path / "workspace" / "20260421010203").resolve()
    assert resolved.is_dir()


def test_prepare_session_cwd_uses_project_root_workspace_from_subdir(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    invocation_dir = project_root / "nested" / "pkg"
    invocation_dir.mkdir(parents=True)
    (project_root / ".git").mkdir()

    resolved = prepare_session_cwd(
        invocation_dir,
        mode="isolated",
        timestamp_factory=lambda: "20260421010203",
    )

    assert resolved == (project_root / "workspace" / "20260421010203").resolve()
    assert resolved.is_dir()


def test_prepare_session_cwd_retries_when_timestamp_already_exists(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "code2workspace_cli.session_workspace.find_project_root",
        lambda _path=None: None,
    )
    existing = tmp_path / "workspace" / "20260421010203"
    existing.mkdir(parents=True)
    timestamps = iter(["20260421010203", "20260421010204"])

    resolved = prepare_session_cwd(
        tmp_path,
        mode="isolated",
        timestamp_factory=lambda: next(timestamps),
    )

    assert resolved == (tmp_path / "workspace" / "20260421010204").resolve()
    assert resolved.is_dir()


def test_resolve_default_session_invocation_cwd_keeps_non_home_path(
    tmp_path: Path,
) -> None:
    assert resolve_default_session_invocation_cwd(tmp_path) == tmp_path.resolve()


def test_resolve_default_session_invocation_cwd_uses_repo_root_for_home(
    monkeypatch,
) -> None:
    home = Path.home().resolve()
    repo_root = home / "repo-root"
    monkeypatch.setattr(
        "code2workspace_cli.session_workspace.find_project_root",
        lambda path=None: repo_root,
    )

    assert resolve_default_session_invocation_cwd(home) == repo_root
