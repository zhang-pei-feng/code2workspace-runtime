"""Supervisor graph planning and execution primitives."""

from __future__ import annotations

import asyncio
import ast
from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.errors import GraphBubbleUp

CapabilityBundle = Literal[
    "repo_fetch",
    "docker_build_run",
    "wdl_run",
    "data_filter",
    "operator_filter",
    "metric_compute",
    "summarize",
    "validate",
    "plan",
    "task_manage",
    "web_search",
    "web_fetch",
    "db_access",
    "api_call",
]

TaskType = Literal["generic", "github2workspace", "benchmark", "report"]
WorkerStatus = Literal["completed", "blocked", "failed", "partial"]
DecisionType = Literal["stop", "replan", "continue"]
GenericApproach = Literal["simple", "medium", "difficult"]

_CAPABILITY_BUNDLES: set[str] = {
    "repo_fetch",
    "docker_build_run",
    "wdl_run",
    "data_filter",
    "operator_filter",
    "metric_compute",
    "summarize",
    "validate",
    "plan",
    "task_manage",
    "web_search",
    "web_fetch",
    "db_access",
    "api_call",
}

@dataclass(slots=True)
class TaskNode:
    node_id: str
    title: str
    objective: str
    capability_bundles: list[CapabilityBundle]
    metadata: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class TaskEdge:
    source: str
    target: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(slots=True)
class TaskGraph:
    graph_id: str
    task_type: TaskType
    round_index: int
    nodes: list[TaskNode]
    edges: list[TaskEdge]
    metadata: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "graph_id": self.graph_id,
            "task_type": self.task_type,
            "round_index": self.round_index,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class WorkerTaskEnvelope:
    node: TaskNode
    round_index: int
    workspace_dir: str
    recursion_depth: int
    recursion_budget: int

    def to_dict(self) -> dict[str, object]:
        return {
            "node": self.node.to_dict(),
            "round_index": self.round_index,
            "workspace_dir": self.workspace_dir,
            "recursion_depth": self.recursion_depth,
            "recursion_budget": self.recursion_budget,
        }


@dataclass(slots=True)
class WorkerResult:
    status: WorkerStatus
    summary: str
    artifacts: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    next_action_hint: str | None = None
    failure_reason: str | None = None
    spawned_subgraph: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class WorkerNodeResult:
    node_id: str
    status: WorkerStatus
    summary: str
    artifacts: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    next_action_hint: str | None = None
    failure_reason: str | None = None
    spawned_subgraph: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class TaskExecutionRound:
    graph: TaskGraph
    node_results: list[WorkerNodeResult]

    @property
    def completed_count(self) -> int:
        return sum(item.status == "completed" for item in self.node_results)

    @property
    def failed_count(self) -> int:
        return sum(item.status == "failed" for item in self.node_results)

    @property
    def blocked_count(self) -> int:
        return sum(item.status == "blocked" for item in self.node_results)

    @property
    def partial_count(self) -> int:
        return sum(item.status == "partial" for item in self.node_results)

    def to_dict(self) -> dict[str, object]:
        return {
            "graph": self.graph.to_dict(),
            "node_results": [item.to_dict() for item in self.node_results],
            "completed_count": self.completed_count,
            "failed_count": self.failed_count,
            "blocked_count": self.blocked_count,
            "partial_count": self.partial_count,
        }


@dataclass(slots=True)
class SupervisorDecision:
    decision: DecisionType
    reason: str
    failed_nodes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class CaseTraceRecord:
    task_type: str
    summary: str
    run_dir: str | None = None
    score: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class CaseIndexEntry:
    task: str
    task_type: str
    summary: str
    run_dir: str
    score: float = 0.0

    def to_case_trace_record(self) -> CaseTraceRecord:
        return CaseTraceRecord(
            task_type=self.task_type,
            summary=self.summary,
            run_dir=self.run_dir,
            score=self.score,
        )


@dataclass(frozen=True, slots=True)
class TaskGuidance:
    """Experience guidance for a known task family."""

    guidance_id: str
    task_type: TaskType
    markers: tuple[str, ...]
    summary: str


@dataclass(slots=True)
class TaskClassification:
    """Planner-facing task classification."""

    primary_type: TaskType
    guidance_ids: list[str]


