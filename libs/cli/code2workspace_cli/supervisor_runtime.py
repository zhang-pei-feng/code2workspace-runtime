"""Supervisor-enabled orchestration runtime for CLI long tasks."""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain.agents import AgentState
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.config import get_stream_writer
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.types import Checkpointer, Command, interrupt

from code2workspace.orchestration_runtime import (
    CaseIndexEntry,
    CaseTraceRecord,
    GenericApproach,
    HeuristicSupervisorPlanner,
    ModelRefusalError,
    SupervisorDecision,
    TaskClassification,
    TaskNode,
    WorkerResult,
    _extract_model_refusal_message,
    classify_task,
    classify_task_with_model,
    decide_supervisor_step,
    execute_graph_round,
)
from code2workspace_cli.supervisor_capabilities import (
    describe_capabilities,
    family_guidance_lines,
    node_guidance_lines,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from langgraph.pregel import Pregel


@dataclass(slots=True)
class SupervisorRunResult:
    run_dir: Path
    final_summary: str
    user_response: str
    final_decision: SupervisorDecision
    round_count: int


@dataclass(frozen=True, slots=True)
class GenericApproachOption:
    approach: GenericApproach
    label: str
    summary: str
    execution_focus: str

    def to_dict(self) -> dict[str, str]:
        return {
            "approach": self.approach,
            "label": self.label,
            "summary": self.summary,
            "execution_focus": self.execution_focus,
        }


@dataclass(frozen=True, slots=True)
class SupervisorWorkerSubagent:
    """Runnable-backed worker selected directly by the supervisor runtime."""

    name: str
    runnable: Any
    node_ids: frozenset[str] = frozenset()

    def matches(self, node: TaskNode) -> bool:
        return node.node_id in self.node_ids


class SupervisorWorkerRunner:
    """Execute supervisor nodes through deterministic adapters or subagents."""

    def __init__(
        self,
        *,
        base_agent: Any,
        workspace_root: Path,
        default_subagent: Any | None = None,
        subagents: list[SupervisorWorkerSubagent] | None = None,
    ) -> None:
        self._base_agent = base_agent
        self._workspace_root = workspace_root
        self._default_subagent = default_subagent
        self._subagents = list(subagents or [])

    async def run(self, node: TaskNode) -> WorkerResult:
        deterministic = await asyncio.to_thread(
            _maybe_run_deterministic_worker,
            node=node,
            workspace_root=self._workspace_root,
        )
        if deterministic is not None:
            return deterministic
        return await _invoke_worker_runnable(
            agent=self._select_runnable(node),
            node=node,
            workspace_root=self._workspace_root,
        )

    def _select_runnable(self, node: TaskNode) -> Any:
        for subagent in self._subagents:
            if subagent.matches(node):
                return subagent.runnable
        return self._default_subagent or self._base_agent


_BENCHMARK_HELPER_RELATIVE_PATH = (
    ".code2workspace/skills/orchestration/benchmark-workflow-orchestrator/scripts/benchmark_workflow.py"
)
_TASK_PATH_RE = re.compile(r"(/[^ \n\t,;:]+)")
_TRANSIENT_WORKER_ERROR_MARKERS = (
    "APIConnectionError",
    "InternalServerError",
    "RemoteProtocolError",
    "APIError",
    "upstream_error",
    "Upstream request failed",
    "Connection error",
    "Error code: 502",
    "Error code: 503",
    "Error code: 504",
)
_GENERIC_WORKER_TRANSIENT_RETRIES = 2
_WORKER_PROMPT_MODE_ENV = "CODE2WORKSPACE_SUPERVISOR_WORKER_PROMPT_MODE"
_DEFAULT_WORKER_PROMPT_MODE = "messages"


class SQLiteCaseIndex:
    """Rebuildable case index over canonical workspace artifacts."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cases (
                    run_dir TEXT PRIMARY KEY,
                    task TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    searchable_text TEXT NOT NULL
                )
                """
            )

    def rebuild_from_workspace_root(self, workspace_root: Path) -> None:
        runs = _discover_run_dirs(workspace_root)
        with self._connect() as conn:
            conn.execute("DELETE FROM cases")
            for run_dir in runs:
                record = self._load_case_entry(run_dir)
                if record is None:
                    continue
                conn.execute(
                    """
                    INSERT OR REPLACE INTO cases
                    (run_dir, task, task_type, summary, searchable_text)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        record.run_dir,
                        record.task,
                        record.task_type,
                        record.summary,
                        f"{record.task}\n{record.summary}",
                    ),
                )

    def search(self, query: str, *, limit: int = 3) -> list[CaseTraceRecord]:
        query_tokens = _tokenize(query)
        rows: list[CaseIndexEntry] = []
        with self._connect() as conn:
            for row in conn.execute(
                "SELECT run_dir, task, task_type, summary, searchable_text FROM cases"
            ):
                run_dir, task, task_type, summary, searchable_text = row
                searchable = str(searchable_text)
                score = sum(token in searchable.casefold() for token in query_tokens)
                if score == 0:
                    continue
                rows.append(
                    CaseIndexEntry(
                        run_dir=str(run_dir),
                        task=str(task),
                        task_type=str(task_type),
                        summary=str(summary),
                        score=float(score),
                    )
                )
        rows.sort(key=lambda item: item.score, reverse=True)
        return [item.to_case_trace_record() for item in rows[:limit]]

    def _load_case_entry(self, run_dir: Path) -> CaseIndexEntry | None:
        request_path = run_dir / "request.json"
        decision_path = run_dir / "final_decision.json"
        summary_path = run_dir / "final_summary.md"
        if not (request_path.exists() and decision_path.exists() and summary_path.exists()):
            return None
        request_payload = json.loads(request_path.read_text(encoding="utf-8"))
        decision_payload = json.loads(decision_path.read_text(encoding="utf-8"))
        return CaseIndexEntry(
            run_dir=str(run_dir),
            task=str(request_payload.get("task", "")),
            task_type=str(decision_payload.get("task_type", "generic")),
            summary=summary_path.read_text(encoding="utf-8"),
        )


async def run_supervisor_orchestration(
    *,
    task: str,
    workspace_root: Path,
    worker_runner,
    planner: HeuristicSupervisorPlanner | None = None,
    max_rounds: int = 2,
    classification: TaskClassification | None = None,
    classification_details: dict[str, Any] | None = None,
    generic_approach_selector: Callable[
        [list[GenericApproachOption], Path], Awaitable[GenericApproach]
    ]
    | None = None,
) -> SupervisorRunResult:
    planner = planner or HeuristicSupervisorPlanner()
    run_dir = _new_run_dir(workspace_root)

    case_root = _case_collection_root(workspace_root)
    index = SQLiteCaseIndex(case_root / "orchestration_case_index.sqlite3")
    index.rebuild_from_workspace_root(case_root)
    retrieved_cases = index.search(task, limit=3)

    _write_json(run_dir / "request.json", {"task": task, "workspace_root": str(workspace_root)})
    _write_json(
        run_dir / "retrieved_cases.json",
        {"cases": [item.to_dict() for item in retrieved_cases]},
    )

    task_classification = classification or classify_task(task)
    task_type = task_classification.primary_type
    _write_json(
        run_dir / "task_classification.json",
        {
            "task": task,
            "task_type": task_type,
            "guidance_ids": list(task_classification.guidance_ids),
            "details": classification_details or {"source": "rules_fallback"},
        },
    )
    _emit_supervisor_event(
        kind="run_started",
        task=task,
        task_type=task_type,
        guidance_ids=list(task_classification.guidance_ids),
        classification_details=classification_details or {"source": "rules_fallback"},
        run_dir=str(run_dir),
        retrieved_case_count=len(retrieved_cases),
    )
    rounds = []
    selected_generic_approach: GenericApproach | None = None
    final_decision = SupervisorDecision(decision="stop", reason="No rounds executed.")
    while True:
        graph = planner.plan_round(
            task=task,
            retrieved_cases=retrieved_cases,
            prior_rounds=rounds,
            generic_approach=selected_generic_approach,
            classification_override=task_classification,
        )
        _write_json(run_dir / f"graph_round_{graph.round_index}.json", graph.to_dict())
        _emit_supervisor_event(
            kind="round_started",
            round_index=graph.round_index,
            graph_id=graph.graph_id,
            task_type=graph.task_type,
            node_ids=[node.node_id for node in graph.nodes],
        )
        try:
            round_result = await execute_graph_round(
                graph,
                lambda node: _run_worker_and_capture(
                    node=node,
                    graph_round=graph.round_index,
                    run_dir=run_dir,
                    worker_runner=worker_runner,
                ),
            )
        except ModelRefusalError as exc:
            return _finalize_refusal_run(
                run_dir=run_dir,
                task=task,
                task_type=task_type,
                rounds=rounds,
                refusal=exc,
            )
        rounds.append(round_result)
        if task_type == "generic" and _generic_analysis_round_finished(round_result):
            _emit_supervisor_event(
                kind="generic_plan_created",
                run_dir=str(run_dir),
                has_spawned_subgraph=bool(round_result.node_results[0].spawned_subgraph),
            )
            continue
        if task_type == "benchmark" and _benchmark_register_round_finished(round_result):
            _emit_supervisor_event(
                kind="benchmark_register_completed",
                round_index=round_result.graph.round_index,
                selected_tools=_benchmark_selected_tools_from_round_result(round_result),
            )
            continue
        final_decision = decide_supervisor_step(round_result, max_rounds=max_rounds)
        if final_decision.decision == "replan":
            continue
        break

    final_summary = _render_final_summary(
        task=task,
        rounds=rounds,
        decision=final_decision,
        generic_approach=selected_generic_approach,
    )
    try:
        user_response = await _render_user_response(
            task=task,
            task_type=task_type,
            rounds=rounds,
            decision=final_decision,
            final_summary=final_summary,
            run_dir=run_dir,
            worker_runner=worker_runner,
        )
    except ModelRefusalError as exc:
        return _finalize_refusal_run(
            run_dir=run_dir,
            task=task,
            task_type=task_type,
            rounds=rounds,
            refusal=exc,
        )
    (run_dir / "final_summary.md").write_text(final_summary, encoding="utf-8")
    (run_dir / "final_response.md").write_text(user_response, encoding="utf-8")
    _write_json(
        run_dir / "final_decision.json",
        {
            "decision": final_decision.decision,
            "reason": final_decision.reason,
            "failed_nodes": final_decision.failed_nodes,
            "task_type": task_type,
            "generic_approach": selected_generic_approach,
            "round_count": len(rounds),
            "run_dir": str(run_dir),
            "final_response_path": str(run_dir / "final_response.md"),
        },
    )
    _emit_supervisor_event(
        kind="run_finished",
        decision=final_decision.decision,
        reason=final_decision.reason,
        failed_nodes=list(final_decision.failed_nodes),
        generic_approach=selected_generic_approach,
        round_count=len(rounds),
        run_dir=str(run_dir),
    )
    index.rebuild_from_workspace_root(case_root)
    return SupervisorRunResult(
        run_dir=run_dir,
        final_summary=final_summary,
        user_response=user_response,
        final_decision=final_decision,
        round_count=len(rounds),
    )


def build_supervisor_enabled_agent(
    *,
    base_agent,
    fallback_agent=None,
    workspace_root: Path,
    worker_agent=None,
    worker_subagents: list[SupervisorWorkerSubagent] | None = None,
    classifier_model=None,
    enable_generic_ask_user: bool = True,
    checkpointer: Checkpointer | None = None,
) -> Pregel:
    """Wrap the base agent with supervisor routing for supported task types."""
    fallback_agent = fallback_agent or base_agent

    async def route(state: AgentState) -> Command[str]:
        task = _latest_human_text(state)
        if _latest_human_route_mode(state) == "fallback":
            return Command(goto="fallback")
        if task:
            return Command(goto="supervise")
        return Command(goto="fallback")

    async def supervise(state: AgentState) -> dict[str, object]:
        task = _latest_human_text(state) or ""
        try:
            task_classification, classification_details = await classify_task_with_model(
                model=classifier_model,
                task=task,
            )
        except ModelRefusalError as exc:
            return {"messages": [AIMessage(content=exc.user_message)]}
        worker_runner = SupervisorWorkerRunner(
            base_agent=base_agent,
            workspace_root=workspace_root,
            default_subagent=worker_agent,
            subagents=worker_subagents,
        )
        result = await run_supervisor_orchestration(
            task=task,
            workspace_root=workspace_root,
            worker_runner=worker_runner.run,
            classification=task_classification,
            classification_details=classification_details,
            generic_approach_selector=(
                _select_generic_approach_with_user
                if enable_generic_ask_user
                else None
            ),
        )
        return {"messages": [AIMessage(content=result.user_response)]}

    builder = StateGraph(AgentState)
    builder.add_node("route", route)
    builder.add_node("fallback", fallback_agent)
    builder.add_node("supervise", supervise)
    builder.add_edge(START, "route")
    builder.add_edge("fallback", END)
    builder.add_edge("supervise", END)
    return builder.compile(checkpointer=checkpointer)


def _generic_analysis_round_finished(round_result: Any) -> bool:
    return (
        round_result.graph.task_type == "generic"
        and round_result.graph.round_index == 1
        and [item.node_id for item in round_result.node_results] == ["init_generic"]
        and len(getattr(round_result.graph, "nodes", []) or []) == 1
        and round_result.node_results[0].status == "completed"
    )


def _benchmark_register_round_finished(round_result: Any) -> bool:
    return (
        round_result.graph.task_type == "benchmark"
        and [item.node_id for item in round_result.node_results] in (["register"], ["retry_register"])
        and round_result.node_results[0].status == "completed"
        and bool(_benchmark_selected_tools_from_round_result(round_result))
    )


def _benchmark_selected_tools_from_round_result(round_result: Any) -> list[str]:
    if not round_result.node_results:
        return []
    payload = round_result.node_results[0].spawned_subgraph
    if not isinstance(payload, dict):
        return []
    selected_tools = payload.get("selected_tools")
    if not isinstance(selected_tools, list):
        return []
    return [str(item) for item in selected_tools if isinstance(item, str) and item.strip()]


def _build_generic_approach_options(
    *,
    task: str,
    analysis_summary: str,
) -> list[GenericApproachOption]:
    task_hint = task.strip().splitlines()[0][:120] if task.strip() else "the requested task"
    analysis_hint = analysis_summary.strip()[:220] or "The analysis node completed."
    return [
        GenericApproachOption(
            approach="simple",
            label="简单",
            summary=f"只完成最小可用结果：围绕 `{task_hint}` 做一个低成本、低风险的第一版。",
            execution_focus=(
                "Use the analysis result to produce the smallest verified answer or artifact; "
                "skip optional checks and broad exploration."
            ),
        ),
        GenericApproachOption(
            approach="medium",
            label="中等",
            summary=f"完成主目标并做基础验证：基于分析结论 `{analysis_hint}` 走一条均衡执行路径。",
            execution_focus=(
                "Complete the main requested outcome with focused verification, concise artifacts, "
                "and a clear summary of tradeoffs."
            ),
        ),
        GenericApproachOption(
            approach="difficult",
            label="困难",
            summary="做更完整的版本：覆盖更多边界、验证和产物整理，但仍不越过原始需求边界。",
            execution_focus=(
                "Broaden implementation or investigation depth, include stronger verification, "
                "and preserve richer evidence for follow-up work."
            ),
        ),
    ]


def _write_generic_approach_options(
    *,
    run_dir: Path,
    options: list[GenericApproachOption],
) -> None:
    payload = {"options": [option.to_dict() for option in options]}
    _write_json(run_dir / "generic_approach_options.json", payload)
    lines = ["# Generic Approach Options", ""]
    for option in options:
        lines.extend(
            [
                f"## {option.label}",
                "",
                f"- approach: `{option.approach}`",
                f"- summary: {option.summary}",
                f"- execution_focus: {option.execution_focus}",
                "",
            ]
        )
    (run_dir / "generic_approach_options.md").write_text(
        "\n".join(lines).rstrip() + "\n",
        encoding="utf-8",
    )


async def _select_generic_approach_with_user(
    options: list[GenericApproachOption],
    run_dir: Path,
) -> GenericApproach:
    choices = [
        {"value": f"{option.label} ({option.approach}) - {option.summary}"}
        for option in options
    ]
    response = interrupt(
        {
            "type": "ask_user",
            "tool_call_id": f"generic-approach-{run_dir.name}",
            "questions": [
                {
                    "question": "请选择 generic 任务的执行复杂度。Supervisor 已完成任务分析，选择后再继续执行。",
                    "type": "multiple_choice",
                    "choices": choices,
                    "required": True,
                }
            ],
        }
    )
    return _parse_generic_approach_response(response, options)


def _parse_generic_approach_response(
    response: object,
    options: list[GenericApproachOption],
) -> GenericApproach:
    if not isinstance(response, dict):
        return "medium"
    answers = response.get("answers")
    if not isinstance(answers, list) or not answers:
        return "medium"
    answer = str(answers[0]).casefold()
    aliases: dict[GenericApproach, tuple[str, ...]] = {
        "simple": ("simple", "简单", "简易", "低", "轻量"),
        "medium": ("medium", "中等", "均衡", "普通"),
        "difficult": ("difficult", "困难", "复杂", "完整", "深入"),
    }
    for option in options:
        if any(alias in answer for alias in aliases[option.approach]):
            return option.approach
    return "medium"


def _generic_approach_label(
    approach: GenericApproach,
    options: list[GenericApproachOption],
) -> str:
    return next(
        (option.label for option in options if option.approach == approach),
        "中等",
    )


def _is_transient_worker_exception(exc: Exception) -> bool:
    rendered = f"{type(exc).__name__}: {exc}"
    return any(marker in rendered for marker in _TRANSIENT_WORKER_ERROR_MARKERS)


def _emit_supervisor_event(**payload: object) -> None:
    """Best-effort emit of supervisor runtime events into the graph stream."""
    try:
        stream_writer = get_stream_writer()
    except RuntimeError:
        return
    try:
        stream_writer({"supervisor_event": payload})
    except Exception:
        return


def _new_run_dir(workspace_root: Path) -> Path:
    """Create a unique orchestration run directory under the workspace root."""
    base = workspace_root / "orchestration_runs"
    while True:
        candidate = base / _run_id()
        try:
            (candidate / "node_traces").mkdir(parents=True, exist_ok=False)
            (candidate / "worker_outputs").mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        return candidate


async def _invoke_worker_agent(*, agent, node: TaskNode, workspace_root: Path) -> WorkerResult:
    deterministic = await asyncio.to_thread(
        _maybe_run_deterministic_worker,
        node=node,
        workspace_root=workspace_root,
    )
    if deterministic is not None:
        return deterministic
    return await _invoke_worker_runnable(
        agent=agent,
        node=node,
        workspace_root=workspace_root,
    )


async def _invoke_worker_runnable(*, agent, node: TaskNode, workspace_root: Path) -> WorkerResult:
    prompt = _build_worker_prompt(node=node, workspace_root=workspace_root)
    invoke_payload, invoke_kwargs = _build_worker_invoke_request(prompt)
    last_error: Exception | None = None
    for attempt in range(_GENERIC_WORKER_TRANSIENT_RETRIES + 1):
        try:
            result = await agent.ainvoke(invoke_payload, **invoke_kwargs)
            messages = result.get("messages", []) if isinstance(result, dict) else []
            _record_worker_tool_events(node=node, messages=messages)
            refusal_message = _extract_messages_refusal(messages)
            if refusal_message is not None:
                raise ModelRefusalError(
                    message=refusal_message,
                    stage=f"worker:{node.node_id}",
                    details={"node_id": node.node_id},
                )
            final_text = _last_ai_text(messages)
            parsed = _parse_worker_result(final_text)
            return parsed
        except Exception as exc:
            last_error = exc
            if attempt >= _GENERIC_WORKER_TRANSIENT_RETRIES or not _is_transient_worker_exception(exc):
                raise
            _emit_supervisor_event(
                kind="worker_retrying",
                node_id=node.node_id,
                title=node.title,
                attempt=attempt + 2,
                error=f"{type(exc).__name__}: {exc}",
            )
            await asyncio.sleep(0.5 * (attempt + 1))
    assert last_error is not None
    raise last_error


def _record_worker_tool_events(*, node: TaskNode, messages: list[object]) -> None:
    """Expose internal worker/subagent tool calls through supervisor events."""
    tool_names_by_id: dict[str, str] = {}
    for message in messages:
        for tool_call in _message_tool_calls(message):
            tool_call_id = str(tool_call.get("id") or "")
            tool_name = str(tool_call.get("name") or "unknown")
            if tool_call_id:
                tool_names_by_id[tool_call_id] = tool_name
            event = {
                "event": "worker_tool_call",
                "node_id": node.node_id,
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "args_preview": _compact_event_value(tool_call.get("args")),
            }
            _append_worker_tool_activity(node=node, event=event)
            _emit_supervisor_event(kind="worker_tool_call", **event)

        tool_result = _message_tool_result(message, tool_names_by_id)
        if tool_result is None:
            continue
        event = {
            "event": "worker_tool_result",
            "node_id": node.node_id,
            **tool_result,
        }
        _append_worker_tool_activity(node=node, event=event)
        _emit_supervisor_event(kind="worker_tool_result", **event)


def _extract_messages_refusal(messages: list[object]) -> str | None:
    for message in messages:
        refusal_message = _extract_model_refusal_message(message)
        if refusal_message is not None:
            return refusal_message
    return None


def _message_tool_calls(message: object) -> list[dict[str, object]]:
    tool_calls = getattr(message, "tool_calls", None)
    if not isinstance(tool_calls, list):
        return []
    return [item for item in tool_calls if isinstance(item, dict)]


def _message_tool_result(
    message: object,
    tool_names_by_id: dict[str, str],
) -> dict[str, object] | None:
    if not isinstance(message, ToolMessage):
        return None
    tool_call_id = str(getattr(message, "tool_call_id", "") or "")
    tool_name = str(getattr(message, "name", "") or tool_names_by_id.get(tool_call_id, "unknown"))
    status = str(getattr(message, "status", "") or "success")
    return {
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
        "status": status,
        "result_preview": _compact_event_value(getattr(message, "content", "")),
    }


def _append_worker_tool_activity(*, node: TaskNode, event: dict[str, object]) -> None:
    run_dir_raw = node.metadata.get("run_dir") if isinstance(node.metadata, dict) else None
    if not isinstance(run_dir_raw, str) or not run_dir_raw:
        return
    try:
        activity_path = Path(run_dir_raw) / "tool_activity.jsonl"
        with activity_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        return


def _finalize_refusal_run(
    *,
    run_dir: Path,
    task: str,
    task_type: str,
    rounds,
    refusal: ModelRefusalError,
) -> SupervisorRunResult:
    final_decision = SupervisorDecision(
        decision="stop",
        reason=f"model_refusal:{refusal.stage}",
    )
    final_summary = (
        "# Supervisor Summary\n\n"
        f"- Task: {task}\n"
        "- Decision: stop\n"
        f"- Reason: model refusal during {refusal.stage}\n"
    )
    user_response = refusal.user_message
    (run_dir / "final_summary.md").write_text(final_summary, encoding="utf-8")
    (run_dir / "final_response.md").write_text(user_response, encoding="utf-8")
    _write_json(
        run_dir / "final_decision.json",
        {
            "decision": final_decision.decision,
            "reason": final_decision.reason,
            "failed_nodes": [],
            "task_type": task_type,
            "generic_approach": None,
            "round_count": len(rounds),
            "run_dir": str(run_dir),
            "final_response_path": str(run_dir / "final_response.md"),
            "refusal_details": refusal.details,
        },
    )
    _emit_supervisor_event(
        kind="run_finished",
        decision=final_decision.decision,
        reason=final_decision.reason,
        failed_nodes=[],
        generic_approach=None,
        round_count=len(rounds),
        run_dir=str(run_dir),
    )
    return SupervisorRunResult(
        run_dir=run_dir,
        final_summary=final_summary,
        user_response=user_response,
        final_decision=final_decision,
        round_count=len(rounds),
    )


def _compact_event_value(value: object, *, max_chars: int = 500) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except TypeError:
            text = str(value)
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _build_worker_invoke_request(
    prompt: str,
) -> tuple[dict[str, list[HumanMessage | SystemMessage]], dict[str, object]]:
    """Build the nested worker-agent request in the configured prompt mode."""
    mode = os.environ.get(
        _WORKER_PROMPT_MODE_ENV,
        _DEFAULT_WORKER_PROMPT_MODE,
    ).strip().lower()
    user_message = HumanMessage(
        content="Execute the assigned node and return the required JSON only."
    )
    if mode == "messages":
        return (
            {
                "messages": [
                    SystemMessage(content=prompt),
                    user_message,
                ]
            },
            {},
        )
    return (
        {
            "messages": [user_message],
        },
        {"context": {"system_prompt": prompt}},
    )


def _maybe_run_deterministic_worker(*, node: TaskNode, workspace_root: Path) -> WorkerResult | None:
    metadata = node.metadata if isinstance(node.metadata, dict) else {}
    if metadata.get("task_type") != "benchmark":
        return None
    if node.node_id in {"register", "retry_register"}:
        return _run_deterministic_benchmark_register(node=node, workspace_root=workspace_root)
    if node.node_id == "summarize":
        return _run_deterministic_benchmark_summary(node=node, workspace_root=workspace_root)
    if _looks_like_user_delivery_node(node.node_id):
        return None
    repo = _benchmark_repo_from_node_id(node.node_id)
    if repo is not None:
        return _run_deterministic_benchmark_case(
            node=node,
            workspace_root=workspace_root,
            repo=repo,
        )
    return None


def _run_deterministic_benchmark_register(*, node: TaskNode, workspace_root: Path) -> WorkerResult:
    metadata = node.metadata if isinstance(node.metadata, dict) else {}
    run_dir_raw = metadata.get("run_dir")
    if not isinstance(run_dir_raw, str) or not run_dir_raw.strip():
        return WorkerResult(
            status="failed",
            summary="Benchmark register helper is missing run_dir metadata.",
            failure_reason="missing_run_dir",
        )
    selected_tools = [
        str(item)
        for item in metadata.get("selected_tools", [])
        if isinstance(item, str) and item.strip()
    ]
    excluded_tools = {
        str(item)
        for item in metadata.get("excluded_tools", [])
        if isinstance(item, str) and item.strip()
    }
    benchmark_root_raw = metadata.get("benchmark_root")
    benchmark_root = (
        benchmark_root_raw.strip()
        if isinstance(benchmark_root_raw, str)
        else ""
    )
    if not selected_tools:
        task = str(metadata.get("task", "")).strip()
        if not excluded_tools and not benchmark_root and _task_contains_url(task):
            return WorkerResult(
                status="failed",
                summary=(
                    "Benchmark register could not find local benchmark assets for the provided URLs. "
                    "URL-only benchmark requests need a preparation step that clones/builds tools and "
                    "materializes WDL/input assets before tool selection can run."
                ),
                failure_reason="missing_benchmark_assets",
                next_action_hint="prepare_benchmark_assets_from_urls",
            )
        return WorkerResult(
            status="failed",
            summary="Benchmark register helper is missing selected_tools metadata.",
            failure_reason="missing_selected_tools",
        )
    violating_tools = [tool for tool in selected_tools if tool in excluded_tools]
    if violating_tools:
        return WorkerResult(
            status="failed",
            summary=(
                "Benchmark register selected tools that violate the user's explicit "
                f"exclusion constraint: {', '.join(violating_tools)}."
            ),
            failure_reason="selected_tools_violate_exclusion_constraint",
        )
    repo_root = _locate_repo_root_for_benchmark(node=node, workspace_root=workspace_root)
    if repo_root is None:
        return WorkerResult(
            status="failed",
            summary="Could not locate the repository root for deterministic benchmark registration.",
            failure_reason="missing_repo_root",
        )
    helper_script = repo_root / _BENCHMARK_HELPER_RELATIVE_PATH
    if not helper_script.exists():
        return WorkerResult(
            status="failed",
            summary="Benchmark helper script is missing from the repository.",
            failure_reason="missing_benchmark_helper_script",
        )
    run_dir = Path(run_dir_raw)
    task = str(metadata.get("task", "")).strip() or "benchmark register"
    command_results: list[dict[str, object]] = []
    init_command = [
        sys.executable,
        str(helper_script),
        "init",
        "--task",
        task,
        "--output-dir",
        str(run_dir),
    ]
    if benchmark_root:
        init_command.extend(["--benchmark-root", benchmark_root])
    init_command.extend(["--repos", *selected_tools])
    try:
        command_results.append(
            _run_helper_json_command(
                init_command,
                cwd=repo_root,
                phase="init",
            )
        )
        command_results.append(
            _run_helper_json_command(
                [
                    sys.executable,
                    str(helper_script),
                    "resolve-datasets",
                    "--run-dir",
                    str(run_dir),
                ],
                cwd=repo_root,
                phase="resolve-datasets",
            )
        )
        for repo in selected_tools:
            command_results.append(
                _run_helper_json_command(
                    [
                        sys.executable,
                        str(helper_script),
                        "prepare-case",
                        "--repo",
                        repo,
                        "--run-dir",
                        str(run_dir),
                    ],
                    cwd=repo_root,
                    phase=f"prepare-case:{repo}",
                )
            )
            command_results.append(
                _run_helper_json_command(
                    [
                        sys.executable,
                        str(helper_script),
                        "execution-ready",
                        "--repo",
                        repo,
                        "--run-dir",
                        str(run_dir),
                    ],
                    cwd=repo_root,
                    phase=f"execution-ready:{repo}",
                )
            )
    except RuntimeError as exc:
        return WorkerResult(
            status="failed",
            summary="Deterministic benchmark register helper failed.",
            failure_reason=str(exc),
        )

    metric_plan = _write_metric_plan(run_dir=run_dir, selected_tools=selected_tools)
    readiness_rows: list[dict[str, object]] = []
    artifacts = [
        str(run_dir / "benchmark_plan.json"),
        str(run_dir / "benchmark_plan.md"),
        str(run_dir / "dataset_resolution.json"),
        str(run_dir / "dataset_resolution.md"),
        str(run_dir / "metric_plan.json"),
        str(run_dir / "metric_plan.md"),
    ]
    shared_datasets: list[str] = []
    all_ready = True
    for repo in selected_tools:
        case_dir = run_dir / "cases" / repo
        manifest_path = case_dir / "manifest.json"
        ready_path = case_dir / "execution_ready.json"
        dataset_selection_path = case_dir / "dataset_selection.json"
        dataset_manifest_path = case_dir / "dataset_manifest.json"
        agent_task_path = case_dir / "agent_task.md"
        case_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        ready_payload = json.loads(ready_path.read_text(encoding="utf-8"))
        dataset_key = str(case_manifest.get("dataset_key", ""))
        if dataset_key and dataset_key not in shared_datasets:
            shared_datasets.append(dataset_key)
        ready = bool(ready_payload.get("ready"))
        all_ready = all_ready and ready
        readiness_rows.append(
            {
                "repo": repo,
                "dataset_key": dataset_key,
                "ready": ready,
                "runtime_image": ready_payload.get("runtime_image"),
                "wdl_path": ready_payload.get("wdl_path"),
                "inputs_json_path": ready_payload.get("inputs_json_path"),
            }
        )
        artifacts.extend(
            [
                str(dataset_selection_path),
                str(dataset_manifest_path),
                str(agent_task_path),
                str(ready_path),
            ]
        )
    register_report = {
        "task": task,
        "repo_root": str(repo_root),
        "run_dir": str(run_dir),
        "selected_tools": selected_tools,
        "shared_datasets": shared_datasets,
        "metric_plan": metric_plan,
        "readiness": readiness_rows,
        "command_results": command_results,
    }
    _write_json(run_dir / "register_report.json", register_report)
    artifacts.append(str(run_dir / "register_report.json"))
    if shared_datasets:
        dataset_summary = ", ".join(shared_datasets)
    else:
        dataset_summary = "unknown dataset"
    tool_summary = ", ".join(selected_tools)
    if all_ready:
        return WorkerResult(
            status="completed",
            summary=f"Registered benchmark subset for {dataset_summary}: {tool_summary}.",
            artifacts=artifacts,
            evidence=[str(run_dir / "register_report.json")],
            spawned_subgraph={
                "selected_tools": selected_tools,
                "dataset_keys": shared_datasets,
                "register_report": str(run_dir / "register_report.json"),
            },
        )
    return WorkerResult(
        status="partial",
        summary=f"Registered benchmark subset for {dataset_summary}, but some execution-ready artifacts are still incomplete: {tool_summary}.",
        artifacts=artifacts,
        evidence=[str(run_dir / "register_report.json")],
        failure_reason="execution_ready_incomplete",
        spawned_subgraph={
            "selected_tools": selected_tools,
            "dataset_keys": shared_datasets,
            "register_report": str(run_dir / "register_report.json"),
        },
    )


def _run_deterministic_benchmark_case(
    *,
    node: TaskNode,
    workspace_root: Path,
    repo: str,
) -> WorkerResult:
    metadata = node.metadata if isinstance(node.metadata, dict) else {}
    run_dir_raw = metadata.get("run_dir")
    if not isinstance(run_dir_raw, str) or not run_dir_raw.strip():
        return WorkerResult(
            status="failed",
            summary=f"Benchmark {repo} helper is missing run_dir metadata.",
            failure_reason="missing_run_dir",
        )
    repo_root = _locate_repo_root_for_benchmark(node=node, workspace_root=workspace_root)
    if repo_root is None:
        return WorkerResult(
            status="failed",
            summary=f"Could not locate the repository root for deterministic benchmark execution of {repo}.",
            failure_reason="missing_repo_root",
        )
    helper_script = repo_root / _BENCHMARK_HELPER_RELATIVE_PATH
    if not helper_script.exists():
        return WorkerResult(
            status="failed",
            summary="Benchmark helper script is missing from the repository.",
            failure_reason="missing_benchmark_helper_script",
        )
    run_dir = Path(run_dir_raw)
    case_dir = run_dir / "cases" / repo
    manifest_path = case_dir / "manifest.json"
    if not manifest_path.exists():
        return WorkerResult(
            status="failed",
            summary=f"Benchmark case manifest for {repo} is missing.",
            failure_reason="missing_case_manifest",
        )
    status_path = case_dir / "run" / "status.json"
    if not _status_payload_is_success(status_path):
        try:
            _run_helper_json_command(
                [
                    sys.executable,
                    str(helper_script),
                    "run-repo-native",
                    "--repo",
                    repo,
                    "--run-dir",
                    str(run_dir),
                ],
                cwd=repo_root,
                phase=f"run-repo-native:{repo}",
            )
        except RuntimeError as exc:
            return WorkerResult(
                status="failed",
                summary=f"Deterministic benchmark execution failed for {repo}.",
                failure_reason=str(exc),
            )
    if not status_path.exists():
        return WorkerResult(
            status="failed",
            summary=f"Benchmark execution for {repo} did not produce run/status.json.",
            failure_reason="missing_run_status",
        )
    status_payload = json.loads(status_path.read_text(encoding="utf-8"))
    analysis_paths = _ensure_benchmark_analysis(
        repo=repo,
        run_dir=run_dir,
        repo_root=repo_root,
        helper_script=helper_script,
    )
    result_manifest = _write_benchmark_result_manifest(
        repo=repo,
        case_dir=case_dir,
        status_payload=status_payload,
    )
    artifacts = [
        str(status_path),
        str(case_dir / "run" / "repo_native.log"),
        *[
            str(path)
            for path in _benchmark_expected_output_paths(repo=repo, case_dir=case_dir, status_payload=status_payload)
            if path.exists()
        ],
        str(case_dir / "run" / "result_manifest.json"),
    ]
    artifacts.extend(str(path) for path in analysis_paths if path.exists())
    evidence = [
        str(case_dir / "execution_ready.json"),
        str(case_dir / "dataset_manifest.json"),
        str(status_path),
        str(case_dir / "run" / "result_manifest.json"),
    ]
    run_succeeded = bool(status_payload.get("success"))
    expected_output_found = any(
        item["exists"] for item in result_manifest["expected_outputs"].values()
    )
    success = run_succeeded and expected_output_found
    summary = (
        f"Executed the staged {repo} benchmark via the prepared helper path; "
        f"repo-native run {'succeeded' if run_succeeded else 'did not complete successfully'}"
    )
    if status_payload.get("returncode") is not None:
        summary += f" with exit code {status_payload['returncode']}."
    else:
        summary += "."
    if run_succeeded and not expected_output_found:
        return WorkerResult(
            status="partial",
            summary=summary + " Expected benchmark output files were not found, so this case is partial.",
            artifacts=artifacts,
            evidence=evidence,
            next_action_hint="summary",
            failure_reason="expected_outputs_missing",
        )
    if success:
        return WorkerResult(
            status="completed",
            summary=summary,
            artifacts=artifacts,
            evidence=evidence,
            next_action_hint="summary",
        )
    return WorkerResult(
        status="failed",
        summary=summary,
        artifacts=artifacts,
        evidence=evidence,
        next_action_hint="summary",
        failure_reason=_optional_str(status_payload.get("failure_reason")) or "repo_native_failed",
    )


def _run_deterministic_benchmark_summary(*, node: TaskNode, workspace_root: Path) -> WorkerResult:
    metadata = node.metadata if isinstance(node.metadata, dict) else {}
    run_dir_raw = metadata.get("run_dir")
    if not isinstance(run_dir_raw, str) or not run_dir_raw.strip():
        return WorkerResult(
            status="failed",
            summary="Benchmark summary helper is missing run_dir metadata.",
            failure_reason="missing_run_dir",
        )
    run_dir = Path(run_dir_raw)
    selected_tools = [
        str(item)
        for item in metadata.get("selected_tools", [])
        if isinstance(item, str) and item.strip()
    ]
    if not selected_tools:
        cases_root = run_dir / "cases"
        if cases_root.exists():
            selected_tools = sorted(path.name for path in cases_root.iterdir() if path.is_dir())
    repo_root = _locate_repo_root_for_benchmark(node=node, workspace_root=workspace_root)
    helper_script = repo_root / _BENCHMARK_HELPER_RELATIVE_PATH if repo_root is not None else None

    rows: list[dict[str, object]] = []
    artifacts: list[str] = []
    evidence: list[str] = []
    completed_cases = 0
    for repo in selected_tools:
        case_dir = run_dir / "cases" / repo
        manifest_path = case_dir / "manifest.json"
        if not manifest_path.exists():
            rows.append({"repo": repo, "status": "missing_case_manifest"})
            continue
        if helper_script is not None and helper_script.exists():
            analysis_paths = _ensure_benchmark_analysis(
                repo=repo,
                run_dir=run_dir,
                repo_root=repo_root,
                helper_script=helper_script,
            )
            artifacts.extend(str(path) for path in analysis_paths if path.exists())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        status_path = case_dir / "run" / "status.json"
        analysis_path = case_dir / "analysis.json"
        status_payload = (
            json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}
        )
        analysis_payload = (
            json.loads(analysis_path.read_text(encoding="utf-8"))
            if analysis_path.exists()
            else {"artifact_paths": [], "metrics": {}, "artifact_checksums": {}}
        )
        success = bool(status_payload.get("success"))
        if success:
            completed_cases += 1
        rows.append(
            {
                "repo": repo,
                "dataset_key": manifest.get("dataset_key"),
                "success": success,
                "returncode": status_payload.get("returncode"),
                "artifact_paths": analysis_payload.get("artifact_paths", []),
                "metrics": analysis_payload.get("metrics", {}),
            }
        )
        artifacts.extend(
            [
                str(path)
                for path in (
                    status_path,
                    analysis_path,
                    case_dir / "analysis.md",
                    case_dir / "run" / "result_manifest.json",
                )
                if path.exists()
            ]
        )
        evidence.extend(str(path) for path in (status_path, analysis_path) if path.exists())
        manifest["phase_status"] = {
            **dict(manifest.get("phase_status", {})),
            "analysis": "completed" if analysis_path.exists() else manifest.get("phase_status", {}).get("analysis"),
            "summary": "completed",
        }
        _write_json(manifest_path, manifest)

    overall_completed = bool(rows) and completed_cases == len(selected_tools)
    comparison = _build_benchmark_comparison(rows)
    payload = {
        "run_dir": str(run_dir),
        "selected_tools": selected_tools,
        "case_count": len(selected_tools),
        "completed_cases": completed_cases,
        "status": "completed" if overall_completed else "partial",
        "comparison": comparison,
        "rows": rows,
    }
    _write_json(run_dir / "benchmark_supervisor_summary.json", payload)
    lines = [
        "# Benchmark Supervisor Summary",
        "",
        f"- run_dir: `{run_dir}`",
        f"- selected_tools: `{', '.join(selected_tools)}`",
        f"- completed_cases: `{completed_cases}` / `{len(selected_tools)}`",
        f"- status: `{payload['status']}`",
        "",
        "## Rows",
        "",
    ]
    lines.extend(
        f"- `{row['repo']}`: success=`{row.get('success')}`, returncode=`{row.get('returncode')}`, metrics=`{row.get('metrics')}`"
        for row in rows
    )
    if comparison:
        lines.extend(
            [
                "",
                "## Comparison",
                "",
                f"- best_n50: `{comparison['best_n50']}`",
                f"- lowest_contig_count: `{comparison['lowest_contig_count']}`",
                f"- largest_assembly_size: `{comparison['largest_assembly_size']}`",
                f"- overall: {comparison['overall']}",
            ]
        )
    (run_dir / "benchmark_supervisor_summary.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    artifacts.extend(
        [
            str(run_dir / "benchmark_supervisor_summary.json"),
            str(run_dir / "benchmark_supervisor_summary.md"),
        ]
    )
    evidence.append(str(run_dir / "benchmark_supervisor_summary.json"))
    summary = (
        f"Summarized benchmark results for {', '.join(selected_tools)}; "
        f"{completed_cases}/{len(selected_tools)} cases completed."
    )
    if comparison:
        summary += f" {comparison['overall']}"
    if overall_completed:
        return WorkerResult(
            status="completed",
            summary=summary,
            artifacts=artifacts,
            evidence=evidence,
        )
    return WorkerResult(
        status="partial",
        summary=summary,
        artifacts=artifacts,
        evidence=evidence,
        failure_reason="benchmark_cases_incomplete",
    )


def _build_benchmark_comparison(rows: list[dict[str, object]]) -> dict[str, str] | None:
    completed_rows = [
        row
        for row in rows
        if row.get("success") is True and isinstance(row.get("metrics"), dict)
    ]
    if not completed_rows:
        return None

    best_n50 = _best_metric_repo(completed_rows, "n50", higher_is_better=True)
    lowest_contigs = _best_metric_repo(completed_rows, "contig_count", higher_is_better=False)
    largest_assembly = _best_metric_repo(completed_rows, "assembly_size", higher_is_better=True)
    winners = [repo for repo in (best_n50, largest_assembly) if repo]
    if winners:
        overall_repo = max(set(winners), key=winners.count)
    else:
        overall_repo = lowest_contigs

    if overall_repo and lowest_contigs and overall_repo != lowest_contigs:
        overall = (
            f"Overall favors {overall_repo} by N50/assembly-size strength, "
            f"while {lowest_contigs} has the lower contig_count."
        )
    elif overall_repo:
        overall = f"Overall favors {overall_repo} across the available comparison signals."
    else:
        overall = "No single best tool could be identified from the available metrics."

    return {
        "best_n50": best_n50 or "unknown",
        "lowest_contig_count": lowest_contigs or "unknown",
        "largest_assembly_size": largest_assembly or "unknown",
        "overall": overall,
    }


def _best_metric_repo(
    rows: list[dict[str, object]],
    metric_name: str,
    *,
    higher_is_better: bool,
) -> str | None:
    scored: list[tuple[float, str]] = []
    for row in rows:
        metrics = row.get("metrics")
        if not isinstance(metrics, dict):
            continue
        value = metrics.get(metric_name)
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        repo = row.get("repo")
        if isinstance(repo, str) and repo:
            scored.append((float(value), repo))
    if not scored:
        return None
    value, repo = (max if higher_is_better else min)(scored, key=lambda item: item[0])
    return repo


def _benchmark_repo_from_node_id(node_id: str) -> str | None:
    candidate = node_id.removeprefix("retry_")
    if candidate in {"register", "summarize"} or _looks_like_user_delivery_node(candidate):
        return None
    return candidate or None


def _status_payload_is_success(status_path: Path) -> bool:
    if not status_path.exists():
        return False
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return bool(payload.get("success"))


def _task_contains_url(task: str) -> bool:
    return bool(re.search(r"https?://\S+", task))


def _ensure_benchmark_analysis(
    *,
    repo: str,
    run_dir: Path,
    repo_root: Path,
    helper_script: Path,
) -> tuple[Path, Path]:
    analysis_json = run_dir / "cases" / repo / "analysis.json"
    analysis_md = run_dir / "cases" / repo / "analysis.md"
    if analysis_json.exists() and analysis_md.exists():
        return analysis_json, analysis_md
    try:
        _run_helper_json_command(
            [
                sys.executable,
                str(helper_script),
                "analyze-case",
                "--repo",
                repo,
                "--run-dir",
                str(run_dir),
            ],
            cwd=repo_root,
            phase=f"analyze-case:{repo}",
        )
    except RuntimeError:
        pass
    return analysis_json, analysis_md


def _write_benchmark_result_manifest(
    *,
    repo: str,
    case_dir: Path,
    status_payload: dict[str, object],
) -> dict[str, object]:
    manifest = json.loads((case_dir / "manifest.json").read_text(encoding="utf-8"))
    output_paths = _benchmark_expected_output_paths(
        repo=repo,
        case_dir=case_dir,
        status_payload=status_payload,
    )
    expected_outputs = {
        path.name: {
            "path": str(path),
            "exists": path.exists(),
            "size_bytes": path.stat().st_size if path.exists() else None,
        }
        for path in output_paths
    }
    log_path = case_dir / "run" / "repo_native.log"
    output_log = next((path for path in output_paths if path.name == "log" or path.suffix == ".log"), None)
    payload = {
        "repo": repo,
        "dataset_key": manifest.get("dataset_key"),
        "status": "completed" if bool(status_payload.get("success")) else "failed",
        "success": bool(status_payload.get("success")),
        "returncode": status_payload.get("returncode"),
        "elapsed_seconds": status_payload.get("elapsed_seconds"),
        "command": status_payload.get("command"),
        "log_path": str(log_path),
        "output_dir": status_payload.get("output_dir"),
        "expected_outputs": expected_outputs,
        "lightweight_evidence": {
            "repo_native_log": {
                "path": str(log_path),
                "size_bytes": log_path.stat().st_size if log_path.exists() else None,
            },
            "log_tail_summary": _tail_nonempty_lines(output_log or log_path),
        },
        "failure_reason": status_payload.get("failure_reason"),
    }
    _write_json(case_dir / "run" / "result_manifest.json", payload)
    return payload


def _benchmark_expected_output_paths(
    *,
    repo: str,
    case_dir: Path,
    status_payload: dict[str, object],
) -> list[Path]:
    output_dir_raw = status_payload.get("output_dir")
    output_dir = Path(output_dir_raw) if isinstance(output_dir_raw, str) else case_dir / "run" / "repo_native_output"
    manifest_path = case_dir / "manifest.json"
    expected_outputs: list[str] = []
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = {}
        expected_outputs = [
            str(item)
            for item in manifest.get("expected_outputs", [])
            if isinstance(item, str) and item
        ]
    if expected_outputs:
        return [output_dir / name for name in expected_outputs]
    status_artifacts = [
        Path(item)
        for item in status_payload.get("output_artifacts", [])
        if isinstance(item, str) and item
    ]
    return status_artifacts


def _tail_nonempty_lines(path: Path, *, limit: int = 3) -> list[str]:
    if not path.exists():
        return []
    lines = [line.strip() for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
    return lines[-limit:]


async def _run_worker_and_capture(
    *,
    node: TaskNode,
    graph_round: int,
    run_dir: Path,
    worker_runner,
) -> WorkerResult:
    node = _augment_node_with_runtime_context(node=node, graph_round=graph_round, run_dir=run_dir)
    with (run_dir / "tool_activity.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "event": "node_started",
                    "round_index": graph_round,
                    "node_id": node.node_id,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    _emit_supervisor_event(
        kind="node_started",
        round_index=graph_round,
        node_id=node.node_id,
        title=node.title,
        objective=node.objective,
        capability_bundles=list(node.capability_bundles),
    )
    try:
        result = await worker_runner(node)
    except GraphBubbleUp:
        with (run_dir / "tool_activity.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "event": "node_interrupted",
                        "round_index": graph_round,
                        "node_id": node.node_id,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        _emit_supervisor_event(
            kind="node_interrupted",
            round_index=graph_round,
            node_id=node.node_id,
            title=node.title,
        )
        raise
    except Exception as exc:
        with (run_dir / "tool_activity.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "event": "node_exception",
                        "round_index": graph_round,
                        "node_id": node.node_id,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        _emit_supervisor_event(
            kind="node_exception",
            round_index=graph_round,
            node_id=node.node_id,
            title=node.title,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    payload = {
        "round_index": graph_round,
        "node": node.to_dict(),
        "result": result.to_dict(),
    }
    _write_json(run_dir / "worker_outputs" / f"{node.node_id}.json", payload)
    _write_json(run_dir / "node_traces" / f"{node.node_id}.json", payload)
    with (run_dir / "tool_activity.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "event": "node_finished",
                    "round_index": graph_round,
                    "node_id": node.node_id,
                    "status": result.status,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    _emit_supervisor_event(
        kind="node_finished",
        round_index=graph_round,
        node_id=node.node_id,
        title=node.title,
        status=result.status,
        summary=result.summary,
        failure_reason=result.failure_reason,
        next_action_hint=result.next_action_hint,
        artifacts=list(result.artifacts),
        evidence=list(result.evidence),
    )
    return result


def _augment_node_with_runtime_context(
    *,
    node: TaskNode,
    graph_round: int,
    run_dir: Path,
) -> TaskNode:
    """Attach generic runtime context for downstream workers."""

    prior_worker_outputs = sorted(
        str(path)
        for path in (run_dir / "worker_outputs").glob("*.json")
        if path.is_file()
    )
    prior_node_traces = sorted(
        str(path)
        for path in (run_dir / "node_traces").glob("*.json")
        if path.is_file()
    )
    run_dir_artifacts = sorted(
        str(path)
        for path in run_dir.rglob("*")
        if path.is_file() and path.suffix in {".json", ".md", ".txt", ".log"}
    )
    prior_worker_output_payloads: dict[str, object] = {}
    for path in (run_dir / "worker_outputs").glob("*.json"):
        if not path.is_file():
            continue
        try:
            prior_worker_output_payloads[path.name] = _summarize_worker_output_payload(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except (json.JSONDecodeError, OSError):
            continue
    runtime_metadata = {
        "graph_round": graph_round,
        "run_dir": str(run_dir),
        "prior_worker_outputs": _compact_path_list(prior_worker_outputs),
        "prior_node_traces": _compact_path_list(prior_node_traces),
        "run_dir_artifacts": _compact_path_list(run_dir_artifacts),
        "prior_worker_output_payloads": prior_worker_output_payloads,
    }
    return TaskNode(
        node_id=node.node_id,
        title=node.title,
        objective=node.objective,
        capability_bundles=list(node.capability_bundles),
        metadata={**node.metadata, **runtime_metadata},
    )


def _build_worker_prompt(*, node: TaskNode, workspace_root: Path) -> str:
    bundles = ", ".join(node.capability_bundles)
    now_utc = datetime.now(UTC)
    local_now = now_utc.astimezone()
    capability_summaries, allowed_tools, implementation_kinds = describe_capabilities(
        node.capability_bundles
    )
    metadata_hint = ""
    if node.metadata:
        metadata_hint = _compact_metadata_hint(node.metadata)
    guidance_lines = node_guidance_lines(node.node_id)
    guidance_ids = node.metadata.get("guidance_ids", []) if isinstance(node.metadata, dict) else []
    if (
        isinstance(node.metadata, dict)
        and node.metadata.get("task_type") == "benchmark"
        and node.node_id not in {"register", "summarize"}
    ):
        guidance_lines.extend(node_guidance_lines("benchmark_case"))
    if isinstance(guidance_ids, list):
        guidance_lines.extend(family_guidance_lines([str(item) for item in guidance_ids]))
    if "report" in node.node_id or "lane" in node.node_id:
        guidance_lines.append(
            "For report-oriented nodes, prefer writing concrete report artifacts into the workspace such as request notes, lane notes, evidence summaries, or final_report.md when relevant."
        )
        guidance_lines.append(
            "For report-oriented nodes, record the main evidence sources used, their dates or freshness when relevant, and whether they directly support the claim or only provide proxy context."
        )
    if node.node_id == "init_generic":
        guidance_lines.extend(
            [
                "This node plans the next generic graph; do not answer the user fully in this node.",
                "Use the user's request to choose a flexible graph shape. You may use examples such as direct answer, code inspect/fix/verify, data inspect/analyze/recommend, migration planning, or research/evidence judgment, but do not force a template.",
                "Research and evidence-judgment questions are the main generic scenario. For simple questions use one synthesis node; for harder ones create sequential or parallel evidence/source/analysis lanes as needed.",
                "Put the next-round graph in spawned_subgraph with keys nodes and edges. Each node must include node_id, title, objective, and capability_bundles. Each edge must include source and target.",
                "Use only these capability_bundles: repo_fetch, docker_build_run, wdl_run, data_filter, operator_filter, metric_compute, summarize, validate, plan, task_manage, web_search, web_fetch, db_access, api_call.",
                "Always include a final summarize node unless the graph has exactly one direct synthesis node followed by summarize.",
            ]
        )
    if node.node_id.startswith("summarize") or node.node_id == "summarize":
        guidance_lines.extend(
            [
                "This is a synthesis node. Prefer using prior worker outputs and traces rather than opening new exploratory work.",
                "Do not start fresh evidence collection, broad repo scans, or unrelated validation unless a hard blocker in prior outputs requires one tiny targeted check.",
                "Stop as soon as you can produce the requested summary or final answer from existing worker results.",
            ]
        )
    if _looks_like_user_delivery_node(node.node_id):
        guidance_lines.append(
            "If this node prepares the final user-facing answer, put that answer itself in summary; put process notes, worker contributions, blockers, and evidence details in evidence or next_action_hint instead."
        )
    if node.node_id == "final_response":
        guidance_lines.extend(
            [
                "You are the final chat-facing answer editor. Write the answer the user should see, not an execution log.",
                "Use only the source material already gathered by supervisor and workers. Do not invent files, commands, evidence, test results, or completion status.",
                "Lead with useful information that was successfully obtained. Mention unfinished, failed, or blocked work only when it materially changes what the user can rely on.",
                "For report, risk assessment, monitoring, or judgment-style answers, include a brief evidence-source explanation unless the user's requested format forbids it. Name the source categories and preserve direct-vs-inferred evidence distinctions.",
                "When something is partial, phrase it calmly and briefly; do not over-emphasize internal node names, rounds, worker contributions, or supervisor diagnostics.",
                "Respect strict output constraints from the original user request. If the user asked for an exact short answer, put only that answer in summary.",
                "Return JSON only; the summary field must contain the final natural-language response itself.",
            ]
        )
    guidance_block = "\n".join(f"- {line}" for line in guidance_lines)
    capability_block = "\n".join(capability_summaries)
    implementation_block = "\n".join(implementation_kinds)
    allowed_tools_block = ", ".join(allowed_tools)
    return (
        "You are a generic worker node executor.\n"
        "Execute only the assigned node.\n"
        "Once the node's minimum required outputs exist, stop immediately and return the structured JSON result.\n"
        "Do not continue exploring after the node has enough information to hand control back to supervisor.\n"
        f"Workspace root: {workspace_root}\n"
        f"Current UTC time: {now_utc.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
        f"Current local time: {local_now.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
        "If the node depends on latest or recent information, use these timestamps as the time anchor, verify freshness explicitly, and prefer absolute dates in artifacts.\n"
        f"Node ID: {node.node_id}\n"
        f"Objective: {node.objective}\n"
        f"Original task: {node.metadata.get('task', '')}\n"
        f"Task paths: {node.metadata.get('task_paths', [])}\n"
        f"Run directory: {node.metadata.get('run_dir', '')}\n"
        f"Prior worker outputs: {_compact_path_list(list(node.metadata.get('prior_worker_outputs', []) or []))}\n"
        f"Prior node traces: {_compact_path_list(list(node.metadata.get('prior_node_traces', []) or []))}\n"
        f"Run-dir artifacts: {_compact_path_list(list(node.metadata.get('run_dir_artifacts', []) or []))}\n"
        f"Prior worker output payloads: {json.dumps(node.metadata.get('prior_worker_output_payloads', {}), ensure_ascii=False)}\n"
        f"Allowed capability bundles: {bundles}\n"
        f"Capability details:\n{capability_block}\n"
        f"Implementation style:\n{implementation_block}\n"
        f"Preferred tool surface: {allowed_tools_block}\n"
        f"{metadata_hint}\n"
        f"{_final_response_source_material(node)}"
        f"Node guidance:\n{guidance_block}\n\n"
        "Return JSON only with keys: "
        "status, summary, artifacts, evidence, next_action_hint, failure_reason, spawned_subgraph.\n"
        "status must be one of completed, blocked, failed, partial."
    )


def _compact_path_list(paths: list[str], *, max_items: int = 8) -> list[str]:
    if len(paths) <= max_items:
        return paths
    remaining = len(paths) - max_items
    return [*paths[:max_items], f"... {remaining} more paths omitted"]


def _summarize_worker_output_payload(payload: object) -> object:
    if not isinstance(payload, dict):
        return payload
    node = payload.get("node") if isinstance(payload.get("node"), dict) else {}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    summary = {
        "node_id": node.get("node_id"),
        "title": node.get("title"),
        "status": result.get("status"),
        "summary": result.get("summary"),
        "next_action_hint": result.get("next_action_hint"),
        "failure_reason": result.get("failure_reason"),
        "artifacts_count": len(result.get("artifacts", []) or []),
        "evidence_count": len(result.get("evidence", []) or []),
    }
    if isinstance(result.get("spawned_subgraph"), dict):
        spawned = result.get("spawned_subgraph")
        nodes = spawned.get("nodes") if isinstance(spawned.get("nodes"), list) else []
        edges = spawned.get("edges") if isinstance(spawned.get("edges"), list) else []
        summary["spawned_subgraph_summary"] = {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "node_ids": [
                str(item.get("node_id"))
                for item in nodes[:8]
                if isinstance(item, dict) and item.get("node_id")
            ],
        }
    return summary


def _compact_metadata_hint(metadata: dict[str, Any]) -> str:
    hint_payload = {
        "task_type": metadata.get("task_type"),
        "graph_round": metadata.get("graph_round"),
        "guidance_ids": metadata.get("guidance_ids", []),
        "worker_role": metadata.get("worker_role"),
    }
    if metadata.get("retrieved_cases"):
        retrieved_cases = metadata.get("retrieved_cases")
        if isinstance(retrieved_cases, list):
            hint_payload["retrieved_case_count"] = len(retrieved_cases)
            hint_payload["retrieved_case_types"] = [
                str(item.get("task_type"))
                for item in retrieved_cases[:4]
                if isinstance(item, dict)
            ]
    return f"\nNode metadata summary: {json.dumps(hint_payload, ensure_ascii=False, sort_keys=True)}"


def _final_response_source_material(node: TaskNode) -> str:
    if node.node_id != "final_response":
        return ""
    metadata = node.metadata if isinstance(node.metadata, dict) else {}
    payload = {
        "decision": metadata.get("final_decision"),
        "reason": metadata.get("final_decision_reason"),
        "failed_nodes": metadata.get("failed_nodes", []),
        "task_type": metadata.get("task_type"),
    }
    return (
        "Final response source material:\n"
        f"- Original user task: {metadata.get('task', '')}\n"
        f"- Supervisor decision: {json.dumps(payload, ensure_ascii=False, sort_keys=True)}\n"
        f"- Supervisor final summary:\n{metadata.get('final_summary', '')}\n"
        "End final response source material.\n"
    )


def _parse_worker_result(text: str) -> WorkerResult:
    payload = _extract_json_object(text)
    if payload is None:
        return WorkerResult(status="completed", summary=text.strip() or "Worker completed.")
    status = str(payload.get("status", "completed"))
    if status not in {"completed", "blocked", "failed", "partial"}:
        status = "partial"
    return WorkerResult(
        status=status,  # type: ignore[arg-type]
        summary=str(payload.get("summary", text.strip() or "Worker completed.")),
        artifacts=[str(item) for item in payload.get("artifacts", []) or []],
        evidence=[str(item) for item in payload.get("evidence", []) or []],
        next_action_hint=_optional_str(payload.get("next_action_hint")),
        failure_reason=_optional_str(payload.get("failure_reason")),
        spawned_subgraph=payload.get("spawned_subgraph")
        if isinstance(payload.get("spawned_subgraph"), dict)
        else None,
    )


def _extract_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None
    fenced = re.search(r"```json\s*(\{.*?\})\s*```", stripped, re.DOTALL)
    candidate = fenced.group(1) if fenced else stripped
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        payload = _extract_payload_from_python_repr(candidate)
        if payload is None:
            return None
    return _extract_payload_from_object(payload)


def _extract_payload_from_python_repr(text: str) -> dict[str, Any] | None:
    """Best-effort parse for Python repr payloads from model responses."""

    try:
        payload = ast.literal_eval(text)
    except (SyntaxError, ValueError):
        return None
    return _extract_payload_from_object(payload)


def _extract_payload_from_object(payload: object) -> dict[str, Any] | None:
    """Extract the first worker-result JSON object from nested payload shapes."""

    if isinstance(payload, dict):
        if _looks_like_worker_result_dict(payload):
            return payload
        text_value = payload.get("text")
        if isinstance(text_value, str):
            nested = _extract_json_object(text_value)
            if nested is not None:
                return nested
        content_value = payload.get("content")
        if isinstance(content_value, str):
            nested = _extract_json_object(content_value)
            if nested is not None:
                return nested
        return None
    if isinstance(payload, list):
        for item in payload:
            nested = _extract_payload_from_object(item)
            if nested is not None:
                return nested
    return None


def _looks_like_worker_result_dict(payload: dict[str, Any]) -> bool:
    """Return whether the dict resembles the expected worker result schema."""

    return "status" in payload and (
        "summary" in payload
        or "artifacts" in payload
        or "evidence" in payload
        or "failure_reason" in payload
    )


def _looks_like_user_delivery_node(node_id: str) -> bool:
    normalized = node_id.lower()
    if normalized in {
        "compose_generic",
        "compose_report",
        "final_summarize",
        "final_summary",
        "final_answer",
        "final_response",
    }:
        return True
    return any(
        marker in normalized
        for marker in ("answer", "reply", "response", "deliver")
    )


def _last_ai_text(messages: list[Any]) -> str:
    for message in reversed(messages):
        if getattr(message, "type", None) != "ai":
            continue
        content = getattr(message, "content", "")
        if isinstance(content, str):
            return content
        rendered = str(content).strip()
        if rendered:
            return rendered
    return ""


def _message_text(response: object) -> str:
    if isinstance(response, AIMessage):
        content = response.content
        if isinstance(content, str):
            return content
    content = getattr(response, "content", "")
    if isinstance(content, str):
        return content
    return str(response)


def _extract_task_classifier_payload(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None
    fenced = re.search(r"```json\s*(\{.*?\})\s*```", stripped, re.DOTALL)
    candidate = fenced.group(1) if fenced else stripped
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


async def _render_user_response(
    *,
    task: str,
    task_type: str,
    rounds,
    decision: SupervisorDecision,
    final_summary: str,
    run_dir: Path,
    worker_runner,
) -> str:
    """Use a final LLM pass for the chat-facing answer.

    The full supervisor diagnostics remain in final_summary.md. The finalizer is
    intentionally narrow: it may rewrite and select from existing worker results,
    but it must not add new facts or turn partial work into a claimed success.
    """

    fallback = _select_user_response_candidate(task_type=task_type, rounds=rounds)
    fallback = fallback or final_summary
    finalizer_node = TaskNode(
        node_id="final_response",
        title="Write final user response",
        objective=(
            "Turn the supervisor run result into the final answer shown to the user. "
            "Prefer successful findings and useful next information; keep failures brief "
            "unless they change the answer's reliability."
        ),
        capability_bundles=["summarize", "validate"],
        metadata={
            "task": task,
            "task_type": task_type,
            "final_summary": final_summary,
            "final_decision": decision.decision,
            "final_decision_reason": decision.reason,
            "failed_nodes": list(decision.failed_nodes),
            "fallback_candidate": fallback,
        },
    )
    try:
        result = await _run_worker_and_capture(
            node=finalizer_node,
            graph_round=len(rounds) + 1,
            run_dir=run_dir,
            worker_runner=worker_runner,
        )
    except GraphBubbleUp:
        raise
    except Exception:
        return fallback
    if result.status == "completed" and result.summary.strip():
        return _clean_user_response_text(result.summary)
    return fallback


def _select_user_response_candidate(*, task_type: str, rounds) -> str | None:
    candidates: list[tuple[int, str]] = []
    for round_offset, round_result in enumerate(reversed(rounds)):
        round_penalty = round_offset * 10
        for item_offset, item in enumerate(reversed(round_result.node_results)):
            if item.status != "completed":
                continue
            cleaned = _clean_user_response_text(str(item.summary or ""))
            if not cleaned:
                continue
            score = (
                _user_response_node_score(str(item.node_id), task_type=task_type)
                - round_penalty
                - item_offset
            )
            score += _user_response_text_score(cleaned)
            candidates.append((score, cleaned))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    best_score, best_text = candidates[0]
    if best_score < 0:
        return None
    return best_text


def _user_response_node_score(node_id: str, *, task_type: str) -> int:
    normalized = node_id.lower()
    score = 0
    if normalized.startswith(("init_", "retry_")):
        score -= 60
    if normalized == "summarize":
        score -= 15
    if task_type == "generic":
        score += 10
    if normalized in {"final_answer", "final_response", "final_summarize"}:
        score += 80
    elif normalized in {"compose_generic", "compose_report"}:
        score += 60
    elif _looks_like_user_delivery_node(normalized):
        score += 45
    return score


def _user_response_text_score(text: str) -> int:
    lowered = text.casefold()
    score = 0
    diagnostic_markers = (
        "worker 贡献",
        "blockers",
        "下一步建议",
        "最强已验证结果",
        "supervisor summary",
        "round ",
        "node ",
    )
    if any(marker in lowered for marker in diagnostic_markers):
        score -= 35
    if len(text) <= 500:
        score += 10
    if "\n\n" not in text and text.count("\n") <= 2:
        score += 10
    return score


def _clean_user_response_text(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    quoted = _extract_quoted_final_answer(stripped)
    if quoted is not None:
        return quoted
    return stripped


def _extract_quoted_final_answer(text: str) -> str | None:
    if len(text) > 800:
        return None
    if not any(marker in text for marker in ("回复", "答案", "输出", "交付", "answer", "response")):
        return None
    matches = re.findall(r"[“\"]([^”\"]{1,500})[”\"]", text)
    if not matches:
        return None
    candidate = matches[-1].strip()
    return candidate or None


def _render_final_summary(
    *,
    task: str,
    rounds,
    decision: SupervisorDecision,
    generic_approach: GenericApproach | None = None,
) -> str:
    lines = [
        f"# Supervisor Summary",
        "",
        f"- Task: {task}",
        f"- Decision: {decision.decision}",
        f"- Reason: {decision.reason}",
        "",
        "## Rounds",
    ]
    if generic_approach is not None:
        lines.insert(5, f"- Generic approach: {generic_approach}")
    for round_result in rounds:
        lines.append(
            f"- Round {round_result.graph.round_index}: "
            f"{round_result.completed_count} completed, "
            f"{round_result.failed_count} failed, "
            f"{round_result.blocked_count} blocked, "
            f"{round_result.partial_count} partial"
        )
        for item in round_result.node_results:
            lines.append(f"  - {item.node_id}: {item.status} - {item.summary}")
    return "\n".join(lines) + "\n"


def _write_metric_plan(*, run_dir: Path, selected_tools: list[str]) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    shared_datasets: list[str] = []
    for repo in selected_tools:
        manifest = json.loads((run_dir / "cases" / repo / "manifest.json").read_text(encoding="utf-8"))
        dataset_key = str(manifest.get("dataset_key", ""))
        if dataset_key and dataset_key not in shared_datasets:
            shared_datasets.append(dataset_key)
        rows.append(
            {
                "repo": repo,
                "dataset_key": dataset_key,
                "metric_keys": [str(item) for item in manifest.get("metric_keys", [])],
                "selected_input_files": manifest.get("selected_input_files", {}),
            }
        )
    payload = {
        "run_dir": str(run_dir),
        "selected_tools": selected_tools,
        "shared_datasets": shared_datasets,
        "rows": rows,
    }
    _write_json(run_dir / "metric_plan.json", payload)
    lines = [
        "# Metric Plan",
        "",
        f"- run_dir: `{run_dir}`",
        f"- selected_tools: `{', '.join(selected_tools)}`",
        f"- shared_datasets: `{', '.join(shared_datasets) if shared_datasets else 'none'}`",
        "",
        "## Rows",
        "",
    ]
    lines.extend(
        f"- `{row['repo']}` -> dataset `{row['dataset_key']}`, metrics `{', '.join(row['metric_keys'])}`"
        for row in rows
    )
    (run_dir / "metric_plan.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return payload


def _run_helper_json_command(
    command: list[str],
    *,
    cwd: Path,
    phase: str,
) -> dict[str, object]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    if completed.returncode != 0:
        raise RuntimeError(
            f"{phase} exited with code {completed.returncode}: {stderr or stdout or 'no output'}"
        )
    try:
        payload = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{phase} returned non-JSON output: {stdout or stderr}") from exc
    return {
        "phase": phase,
        "command": command,
        "stdout": payload,
    }


def _locate_repo_root_for_benchmark(*, node: TaskNode, workspace_root: Path) -> Path | None:
    task = str(node.metadata.get("task", "")) if isinstance(node.metadata, dict) else ""
    for match in _TASK_PATH_RE.finditer(task):
        candidate = Path(match.group(1).rstrip("。.,)"))
        repo_root = _walk_to_repo_root(candidate)
        if repo_root is not None:
            return repo_root
    if workspace_root.parent.name == "workspace":
        repo_root = _walk_to_repo_root(workspace_root.parent.parent)
        if repo_root is not None:
            return repo_root
    return _walk_to_repo_root(Path(__file__).resolve().parents[3])


def _walk_to_repo_root(candidate: Path) -> Path | None:
    start = candidate if candidate.is_dir() else candidate.parent
    for path in (start, *start.parents):
        if (path / _BENCHMARK_HELPER_RELATIVE_PATH).exists():
            return path
    return None


def _discover_run_dirs(workspace_root: Path) -> list[Path]:
    candidates = set(workspace_root.glob("orchestration_runs/*"))
    candidates.update(workspace_root.glob("*/orchestration_runs/*"))
    return sorted(path for path in candidates if path.is_dir())


def _case_collection_root(workspace_root: Path) -> Path:
    if workspace_root.parent.name == "workspace":
        return workspace_root.parent
    return workspace_root


def _run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9_.-]+", text.casefold())


def _latest_human_text(state: dict[str, object]) -> str | None:
    messages = state.get("messages") or []
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if getattr(message, "type", None) != "human":
            continue
        content = getattr(message, "content", None)
        if isinstance(content, str) and content.strip():
            return content
        rendered = str(content).strip()
        if rendered:
            return rendered
    return None


def _latest_human_route_mode(state: dict[str, object]) -> str | None:
    """Return any explicit routing override stored on the latest human message."""
    messages = state.get("messages") or []
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if getattr(message, "type", None) != "human":
            continue
        additional_kwargs = getattr(message, "additional_kwargs", None)
        if not isinstance(additional_kwargs, dict):
            return None
        route = additional_kwargs.get("code2workspace_route")
        if isinstance(route, str) and route.strip():
            return route.strip().lower()
        return None
    return None
