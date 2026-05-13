"""Tests for the CLI supervisor runtime."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.errors import GraphInterrupt

from code2workspace.orchestration_runtime import (
    ModelRefusalError,
    TaskGraph,
    TaskNode,
    WorkerResult,
    classify_task_with_model,
    execute_graph_round,
)
from code2workspace_cli.agent import create_cli_agent
from code2workspace_cli.supervisor_runtime import (
    _build_worker_invoke_request,
    _build_benchmark_comparison,
    _build_worker_prompt,
    _invoke_worker_agent,
    _latest_human_route_mode,
    _run_worker_and_capture,
    _parse_worker_result,
    SQLiteCaseIndex,
    SupervisorWorkerRunner,
    build_supervisor_enabled_agent,
    run_supervisor_orchestration,
)


def _make_settings(tmp_path: Path) -> Mock:
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    settings = Mock()
    settings.ensure_agent_dir.return_value = agent_dir
    settings.ensure_user_skills_dir.return_value = skills_dir
    settings.get_project_skills_dir.return_value = None
    settings.get_built_in_skills_dir.return_value = skills_dir
    settings.get_user_agent_md_path.return_value = agent_dir / "AGENTS.md"
    settings.get_project_agent_md_path.return_value = []
    settings.get_user_agents_dir.return_value = tmp_path / "agents"
    settings.get_project_agents_dir.return_value = None
    settings.model_name = None
    settings.model_provider = None
    settings.model_unsupported_modalities = frozenset()
    settings.model_context_limit = None
    settings.project_root = None
    settings.get_user_agent_skills_dir.return_value = skills_dir
    settings.get_project_agent_skills_dir.return_value = None
    settings.get_user_claude_skills_dir.return_value = tmp_path / "claude"
    settings.get_project_claude_skills_dir.return_value = None
    return settings


@pytest.mark.asyncio
async def test_classify_task_for_supervisor_uses_llm_when_confident() -> None:
    model = Mock()
    model.ainvoke = AsyncMock(
        return_value=AIMessage(
            content='{"task_type":"report","confidence":0.91,"reason":"Formal risk assessment request.","matched_signals":["风险评估","WHO 风格"]}'
        )
    )

    classification, details = await classify_task_with_model(
        model=model,
        task="请按 WHO 风格写一份 XFG.1.1 毒株风险评估报告。",
    )

    assert classification.primary_type == "report"
    assert details["source"] == "llm_classifier"
    assert details["confidence"] == pytest.approx(0.91)


@pytest.mark.asyncio
async def test_classify_task_for_supervisor_falls_back_to_rules_on_invalid_output() -> None:
    model = Mock()
    model.ainvoke = AsyncMock(return_value=AIMessage(content="not json"))

    classification, details = await classify_task_with_model(
        model=model,
        task="基于仓库中涉及的真实测试数据和任务脚本信息，仓库地址：https://github.com/ablab/spades",
    )

    assert classification.primary_type == "github2workspace"
    assert details["source"] == "rules_fallback"
    assert details["llm_error"] == "invalid_classifier_output"
    assert details["llm_attempts"][0]["content_preview"] == "not json"


@pytest.mark.asyncio
async def test_build_supervisor_enabled_agent_returns_refusal_message_from_classifier(
    tmp_path: Path,
) -> None:
    base_agent = AsyncMock()
    fallback_agent = AsyncMock()

    class _Classifier:
        async def ainvoke(self, _messages):
            return AIMessage(
                content="",
                response_metadata={
                    "model_name": "claude-sonnet-4-6",
                    "model_provider": "anthropic",
                    "stop_reason": "refusal",
                },
            )

    agent = build_supervisor_enabled_agent(
        base_agent=base_agent,
        fallback_agent=fallback_agent,
        workspace_root=tmp_path,
        classifier_model=_Classifier(),
        enable_generic_ask_user=False,
    )

    result = await agent.ainvoke({"messages": [HumanMessage(content="test task")]})

    assert result["messages"][-1].content == "The model refused to answer this request."


@pytest.mark.asyncio
async def test_run_supervisor_orchestration_writes_artifacts_and_replans(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace" / "20260430120000"
    workspace.mkdir(parents=True)
    attempts = {"build": 0}

    async def worker_runner(node: TaskNode) -> WorkerResult:
        if node.node_id == "inspect":
            return WorkerResult(status="completed", summary="inspect ok")
        if node.node_id in {"build", "retry_build"}:
            attempts["build"] += 1
            if attempts["build"] == 1:
                return WorkerResult(
                    status="failed",
                    summary="build failed",
                    failure_reason="docker_error",
                )
            return WorkerResult(status="completed", summary="build ok")
        if node.node_id == "wdl":
            return WorkerResult(status="completed", summary="wdl ok")
        return WorkerResult(status="completed", summary=f"{node.node_id} ok")

    result = await run_supervisor_orchestration(
        task="把这个 GitHub 仓库变成有工作流的可运行 workspace：https://github.com/ablab/spades",
        workspace_root=workspace,
        worker_runner=worker_runner,
    )

    run_dir = result.run_dir
    assert (run_dir / "request.json").exists()
    assert (run_dir / "retrieved_cases.json").exists()
    assert (run_dir / "graph_round_1.json").exists()
    assert (run_dir / "graph_round_2.json").exists()
    assert (run_dir / "final_decision.json").exists()
    assert (run_dir / "final_summary.md").exists()
    assert (run_dir / "worker_outputs" / "build.json").exists()
    assert (run_dir / "worker_outputs" / "retry_build.json").exists()

    final_payload = json.loads((run_dir / "final_decision.json").read_text())
    assert final_payload["decision"] == "stop"
    assert result.round_count == 2


@pytest.mark.asyncio
async def test_run_supervisor_orchestration_supports_report_tasks(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace" / "20260430130000"
    workspace.mkdir(parents=True)

    async def worker_runner(node: TaskNode) -> WorkerResult:
        return WorkerResult(
            status="completed",
            summary=f"{node.node_id} ok",
            artifacts=[f"{node.node_id}.md"],
        )

    result = await run_supervisor_orchestration(
        task="写一份近期全球呼吸系统病原流行情况报告（新冠、流感、RSV），附 evidence layers。",
        workspace_root=workspace,
        worker_runner=worker_runner,
    )

    run_dir = result.run_dir
    final_payload = json.loads((run_dir / "final_decision.json").read_text())
    graph_payload = json.loads((run_dir / "graph_round_1.json").read_text())

    assert final_payload["task_type"] == "report"
    assert final_payload["decision"] == "stop"
    assert graph_payload["task_type"] == "report"
    assert (run_dir / "worker_outputs" / "compose_report.json").exists()


@pytest.mark.asyncio
async def test_run_supervisor_orchestration_supports_generic_tasks(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace" / "20260430133000"
    workspace.mkdir(parents=True)

    async def worker_runner(node: TaskNode) -> WorkerResult:
        assert node.node_id in {
            "init_generic",
            "compose_generic",
            "summarize",
            "final_response",
        }
        if node.node_id == "init_generic":
            return WorkerResult(
                status="completed",
                summary="planned flexible direct-answer graph",
                spawned_subgraph={
                    "nodes": [
                        {
                            "node_id": "compose_generic",
                            "title": "Compose direct answer",
                            "objective": "Answer the user directly.",
                            "capability_bundles": ["summarize", "validate"],
                        },
                        {
                            "node_id": "summarize",
                            "title": "Summarize",
                            "objective": "Summarize the answer.",
                            "capability_bundles": ["summarize"],
                        },
                    ],
                    "edges": [{"source": "compose_generic", "target": "summarize"}],
                },
            )
        return WorkerResult(status="completed", summary=f"{node.node_id} ok")

    result = await run_supervisor_orchestration(
        task="帮我整理这个任务的执行思路并给出下一步建议。",
        workspace_root=workspace,
        worker_runner=worker_runner,
    )

    run_dir = result.run_dir
    graph_1_payload = json.loads((run_dir / "graph_round_1.json").read_text())
    graph_2_payload = json.loads((run_dir / "graph_round_2.json").read_text())
    final_payload = json.loads((run_dir / "final_decision.json").read_text())
    assert graph_1_payload["task_type"] == "generic"
    assert [node["node_id"] for node in graph_1_payload["nodes"]] == [
        "init_generic",
    ]
    assert [node["node_id"] for node in graph_2_payload["nodes"]] == [
        "compose_generic",
        "summarize",
    ]
    assert graph_2_payload["metadata"]["planner_generated"] is True
    assert final_payload["generic_approach"] is None
    assert not (run_dir / "generic_approach_options.json").exists()
    assert not (run_dir / "generic_approach_selection.json").exists()


@pytest.mark.asyncio
async def test_run_supervisor_orchestration_returns_user_facing_response(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace" / "20260430134500"
    workspace.mkdir(parents=True)

    async def worker_runner(node: TaskNode) -> WorkerResult:
        if node.node_id == "init_generic":
            return WorkerResult(
                status="completed",
                summary="planned greeting graph",
                spawned_subgraph={
                    "nodes": [
                        {
                            "node_id": "direct_greeting_reply",
                            "title": "Generate greeting",
                            "objective": "Reply to the greeting.",
                            "capability_bundles": ["summarize"],
                        },
                        {
                            "node_id": "final_summarize",
                            "title": "Final answer",
                            "objective": "Produce the final user-facing answer.",
                            "capability_bundles": ["summarize", "validate"],
                        },
                    ],
                    "edges": [
                        {
                            "source": "direct_greeting_reply",
                            "target": "final_summarize",
                        }
                    ],
                },
            )
        if node.node_id == "direct_greeting_reply":
            return WorkerResult(
                status="completed",
                summary='已生成回复："你好！很高兴见到你。"',
            )
        if node.node_id == "final_summarize":
            return WorkerResult(status="completed", summary="你好！很高兴见到你。")
        if node.node_id == "final_response":
            return WorkerResult(status="completed", summary="你好！很高兴见到你。")
        return WorkerResult(
            status="completed",
            summary=(
                "最强已验证结果：最终回复为“你好！很高兴见到你。”\n\n"
                "worker 贡献：已完成。\n\n下一步建议：直接发送。"
            ),
        )

    result = await run_supervisor_orchestration(
        task="你好",
        workspace_root=workspace,
        worker_runner=worker_runner,
    )

    assert result.user_response == "你好！很高兴见到你。"
    assert "Supervisor Summary" in result.final_summary
    assert (result.run_dir / "final_response.md").read_text(encoding="utf-8") == (
        "你好！很高兴见到你。"
    )


@pytest.mark.asyncio
async def test_run_supervisor_orchestration_stops_on_worker_refusal(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace" / "20260430134600"
    workspace.mkdir(parents=True)

    async def worker_runner(node: TaskNode) -> WorkerResult:
        if node.node_id == "init_generic":
            return WorkerResult(
                status="completed",
                summary="planned one-node graph",
                spawned_subgraph={
                    "nodes": [
                        {
                            "node_id": "worker_solution",
                            "title": "Solve",
                            "objective": "Solve the task.",
                            "capability_bundles": ["summarize"],
                        }
                    ],
                    "edges": [],
                },
            )
        raise ModelRefusalError(
            message="The model refused to answer this request.",
            stage=f"worker:{node.node_id}",
        )

    result = await run_supervisor_orchestration(
        task="帮我做一个会被拒绝的请求。",
        workspace_root=workspace,
        worker_runner=worker_runner,
    )

    assert result.user_response == "The model refused to answer this request."
    assert result.final_decision.reason == "model_refusal:worker:worker_solution"
    assert (result.run_dir / "final_response.md").read_text(encoding="utf-8") == (
        "The model refused to answer this request."
    )


def test_sqlite_case_index_rebuild_and_retrieve(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    run_dir = workspace_root / "20260430120000" / "orchestration_runs" / "run-a"
    run_dir.mkdir(parents=True)
    (run_dir / "request.json").write_text(
        json.dumps({"task": "benchmark spades megahit covid assembly"}),
        encoding="utf-8",
    )
    (run_dir / "final_decision.json").write_text(
        json.dumps({"decision": "stop", "task_type": "benchmark"}),
        encoding="utf-8",
    )
    (run_dir / "final_summary.md").write_text(
        "spades and megahit benchmark summary",
        encoding="utf-8",
    )

    index = SQLiteCaseIndex(workspace_root / "orchestration_case_index.sqlite3")
    index.rebuild_from_workspace_root(workspace_root)
    hits = index.search("megahit benchmark", limit=3)

    assert hits
    assert hits[0].task_type == "benchmark"
    assert "megahit" in hits[0].summary


def test_latest_human_route_mode_reads_fallback_override() -> None:
    state = {
        "messages": [
            HumanMessage(content="hello"),
            HumanMessage(
                content="plain chat",
                additional_kwargs={"code2workspace_route": "fallback"},
            ),
        ]
    }

    assert _latest_human_route_mode(state) == "fallback"


@pytest.mark.asyncio
async def test_run_worker_and_capture_logs_and_reraises_interrupt(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "orchestration_runs" / "run-interrupt"
    run_dir.mkdir(parents=True)
    node = TaskNode(
        node_id="execute_task",
        title="Execute",
        objective="execute",
        capability_bundles=["validate"],
    )

    async def worker_runner(node: TaskNode) -> WorkerResult:  # noqa: ARG001
        raise GraphInterrupt(())

    with pytest.raises(GraphInterrupt):
        await _run_worker_and_capture(
            node=node,
            graph_round=2,
            run_dir=run_dir,
            worker_runner=worker_runner,
        )

    activity = (run_dir / "tool_activity.jsonl").read_text(encoding="utf-8")
    assert '"event": "node_started"' in activity
    assert '"event": "node_interrupted"' in activity


def test_create_cli_agent_wraps_default_agent_with_supervisor_runtime(
    tmp_path: Path,
) -> None:
    mock_wrapped_agent = Mock()
    mock_wrapped_agent.with_config.return_value = mock_wrapped_agent
    fake_model = Mock()
    fake_model.profile = {"max_input_tokens": 200000}
    resolved_report_model = Mock()
    resolved_report_model.profile = {"max_input_tokens": 1000000}
    built_agents: list[Mock] = []

    def _fake_workspace_agent(**_: object) -> Mock:
        agent = Mock()
        agent.with_config.return_value = agent
        built_agents.append(agent)
        return agent

    with (
        patch.dict("os.environ", {}, clear=True),
        patch("code2workspace_cli.agent.settings", _make_settings(tmp_path)),
        patch("code2workspace_cli.agent.create_workspace_agent", side_effect=_fake_workspace_agent),
        patch(
            "code2workspace_cli.agent._resolve_report_worker_model",
            return_value=resolved_report_model,
        ),
        patch("code2workspace._models.init_chat_model", return_value=fake_model),
        patch("code2workspace.middleware.summarization.create_summarization_tool_middleware"),
        patch(
            "code2workspace_cli.agent.build_supervisor_enabled_agent",
            return_value=mock_wrapped_agent,
        ) as mock_build,
    ):
        agent, _backend = create_cli_agent(
            model="fake-model",
            assistant_id="test",
            enable_memory=False,
            enable_skills=False,
            enable_shell=False,
        )

    assert agent is mock_wrapped_agent
    assert mock_build.call_count == 1
    assert len(built_agents) == 4
    assert mock_build.call_args.kwargs["base_agent"] is built_agents[0]
    assert mock_build.call_args.kwargs["worker_agent"] is built_agents[1]
    assert mock_build.call_args.kwargs["fallback_agent"] is built_agents[3]
    worker_subagents = mock_build.call_args.kwargs["worker_subagents"]
    assert len(worker_subagents) == 7
    assert {item.runnable for item in worker_subagents} == {built_agents[2]}
    assert any(
        item.name == "report:monitoring_lane"
        and item.node_ids == frozenset({"monitoring_lane", "retry_monitoring_lane"})
        for item in worker_subagents
    )


def test_create_cli_agent_allows_report_worker_model_env_overrides(
    tmp_path: Path,
) -> None:
    mock_wrapped_agent = Mock()
    mock_wrapped_agent.with_config.return_value = mock_wrapped_agent
    fake_model = Mock()
    fake_model.profile = {"max_input_tokens": 200000}
    created_models: list[object] = []
    built_agents: list[Mock] = []
    resolved_models = {
        "openai_paid:gpt-5.4": Mock(name="resolved-default-report-model"),
        "openai_paid:gpt-5.4-compose": Mock(name="resolved-compose-report-model"),
        "openai_paid:gpt-5.4-final": Mock(name="resolved-final-report-model"),
    }
    for model in resolved_models.values():
        model.profile = {"max_input_tokens": 1000000}

    def _fake_workspace_agent(**kwargs: object) -> Mock:
        agent = Mock()
        agent.with_config.return_value = agent
        built_agents.append(agent)
        created_models.append(kwargs["model"])
        return agent

    with (
        patch.dict(
            "os.environ",
            {
                "CODE2WORKSPACE_SUPERVISOR_REPORT_MODEL": "openai_paid:gpt-5.4",
                "CODE2WORKSPACE_SUPERVISOR_REPORT_COMPOSE_MODEL": "openai_paid:gpt-5.4-compose",
                "CODE2WORKSPACE_SUPERVISOR_REPORT_FINAL_RESPONSE_MODEL": "openai_paid:gpt-5.4-final",
            },
            clear=True,
        ),
        patch("code2workspace_cli.agent.settings", _make_settings(tmp_path)),
        patch("code2workspace_cli.agent.create_workspace_agent", side_effect=_fake_workspace_agent),
        patch(
            "code2workspace_cli.agent._resolve_report_worker_model",
            side_effect=lambda spec: resolved_models[spec],
        ) as mock_resolve_report_model,
        patch("code2workspace._models.init_chat_model", return_value=fake_model),
        patch("code2workspace.middleware.summarization.create_summarization_tool_middleware"),
        patch(
            "code2workspace_cli.agent.build_supervisor_enabled_agent",
            return_value=mock_wrapped_agent,
        ) as mock_build,
    ):
        create_cli_agent(
            model="fake-model",
            assistant_id="test",
            enable_memory=False,
            enable_skills=False,
            enable_shell=False,
        )

    assert created_models.count(resolved_models["openai_paid:gpt-5.4"]) == 1
    assert resolved_models["openai_paid:gpt-5.4-compose"] in created_models
    assert resolved_models["openai_paid:gpt-5.4-final"] in created_models
    assert [item.args[0] for item in mock_resolve_report_model.call_args_list] == [
        "openai_paid:gpt-5.4",
        "openai_paid:gpt-5.4-compose",
        "openai_paid:gpt-5.4-final",
    ]
    worker_subagents = mock_build.call_args.kwargs["worker_subagents"]
    by_name = {item.name: item for item in worker_subagents}
    assert by_name["report:compose_report"].runnable is built_agents[3]
    assert by_name["report:final_response"].runnable is built_agents[4]


def test_resolve_report_worker_model_falls_back_from_openai_paid_alias() -> None:
    primary_error = Exception("unsupported provider")
    resolved_fallback = Mock()
    resolved_fallback.profile = {"max_input_tokens": 1000000}

    with patch("code2workspace_cli.config.create_model") as mock_create_model:
        mock_create_model.side_effect = [
            __import__("code2workspace_cli.model_config", fromlist=["ModelConfigError"]).ModelConfigError(primary_error),
            Mock(model=resolved_fallback),
        ]

        model = __import__("code2workspace_cli.agent", fromlist=["_resolve_report_worker_model"])._resolve_report_worker_model(
            "openai_paid:gpt-5.4"
        )

    assert model is resolved_fallback
    assert [call.args[0] for call in mock_create_model.call_args_list] == [
        "openai_paid:gpt-5.4",
        "openai:gpt-5.4",
    ]


def test_build_worker_prompt_uses_capability_registry_and_guidance() -> None:
    node = TaskNode(
        node_id="inspect",
        title="Inspect repository",
        objective="Inspect repository assets",
        capability_bundles=["repo_fetch", "validate"],
        metadata={
            "guidance_ids": ["github2workspace_pipeline"],
            "task": "把这个 GitHub 仓库变成有工作流的可运行 workspace：https://github.com/ablab/spades",
            "task_paths": ["/tmp/supervisor-workspace"],
            "run_dir": "/tmp/supervisor-workspace/orchestration_runs/run-1",
            "prior_worker_outputs": ["/tmp/supervisor-workspace/orchestration_runs/run-1/worker_outputs/register.json"],
            "prior_node_traces": ["/tmp/supervisor-workspace/orchestration_runs/run-1/node_traces/register.json"],
        },
    )

    prompt = _build_worker_prompt(
        node=node,
        workspace_root=Path("/tmp/supervisor-workspace"),
    )

    assert "Preferred tool surface:" in prompt
    assert "execute" in prompt
    assert "read_file" in prompt
    assert "Capability details:" in prompt
    assert "repo_fetch" in prompt
    assert "validate" in prompt
    assert "materialize the repository into the current workspace" in prompt
    assert "bundled datasets" in prompt
    assert "Earlier nodes may spend time discovering the safest real validation path" in prompt
    assert "Original task:" in prompt
    assert "Task paths:" in prompt
    assert "Run directory:" in prompt
    assert "Prior worker outputs:" in prompt
    assert "Node metadata summary:" in prompt


def test_build_worker_prompt_compacts_large_prior_worker_payloads() -> None:
    node = TaskNode(
        node_id="compose_generic",
        title="Compose generic answer",
        objective="Compose a normal long-form answer",
        capability_bundles=["summarize", "validate"],
        metadata={
            "task": "下一波新冠/流感阳性率的高峰会在什么时间？",
            "task_type": "generic",
            "graph_round": 2,
            "prior_worker_outputs": [f"/tmp/run/worker_outputs/{i}.json" for i in range(12)],
            "prior_node_traces": [f"/tmp/run/node_traces/{i}.json" for i in range(12)],
            "run_dir_artifacts": [f"/tmp/run/artifacts/{i}.json" for i in range(12)],
            "prior_worker_output_payloads": {
                "worker_context.json": {
                    "node_id": "worker_context",
                    "status": "completed",
                    "summary": "context ok",
                    "artifacts_count": 2,
                    "evidence_count": 5,
                }
            },
            "retrieved_cases": [
                {"task_type": "generic", "summary": "a" * 4000},
                {"task_type": "report", "summary": "b" * 4000},
            ],
        },
    )

    prompt = _build_worker_prompt(
        node=node,
        workspace_root=Path("/tmp/supervisor-workspace"),
    )

    assert "... 4 more paths omitted" in prompt
    assert "retrieved_case_count" in prompt
    assert "retrieved_case_types" in prompt
    assert '"status": "completed"' in prompt
    assert '"summary": "context ok"' in prompt
    assert "where the evidence came from" in prompt
    assert "direct evidence, inferred evidence, or unresolved gaps" in prompt
    assert "aaaa" not in prompt
    assert "bbbb" not in prompt


def test_build_worker_prompt_final_response_preserves_evidence_sources() -> None:
    node = TaskNode(
        node_id="final_response",
        title="Write final user response",
        objective="Turn the supervisor run result into the final answer shown to the user.",
        capability_bundles=["summarize", "validate"],
        metadata={
            "task": "写一份风险研判",
            "task_type": "report",
            "final_summary": "summary",
            "final_decision": "stop",
            "final_decision_reason": "done",
            "failed_nodes": [],
            "fallback_candidate": "candidate",
        },
    )

    prompt = _build_worker_prompt(
        node=node,
        workspace_root=Path("/tmp/supervisor-workspace"),
    )

    assert "brief evidence-source explanation" in prompt
    assert "source categories" in prompt
    assert "direct-vs-inferred evidence distinctions" in prompt


def test_build_worker_prompt_includes_wdl_node_guidance() -> None:
    node = TaskNode(
        node_id="wdl",
        title="Run WDL workflow",
        objective="Generate or repair WDL inputs and run miniwdl workflow validation.",
        capability_bundles=["wdl_run", "validate"],
        metadata={
            "guidance_ids": ["github2workspace_pipeline"],
            "task": (
                "基于仓库中涉及的真实测试数据和任务脚本信息，完成仓库镜像的构建与基础验证；"
                "编写并保存 spades_Dockerfile，构建镜像 spades，并在容器内成功运行至少一个基于真实数据的测试案例，将结果存入 results/docker_test。"
                "随后编写 spades.wdl，runtime 指定为 spades，实际运行 scripts/run-miniwdl.sh 直到出现 Succeeded，"
                "并把结果分别存入 results/wdl_result 与 results/wdl_file。仓库地址：https://github.com/ablab/spades"
            ),
            "run_dir": "/tmp/supervisor-workspace/orchestration_runs/run-2",
            "prior_worker_outputs": [
                "/tmp/supervisor-workspace/orchestration_runs/run-2/worker_outputs/inspect.json",
                "/tmp/supervisor-workspace/orchestration_runs/run-2/worker_outputs/build.json",
            ],
            "run_dir_artifacts": [
                "/tmp/supervisor-workspace/spades_Dockerfile",
                "/tmp/supervisor-workspace/results/docker_test/container_test.log",
            ],
        },
    )

    prompt = _build_worker_prompt(
        node=node,
        workspace_root=Path("/tmp/supervisor-workspace"),
    )

    assert "spades.wdl" in prompt
    assert "results/wdl_result" in prompt
    assert "Succeeded" in prompt


def test_parse_worker_result_extracts_direct_json() -> None:
    result = _parse_worker_result(
        '{"status":"partial","summary":"need inputs","failure_reason":"missing data"}'
    )

    assert result.status == "partial"
    assert result.summary == "need inputs"
    assert result.failure_reason == "missing data"


def test_parse_worker_result_extracts_embedded_json_from_python_repr_list() -> None:
    text = """[
{'id': 'rs_x', 'summary': [], 'type': 'reasoning'},
{'type': 'text', 'text': '{"status":"partial","summary":"registered but not ready","failure_reason":"inputs missing"}'}
]"""

    result = _parse_worker_result(text)

    assert result.status == "partial"
    assert result.summary == "registered but not ready"
    assert result.failure_reason == "inputs missing"


def test_parse_worker_result_preserves_spawned_subgraph() -> None:
    result = _parse_worker_result(
        '{"status":"completed","summary":"registered","spawned_subgraph":{"selected_tools":["canu","Flye"],"dataset_keys":["long-read-ecoli-pacbio"]}}'
    )

    assert result.status == "completed"
    assert result.spawned_subgraph == {
        "selected_tools": ["canu", "Flye"],
        "dataset_keys": ["long-read-ecoli-pacbio"],
    }


@pytest.mark.asyncio
async def test_invoke_worker_agent_uses_messages_mode_by_default(
    tmp_path: Path,
) -> None:
    node = TaskNode(
        node_id="analyze_task",
        title="Analyze task",
        objective="Analyze the request",
        capability_bundles=["plan", "validate"],
        metadata={"task": "帮我分析一下这个问题", "task_type": "generic"},
    )
    agent = AsyncMock()
    agent.ainvoke.return_value = {
        "messages": [
            AIMessage(
                content='{"status":"completed","summary":"ok","artifacts":[],"evidence":[]}'
            )
        ]
    }

    await _invoke_worker_agent(
        agent=agent,
        node=node,
        workspace_root=tmp_path,
    )

    payload = agent.ainvoke.await_args.args[0]
    messages = payload["messages"]
    assert len(messages) == 2
    assert isinstance(messages[0], SystemMessage)
    assert "You are a generic worker node executor." in str(messages[0].content)
    assert isinstance(messages[1], HumanMessage)
    assert "return the required JSON only" in str(messages[1].content)
    assert agent.ainvoke.await_args.kwargs == {}


def test_build_worker_invoke_request_uses_messages_mode_by_default(monkeypatch) -> None:
    monkeypatch.delenv("CODE2WORKSPACE_SUPERVISOR_WORKER_PROMPT_MODE", raising=False)

    payload, kwargs = _build_worker_invoke_request("Worker prompt")

    assert len(payload["messages"]) == 2
    assert isinstance(payload["messages"][0], SystemMessage)
    assert isinstance(payload["messages"][1], HumanMessage)
    assert kwargs == {}


def test_build_worker_invoke_request_uses_context_mode_when_requested(monkeypatch) -> None:
    monkeypatch.setenv("CODE2WORKSPACE_SUPERVISOR_WORKER_PROMPT_MODE", "context")

    payload, kwargs = _build_worker_invoke_request("Worker prompt")

    assert len(payload["messages"]) == 1
    assert isinstance(payload["messages"][0], HumanMessage)
    assert kwargs["context"]["system_prompt"] == "Worker prompt"


@pytest.mark.asyncio
async def test_invoke_worker_agent_retries_transient_errors(
    tmp_path: Path,
) -> None:
    class APIConnectionError(Exception):
        pass

    node = TaskNode(
        node_id="analyze_task",
        title="Analyze task",
        objective="Analyze the request",
        capability_bundles=["plan", "validate"],
        metadata={"task": "帮我分析一下这个问题", "task_type": "generic"},
    )
    agent = AsyncMock()
    agent.ainvoke.side_effect = [
        APIConnectionError("Connection error"),
        {
            "messages": [
                AIMessage(
                    content='{"status":"completed","summary":"ok","artifacts":[],"evidence":[]}'
                )
            ]
        },
    ]

    result = await _invoke_worker_agent(
        agent=agent,
        node=node,
        workspace_root=tmp_path,
    )

    assert result.status == "completed"
    assert agent.ainvoke.await_count == 2


@pytest.mark.asyncio
async def test_invoke_worker_agent_records_internal_tool_calls(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "orchestration_runs" / "run-a"
    run_dir.mkdir(parents=True)
    node = TaskNode(
        node_id="worker_solution",
        title="Solve",
        objective="Use a tool and answer",
        capability_bundles=["summarize", "validate"],
        metadata={
            "task": "inspect files",
            "task_type": "generic",
            "run_dir": str(run_dir),
        },
    )
    agent = AsyncMock()
    agent.ainvoke.return_value = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {"file_path": "README.md"},
                        "id": "call_read",
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(
                content="README contents",
                tool_call_id="call_read",
                status="success",
            ),
            AIMessage(
                content='{"status":"completed","summary":"done","artifacts":[],"evidence":[]}'
            ),
        ]
    }

    result = await _invoke_worker_agent(
        agent=agent,
        node=node,
        workspace_root=tmp_path,
    )

    assert result.status == "completed"
    activity = (run_dir / "tool_activity.jsonl").read_text(encoding="utf-8")
    assert '"event": "worker_tool_call"' in activity
    assert '"tool_name": "read_file"' in activity
    assert '"args_preview": "{\\"file_path\\": \\"README.md\\"}"' in activity
    assert '"event": "worker_tool_result"' in activity
    assert '"result_preview": "README contents"' in activity


@pytest.mark.asyncio
async def test_supervisor_worker_runner_prefers_worker_subagent(
    tmp_path: Path,
) -> None:
    node = TaskNode(
        node_id="worker_solution",
        title="Solve",
        objective="Solve the request",
        capability_bundles=["summarize", "validate"],
        metadata={"task": "hello", "task_type": "generic"},
    )
    base_agent = AsyncMock()
    worker_agent = AsyncMock()
    worker_agent.ainvoke.return_value = {
        "messages": [
            AIMessage(
                content='{"status":"completed","summary":"from worker subagent","artifacts":[],"evidence":[]}'
            )
        ]
    }
    runner = SupervisorWorkerRunner(
        base_agent=base_agent,
        default_subagent=worker_agent,
        workspace_root=tmp_path,
    )

    result = await runner.run(node)

    assert result.status == "completed"
    assert result.summary == "from worker subagent"
    worker_agent.ainvoke.assert_awaited_once()
    base_agent.ainvoke.assert_not_called()


def test_build_benchmark_comparison_handles_mixed_metric_winners() -> None:
    comparison = _build_benchmark_comparison(
        [
            {
                "repo": "megahit",
                "success": True,
                "metrics": {
                    "contig_count": 472,
                    "assembly_size": 4530520,
                    "n50": 18360,
                },
            },
            {
                "repo": "spades",
                "success": True,
                "metrics": {
                    "contig_count": 1191,
                    "assembly_size": 4582516,
                    "n50": 24099,
                },
            },
        ]
    )

    assert comparison == {
        "best_n50": "spades",
        "lowest_contig_count": "megahit",
        "largest_assembly_size": "spades",
        "overall": (
            "Overall favors spades by N50/assembly-size strength, "
            "while megahit has the lower contig_count."
        ),
    }


@pytest.mark.asyncio
async def test_invoke_worker_agent_uses_deterministic_benchmark_register_helper(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    run_dir = workspace_root / "orchestration_runs" / "run-1"
    run_dir.mkdir(parents=True)
    node = TaskNode(
        node_id="register",
        title="Register benchmark cases",
        objective="Confirm benchmark inputs, register each tool/case pair, and verify staged constraints before execution.",
        capability_bundles=["plan", "task_manage", "validate"],
        metadata={
            "task_type": "benchmark",
            "task": "在本地 benchmark 目录里只选择 spades 和 megahit，先完成 register。",
            "run_dir": str(run_dir),
            "selected_tools": ["spades", "megahit"],
        },
    )
    agent = AsyncMock()
    agent.ainvoke.side_effect = AssertionError("register helper path should bypass the model")

    def fake_register_helper(command: list[str], *, cwd: Path, phase: str) -> dict[str, object]:
        if phase == "resolve-datasets":
            (run_dir / "dataset_resolution.json").write_text("{}", encoding="utf-8")
            (run_dir / "dataset_resolution.md").write_text("# datasets\n", encoding="utf-8")
            return {"phase": phase, "command": command, "stdout": {}}
        if phase == "init":
            (run_dir / "benchmark_plan.json").write_text("{}", encoding="utf-8")
            (run_dir / "benchmark_plan.md").write_text("# plan\n", encoding="utf-8")
            return {"phase": phase, "command": command, "stdout": {"case_order": ["spades", "megahit"]}}
        repo = phase.split(":", 1)[1]
        case_dir = run_dir / "cases" / repo
        case_dir.mkdir(parents=True, exist_ok=True)
        if phase.startswith("prepare-case:"):
            (case_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "dataset_key": "short-read-ecoli-srr001666",
                        "metric_keys": ["contig_count", "assembly_size", "n50"],
                        "selected_input_files": {"reads_1": "/tmp/r1", "reads_2": "/tmp/r2"},
                    }
                ),
                encoding="utf-8",
            )
            (case_dir / "dataset_selection.json").write_text("{}", encoding="utf-8")
            (case_dir / "dataset_manifest.json").write_text("{}", encoding="utf-8")
            (case_dir / "agent_task.md").write_text("# task\n", encoding="utf-8")
            return {"phase": phase, "command": command, "stdout": {"repo": repo}}
        if phase.startswith("execution-ready:"):
            (case_dir / "execution_ready.json").write_text(
                json.dumps(
                    {
                        "ready": True,
                        "runtime_image": f"benchmark/{repo}",
                        "wdl_path": f"/tmp/{repo}.wdl",
                        "inputs_json_path": f"/tmp/{repo}.json",
                    }
                ),
                encoding="utf-8",
            )
            return {"phase": phase, "command": command, "stdout": {"repo": repo}}
        raise AssertionError(f"unexpected phase: {phase}")

    with patch("code2workspace_cli.supervisor_runtime._run_helper_json_command", side_effect=fake_register_helper):
        result = await _invoke_worker_agent(
            agent=agent,
            node=node,
            workspace_root=workspace_root,
        )

    assert result.status == "completed"
    assert "spades" in result.summary
    assert result.spawned_subgraph == {
        "selected_tools": ["spades", "megahit"],
        "dataset_keys": ["short-read-ecoli-srr001666"],
        "register_report": str(run_dir / "register_report.json"),
    }
    assert (run_dir / "benchmark_plan.json").exists()
    assert (run_dir / "dataset_resolution.json").exists()
    assert (run_dir / "metric_plan.json").exists()
    assert (run_dir / "cases" / "spades" / "execution_ready.json").exists()
    assert (run_dir / "cases" / "megahit" / "execution_ready.json").exists()


@pytest.mark.asyncio
async def test_benchmark_register_rejects_tools_that_violate_exclusions(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    run_dir = workspace_root / "orchestration_runs" / "run-exclude"
    run_dir.mkdir(parents=True)
    node = TaskNode(
        node_id="register",
        title="Register benchmark cases",
        objective="Confirm benchmark inputs, register each tool/case pair, and verify staged constraints before execution.",
        capability_bundles=["plan", "task_manage", "validate"],
        metadata={
            "task_type": "benchmark",
            "task": "并行跑多个算子，但不要选 spades 和 megahit。",
            "run_dir": str(run_dir),
            "selected_tools": ["spades", "megahit"],
            "excluded_tools": ["spades", "megahit"],
        },
    )
    agent = AsyncMock()
    agent.ainvoke.side_effect = AssertionError("register helper path should bypass the model")

    result = await _invoke_worker_agent(
        agent=agent,
        node=node,
        workspace_root=workspace_root,
    )

    assert result.status == "failed"
    assert result.failure_reason == "selected_tools_violate_exclusion_constraint"
    assert "spades" in result.summary
    assert "megahit" in result.summary


@pytest.mark.asyncio
async def test_benchmark_register_fails_fast_when_no_tools_remain_after_exclusion(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    run_dir = workspace_root / "orchestration_runs" / "run-empty"
    run_dir.mkdir(parents=True)
    node = TaskNode(
        node_id="register",
        title="Register benchmark cases",
        objective="Confirm benchmark inputs, register each tool/case pair, and verify staged constraints before execution.",
        capability_bundles=["plan", "task_manage", "validate"],
        metadata={
            "task_type": "benchmark",
            "task": "不要选 spades 和 megahit。",
            "run_dir": str(run_dir),
            "selected_tools": [],
            "excluded_tools": ["spades", "megahit"],
        },
    )
    agent = AsyncMock()
    agent.ainvoke.side_effect = AssertionError("empty register case should still bypass the model")

    result = await _invoke_worker_agent(
        agent=agent,
        node=node,
        workspace_root=workspace_root,
    )

    assert result.status == "failed"
    assert result.failure_reason == "missing_selected_tools"


@pytest.mark.asyncio
async def test_benchmark_register_reports_url_only_assets_missing(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    run_dir = workspace_root / "orchestration_runs" / "run-url-only"
    run_dir.mkdir(parents=True)
    node = TaskNode(
        node_id="register",
        title="Register benchmark cases",
        objective="Confirm benchmark inputs, register each tool/case pair, and verify staged constraints before execution.",
        capability_bundles=["plan", "task_manage", "validate"],
        metadata={
            "task_type": "benchmark",
            "task": "https://github.com/example/tool-a\nhttps://github.com/example/tool-b\n请选择共享数据集做 benchmark。",
            "run_dir": str(run_dir),
            "selected_tools": [],
            "excluded_tools": [],
            "benchmark_root": None,
        },
    )
    agent = AsyncMock()
    agent.ainvoke.side_effect = AssertionError("URL-only register should still bypass the model")

    result = await _invoke_worker_agent(
        agent=agent,
        node=node,
        workspace_root=workspace_root,
    )

    assert result.status == "failed"
    assert result.failure_reason == "missing_benchmark_assets"
    assert result.next_action_hint == "prepare_benchmark_assets_from_urls"
    assert "URL-only benchmark requests" in result.summary


@pytest.mark.asyncio
async def test_run_supervisor_orchestration_expands_benchmark_after_register_round(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace" / "20260506093000"
    workspace.mkdir(parents=True)

    async def worker_runner(node: TaskNode) -> WorkerResult:
        if node.node_id == "register":
            return WorkerResult(
                status="completed",
                summary="register ok",
                spawned_subgraph={"selected_tools": ["canu", "Flye"]},
            )
        return WorkerResult(status="completed", summary=f"{node.node_id} ok")

    result = await run_supervisor_orchestration(
        task="我想评估 /tmp/benchmark 里的 long-read-ecoli-pacbio benchmark 数据集在不同组装工具上的表现。",
        workspace_root=workspace,
        worker_runner=worker_runner,
    )

    graph_1_payload = json.loads((result.run_dir / "graph_round_1.json").read_text())
    graph_2_payload = json.loads((result.run_dir / "graph_round_2.json").read_text())

    assert [node["node_id"] for node in graph_1_payload["nodes"]] == ["register"]
    assert [node["node_id"] for node in graph_2_payload["nodes"]] == [
        "canu",
        "Flye",
        "summarize",
    ]


@pytest.mark.asyncio
async def test_invoke_worker_agent_uses_deterministic_benchmark_repo_helper(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    run_dir = workspace_root / "orchestration_runs" / "run-2"
    run_dir.mkdir(parents=True)
    register_node = TaskNode(
        node_id="register",
        title="Register benchmark cases",
        objective="Confirm benchmark inputs, register each tool/case pair, and verify staged constraints before execution.",
        capability_bundles=["plan", "task_manage", "validate"],
        metadata={
            "task_type": "benchmark",
            "task": "在本地 benchmark 目录里只选择 spades 和 megahit，先完成 register。",
            "run_dir": str(run_dir),
            "selected_tools": ["spades", "megahit"],
        },
    )
    def fake_register_helper(command: list[str], *, cwd: Path, phase: str) -> dict[str, object]:
        if phase == "resolve-datasets":
            (run_dir / "dataset_resolution.json").write_text("{}", encoding="utf-8")
            (run_dir / "dataset_resolution.md").write_text("# datasets\n", encoding="utf-8")
            return {"phase": phase, "command": command, "stdout": {}}
        if phase == "init":
            (run_dir / "benchmark_plan.json").write_text("{}", encoding="utf-8")
            (run_dir / "benchmark_plan.md").write_text("# plan\n", encoding="utf-8")
            return {"phase": phase, "command": command, "stdout": {"case_order": ["spades", "megahit"]}}
        repo = phase.split(":", 1)[1]
        case_dir = run_dir / "cases" / repo
        case_dir.mkdir(parents=True, exist_ok=True)
        if phase.startswith("prepare-case:"):
            (case_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "dataset_key": "short-read-ecoli-srr001666",
                        "metric_keys": ["contig_count", "assembly_size", "n50"],
                        "expected_outputs": ["final.contigs.fa", "log"],
                        "selected_input_files": {"reads_1": "/tmp/r1", "reads_2": "/tmp/r2"},
                    }
                ),
                encoding="utf-8",
            )
            (case_dir / "dataset_selection.json").write_text("{}", encoding="utf-8")
            (case_dir / "dataset_manifest.json").write_text("{}", encoding="utf-8")
            (case_dir / "agent_task.md").write_text("# task\n", encoding="utf-8")
            return {"phase": phase, "command": command, "stdout": {"repo": repo}}
        if phase.startswith("execution-ready:"):
            (case_dir / "execution_ready.json").write_text(
                json.dumps(
                    {
                        "ready": True,
                        "runtime_image": f"benchmark/{repo}",
                        "wdl_path": f"/tmp/{repo}.wdl",
                        "inputs_json_path": f"/tmp/{repo}.json",
                    }
                ),
                encoding="utf-8",
            )
            return {"phase": phase, "command": command, "stdout": {"repo": repo}}
        raise AssertionError(f"unexpected phase: {phase}")

    with patch("code2workspace_cli.supervisor_runtime._run_helper_json_command", side_effect=fake_register_helper):
        await _invoke_worker_agent(
            agent=AsyncMock(),
            node=register_node,
            workspace_root=workspace_root,
        )

    node = TaskNode(
        node_id="megahit",
        title="Run megahit",
        objective="Execute the staged benchmark workload for megahit and record outputs, logs, and failure reasons.",
        capability_bundles=["docker_build_run", "wdl_run", "metric_compute"],
        metadata={
            "task_type": "benchmark",
            "task": "运行 megahit benchmark。",
            "run_dir": str(run_dir),
        },
    )
    agent = AsyncMock()
    agent.ainvoke.side_effect = AssertionError("benchmark repo helper path should bypass the model")

    def fake_helper(command: list[str], *, cwd: Path, phase: str) -> dict[str, object]:
        case_dir = run_dir / "cases" / "megahit"
        output_dir = case_dir / "run" / "repo_native_output"
        output_dir.mkdir(parents=True, exist_ok=True)
        if phase == "run-repo-native:megahit":
            (case_dir / "run" / "repo_native.log").write_text(
                "ALL DONE\n",
                encoding="utf-8",
            )
            (output_dir / "final.contigs.fa").write_text(">c1\nAAAA\n", encoding="utf-8")
            (output_dir / "log").write_text("done\n", encoding="utf-8")
            (case_dir / "run" / "status.json").write_text(
                json.dumps(
                    {
                        "success": True,
                        "returncode": 0,
                        "elapsed_seconds": 1.5,
                        "command": command,
                        "log_path": str(case_dir / "run" / "repo_native.log"),
                        "output_dir": str(output_dir),
                        "output_artifacts": [
                            str(output_dir / "final.contigs.fa"),
                            str(output_dir / "log"),
                        ],
                    }
                ),
                encoding="utf-8",
            )
            return {"phase": phase, "command": command, "stdout": {"repo": "megahit"}}
        if phase == "analyze-case:megahit":
            (case_dir / "analysis.json").write_text(
                json.dumps(
                    {
                        "family": "short-read-assembly",
                        "artifact_paths": [
                            str(output_dir / "final.contigs.fa"),
                            str(output_dir / "log"),
                        ],
                        "artifact_checksums": {},
                        "metrics": {
                            "contig_count": 1,
                            "assembly_size": 4,
                            "n50": 4,
                        },
                    }
                ),
                encoding="utf-8",
            )
            (case_dir / "analysis.md").write_text("# analysis\n", encoding="utf-8")
            return {"phase": phase, "command": command, "stdout": {"repo": "megahit"}}
        raise AssertionError(f"unexpected phase: {phase}")

    with patch("code2workspace_cli.supervisor_runtime._run_helper_json_command", side_effect=fake_helper):
        result = await _invoke_worker_agent(
            agent=agent,
            node=node,
            workspace_root=workspace_root,
        )

    assert result.status == "completed"
    assert "megahit" in result.summary
    assert (run_dir / "cases" / "megahit" / "run" / "result_manifest.json").exists()


@pytest.mark.asyncio
async def test_deterministic_benchmark_repo_helper_marks_missing_expected_outputs_partial(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    run_dir = workspace_root / "orchestration_runs" / "run-partial"
    case_dir = run_dir / "cases" / "canu"
    (case_dir / "run" / "repo_native_output").mkdir(parents=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "manifest.json").write_text(
        json.dumps(
            {
                "dataset_key": "long-read-canu-pacbio",
                "family": "long-read-assembly",
                "metric_keys": ["contig_count", "assembly_size", "n50"],
                "expected_outputs": ["assembly.fasta"],
                "phase_status": {},
            }
        ),
        encoding="utf-8",
    )
    (case_dir / "execution_ready.json").write_text("{}", encoding="utf-8")
    (case_dir / "dataset_manifest.json").write_text("{}", encoding="utf-8")
    (case_dir / "run" / "repo_native.log").write_text("stopped after intermediate stage\n", encoding="utf-8")

    node = TaskNode(
        node_id="canu",
        title="Run canu",
        objective="Execute the staged benchmark workload for canu and record outputs, logs, and failure reasons.",
        capability_bundles=["docker_build_run", "wdl_run", "metric_compute"],
        metadata={
            "task_type": "benchmark",
            "task": "运行 canu benchmark。",
            "run_dir": str(run_dir),
        },
    )
    agent = AsyncMock()
    agent.ainvoke.side_effect = AssertionError("benchmark repo helper path should bypass the model")

    def fake_helper(command: list[str], *, cwd: Path, phase: str) -> dict[str, object]:
        if phase == "run-repo-native:canu":
            (case_dir / "run" / "status.json").write_text(
                json.dumps(
                    {
                        "success": True,
                        "returncode": 0,
                        "elapsed_seconds": 1.5,
                        "command": command,
                        "log_path": str(case_dir / "run" / "repo_native.log"),
                        "output_dir": str(case_dir / "run" / "repo_native_output"),
                        "output_artifacts": [],
                    }
                ),
                encoding="utf-8",
            )
            return {"phase": phase, "command": command, "stdout": {"repo": "canu"}}
        if phase == "analyze-case:canu":
            (case_dir / "analysis.json").write_text(
                json.dumps(
                    {
                        "family": "long-read-assembly",
                        "artifact_paths": [],
                        "artifact_checksums": {},
                        "metrics": {},
                    }
                ),
                encoding="utf-8",
            )
            (case_dir / "analysis.md").write_text("# analysis\n", encoding="utf-8")
            return {"phase": phase, "command": command, "stdout": {"repo": "canu"}}
        raise AssertionError(f"unexpected phase: {phase}")

    with patch("code2workspace_cli.supervisor_runtime._run_helper_json_command", side_effect=fake_helper):
        result = await _invoke_worker_agent(
            agent=agent,
            node=node,
            workspace_root=workspace_root,
        )

    assert result.status == "partial"
    assert result.failure_reason == "expected_outputs_missing"
    assert "Expected benchmark output files were not found" in result.summary


@pytest.mark.asyncio
async def test_deterministic_benchmark_workers_run_in_parallel_ready_batch(
    tmp_path: Path,
) -> None:
    graph = TaskGraph(
        graph_id="benchmark-r1",
        task_type="benchmark",
        round_index=1,
        nodes=[
            TaskNode(
                node_id="spades",
                title="Run spades",
                objective="run spades",
                capability_bundles=["docker_build_run", "wdl_run", "metric_compute"],
                metadata={"task_type": "benchmark"},
            ),
            TaskNode(
                node_id="megahit",
                title="Run megahit",
                objective="run megahit",
                capability_bundles=["docker_build_run", "wdl_run", "metric_compute"],
                metadata={"task_type": "benchmark"},
            ),
        ],
        edges=[],
    )
    active = 0
    max_active = 0
    lock = threading.Lock()

    def fake_deterministic_worker(*, node: TaskNode, workspace_root: Path) -> WorkerResult | None:
        nonlocal active, max_active
        if node.node_id not in {"spades", "megahit"}:
            return None
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.1)
        with lock:
            active -= 1
        return WorkerResult(status="completed", summary=f"{node.node_id} ok")

    agent = AsyncMock()
    agent.ainvoke.side_effect = AssertionError("deterministic worker should bypass the model")

    with patch(
        "code2workspace_cli.supervisor_runtime._maybe_run_deterministic_worker",
        side_effect=fake_deterministic_worker,
    ):
        result = await execute_graph_round(
            graph,
            lambda node: _invoke_worker_agent(
                agent=agent,
                node=node,
                workspace_root=tmp_path,
            ),
        )

    assert result.completed_count == 2
    assert max_active == 2


@pytest.mark.asyncio
async def test_invoke_worker_agent_uses_deterministic_benchmark_summary_helper(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    run_dir = workspace_root / "orchestration_runs" / "run-3"
    case_dir = run_dir / "cases" / "megahit"
    case_dir.mkdir(parents=True)
    (case_dir / "run").mkdir()
    (case_dir / "manifest.json").write_text(
        json.dumps(
            {
                "repo_name": "megahit",
                "family": "short-read-assembly",
                "dataset_key": "short-read-ecoli-srr001666",
                "metric_keys": ["contig_count", "n50", "assembly_size"],
                "phase_status": {"analysis": "pending", "summary": "pending"},
                "expected_outputs": ["final.contigs.fa", "log"],
            }
        ),
        encoding="utf-8",
    )
    (case_dir / "run" / "status.json").write_text(
        json.dumps(
            {
                "success": True,
                "returncode": 0,
                "elapsed_seconds": 1.0,
                "output_artifacts": [
                    str(case_dir / "run" / "repo_native_output" / "final.contigs.fa"),
                    str(case_dir / "run" / "repo_native_output" / "log"),
                ],
            }
        ),
        encoding="utf-8",
    )
    output_dir = case_dir / "run" / "repo_native_output"
    output_dir.mkdir()
    (output_dir / "final.contigs.fa").write_text(">c1\nAAAA\n", encoding="utf-8")
    (output_dir / "log").write_text("done\n", encoding="utf-8")
    node = TaskNode(
        node_id="summarize",
        title="Summarize benchmark outcomes",
        objective="Aggregate tool results, metrics, blockers, and output paths into a benchmark summary.",
        capability_bundles=["metric_compute", "summarize"],
        metadata={
            "task_type": "benchmark",
            "task": "汇总 benchmark 结果。",
            "run_dir": str(run_dir),
            "selected_tools": ["megahit"],
        },
    )
    agent = AsyncMock()
    agent.ainvoke.side_effect = AssertionError("benchmark summarize helper path should bypass the model")

    def fake_helper(command: list[str], *, cwd: Path, phase: str) -> dict[str, object]:
        if phase != "analyze-case:megahit":
            raise AssertionError(f"unexpected phase: {phase}")
        (case_dir / "analysis.json").write_text(
            json.dumps(
                {
                    "family": "short-read-assembly",
                    "artifact_paths": [
                        str(output_dir / "final.contigs.fa"),
                        str(output_dir / "log"),
                    ],
                    "artifact_checksums": {},
                    "metrics": {
                        "contig_count": 1,
                        "assembly_size": 4,
                        "n50": 4,
                    },
                }
            ),
            encoding="utf-8",
        )
        (case_dir / "analysis.md").write_text("# analysis\n", encoding="utf-8")
        return {"phase": phase, "command": command, "stdout": {"repo": "megahit"}}

    with patch("code2workspace_cli.supervisor_runtime._run_helper_json_command", side_effect=fake_helper):
        result = await _invoke_worker_agent(
            agent=agent,
            node=node,
            workspace_root=workspace_root,
        )

    assert result.status == "completed"
    assert (run_dir / "benchmark_supervisor_summary.json").exists()
    assert (run_dir / "benchmark_supervisor_summary.md").exists()


@pytest.mark.asyncio
async def test_benchmark_final_response_uses_worker_agent_not_case_helper(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    run_dir = workspace_root / "orchestration_runs" / "run-final"
    run_dir.mkdir(parents=True)
    node = TaskNode(
        node_id="final_response",
        title="Write final user response",
        objective="Turn benchmark results into the final user-facing answer.",
        capability_bundles=["summarize", "validate"],
        metadata={
            "task_type": "benchmark",
            "task": "汇总 benchmark 结果。",
            "run_dir": str(run_dir),
            "selected_tools": ["spades", "megahit"],
        },
    )
    agent = AsyncMock()
    agent.ainvoke.return_value = {
        "messages": [
            AIMessage(
                content=json.dumps(
                    {
                        "status": "completed",
                        "summary": "spades 和 megahit 均已完成，spades 在 N50 上更优。",
                    },
                    ensure_ascii=False,
                )
            )
        ]
    }

    result = await _invoke_worker_agent(
        agent=agent,
        node=node,
        workspace_root=workspace_root,
    )

    assert result.status == "completed"
    assert result.summary == "spades 和 megahit 均已完成，spades 在 N50 上更优。"
    assert agent.ainvoke.await_count == 1