class ModelRefusalError(RuntimeError):
    """Raised when an upstream model explicitly refuses to answer."""

    def __init__(
        self,
        *,
        message: str,
        stage: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.user_message = message
        self.stage = stage
        self.details = details or {}


_SPECIAL_TASK_TYPES = {"benchmark", "github2workspace", "report", "generic"}
_TASK_CLASSIFIER_CONFIDENCE_THRESHOLD = 0.55
_TASK_CLASSIFIER_SYSTEM_PROMPT = """You classify a user task for a supervisor runtime.

Choose exactly one task_type from:
- benchmark
- github2workspace
- report
- generic

Definitions:
- benchmark: compare tools or workflows on shared datasets and summarize metrics or results
- github2workspace: turn a repository into a runnable workspace, usually involving repository inspection, Docker, validation, WDL, or Cromwell
- report: write a formal evidence-backed report, risk assessment, monitoring brief, or synthesis
- generic: anything else

Important disambiguation rules:
- If the task is primarily about fixing code in a single repository, producing a patch, or satisfying one or more repo-local tests, classify it as generic even if the prompt contains words like "benchmark", "FAIL_TO_PASS", "instance_id", "hints", or copied benchmark metadata.
- Only classify as benchmark when the task is actually asking to compare multiple tools, workflows, or configurations on shared datasets and then summarize metrics, outcomes, or scorecards.
- A software-repair task wrapped in benchmark-style metadata is still generic unless it explicitly asks for cross-tool evaluation or metric comparison.
- Only classify as report when the user is asking for a formal deliverable such as a report, brief, formal assessment, presentation-ready writeup, or WHO-style structured synthesis.
- If the user explicitly asks for a quick analysis, oral judgment, direct answer, normal discussion, or says things like "不要正式写作", "先给我一个口头判断", or "区分证据和猜测", classify as generic unless the prompt still clearly demands a formal report artifact.

Return JSON only with keys:
{
  "task_type": "...",
  "confidence": 0.0,
  "reason": "...",
  "matched_signals": ["..."]
}
"""


_TASK_GUIDANCE_REGISTRY: tuple[TaskGuidance, ...] = (
    TaskGuidance(
        guidance_id="benchmark_family",
        task_type="benchmark",
        markers=("benchmark", "workflow comparison", "组装 benchmark", "staged benchmark"),
        summary="Use staged registration, per-tool fan-out, metrics aggregation, and retry failed tools before summarizing.",
    ),
    TaskGuidance(
        guidance_id="github2workspace_pipeline",
        task_type="github2workspace",
        markers=("github.com/", "github2workspace", "可运行 workspace", "runnable workspace"),
        summary="Use inspect -> build -> workflow validation -> summarize, and replan from the first failed phase.",
    ),
    TaskGuidance(
        guidance_id="report_synthesis",
        task_type="report",
        markers=("报告", "风险评估", "monitoring brief", "risk assessment", "formal report", "who 风格"),
        summary="Use init -> parallel evidence lanes -> compose -> summarize, preserving evidence layers and uncertainty.",
    ),
)

_GENERIC_FORMAL_NEGATIVE_MARKERS = (
    "不要正式写作",
    "口头判断",
    "直接说",
    "直接回答",
    "先帮我分析",
    "区分证据和猜测",
)


def classify_task(task: str) -> TaskClassification:
    """Classify a task and attach any matched guidance ids."""

    lowered = task.casefold()
    if any(marker.casefold() in lowered for marker in _GENERIC_FORMAL_NEGATIVE_MARKERS):
        return TaskClassification(primary_type="generic", guidance_ids=[])
    guidance_ids: list[str] = []
    primary_type: TaskType = "generic"
    for guidance in _TASK_GUIDANCE_REGISTRY:
        if any(marker.casefold() in lowered for marker in guidance.markers):
            guidance_ids.append(guidance.guidance_id)
            if primary_type == "generic":
                primary_type = guidance.task_type
    return TaskClassification(primary_type=primary_type, guidance_ids=guidance_ids)


def detect_task_type(task: str) -> TaskType:
    return classify_task(task).primary_type


async def classify_task_with_model(
    *,
    model,
    task: str,
) -> tuple[TaskClassification, dict[str, Any]]:
    """Use an LLM to classify the main task family, then fall back to rules."""

    rule_classification = classify_task(task)
    rule_details: dict[str, Any] = {
        "source": "rules_fallback",
        "task_type": rule_classification.primary_type,
        "guidance_ids": list(rule_classification.guidance_ids),
    }
    if model is None or not task.strip():
        return rule_classification, rule_details

    classifier_messages = [
        SystemMessage(content=_TASK_CLASSIFIER_SYSTEM_PROMPT),
        HumanMessage(content=f"Task:\n{task}\n\nReturn JSON only."),
    ]
    attempts: list[dict[str, Any]] = []
    payload: dict[str, Any] | None = None
    text = ""
    max_attempts = 2
    for attempt_index in range(max_attempts):
        try:
            response = await model.ainvoke(classifier_messages)
        except Exception as exc:
            rule_details["llm_error"] = f"{type(exc).__name__}: {exc}"
            rule_details["llm_attempts"] = attempts
            return rule_classification, rule_details

        text = _message_text(response)
        attempt_details = _classifier_attempt_details(
            response=response,
            text=text,
            attempt=attempt_index + 1,
        )
        refusal_message = _extract_model_refusal_message(response)
        attempt_details["refusal_message"] = refusal_message
        payload = _extract_task_classifier_payload(text)
        attempt_details["parsed_payload_found"] = payload is not None
        attempts.append(attempt_details)
        if refusal_message is not None:
            raise ModelRefusalError(
                message=refusal_message,
                stage="classifier",
                details={"llm_attempts": attempts},
            )
        if payload is not None:
            break
        if text.strip() or attempt_index + 1 >= max_attempts:
            break

    if payload is None:
        rule_details["llm_error"] = "invalid_classifier_output"
        rule_details["raw_output"] = text
        rule_details["llm_attempts"] = attempts
        return rule_classification, rule_details

    task_type = payload.get("task_type")
    confidence = payload.get("confidence")
    reason = payload.get("reason")
    matched_signals = payload.get("matched_signals")
    if (
        not isinstance(task_type, str)
        or task_type not in _SPECIAL_TASK_TYPES
        or not isinstance(confidence, (int, float))
        or float(confidence) < _TASK_CLASSIFIER_CONFIDENCE_THRESHOLD
    ):
        rule_details["llm_candidate"] = {
            "task_type": task_type,
            "confidence": confidence,
            "reason": reason,
            "matched_signals": matched_signals,
        }
        rule_details["llm_error"] = "low_confidence_or_invalid_task_type"
        rule_details["llm_attempts"] = attempts
        return rule_classification, rule_details

    llm_classification = classification_for_task_type(task_type)
    return llm_classification, {
        "source": "llm_classifier",
        "task_type": llm_classification.primary_type,
        "guidance_ids": list(llm_classification.guidance_ids),
        "confidence": float(confidence),
        "reason": str(reason) if isinstance(reason, str) else "",
        "matched_signals": [
            str(item)
            for item in matched_signals
            if isinstance(matched_signals, list)
            for item in matched_signals
        ],
        "llm_attempts": attempts,
        "rules_fallback_task_type": rule_classification.primary_type,
    }


def classification_for_task_type(task_type: TaskType) -> TaskClassification:
    """Build a classification object from an explicit task type."""

    guidance_ids = [
        guidance.guidance_id
        for guidance in _TASK_GUIDANCE_REGISTRY
        if guidance.task_type == task_type
    ]
    return TaskClassification(primary_type=task_type, guidance_ids=guidance_ids)


def _message_text(response: object) -> str:
    if isinstance(response, AIMessage):
        content = response.content
        if isinstance(content, str):
            return content
        return str(content)
    content = getattr(response, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        rendered = "\n".join(str(item) for item in content if str(item).strip())
        if rendered.strip():
            return rendered
    return str(response)


def _extract_model_refusal_message(response: object) -> str | None:
    response_metadata = getattr(response, "response_metadata", None)
    stop_reason = None
    if isinstance(response_metadata, dict):
        stop_reason = response_metadata.get("stop_reason")
    if stop_reason != "refusal":
        return None

    content = getattr(response, "content", None)
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    return text.strip()
    return "The model refused to answer this request."


def _classifier_attempt_details(
    *,
    response: object,
    text: str,
    attempt: int,
) -> dict[str, Any]:
    content = getattr(response, "content", None)
    response_metadata = getattr(response, "response_metadata", None)
    usage_metadata = getattr(response, "usage_metadata", None)
    return {
        "attempt": attempt,
        "response_type": type(response).__name__,
        "content_type": type(content).__name__ if content is not None else "NoneType",
        "content_is_empty": not text.strip(),
        "content_preview": _preview_text(text),
        "response_repr_preview": _preview_text(repr(response)),
        "response_metadata": _normalize_classifier_debug_value(response_metadata),
        "usage_metadata": _normalize_classifier_debug_value(usage_metadata),
    }


def _normalize_classifier_debug_value(value: object) -> object:
    try:
        encoded = json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return _preview_text(str(value))
    if len(encoded) <= 1200:
        return json.loads(encoded)
    return _preview_text(encoded, max_chars=1200)


def _preview_text(text: str, *, max_chars: int = 600) -> str:
    stripped = text.strip()
    if len(stripped) <= max_chars:
        return stripped
    return stripped[: max_chars - 3].rstrip() + "..."


def _extract_task_classifier_payload(text: str) -> dict[str, Any] | None:
    payload = _extract_json_object(text)
    if payload is None:
        return None
    return _extract_payload_from_object(payload)


def _extract_json_object(text: str) -> Any | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    try:
        return ast.literal_eval(stripped)
    except (SyntaxError, ValueError):
        pass
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < 0 or end <= start:
        return None
    try:
        return json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return None


def _extract_payload_from_object(payload: object) -> dict[str, Any] | None:
    if isinstance(payload, dict):
        task_type = payload.get("task_type")
        confidence = payload.get("confidence")
        if isinstance(task_type, str) and isinstance(confidence, (int, float)):
            return payload
        text_value = payload.get("text")
        if isinstance(text_value, str):
            nested = _extract_json_object(text_value)
            if nested is not None:
                return _extract_payload_from_object(nested)
        content_value = payload.get("content")
        if isinstance(content_value, str):
            nested = _extract_json_object(content_value)
            if nested is not None:
                return _extract_payload_from_object(nested)
        return None
    if isinstance(payload, list):
        for item in payload:
            nested = _extract_payload_from_object(item)
            if nested is not None:
                return nested
    return None


class HeuristicSupervisorPlanner:
    """Build graph rounds for all tasks; known families add guidance/templates."""

    def plan_round(
        self,
        *,
        task: str,
        retrieved_cases: list[CaseTraceRecord],
        prior_rounds: list[TaskExecutionRound],
        generic_approach: GenericApproach | None = None,
        classification_override: TaskClassification | None = None,
    ) -> TaskGraph:
        classification = classification_override or classify_task(task)
        round_index = len(prior_rounds) + 1
        if classification.primary_type == "github2workspace":
            return self._plan_github2workspace(
                task=task,
                retrieved_cases=retrieved_cases,
                prior_rounds=prior_rounds,
                round_index=round_index,
                guidance_ids=classification.guidance_ids,
            )
        if classification.primary_type == "benchmark":
            return self._plan_benchmark(
                task=task,
                retrieved_cases=retrieved_cases,
                prior_rounds=prior_rounds,
                round_index=round_index,
                guidance_ids=classification.guidance_ids,
            )
        if classification.primary_type == "report":
            return self._plan_report(
                task=task,
                retrieved_cases=retrieved_cases,
                prior_rounds=prior_rounds,
                round_index=round_index,
                guidance_ids=classification.guidance_ids,
            )
        return self._plan_generic(
            task=task,
            retrieved_cases=retrieved_cases,
            prior_rounds=prior_rounds,
            round_index=round_index,
            guidance_ids=classification.guidance_ids,
            generic_approach=generic_approach,
        )

    def _plan_generic(
        self,
        *,
        task: str,
        retrieved_cases: list[CaseTraceRecord],
        prior_rounds: list[TaskExecutionRound],
        round_index: int,
        guidance_ids: list[str],
        generic_approach: GenericApproach | None,
    ) -> TaskGraph:
        if not prior_rounds:
            nodes = [
                TaskNode(
                    node_id="init_generic",
                    title="Plan generic graph",
                    objective=(
                        "Analyze the generic user request and create a flexible execution graph for the next round. "
                        "Do not assume a fixed template. Choose the smallest useful graph for the task: direct answer, "
                        "sequential investigation, parallel evidence lanes, code inspect/fix/verify, data inspect/analyze/recommend, "
                        "or any other shape that fits. Research and evidence-judgment questions are the main scenario; scale "
                        "their graph by complexity, from one synthesis node to parallel source/evidence lanes when needed. "
                        "Return a worker result whose spawned_subgraph contains nodes and edges for the next round."
                    ),
                    capability_bundles=["plan", "task_manage", "validate"],
                    metadata={"planner_role": "generic_dynamic_graph_planner"},
                )
            ]
            edges = []
        elif _round_analyze_succeeded(prior_rounds[-1]):
            graph = _generic_graph_from_planner_result(
                prior_round=prior_rounds[-1],
                task=task,
                round_index=round_index,
                guidance_ids=guidance_ids,
                retrieved_cases=retrieved_cases,
            )
            if graph is not None:
                return graph
            nodes, edges = _generic_fallback_execution_nodes()
        else:
            retry_targets = [
                item.node_id
                for item in prior_rounds[-1].node_results
                if item.status in {"failed", "partial", "blocked"}
                and item.node_id != "summarize"
            ]
            retry_target = retry_targets[0] if retry_targets else "worker_solution"
            nodes = [
                TaskNode(
                    node_id=f"retry_{retry_target}",
                    title=f"Retry {retry_target}",
                    objective=f"Retry the {retry_target} phase, preserving concrete evidence and adapting the generic batch plan using prior failures.",
                    capability_bundles=[
                        "plan",
                        "task_manage",
                        "repo_fetch",
                        "docker_build_run",
                        "wdl_run",
                        "data_filter",
                        "operator_filter",
                        "metric_compute",
                        "web_search",
                        "web_fetch",
                        "db_access",
                        "api_call",
                        "validate",
                    ],
                    metadata={"retry_of": retry_target},
                ),
                TaskNode(
                    node_id="compose_generic",
                    title="Compose generic answer",
                    objective="Merge the retried generic worker outputs into one normal long-form user-facing answer.",
                    capability_bundles=["summarize", "validate"],
                ),
                TaskNode(
                    node_id="summarize",
                    title="Summarize generic outcome",
                    objective="Summarize the strongest verified generic outcome, worker contributions, blockers, and next-step recommendation.",
                    capability_bundles=["summarize"],
                ),
            ]
            edges = [
                TaskEdge(source=nodes[0].node_id, target="compose_generic"),
                TaskEdge(source="compose_generic", target="summarize"),
            ]
        return TaskGraph(
            graph_id=f"generic-r{round_index}",
            task_type="generic",
            round_index=round_index,
            nodes=_attach_common_node_metadata(
                nodes,
                task=task,
                task_type="generic",
                guidance_ids=guidance_ids,
            ),
            edges=edges,
            metadata=_graph_metadata(task=task, retrieved_cases=retrieved_cases, guidance_ids=guidance_ids),
        )

    def _plan_github2workspace(
        self,
        *,
        task: str,
        retrieved_cases: list[CaseTraceRecord],
        prior_rounds: list[TaskExecutionRound],
        round_index: int,
        guidance_ids: list[str],
    ) -> TaskGraph:
        if not prior_rounds:
            nodes = [
                TaskNode(
                    node_id="inspect",
                    title="Inspect repository",
                    objective="Inspect the repository, identify build/test assets, and record validation prerequisites.",
                    capability_bundles=["repo_fetch", "validate"],
                ),
                TaskNode(
                    node_id="build",
                    title="Build Docker image",
                    objective="Build or repair the Docker image and run a minimal container validation attempt.",
                    capability_bundles=["docker_build_run", "validate"],
                ),
                TaskNode(
                    node_id="wdl",
                    title="Run WDL workflow",
                    objective="Generate or repair WDL inputs and run Cromwell workflow validation.",
                    capability_bundles=["wdl_run", "validate"],
                ),
                TaskNode(
                    node_id="summarize",
                    title="Summarize workspace outcome",
                    objective="Summarize the strongest validated workspace state, blockers, and artifact paths.",
                    capability_bundles=["summarize"],
                ),
            ]
            edges = [
                TaskEdge(source="inspect", target="build"),
                TaskEdge(source="build", target="wdl"),
                TaskEdge(source="wdl", target="summarize"),
            ]
        else:
            first = _first_unresolved_node(prior_rounds[-1], fallback="build")
            if "inspect" in first:
                nodes = [
                    TaskNode(
                        node_id="retry_inspect",
                        title="Retry inspect",
                        objective="Retry repository inspection and correct the earlier repository understanding failure.",
                        capability_bundles=["repo_fetch", "validate"],
                    ),
                    TaskNode(
                        node_id="build",
                        title="Build Docker image",
                        objective="Build or repair the Docker image after refreshed inspection.",
                        capability_bundles=["docker_build_run", "validate"],
                    ),
                    TaskNode(
                        node_id="wdl",
                        title="Run WDL workflow",
                        objective="Retry WDL generation and validation after the repaired build path.",
                        capability_bundles=["wdl_run", "validate"],
                    ),
                    TaskNode(
                        node_id="summarize",
                        title="Summarize workspace outcome",
                        objective="Summarize the latest validated workspace state and remaining blockers.",
                        capability_bundles=["summarize"],
                    ),
                ]
                edges = [
                    TaskEdge(source="retry_inspect", target="build"),
                    TaskEdge(source="build", target="wdl"),
                    TaskEdge(source="wdl", target="summarize"),
                ]
            elif "wdl" in first:
                nodes = [
                    TaskNode(
                        node_id="retry_wdl",
                        title="Retry WDL workflow",
                        objective="Retry WDL generation and Cromwell validation using the latest validated build artifacts.",
                        capability_bundles=["wdl_run", "validate"],
                    ),
                    TaskNode(
                        node_id="summarize",
                        title="Summarize workspace outcome",
                        objective="Summarize the latest validated workspace state and remaining blockers.",
                        capability_bundles=["summarize"],
                    ),
                ]
                edges = [TaskEdge(source="retry_wdl", target="summarize")]
            else:
                nodes = [
                    TaskNode(
                        node_id="retry_build",
                        title="Retry build",
                        objective="Retry Docker build/validation and apply the smallest repair needed for the earlier build failure.",
                        capability_bundles=["docker_build_run", "validate"],
                    ),
                    TaskNode(
                        node_id="wdl",
                        title="Run WDL workflow",
                        objective="Retry WDL generation and Cromwell validation after the repaired build path.",
                        capability_bundles=["wdl_run", "validate"],
                    ),
                    TaskNode(
                        node_id="summarize",
                        title="Summarize workspace outcome",
                        objective="Summarize the latest validated workspace state and remaining blockers.",
                        capability_bundles=["summarize"],
                    ),
                ]
                edges = [
                    TaskEdge(source="retry_build", target="wdl"),
                    TaskEdge(source="wdl", target="summarize"),
                ]
        return TaskGraph(
            graph_id=f"github2workspace-r{round_index}",
            task_type="github2workspace",
            round_index=round_index,
            nodes=_attach_common_node_metadata(
                nodes,
                task=task,
                task_type="github2workspace",
                guidance_ids=guidance_ids,
            ),
            edges=edges,
            metadata=_graph_metadata(task=task, retrieved_cases=retrieved_cases, guidance_ids=guidance_ids),
        )

    def _plan_benchmark(
        self,
        *,
        task: str,
        retrieved_cases: list[CaseTraceRecord],
        prior_rounds: list[TaskExecutionRound],
        round_index: int,
        guidance_ids: list[str],
    ) -> TaskGraph:
        if not prior_rounds:
            nodes = [
                TaskNode(
                    node_id="register",
                    title="Register benchmark cases",
                    objective="Confirm benchmark inputs, register each tool/case pair, and verify staged constraints before execution.",
                    capability_bundles=["plan", "task_manage", "validate"],
                    metadata={
                        "selected_tools": _select_benchmark_tools(task),
                        "excluded_tools": _extract_benchmark_excluded_tools(task),
                        "benchmark_root": _select_benchmark_root(task),
                    },
                )
            ]
            edges: list[TaskEdge] = []
        elif _benchmark_register_round_finished(prior_rounds[-1]):
            tools = _benchmark_selected_tools_from_round(prior_rounds[-1])
            nodes = []
            edges = []
            for tool in tools:
                nodes.append(
                    TaskNode(
                        node_id=tool,
                        title=f"Run {tool}",
                        objective=f"Execute the staged benchmark workload for {tool} and record outputs, logs, and failure reasons.",
                        capability_bundles=["docker_build_run", "wdl_run", "metric_compute"],
                    )
                )
                edges.append(TaskEdge(source=tool, target="summarize"))
            nodes.append(
                TaskNode(
                    node_id="summarize",
                    title="Summarize benchmark outcomes",
                    objective="Aggregate tool results, metrics, blockers, and output paths into a benchmark summary.",
                    capability_bundles=["metric_compute", "summarize"],
                )
            )
        else:
            if _benchmark_register_needs_retry(prior_rounds[-1]):
                nodes = [
                    TaskNode(
                        node_id="retry_register",
                        title="Retry register",
                        objective="Inspect benchmark assets again, choose a compatible tool subset, and write a structured registration result for the next execution round.",
                        capability_bundles=["plan", "task_manage", "validate"],
                    ),
                    TaskNode(
                        node_id="summarize",
                        title="Summarize benchmark outcomes",
                        objective="Summarize retry outcomes, metrics, blockers, and output paths into a benchmark summary.",
                        capability_bundles=["metric_compute", "summarize"],
                    ),
                ]
                edges = [TaskEdge(source="retry_register", target="summarize")]
                return TaskGraph(
                    graph_id=f"benchmark-r{round_index}",
                    task_type="benchmark",
                    round_index=round_index,
                    nodes=_attach_common_node_metadata(
                        nodes,
                        task=task,
                        task_type="benchmark",
                        guidance_ids=guidance_ids,
                    ),
                    edges=edges,
                    metadata=_graph_metadata(task=task, retrieved_cases=retrieved_cases, guidance_ids=guidance_ids),
                )
            failed_tools = [
                item.node_id
                for item in prior_rounds[-1].node_results
                if item.status in {"failed", "partial"}
                and item.node_id not in {"register", "summarize"}
            ]
            if not failed_tools:
                failed_tools = [
                    item.node_id
                    for item in prior_rounds[-1].node_results
                    if item.status == "blocked"
                    and item.node_id not in {"register", "summarize"}
                ]
            nodes = [
                TaskNode(
                    node_id=f"retry_{tool}",
                    title=f"Retry {tool}",
                    objective=f"Retry the benchmark execution for {tool} and preserve structured failure reasons if it still cannot complete.",
                    capability_bundles=["docker_build_run", "wdl_run", "metric_compute"],
                    metadata={"tool": tool},
                )
                for tool in failed_tools
            ]
            nodes.append(
                TaskNode(
                    node_id="summarize",
                    title="Summarize benchmark outcomes",
                    objective="Aggregate retry outcomes, metrics, blockers, and output paths into a benchmark summary.",
                    capability_bundles=["metric_compute", "summarize"],
                )
            )
            edges = [
                TaskEdge(source=node.node_id, target="summarize")
                for node in nodes
                if node.node_id != "summarize"
            ]
        return TaskGraph(
            graph_id=f"benchmark-r{round_index}",
            task_type="benchmark",
            round_index=round_index,
            nodes=_attach_common_node_metadata(
                nodes,
                task=task,
                task_type="benchmark",
                guidance_ids=guidance_ids,
            ),
            edges=edges,
            metadata=_graph_metadata(task=task, retrieved_cases=retrieved_cases, guidance_ids=guidance_ids),
        )

    def _plan_report(
        self,
        *,
        task: str,
        retrieved_cases: list[CaseTraceRecord],
        prior_rounds: list[TaskExecutionRound],
        round_index: int,
        guidance_ids: list[str],
    ) -> TaskGraph:
        if not prior_rounds:
            nodes = [
                TaskNode(
                    node_id="init_report",
                    title="Initialize report run",
                    objective="Create the report run directory, save the request, outline evidence lanes, and define the report contract.",
                    capability_bundles=["plan", "task_manage", "validate"],
                ),
                TaskNode(
                    node_id="monitoring_lane",
                    title="Monitoring lane",
                    objective="Collect monitoring, operational, and official surveillance evidence relevant to the report topic.",
                    capability_bundles=["web_search", "web_fetch", "validate"],
                ),
                TaskNode(
                    node_id="local_data_lane",
                    title="Local data lane",
                    objective="Collect local structured data, registry, API, or database evidence relevant to the report topic.",
                    capability_bundles=["db_access", "api_call", "validate"],
                ),
                TaskNode(
                    node_id="literature_lane",
                    title="Literature and web lane",
                    objective="Collect literature, technical, and primary-source web evidence that complements the other lanes.",
                    capability_bundles=["web_search", "web_fetch", "api_call"],
                ),
                TaskNode(
                    node_id="compose_report",
                    title="Compose report",
                    objective=(
                        "Compose a full-length report from the completed lanes. Prefer a substantial report body "
                        "with clear sections, explicit evidence-to-claim linkage, evidence source notes, and "
                        "preserved uncertainty rather than a short brief."
                    ),
                    capability_bundles=["summarize", "validate"],
                ),
                TaskNode(
                    node_id="summarize",
                    title="Summarize report outcome",
                    objective="Summarize report completion state, output paths, evidence layers, and remaining blockers.",
                    capability_bundles=["summarize"],
                ),
            ]
            edges = [
                TaskEdge(source="init_report", target="monitoring_lane"),
                TaskEdge(source="init_report", target="local_data_lane"),
                TaskEdge(source="init_report", target="literature_lane"),
                TaskEdge(source="monitoring_lane", target="compose_report"),
                TaskEdge(source="local_data_lane", target="compose_report"),
                TaskEdge(source="literature_lane", target="compose_report"),
                TaskEdge(source="compose_report", target="summarize"),
            ]
        else:
            failed_nodes = [
                item.node_id
                for item in prior_rounds[-1].node_results
                if item.status in {"failed", "partial"}
                and item.node_id != "summarize"
            ]
            if not failed_nodes:
                failed_nodes = [
                    item.node_id
                    for item in prior_rounds[-1].node_results
                    if item.status == "blocked"
                    and item.node_id != "summarize"
                ]
            retry_nodes = [
                TaskNode(
                    node_id=f"retry_{node_id}",
                    title=f"Retry {node_id}",
                    objective=f"Retry the {node_id} phase, preserving concrete evidence and blockers in report artifacts.",
                    capability_bundles=_report_retry_bundles(node_id),
                    metadata={"retry_of": node_id},
                )
                for node_id in failed_nodes
            ]
            nodes = [
                *retry_nodes,
                TaskNode(
                    node_id="compose_report",
                    title="Compose report",
                    objective="Compose the report from all available evidence lanes after retries complete.",
                    capability_bundles=["summarize", "validate"],
                ),
                TaskNode(
                    node_id="summarize",
                    title="Summarize report outcome",
                    objective="Summarize report completion state, output paths, evidence layers, and remaining blockers.",
                    capability_bundles=["summarize"],
                ),
            ]
            edges = [TaskEdge(source=node.node_id, target="compose_report") for node in retry_nodes]
            edges.append(TaskEdge(source="compose_report", target="summarize"))
        return TaskGraph(
            graph_id=f"report-r{round_index}",
            task_type="report",
            round_index=round_index,
            nodes=_attach_common_node_metadata(
                nodes,
                task=task,
                task_type="report",
                guidance_ids=guidance_ids,
            ),
            edges=edges,
            metadata=_graph_metadata(task=task, retrieved_cases=retrieved_cases, guidance_ids=guidance_ids),
        )


def _first_unresolved_node(round_result: TaskExecutionRound, *, fallback: str) -> str:
    failed_or_partial = [
        item.node_id for item in round_result.node_results if item.status in {"failed", "partial"}
    ]
    if failed_or_partial:
        return failed_or_partial[0]
    blocked = [item.node_id for item in round_result.node_results if item.status == "blocked"]
    if blocked:
        return blocked[0]
    return fallback


def _benchmark_register_round_finished(round_result: TaskExecutionRound) -> bool:
    if [node.node_id for node in round_result.graph.nodes] != ["register"]:
        return False
    if [item.node_id for item in round_result.node_results] != ["register"]:
        return False
    register = round_result.node_results[0]
    return register.status == "completed" and bool(_benchmark_selected_tools_from_result(register))


def _benchmark_register_needs_retry(round_result: TaskExecutionRound) -> bool:
    graph_node_ids = [node.node_id for node in round_result.graph.nodes]
    if graph_node_ids not in (["register"], ["retry_register"], ["register", "summarize"], ["retry_register", "summarize"]):
        return False
    node_ids = [item.node_id for item in round_result.node_results]
    if "register" not in node_ids and "retry_register" not in node_ids:
        return False
    register = next(
        (
            item
            for item in round_result.node_results
            if item.node_id in {"register", "retry_register"}
        ),
        None,
    )
    if register is None:
        return False
    if register.status == "completed" and _benchmark_selected_tools_from_result(register):
        return False
    return register.status in {"failed", "partial", "blocked"} or not _benchmark_selected_tools_from_result(register)


def _benchmark_selected_tools_from_round(round_result: TaskExecutionRound) -> list[str]:
    for item in round_result.node_results:
        if item.node_id not in {"register", "retry_register"}:
            continue
        tools = _benchmark_selected_tools_from_result(item)
        if tools:
            return tools
    return []


def _benchmark_selected_tools_from_result(result: WorkerNodeResult) -> list[str]:
    payload = result.spawned_subgraph
    if not isinstance(payload, dict):
        return []
    selected_tools = payload.get("selected_tools")
    if not isinstance(selected_tools, list):
        return []
    return [str(item) for item in selected_tools if isinstance(item, str) and item.strip()]


def _round_contains_only_analyze(round_result: TaskExecutionRound) -> bool:
    return [item.node_id for item in round_result.node_results] == ["init_generic"]


def _round_analyze_succeeded(round_result: TaskExecutionRound) -> bool:
    return any(
        item.node_id == "init_generic" and item.status == "completed"
        for item in round_result.node_results
    )


def _generic_graph_from_planner_result(
    *,
    prior_round: TaskExecutionRound,
    task: str,
    round_index: int,
    guidance_ids: list[str],
    retrieved_cases: list[CaseTraceRecord],
) -> TaskGraph | None:
    planner_result = next(
        (item for item in prior_round.node_results if item.node_id == "init_generic"),
        None,
    )
    if planner_result is None or not isinstance(planner_result.spawned_subgraph, dict):
        return None
    payload = planner_result.spawned_subgraph
    raw_nodes = payload.get("nodes")
    raw_edges = payload.get("edges", [])
    if not isinstance(raw_nodes, list) or not raw_nodes:
        return None

    nodes: list[TaskNode] = []
    seen_ids: set[str] = set()
    for index, raw_node in enumerate(raw_nodes, start=1):
        if not isinstance(raw_node, dict):
            continue
        node_id = _sanitize_node_id(str(raw_node.get("node_id") or f"generic_step_{index}"))
        if not node_id or node_id == "init_generic" or node_id in seen_ids:
            node_id = f"generic_step_{index}"
        seen_ids.add(node_id)
        title = str(raw_node.get("title") or node_id.replace("_", " ").title())
        objective = str(raw_node.get("objective") or "Execute this generic subtask.")
        capability_bundles = _sanitize_capability_bundles(raw_node.get("capability_bundles"))
        metadata = raw_node.get("metadata")
        nodes.append(
            TaskNode(
                node_id=node_id,
                title=title[:120],
                objective=objective[:1000],
                capability_bundles=capability_bundles,
                metadata=metadata if isinstance(metadata, dict) else {},
            )
        )

    if not nodes:
        return None
    if "summarize" not in {node.node_id for node in nodes}:
        nodes.append(
            TaskNode(
                node_id="summarize",
                title="Summarize generic outcome",
                objective="Summarize the strongest verified generic outcome, worker contributions, blockers, and next-step recommendation.",
                capability_bundles=["summarize"],
            )
        )

    edges = _sanitize_edges(raw_edges, {node.node_id for node in nodes})
    if not edges and len(nodes) > 1:
        edges = [
            TaskEdge(source=nodes[index].node_id, target=nodes[index + 1].node_id)
            for index in range(len(nodes) - 1)
        ]
    edges = _connect_terminal_nodes_to_summarize(nodes, edges)

    return TaskGraph(
        graph_id=f"generic-r{round_index}",
        task_type="generic",
        round_index=round_index,
        nodes=_attach_common_node_metadata(
            nodes,
            task=task,
            task_type="generic",
            guidance_ids=guidance_ids,
        ),
        edges=edges,
        metadata={
            **_graph_metadata(task=task, retrieved_cases=retrieved_cases, guidance_ids=guidance_ids),
            "planner_generated": True,
            "planner_summary": planner_result.summary,
        },
    )


def _sanitize_node_id(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip().lower()).strip("_")
    if not normalized:
        return ""
    if normalized[0].isdigit():
        normalized = f"step_{normalized}"
    return normalized[:80]


def _sanitize_capability_bundles(value: object) -> list[CapabilityBundle]:
    if not isinstance(value, list):
        return ["summarize", "validate"]
    bundles = [
        str(item)
        for item in value
        if isinstance(item, str) and item in _CAPABILITY_BUNDLES
    ]
    if not bundles:
        return ["summarize", "validate"]
    return list(dict.fromkeys(bundles))  # type: ignore[return-value]


def _sanitize_edges(value: object, node_ids: set[str]) -> list[TaskEdge]:
    if not isinstance(value, list):
        return []
    edges: list[TaskEdge] = []
    seen: set[tuple[str, str]] = set()
    for raw_edge in value:
        if not isinstance(raw_edge, dict):
            continue
        source = _sanitize_node_id(str(raw_edge.get("source") or ""))
        target = _sanitize_node_id(str(raw_edge.get("target") or ""))
        if source not in node_ids or target not in node_ids or source == target:
            continue
        key = (source, target)
        if key in seen:
            continue
        seen.add(key)
        edges.append(TaskEdge(source=source, target=target))
    return edges


def _connect_terminal_nodes_to_summarize(
    nodes: list[TaskNode],
    edges: list[TaskEdge],
) -> list[TaskEdge]:
    node_ids = {node.node_id for node in nodes}
    if "summarize" not in node_ids or len(nodes) <= 1:
        return edges
    existing = {(edge.source, edge.target) for edge in edges}
    outgoing = {edge.source for edge in edges}
    updated = list(edges)
    for node in nodes:
        if node.node_id == "summarize" or node.node_id in outgoing:
            continue
        key = (node.node_id, "summarize")
        if key not in existing:
            updated.append(TaskEdge(source=node.node_id, target="summarize"))
            existing.add(key)
    return updated


def _generic_fallback_execution_nodes() -> tuple[list[TaskNode], list[TaskEdge]]:
    nodes = [
        TaskNode(
            node_id="compose_generic",
            title="Compose generic answer",
            objective="The dynamic generic planner did not return a usable subgraph. Produce the best direct user-facing answer with available context and state any uncertainty.",
            capability_bundles=["summarize", "validate"],
        ),
        TaskNode(
            node_id="summarize",
            title="Summarize generic outcome",
            objective="Summarize the strongest verified generic outcome, worker contributions, blockers, and next-step recommendation.",
            capability_bundles=["summarize"],
        ),
    ]
    return nodes, [TaskEdge(source="compose_generic", target="summarize")]


def _generic_execute_objective(approach: GenericApproach) -> str:
    if approach == "simple":
        return (
            "Execute the simplest useful version of the task: produce the smallest "
            "verified answer or artifact, avoid optional expansion, and preserve clear evidence."
        )
    if approach == "difficult":
        return (
            "Execute the most thorough version of the task: cover edge cases, broaden "
            "verification, and produce richer artifacts while staying within the original request."
        )
    return (
        "Execute the balanced version of the task: complete the main requested outcome "
        "with focused verification and avoid unnecessary expansion."
    )


def _extract_benchmark_tools(task: str) -> list[str]:
    catalog = _load_benchmark_catalog(task)
    if catalog is None:
        return []
    lowered = task.casefold()
    return [
        repo
        for repo in catalog.get("repo_cases", {})
        if str(repo).casefold() in lowered
    ]


def _select_benchmark_tools(task: str) -> list[str]:
    catalog = _load_benchmark_catalog(task)
    if catalog is None:
        return []
    excluded_tools = set(_extract_benchmark_excluded_tools(task))
    explicit_tools = _extract_benchmark_tools(task)
    explicit_tools = [tool for tool in explicit_tools if tool not in excluded_tools]
    if explicit_tools:
        return explicit_tools

    dataset_key = _select_benchmark_dataset_key(task, catalog)
    if dataset_key:
        tools = [
            tool
            for tool in _tools_for_dataset(catalog, dataset_key)
            if tool not in excluded_tools
        ]
        if tools:
            return tools

    dataset_keys = _ordered_candidate_benchmark_dataset_keys(task, catalog)
    for key in dataset_keys:
        tools = [
            tool
            for tool in _tools_for_dataset(catalog, str(key))
            if tool not in excluded_tools
        ]
        if len(tools) >= 2:
            return tools
    for key in dataset_keys:
        tools = [
            tool
            for tool in _tools_for_dataset(catalog, str(key))
            if tool not in excluded_tools
        ]
        if tools:
            return tools
    return []


def _select_benchmark_root(task: str) -> str | None:
    roots = _candidate_benchmark_roots(task)
    if not roots:
        return None
    return str(roots[0].resolve())


def _ordered_candidate_benchmark_dataset_keys(task: str, catalog: dict[str, object]) -> list[str]:
    datasets = catalog.get("datasets", {})
    if not isinstance(datasets, dict):
        return []
    case_dir_hint = _benchmark_case_dir_hint(task)
    keys = [str(key) for key in datasets]
    if case_dir_hint is None:
        return keys
    hinted: list[str] = []
    other: list[str] = []
    for key in keys:
        tools = _tools_for_dataset(catalog, key)
        if _dataset_has_case_dir_hint(catalog, tools, case_dir_hint):
            hinted.append(key)
        else:
            other.append(key)
    return hinted + other


def _benchmark_case_dir_hint(task: str) -> str | None:
    lowered = task.casefold()
    if "新冠病毒组装" in lowered or "病毒组装" in lowered:
        return "新冠病毒组装"
    if "circrna" in lowered or "cirrna" in lowered:
        return "cirrna"
    if "免疫逃逸" in lowered:
        return "免疫逃逸"
    return None


def _dataset_has_case_dir_hint(catalog: dict[str, object], tools: list[str], hint: str) -> bool:
    cases = catalog.get("repo_cases", {})
    if not isinstance(cases, dict):
        return False
    hint_lower = hint.casefold()
    for tool in tools:
        payload = cases.get(tool)
        if isinstance(payload, dict) and hint_lower in str(payload.get("case_dir", "")).casefold():
            return True
    return False


def _select_benchmark_dataset_key(task: str, catalog: dict[str, object]) -> str | None:
    lowered = task.casefold()
    datasets = catalog.get("datasets", {})
    if not isinstance(datasets, dict):
        return None
    for key in datasets:
        if str(key).casefold() in lowered:
            return str(key)
    for key, payload in datasets.items():
        if not isinstance(payload, dict):
            continue
        identifiers = [
            str(payload.get("dataset_id", "")),
            str(payload.get("description", "")),
        ]
        if any(identifier and identifier.casefold() in lowered for identifier in identifiers):
            return str(key)
    return None


def _tools_for_dataset(catalog: dict[str, object], dataset_key: str) -> list[str]:
    datasets = catalog.get("datasets", {})
    cases = catalog.get("repo_cases", {})
    if not isinstance(datasets, dict) or not isinstance(cases, dict):
        return []
    dataset = datasets.get(dataset_key)
    if isinstance(dataset, dict):
        shared_between = [
            str(item)
            for item in dataset.get("shared_between", [])
            if isinstance(item, str) and item in cases
        ]
        if shared_between:
            return shared_between
    return [
        str(repo)
        for repo, payload in cases.items()
        if isinstance(payload, dict) and payload.get("dataset_key") == dataset_key
    ]


def _extract_benchmark_excluded_tools(task: str) -> list[str]:
    lowered = task.casefold()
    cases = _benchmark_catalog_repo_cases(task)
    excluded: list[str] = []
    negative_markers = (
        "不要选",
        "不要用",
        "排除",
        "除外",
        "不包括",
        "别选",
        "except",
        "exclude",
        "without",
    )
    if not any(marker in lowered for marker in negative_markers):
        return excluded
    for repo in sorted(cases):
        repo_name = str(repo)
        if repo_name.casefold() in lowered:
            excluded.append(repo_name)
    return excluded


def _benchmark_catalog_repo_cases(task: str) -> set[str]:
    catalog = _load_benchmark_catalog(task)
    if not isinstance(catalog, dict):
        return set()
    repo_cases = catalog.get("repo_cases", {})
    if not isinstance(repo_cases, dict):
        return set()
    return {str(repo) for repo in repo_cases if isinstance(repo, str)}


def _load_benchmark_catalog(task: str) -> dict[str, object] | None:
    for root in _candidate_benchmark_roots(task):
        catalog = _read_benchmark_catalog(root)
        augmented = _augment_benchmark_catalog_from_files(root, catalog)
        if isinstance(augmented.get("repo_cases"), dict) and augmented["repo_cases"]:
            return augmented
    for path in _candidate_benchmark_catalog_paths(task):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("repo_cases"), dict):
            return payload
    return None


def _read_benchmark_catalog(root: Path) -> dict[str, object]:
    for path in (root / "datasets" / "benchmark_catalog.json", root / "benchmark_catalog.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            payload.setdefault("datasets", {})
            payload.setdefault("repo_cases", {})
            payload.setdefault("dataset_root", str(root / "datasets"))
            payload.setdefault("downloads_root", str(root / "datasets" / "downloads"))
            return payload
    return {
        "dataset_root": str(root / "datasets"),
        "downloads_root": str(root / "datasets" / "downloads"),
        "datasets": {},
        "repo_cases": {},
    }


def _candidate_benchmark_roots(task: str) -> list[Path]:
    roots: list[Path] = []
    for raw_path in _extract_task_paths(task):
        path = Path(raw_path)
        if not path.exists():
            continue
        if path.is_file():
            path = path.parent
        if (path / "experiments" / "benchmark").exists():
            roots.append(path / "experiments" / "benchmark")
        if (path / "datasets").exists() or any(path.rglob("*.wdl")):
            roots.append(path)
    seen: set[Path] = set()
    ordered: list[Path] = []
    for root in roots:
        resolved = root.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        ordered.append(root)
    return ordered


def _augment_benchmark_catalog_from_files(root: Path, catalog: dict[str, object]) -> dict[str, object]:
    datasets = catalog.setdefault("datasets", {})
    repo_cases = catalog.setdefault("repo_cases", {})
    if not isinstance(datasets, dict) or not isinstance(repo_cases, dict):
        return catalog

    discovered: dict[str, dict[str, object]] = {}
    for case_dir in _discover_benchmark_case_dirs(root):
        case = _benchmark_case_from_dir(root, case_dir)
        if case is None:
            continue
        discovered[str(case["repo_name"])] = case

    for repo_name, case in discovered.items():
        repo_cases.setdefault(repo_name, case)

    for case in discovered.values():
        dataset_key = str(case["dataset_key"])
        shared_between = sorted(
            name
            for name, payload in repo_cases.items()
            if isinstance(payload, dict) and str(payload.get("dataset_key")) == dataset_key
        )
        existing = datasets.get(dataset_key)
        if isinstance(existing, dict):
            existing_shared = [str(item) for item in existing.get("shared_between", []) if isinstance(item, str)]
            existing["shared_between"] = sorted(set(existing_shared + shared_between))
            continue
        datasets[dataset_key] = {
            "dataset_id": dataset_key,
            "description": "Shared benchmark inputs inferred from case input JSON files.",
            "family": case.get("family", "unknown"),
            "files": case.get("dataset_files", []),
            "local_candidates": case.get("local_candidates", []),
            "fallback_urls": [],
            "shared_between": shared_between,
            "source": "local benchmark inputs",
            "source_urls": [],
        }
    return catalog


def _discover_benchmark_case_dirs(root: Path) -> list[Path]:
    case_dirs: list[Path] = []
    for input_path in sorted([*root.rglob("inputs.json"), *root.rglob("input.json")]):
        if "datasets" in input_path.parts:
            continue
        case_dir = input_path.parent
        if any(case_dir.glob("*.wdl")):
            case_dirs.append(case_dir)
    return case_dirs


def _benchmark_case_from_dir(root: Path, case_dir: Path) -> dict[str, object] | None:
    input_path = next((path for path in (case_dir / "inputs.json", case_dir / "input.json") if path.exists()), None)
    wdl_path = next(iter(sorted(case_dir.glob("*.wdl"))), None)
    if input_path is None or wdl_path is None:
        return None
    try:
        inputs = json.loads(input_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(inputs, dict):
        return None
    repo_name = _repo_name_from_benchmark_case_dir(case_dir)
    workflow_name = _workflow_name_from_wdl(wdl_path) or repo_name
    input_files = _benchmark_file_inputs(inputs)
    if not input_files:
        return None
    dataset_key = _dataset_key_for_input_files(input_files)
    family = _benchmark_family_for_case(repo_name, workflow_name, input_files)
    rel_case_dir = str(case_dir.relative_to(root.parent.parent if root.parent.name == "experiments" else root))
    rel_wdl = str(wdl_path.relative_to(root.parent.parent if root.parent.name == "experiments" else root))
    rel_inputs = str(input_path.relative_to(root.parent.parent if root.parent.name == "experiments" else root))
    dockerfile_candidates = _known_benchmark_dockerfile_candidates(repo_name)
    return {
        "repo_name": repo_name,
        "repo_url": _known_benchmark_repo_url(repo_name),
        "family": family,
        "dataset_key": dataset_key,
        "image_tag": _runtime_image_from_wdl_text(wdl_path.read_text(encoding="utf-8", errors="replace")) or f"benchmark/{repo_name.casefold()}:latest",
        "repo_native_entry": _default_repo_entry(repo_name),
        "wdl_workflow_name": workflow_name,
        "expected_outputs": _expected_outputs_for_family(family),
        "constraints": ["Use benchmark inputs discovered from the local case directory."],
        "case_dir": rel_case_dir,
        "wdl_path": rel_wdl,
        "inputs_path": rel_inputs,
        "dockerfile_path": dockerfile_candidates[0] if dockerfile_candidates else None,
        "dockerfile_candidates": dockerfile_candidates,
        "wdl_candidates": [rel_wdl],
        "input_json_candidates": [rel_inputs],
        "repo_native_command_candidates": [],
        "local_result_candidates": [],
        "dataset_files": _dataset_files_for_inputs(input_files),
        "local_candidates": sorted(input_files),
    }


def _repo_name_from_benchmark_case_dir(case_dir: Path) -> str:
    return re.sub(r"^\d+[_-]*", "", case_dir.name)


def _workflow_name_from_wdl(wdl_path: Path) -> str | None:
    match = re.search(r"\bworkflow\s+([A-Za-z_][A-Za-z0-9_]*)", wdl_path.read_text(encoding="utf-8", errors="replace"))
    return match.group(1) if match else None


def _benchmark_file_inputs(inputs: dict[str, object]) -> list[str]:
    values: list[str] = []
    for value in inputs.values():
        if not isinstance(value, str):
            continue
        lowered = value.casefold()
        if "/" not in value and not lowered.endswith((".fq", ".fastq", ".fa", ".fasta", ".gz", ".bam", ".bed", ".pt", ".vcf")):
            continue
        if any(lowered.endswith(suffix) for suffix in (".fq", ".fastq", ".fa", ".fasta", ".gz", ".bam", ".bed", ".pt", ".vcf")):
            values.append(value)
    return sorted(dict.fromkeys(values))


def _dataset_key_for_input_files(input_files: list[str]) -> str:
    joined = "\n".join(input_files).casefold()
    if "srr001666" in joined:
        return "short-read-ecoli-srr001666"
    if "pacbio.fastq" in joined:
        return "long-read-canu-pacbio"
    digest = hashlib.sha1("\n".join(input_files).encode("utf-8")).hexdigest()[:10]
    return f"shared-inputs-{digest}"


def _benchmark_family_for_case(repo_name: str, workflow_name: str, input_files: list[str]) -> str:
    lowered = f"{repo_name} {workflow_name} {' '.join(input_files)}".casefold()
    if "spades" in lowered or "megahit" in lowered:
        return "short-read-assembly"
    if "canu" in lowered or "flye" in lowered or "pacbio" in lowered:
        return "long-read-assembly"
    if "trinity" in lowered:
        return "rna-seq-transcriptome"
    if "fieldbio" in lowered or "artic" in lowered:
        return "viral-ont-amplicon"
    if "covid" in lowered or "signal" in lowered:
        return "viral-short-read-pipeline"
    return "unknown"


def _dataset_files_for_inputs(input_files: list[str]) -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    for index, value in enumerate(input_files, start=1):
        name = Path(value).name or f"input_{index}"
        files.append({"logical_name": _logical_input_name(value, index), "uri": value, "filename": name})
    return files


def _logical_input_name(value: str, index: int) -> str:
    lowered = value.casefold()
    if "_1" in lowered or "r1" in lowered or "read1" in lowered:
        return "reads_1"
    if "_2" in lowered or "r2" in lowered or "read2" in lowered:
        return "reads_2"
    if "pacbio" in lowered or lowered.endswith((".fq", ".fastq", ".fq.gz", ".fastq.gz")):
        return "reads" if index == 1 else f"reads_{index}"
    return f"input_{index}"


def _known_benchmark_repo_url(repo_name: str) -> str:
    known = {
        "spades": "https://github.com/ablab/spades",
        "megahit": "https://github.com/voutcn/megahit",
        "canu": "https://github.com/marbl/canu",
        "Flye": "https://github.com/fenderglass/Flye",
        "trinityrnaseq": "https://github.com/trinityrnaseq/trinityrnaseq",
        "covid-19-signal": "https://github.com/jaleezyy/covid-19-signal",
        "fieldbioinformatics": "https://github.com/artic-network/fieldbioinformatics",
    }
    return known.get(repo_name, "")


def _known_benchmark_dockerfile_candidates(repo_name: str) -> list[str]:
    known = {
        "spades": [".workspaces/oneshot/spades/spades_Dockerfile"],
        "megahit": [
            ".workspaces/oneshot/megahit/megahit_Dockerfile",
            ".workspaces/oneshot/megahit/Dockerfile",
        ],
    }
    return known.get(repo_name, [])


def _runtime_image_from_wdl_text(text: str) -> str | None:
    match = re.search(r'docker:\s*"([^"]+)"', text)
    return match.group(1) if match else None


def _default_repo_entry(repo_name: str) -> str:
    defaults = {
        "spades": "spades.py -1 <reads_1> -2 <reads_2>",
        "megahit": "megahit -1 <reads_1> -2 <reads_2> -o out",
        "canu": "canu -p ecoli -d out genomeSize=4.8m -pacbio <reads>",
        "Flye": "flye --pacbio-raw <reads> --out-dir out",
        "trinityrnaseq": "Trinity --left <reads_1> --right <reads_2> --seqType fq --output out",
    }
    return defaults.get(repo_name, "")


def _expected_outputs_for_family(family: str) -> list[str]:
    if family in {"short-read-assembly", "long-read-assembly"}:
        return ["assembly.fasta", "contigs.fasta", "final.contigs.fa"]
    if family == "rna-seq-transcriptome":
        return ["Trinity.fasta"]
    if family in {"viral-short-read-pipeline", "viral-ont-amplicon"}:
        return ["consensus.fasta", "variants.vcf", "coverage.tsv"]
    return []


def _candidate_benchmark_catalog_paths(task: str) -> list[Path]:
    candidates: list[Path] = []
    for raw_path in _extract_task_paths(task):
        base = Path(raw_path)
        candidates.extend(
            [
                base / "datasets" / "benchmark_catalog.json",
                base / "benchmark_catalog.json",
            ]
        )

    seen: set[Path] = set()
    existing: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if candidate.exists():
            existing.append(candidate)
    return existing


def _report_retry_bundles(node_id: str) -> list[CapabilityBundle]:
    if "monitoring" in node_id:
        return ["web_search", "web_fetch", "validate"]
    if "local_data" in node_id:
        return ["db_access", "api_call", "validate"]
    if "literature" in node_id:
        return ["web_search", "web_fetch", "api_call"]
    if "compose" in node_id:
        return ["summarize", "validate"]
    return ["plan", "task_manage", "validate"]


def _graph_metadata(
    *,
    task: str,
    retrieved_cases: list[CaseTraceRecord],
    guidance_ids: list[str],
) -> dict[str, object]:
    return {
        "task": task,
        "task_paths": _extract_task_paths(task),
        "retrieved_cases": [item.to_dict() for item in retrieved_cases],
        "guidance_ids": list(guidance_ids),
        "guidance_summary": [
            guidance.summary
            for guidance in _TASK_GUIDANCE_REGISTRY
            if guidance.guidance_id in guidance_ids
        ],
    }


_ABS_PATH_RE = re.compile(r"(/[^ \n\t,;:，。]+)")


def _extract_task_paths(task: str) -> list[str]:
    """Extract absolute filesystem-like paths from the task text."""

    seen: list[str] = []
    for match in _ABS_PATH_RE.finditer(task):
        candidate = match.group(1).rstrip("。.,)")
        if candidate not in seen:
            seen.append(candidate)
    return seen


def _attach_common_node_metadata(
    nodes: list[TaskNode],
    *,
    task: str,
    task_type: TaskType,
    guidance_ids: list[str],
) -> list[TaskNode]:
    common = {
        "task": task,
        "task_type": task_type,
        "guidance_ids": list(guidance_ids),
    }
    updated: list[TaskNode] = []
    for node in nodes:
        updated.append(
            TaskNode(
                node_id=node.node_id,
                title=node.title,
                objective=node.objective,
                capability_bundles=list(node.capability_bundles),
                metadata={**common, **node.metadata},
            )
        )
    return updated


async def execute_graph_round(
    graph: TaskGraph,
    worker_runner,
) -> TaskExecutionRound:
    """Execute one graph round with dependency-aware parallel scheduling."""

    pending = {node.node_id: node for node in graph.nodes}
    by_target: dict[str, set[str]] = {}
    for edge in graph.edges:
        by_target.setdefault(edge.target, set()).add(edge.source)

    results: list[WorkerNodeResult] = []
    final_status: dict[str, WorkerStatus] = {}

    while pending:
        blocked_nodes = [
            node
            for node in pending.values()
            if any(final_status.get(dep) in {"failed", "blocked"} for dep in by_target.get(node.node_id, set()))
        ]
        if blocked_nodes:
            for node in blocked_nodes:
                results.append(
                    WorkerNodeResult(
                        node_id=node.node_id,
                        status="blocked",
                        summary="Blocked by failed dependency.",
                        failure_reason="blocked_by_failed_dependency",
                    )
                )
                final_status[node.node_id] = "blocked"
                pending.pop(node.node_id, None)
            continue

        ready = [
            node
            for node in pending.values()
            if all(final_status.get(dep) in {"completed", "partial"} for dep in by_target.get(node.node_id, set()))
        ]
        if not ready:
            for node in list(pending.values()):
                results.append(
                    WorkerNodeResult(
                        node_id=node.node_id,
                        status="blocked",
                        summary="Blocked by unresolved dependency chain.",
                        failure_reason="blocked_by_unresolved_dependency",
                    )
                )
                final_status[node.node_id] = "blocked"
                pending.pop(node.node_id, None)
            break

        batch_results = await asyncio.gather(*(_run_worker(node, worker_runner) for node in ready))
        for node, result in zip(ready, batch_results, strict=True):
            results.append(
                WorkerNodeResult(
                    node_id=node.node_id,
                    status=result.status,
                    summary=result.summary,
                    artifacts=list(result.artifacts),
                    evidence=list(result.evidence),
                    next_action_hint=result.next_action_hint,
                    failure_reason=result.failure_reason,
                    spawned_subgraph=result.spawned_subgraph,
                )
            )
            final_status[node.node_id] = result.status
            pending.pop(node.node_id, None)

    return TaskExecutionRound(graph=graph, node_results=results)


async def _run_worker(node: TaskNode, worker_runner) -> WorkerResult:
    try:
        result = await worker_runner(node)
    except ModelRefusalError:
        raise
    except GraphBubbleUp:
        raise
    except Exception as exc:  # pragma: no cover - defensive conversion
        return WorkerResult(
            status="failed",
            summary=f"{node.node_id} failed with an unexpected exception.",
            failure_reason=f"{type(exc).__name__}: {exc}",
        )
    return result


def decide_supervisor_step(
    round_result: TaskExecutionRound,
    *,
    max_rounds: int = 2,
) -> SupervisorDecision:
    if (
        round_result.failed_count == 0
        and round_result.blocked_count == 0
        and round_result.partial_count == 0
    ):
        return SupervisorDecision(decision="stop", reason="All nodes completed.")
    if round_result.graph.round_index >= max_rounds:
        failed_nodes = [
            item.node_id
            for item in round_result.node_results
            if item.status in {"failed", "blocked", "partial"}
        ]
        return SupervisorDecision(
            decision="stop",
            reason="Reached max rounds with unresolved nodes.",
            failed_nodes=failed_nodes,
        )
    failed_nodes = [
        item.node_id
        for item in round_result.node_results
        if item.status in {"failed", "blocked", "partial"}
    ]
    return SupervisorDecision(
        decision="replan",
        reason="Replan to retry unresolved nodes.",
        failed_nodes=failed_nodes,
    )
