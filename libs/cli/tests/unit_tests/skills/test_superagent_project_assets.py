"""Smoke tests for the project-level superagent skills added to code2workspace."""

from pathlib import Path

from code2workspace_cli.skills.load import list_skills


def _repo_root() -> Path:
    path = Path(__file__).resolve()
    for parent in path.parents:
        if (parent / ".code2workspace").exists():
            return parent
    msg = "Could not locate repo root from test path"
    raise RuntimeError(msg)


def test_project_superagent_skills_are_discoverable() -> None:
    project_skills_dir = _repo_root() / ".code2workspace" / "skills"

    skills = list_skills(project_skills_dir=project_skills_dir)
    by_name = {skill["name"]: skill for skill in skills}

    for name in {
        "academic-search",
        "benchmark-workflow-orchestrator",
        "data-governance-ops",
        "epietl-api",
        "respiratory-disease-data-fetcher",
        "respiratory-disease-wide-monitor",
        "virus-variation-query",
    }:
        assert name in by_name
        assert by_name[name]["source"] == "project"
        assert Path(by_name[name]["path"]).exists()
    assert "github2workspace-orchestrator" not in by_name
    assert "paper2workspace-orchestrator" not in by_name
    assert "planning-orchestrator" not in by_name
    assert "deep-research-report" not in by_name
    assert "epidemic-warning-report" not in by_name

def test_shared_helper_directory_is_not_discoverable_as_skill() -> None:
    project_skills_dir = _repo_root() / ".code2workspace" / "skills"

    skills = list_skills(project_skills_dir=project_skills_dir)
    skill_names = {skill["name"] for skill in skills}

    assert "_shared-superagent-helpers" not in skill_names


def test_superagent_scripts_expose_help() -> None:
    scripts = [
        _repo_root() / ".code2workspace" / "skills" / "orchestration" / "benchmark-workflow-orchestrator" / "scripts" / "benchmark_workflow.py",
        _repo_root() / ".code2workspace" / "skills" / "capabilities" / "data-governance-ops" / "scripts" / "governance_ops.py",
    ]

    for script in scripts:
        assert script.exists()


def test_workflow_skills_are_local_only_after_bioos_removal() -> None:
    root = _repo_root()
    benchmark_skill = (
        root / ".code2workspace" / "skills" / "orchestration" / "benchmark-workflow-orchestrator" / "SKILL.md"
    ).read_text(encoding="utf-8")
    benchmark_ui = (
        root / ".code2workspace" / "skills" / "orchestration" / "benchmark-workflow-orchestrator" / "agents" / "openai.yaml"
    ).read_text(encoding="utf-8")
    assert "Bio-OS" not in benchmark_skill
    assert "bioos-operator" not in benchmark_skill
    assert "bioos_ops.py" not in benchmark_skill
    assert "Bio-OS" not in benchmark_ui
    assert "bioos-operator" not in benchmark_ui
