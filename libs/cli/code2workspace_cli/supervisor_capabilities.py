"""Capability-to-tool and guidance registry for the supervisor runtime."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from code2workspace.orchestration_runtime import CapabilityBundle

ImplementationKind = Literal["tool", "guidance", "hybrid"]


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    """Execution contract for one capability bundle."""

    name: CapabilityBundle
    implementation_kind: ImplementationKind
    tool_names: tuple[str, ...]
    summary: str


CAPABILITY_REGISTRY: dict[CapabilityBundle, CapabilitySpec] = {
    "repo_fetch": CapabilitySpec(
        name="repo_fetch",
        implementation_kind="hybrid",
        tool_names=("execute", "read_file", "ls", "glob"),
        summary="Repository fetch and source-tree inspection via shell/file tools.",
    ),
    "docker_build_run": CapabilitySpec(
        name="docker_build_run",
        implementation_kind="hybrid",
        tool_names=("execute", "read_file", "write_file", "edit_file", "ls"),
        summary="Container image build, runtime checks, and log capture.",
    ),
    "wdl_run": CapabilitySpec(
        name="wdl_run",
        implementation_kind="hybrid",
        tool_names=("execute", "read_file", "write_file", "edit_file", "ls"),
        summary="WDL/Cromwell preparation, execution, and validation.",
    ),
    "data_filter": CapabilitySpec(
        name="data_filter",
        implementation_kind="hybrid",
        tool_names=("execute", "read_file", "write_file"),
        summary="Data selection, reshaping, and extraction.",
    ),
    "operator_filter": CapabilitySpec(
        name="operator_filter",
        implementation_kind="guidance",
        tool_names=("read_file", "execute"),
        summary="Tool/operator selection based on task constraints.",
    ),
    "metric_compute": CapabilitySpec(
        name="metric_compute",
        implementation_kind="hybrid",
        tool_names=("execute", "read_file", "write_file"),
        summary="Metric calculation and result-table generation.",
    ),
    "summarize": CapabilitySpec(
        name="summarize",
        implementation_kind="guidance",
        tool_names=("read_file", "write_file"),
        summary="User-facing summary, synthesis, and closeout text.",
    ),
    "validate": CapabilitySpec(
        name="validate",
        implementation_kind="guidance",
        tool_names=("read_file", "ls", "execute"),
        summary="Artifact, log, and prerequisite validation.",
    ),
    "plan": CapabilitySpec(
        name="plan",
        implementation_kind="guidance",
        tool_names=("read_file", "write_file"),
        summary="Planning, decomposition, and execution-order decisions.",
    ),
    "task_manage": CapabilitySpec(
        name="task_manage",
        implementation_kind="guidance",
        tool_names=("read_file", "write_file", "ls"),
        summary="Run bookkeeping, artifact organization, and task-state transitions.",
    ),
    "web_search": CapabilitySpec(
        name="web_search",
        implementation_kind="tool",
        tool_names=("web_search",),
        summary="Search the web for targeted sources and evidence.",
    ),
    "web_fetch": CapabilitySpec(
        name="web_fetch",
        implementation_kind="tool",
        tool_names=("fetch_url",),
        summary="Fetch and inspect a known web resource directly.",
    ),
    "db_access": CapabilitySpec(
        name="db_access",
        implementation_kind="hybrid",
        tool_names=("execute", "read_file"),
        summary="Structured local database or dataset access.",
    ),
    "api_call": CapabilitySpec(
        name="api_call",
        implementation_kind="hybrid",
        tool_names=("fetch_url", "execute"),
        summary="Structured external or local API calls.",
    ),
}


_NODE_GUIDANCE_DEFAULTS: dict[str, tuple[str, ...]] = {
    "register": (
        "This node is registration-first: confirm staged assets, enumerate tool/case pairs, and separate light missing assets from hard blockers.",
        "If lightweight planning artifacts are missing, describe the minimum artifacts required before execution and record them explicitly.",
    ),
    "inspect": (
        "If the task names a repository URL and the source tree is absent, materialize the repository into the current workspace before deeper inspection.",
        "Prefer concrete build/test entrypoints and bundled datasets over speculative assumptions.",
    ),
    "init_report": (
        "Keep this node narrow: create request notes, report contract, and lane scaffolding only.",
        "Do not spend this node on heavy research; return as soon as initialization artifacts exist.",
    ),
    "compose_report": (
        "Compose only from lane evidence already gathered or explicitly note which lanes remain incomplete.",
        "For report and assessment deliverables, include a compact evidence-source note that names the main source categories and distinguishes direct evidence from inferred or proxy evidence.",
    ),
    "init_generic": (
        "Keep this node narrow: define the generic delivery contract, pick 2-3 bounded worker units, and record a one-layer parallel batch plan only.",
        "Do not spend this node on broad execution; stop once the worker split and batch plan are concrete enough for the next round.",
    ),
    "compose_generic": (
        "Compose a normal long-form user-facing answer from worker outputs; avoid turning the result into a formal report artifact unless the user explicitly asked for one.",
        "For judgment or assessment-style answers, briefly state where the evidence came from and which parts are direct evidence, inferred evidence, or unresolved gaps.",
    ),
    "worker_context": (
        "Focus on constraints, evidence, repository facts, or source-backed context needed for the generic task.",
        "When gathering evidence for a judgment task, record source categories, source dates when relevant, and whether each source directly supports the claim or only provides proxy context.",
    ),
    "worker_solution": (
        "Focus on implementation, execution, repair, or solution design needed for the generic task.",
    ),
    "benchmark_case": (
        "If the registered benchmark artifacts already define the exact docker/WDL launch command and inputs, execute that concrete path first instead of re-planning.",
        "After the command exits, inspect the expected output directory and return JSON immediately once the primary assembly outputs and logs are present.",
    ),
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


@lru_cache(maxsize=None)
def _guidance_asset_lines(kind: str, name: str) -> tuple[str, ...]:
    path = (
        _repo_root()
        / ".code2workspace"
        / "skills"
        / "orchestration"
        / "supervisor-guidance"
        / "references"
        / kind
        / f"{name}.md"
    )
    if not path.exists():
        return ()
    lines = [
        line.strip("- ").strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    return tuple(line for line in lines if line)


def describe_capabilities(
    bundles: list[CapabilityBundle],
) -> tuple[list[str], list[str], list[str]]:
    """Return summaries, allowed tools, and implementation kinds for bundles."""

    summaries: list[str] = []
    tool_names: list[str] = []
    kinds: list[str] = []
    seen_tools: set[str] = set()
    for bundle in bundles:
        spec = CAPABILITY_REGISTRY[bundle]
        summaries.append(f"- {bundle}: {spec.summary}")
        kinds.append(f"- {bundle}: {spec.implementation_kind}")
        for tool_name in spec.tool_names:
            if tool_name in seen_tools:
                continue
            seen_tools.add(tool_name)
            tool_names.append(tool_name)
    return summaries, tool_names, kinds


def node_guidance_lines(node_id: str) -> list[str]:
    """Return any node-specific execution guidance lines."""

    lines: list[str] = []
    for key, guidance in _NODE_GUIDANCE_DEFAULTS.items():
        if key in node_id:
            asset_lines = _guidance_asset_lines("nodes", key)
            lines.extend(asset_lines or guidance)
    return lines


def family_guidance_lines(guidance_ids: list[str]) -> list[str]:
    """Return any family-level guidance lines for matched guidance ids."""

    lines: list[str] = []
    for guidance_id in guidance_ids:
        lines.extend(_guidance_asset_lines("families", guidance_id))
    return lines
