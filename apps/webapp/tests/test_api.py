from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
import tomllib
from unittest.mock import AsyncMock, patch
import zipfile
from xml.sax.saxutils import escape

import httpx
from starlette.testclient import TestClient

from apps.webapp import api
from apps.webapp.store import AppStore


class FakeRuntime:
    def __init__(self) -> None:
        self.started: list[dict[str, str]] = []
        self.interrupted: list[str] = []
        self.decisions: list[tuple[str, list[dict[str, str]]]] = []
        self.statuses: dict[str, str] = {}

    async def shutdown(self) -> None:
        return None

    def get_active_status(self, thread_id: str) -> str:
        return self.statuses.get(thread_id, "idle")

    async def start_turn(
        self,
        *,
        thread_id: str,
        turn_id: str,
        assistant_id: str,
        cwd: str,
        prompt: str,
    ) -> None:
        self.started.append(
            {
                "thread_id": thread_id,
                "turn_id": turn_id,
                "assistant_id": assistant_id,
                "cwd": cwd,
                "prompt": prompt,
            }
        )
        self.statuses[thread_id] = "running"

    async def interrupt_thread(self, thread_id: str) -> bool:
        self.interrupted.append(thread_id)
        self.statuses[thread_id] = "interrupted"
        return True

    async def submit_decisions(
        self,
        thread_id: str,
        decisions: list[dict[str, str]],
    ) -> bool:
        self.decisions.append((thread_id, decisions))
        self.statuses[thread_id] = "running"
        return True


def build_client(tmp_path: Path) -> tuple[TestClient, AppStore, FakeRuntime]:
    store = AppStore(tmp_path / "webapp.db")
    runtime = FakeRuntime()
    app = api.create_app(store=store, runtime=runtime)
    return TestClient(app), store, runtime


def build_client_with_settings_root(
    tmp_path: Path,
    settings_root: Path | None = None,
) -> tuple[TestClient, AppStore, FakeRuntime, Path]:
    store = AppStore(tmp_path / "webapp.db")
    runtime = FakeRuntime()
    resolved_settings_root = settings_root or (tmp_path / ".code2workspace")
    app = api.create_app(
        store=store,
        runtime=runtime,
        settings_root=resolved_settings_root,
    )
    return TestClient(app), store, runtime, resolved_settings_root


def test_list_threads_only_returns_web_threads_and_enriches_matching_checkpoint_data(
    tmp_path: Path,
) -> None:
    client, store, runtime = build_client(tmp_path)
    store.create_thread(thread_id="web-1", assistant_id="agent", cwd=str(tmp_path))
    store.create_thread(thread_id="web-2", assistant_id="agent", cwd=str(tmp_path / "second"))
    runtime.statuses["web-1"] = "running"
    persisted = [
        {
            "thread_id": "web-1",
            "agent_name": "agent",
            "updated_at": "2026-04-21T10:00:00+00:00",
            "created_at": "2026-04-21T09:00:00+00:00",
            "cwd": str(tmp_path / "cli-thread"),
            "message_count": 3,
            "initial_prompt": "resume this thread",
            "latest_checkpoint_id": "cp-1",
        }
    ]

    with patch(
        "apps.webapp.api.populate_thread_checkpoint_details",
        new=AsyncMock(return_value=[
            persisted[0],
            {
                "thread_id": "web-2",
                "agent_name": "agent",
                "updated_at": None,
                "created_at": None,
                "cwd": str(tmp_path / "second"),
                "message_count": 0,
                "initial_prompt": None,
            },
        ]),
    ):
        response = client.get("/api/threads")

    assert response.status_code == 200
    payload = response.json()["threads"]
    ids = {item["thread_id"] for item in payload}
    assert ids == {"web-1", "web-2"}
    web_thread = next(item for item in payload if item["thread_id"] == "web-1")
    assert web_thread["active_status"] == "running"
    assert web_thread["message_count"] == 3
    assert web_thread["initial_prompt"] == "resume this thread"
    assert response.json()["page"] == 1
    assert response.json()["page_size"] == 50


def test_list_threads_supports_pagination(tmp_path: Path) -> None:
    client, store, _runtime = build_client(tmp_path)
    store.create_thread(thread_id="web-1", assistant_id="agent", cwd=str(tmp_path))
    store.create_thread(thread_id="web-2", assistant_id="agent", cwd=str(tmp_path))
    store.create_thread(thread_id="web-3", assistant_id="agent", cwd=str(tmp_path))

    with patch(
        "apps.webapp.api.populate_thread_checkpoint_details",
        new=AsyncMock(
            return_value=[
                {
                    "thread_id": "web-1",
                    "agent_name": "agent",
                    "updated_at": None,
                    "created_at": None,
                    "cwd": str(tmp_path),
                    "message_count": 0,
                    "initial_prompt": None,
                },
                {
                    "thread_id": "web-2",
                    "agent_name": "agent",
                    "updated_at": None,
                    "created_at": None,
                    "cwd": str(tmp_path),
                    "message_count": 0,
                    "initial_prompt": None,
                },
                {
                    "thread_id": "web-3",
                    "agent_name": "agent",
                    "updated_at": None,
                    "created_at": None,
                    "cwd": str(tmp_path),
                    "message_count": 0,
                    "initial_prompt": None,
                },
            ]
        ),
    ):
        response = client.get("/api/threads", params={"page": 2, "page_size": 1})

    assert response.status_code == 200
    payload = response.json()
    assert payload["page"] == 2
    assert payload["page_size"] == 1
    assert payload["total"] == 3
    assert len(payload["threads"]) == 1


def test_create_thread_returns_idle_thread_summary(tmp_path: Path) -> None:
    client, _store, _runtime = build_client(tmp_path)

    with patch(
        "apps.webapp.api.prepare_session_cwd",
        return_value=tmp_path / "workspace" / "20260421100000",
    ):
        response = client.post("/api/threads", json={})

    assert response.status_code == 201
    payload = response.json()["thread"]
    assert payload["assistant_id"] == "agent"
    assert payload["active_status"] == "idle"
    assert payload["cwd"].endswith("20260421100000")
    assert payload["model_spec"] is None


def test_patch_thread_updates_title_and_model_spec(tmp_path: Path) -> None:
    client, store, _runtime = build_client(tmp_path)
    store.create_thread(thread_id="web-1", assistant_id="agent", cwd=str(tmp_path))

    response = client.patch(
        "/api/threads/web-1",
        json={
            "title": "Renamed thread",
            "model_spec": "anthropic:claude-sonnet-4-6",
        },
    )

    assert response.status_code == 200
    payload = response.json()["thread"]
    assert payload["title"] == "Renamed thread"
    assert payload["model_spec"] == "anthropic:claude-sonnet-4-6"


def test_post_message_creates_turn_and_starts_runtime(tmp_path: Path) -> None:
    client, store, runtime = build_client(tmp_path)
    thread = store.create_thread(thread_id="web-1", assistant_id="agent", cwd=str(tmp_path))

    response = client.post(
        f"/api/threads/{thread.thread_id}/messages",
        json={"content": "inspect the workspace"},
    )

    assert response.status_code == 202
    payload = response.json()["turn"]
    assert payload["thread_id"] == thread.thread_id
    assert payload["prompt"] == "inspect the workspace"
    assert runtime.started[0]["prompt"] == "inspect the workspace"


def test_get_thread_history_uses_shared_thread_history_helper(tmp_path: Path) -> None:
    client, store, _runtime = build_client(tmp_path)
    thread = store.create_thread(thread_id="web-1", assistant_id="agent", cwd=str(tmp_path))

    with patch(
        "apps.webapp.api.fetch_persisted_thread_history_payload",
        new=AsyncMock(
            return_value=api.ThreadHistoryPayload(
                entries=[
                    api.ThreadHistoryEntry(type=api.ThreadHistoryType.USER, content="hello"),
                    api.ThreadHistoryEntry(
                        type=api.ThreadHistoryType.ASSISTANT,
                        content="world",
                    ),
                ],
                context_tokens=128,
            )
        ),
    ):
        response = client.get(f"/api/threads/{thread.thread_id}/history")

    assert response.status_code == 200
    payload = response.json()
    assert payload["context_tokens"] == 128
    assert [entry["type"] for entry in payload["entries"]] == ["user", "assistant"]


def test_delete_thread_removes_web_metadata_and_checkpoint_rows(tmp_path: Path) -> None:
    client, store, _runtime = build_client(tmp_path)
    thread = store.create_thread(thread_id="web-1", assistant_id="agent", cwd=str(tmp_path))
    turn = store.create_turn(thread.thread_id, "hello")
    store.append_event(thread.thread_id, turn.turn_id, "turn.started", {"prompt": "hello"})

    with patch("apps.webapp.api.delete_thread", new=AsyncMock(return_value=True)):
        response = client.delete(f"/api/threads/{thread.thread_id}")

    assert response.status_code == 200
    assert response.json() == {"deleted": True}
    assert store.get_thread(thread.thread_id) is None
    assert store.list_turns(thread.thread_id) == []


def test_workspace_file_rejects_path_escape(tmp_path: Path) -> None:
    client, store, _runtime = build_client(tmp_path)
    root = tmp_path / "workspace"
    root.mkdir()
    thread = store.create_thread(thread_id="web-1", assistant_id="agent", cwd=str(root))

    response = client.get(
        f"/api/threads/{thread.thread_id}/workspace/file",
        params={"path": "../outside.txt"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_workspace_path"


def test_workspace_upload_writes_files_to_root_directory(tmp_path: Path) -> None:
    client, store, _runtime = build_client(tmp_path)
    root = tmp_path / "workspace"
    root.mkdir()
    thread = store.create_thread(thread_id="web-1", assistant_id="agent", cwd=str(root))

    response = client.post(
        f"/api/threads/{thread.thread_id}/workspace/upload",
        data={"path": ""},
        files=[("files", ("hello.txt", b"hello workspace", "text/plain"))],
    )

    assert response.status_code == 200
    assert (root / "hello.txt").read_text() == "hello workspace"
    assert response.json()["files"] == [
        {"name": "hello.txt", "path": "hello.txt", "size": 15}
    ]


def test_workspace_upload_writes_multiple_files_to_subdirectory(tmp_path: Path) -> None:
    client, store, _runtime = build_client(tmp_path)
    root = tmp_path / "workspace"
    target_dir = root / "inputs"
    target_dir.mkdir(parents=True)
    thread = store.create_thread(thread_id="web-1", assistant_id="agent", cwd=str(root))

    response = client.post(
        f"/api/threads/{thread.thread_id}/workspace/upload",
        data={"path": "inputs"},
        files=[
            ("files", ("a.txt", b"A", "text/plain")),
            ("files", ("b.txt", b"BB", "text/plain")),
        ],
    )

    assert response.status_code == 200
    assert (target_dir / "a.txt").read_text() == "A"
    assert (target_dir / "b.txt").read_text() == "BB"
    assert response.json()["path"] == "inputs"


def test_workspace_upload_rejects_path_escape(tmp_path: Path) -> None:
    client, store, _runtime = build_client(tmp_path)
    root = tmp_path / "workspace"
    root.mkdir()
    thread = store.create_thread(thread_id="web-1", assistant_id="agent", cwd=str(root))

    response = client.post(
        f"/api/threads/{thread.thread_id}/workspace/upload",
        data={"path": "../outside"},
        files=[("files", ("hello.txt", b"hello", "text/plain"))],
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_workspace_path"


def test_workspace_upload_requires_directory_target(tmp_path: Path) -> None:
    client, store, _runtime = build_client(tmp_path)
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "existing.txt").write_text("content")
    thread = store.create_thread(thread_id="web-1", assistant_id="agent", cwd=str(root))

    response = client.post(
        f"/api/threads/{thread.thread_id}/workspace/upload",
        data={"path": "existing.txt"},
        files=[("files", ("hello.txt", b"hello", "text/plain"))],
    )

    assert response.status_code == 400
    assert response.json()["error"] == "workspace_path_not_directory"


def test_workspace_download_returns_file_response(tmp_path: Path) -> None:
    client, store, _runtime = build_client(tmp_path)
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "hello.txt").write_text("hello")
    thread = store.create_thread(thread_id="web-1", assistant_id="agent", cwd=str(root))

    response = client.get(
        f"/api/threads/{thread.thread_id}/workspace/download",
        params={"path": "hello.txt"},
    )

    assert response.status_code == 200
    assert response.content == b"hello"
    assert 'filename="hello.txt"' in response.headers["content-disposition"]


def test_workspace_download_returns_directory_zip(tmp_path: Path) -> None:
    client, store, _runtime = build_client(tmp_path)
    root = tmp_path / "workspace"
    nested = root / "inputs"
    nested.mkdir(parents=True)
    (nested / "a.txt").write_text("A")
    thread = store.create_thread(thread_id="web-1", assistant_id="agent", cwd=str(root))

    response = client.get(
        f"/api/threads/{thread.thread_id}/workspace/download",
        params={"path": "inputs"},
    )

    assert response.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert archive.namelist() == ["inputs/a.txt"]


def test_workspace_download_rejects_path_escape(tmp_path: Path) -> None:
    client, store, _runtime = build_client(tmp_path)
    root = tmp_path / "workspace"
    root.mkdir()
    thread = store.create_thread(thread_id="web-1", assistant_id="agent", cwd=str(root))

    response = client.get(
        f"/api/threads/{thread.thread_id}/workspace/download",
        params={"path": "../outside"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_workspace_path"


def test_workspace_file_extracts_docx_text_for_preview(tmp_path: Path) -> None:
    client, store, _runtime = build_client(tmp_path)
    root = tmp_path / "workspace"
    root.mkdir()
    docx_path = root / "report.docx"
    with zipfile.ZipFile(docx_path, "w") as archive:
      archive.writestr(
          "[Content_Types].xml",
          '<?xml version="1.0" encoding="UTF-8"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"></Types>',
      )
      archive.writestr(
          "word/document.xml",
          '<?xml version="1.0" encoding="UTF-8"?>'
          '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
          "<w:body><w:p><w:r><w:t>"
          f"{escape('Preview me')}"
          "</w:t></w:r></w:p></w:body></w:document>",
      )
    thread = store.create_thread(thread_id="web-1", assistant_id="agent", cwd=str(root))

    response = client.get(
        f"/api/threads/{thread.thread_id}/workspace/file",
        params={"path": "report.docx"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["path"] == "report.docx"
    assert "Preview me" in payload["content"]


def test_workspace_delete_removes_file(tmp_path: Path) -> None:
    client, store, _runtime = build_client(tmp_path)
    root = tmp_path / "workspace"
    root.mkdir()
    file_path = root / "hello.txt"
    file_path.write_text("hello")
    thread = store.create_thread(thread_id="web-1", assistant_id="agent", cwd=str(root))

    response = client.delete(
        f"/api/threads/{thread.thread_id}/workspace/item",
        params={"path": "hello.txt"},
    )

    assert response.status_code == 200
    assert not file_path.exists()
    assert response.json()["deleted"] is True


def test_workspace_delete_removes_directory_recursively(tmp_path: Path) -> None:
    client, store, _runtime = build_client(tmp_path)
    root = tmp_path / "workspace"
    nested = root / "inputs" / "nested"
    nested.mkdir(parents=True)
    (nested / "a.txt").write_text("A")
    thread = store.create_thread(thread_id="web-1", assistant_id="agent", cwd=str(root))

    response = client.delete(
        f"/api/threads/{thread.thread_id}/workspace/item",
        params={"path": "inputs"},
    )

    assert response.status_code == 200
    assert not (root / "inputs").exists()


def test_workspace_delete_rejects_root_path(tmp_path: Path) -> None:
    client, store, _runtime = build_client(tmp_path)
    root = tmp_path / "workspace"
    root.mkdir()
    thread = store.create_thread(thread_id="web-1", assistant_id="agent", cwd=str(root))

    response = client.delete(
        f"/api/threads/{thread.thread_id}/workspace/item",
        params={"path": ""},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "workspace_root_delete_forbidden"


def test_workspace_delete_rejects_path_escape(tmp_path: Path) -> None:
    client, store, _runtime = build_client(tmp_path)
    root = tmp_path / "workspace"
    root.mkdir()
    thread = store.create_thread(thread_id="web-1", assistant_id="agent", cwd=str(root))

    response = client.delete(
        f"/api/threads/{thread.thread_id}/workspace/item",
        params={"path": "../outside"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_workspace_path"


def test_health_endpoint_still_available(tmp_path: Path) -> None:
    client, _store, _runtime = build_client(tmp_path)

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_models_endpoint_reads_configured_models(tmp_path: Path) -> None:
    client, _store, _runtime = build_client(tmp_path)

    with patch.object(
        api.ModelSettingsStore,
        "load_model_selector_payload",
        return_value={
            "models": [
                {
                    "spec": "openai:gpt-5.4",
                    "provider": "openai",
                    "model": "gpt-5.4",
                    "label": "gpt-5.4",
                }
            ],
            "default_model": "openai:gpt-5.4",
            "recent_model": "openai:gpt-5.4",
        },
    ):
        response = client.get("/api/models")

    assert response.status_code == 200
    assert response.json() == {
        "models": [
            {
                "spec": "openai:gpt-5.4",
                "provider": "openai",
                "model": "gpt-5.4",
                "label": "gpt-5.4",
            }
        ],
        "default_model": "openai:gpt-5.4",
        "recent_model": "openai:gpt-5.4",
    }


def test_get_model_settings_returns_templates_and_empty_providers(tmp_path: Path) -> None:
    client, _store, _runtime, _settings_root = build_client_with_settings_root(tmp_path)

    response = client.get("/api/settings/models")

    assert response.status_code == 200
    payload = response.json()
    assert payload["default_model"] is None
    assert payload["providers"] == []
    assert [item["key"] for item in payload["templates"]] == [
        "openai",
        "gemini",
        "deepseek",
        "kimi",
        "glm",
        "qwen",
        "custom_openai_compatible",
    ]


def test_put_model_settings_persists_config_refreshes_models_and_dotenv(
    tmp_path: Path,
) -> None:
    client, _store, _runtime, settings_root = build_client_with_settings_root(tmp_path)

    response = client.put(
        "/api/settings/models",
        json={
            "default_model": "openai:gpt-5.4",
            "providers": [
                {
                    "key": "openai",
                    "label": "OpenAI",
                    "enabled": True,
                    "provider_kind": "native",
                    "base_url": None,
                    "api_key_env": "CODE2WORKSPACE_CLI_OPENAI_API_KEY",
                    "api_key": "sk-openai",
                    "models": ["gpt-5.4"],
                    "test_model": "gpt-5.4",
                },
                {
                    "key": "relay_demo",
                    "label": "Demo Relay",
                    "enabled": True,
                    "provider_kind": "openai_compatible",
                    "base_url": "https://relay.example/v1",
                    "api_key_env": "CODE2WORKSPACE_CLI_RELAY_DEMO_API_KEY",
                    "api_key": "relay-secret",
                    "models": ["moonshot-v1-8k"],
                    "test_model": "moonshot-v1-8k",
                },
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["default_model"] == "openai:gpt-5.4"
    assert [provider["key"] for provider in payload["providers"]] == [
        "openai",
        "relay_demo",
    ]
    assert all(provider["has_api_key"] is True for provider in payload["providers"])

    config_data = tomllib.loads((settings_root / "config.toml").read_text())
    assert config_data["models"]["default"] == "openai:gpt-5.4"
    assert config_data["models"]["providers"]["openai"]["models"] == ["gpt-5.4"]
    assert (
        config_data["models"]["providers"]["relay_demo"]["class_path"]
        == "langchain_openai:ChatOpenAI"
    )
    assert (
        config_data["models"]["providers"]["relay_demo"]["base_url"]
        == "https://relay.example/v1"
    )

    dotenv_text = (settings_root / ".env").read_text()
    assert "CODE2WORKSPACE_CLI_OPENAI_API_KEY=sk-openai" in dotenv_text
    assert "CODE2WORKSPACE_CLI_RELAY_DEMO_API_KEY=relay-secret" in dotenv_text

    models_response = client.get("/api/models")
    assert models_response.status_code == 200
    assert models_response.json() == {
        "models": [
            {
                "spec": "openai:gpt-5.4",
                "provider": "openai",
                "model": "gpt-5.4",
                "label": "gpt-5.4",
            },
            {
                "spec": "relay_demo:moonshot-v1-8k",
                "provider": "relay_demo",
                "model": "moonshot-v1-8k",
                "label": "moonshot-v1-8k",
            },
        ],
        "default_model": "openai:gpt-5.4",
        "recent_model": None,
    }


def test_put_model_settings_rejects_invalid_default_model(tmp_path: Path) -> None:
    client, _store, _runtime, _settings_root = build_client_with_settings_root(tmp_path)

    response = client.put(
        "/api/settings/models",
        json={
            "default_model": "openai:gpt-5.4",
            "providers": [
                {
                    "key": "openai",
                    "label": "OpenAI",
                    "enabled": True,
                    "provider_kind": "native",
                    "base_url": None,
                    "api_key_env": "CODE2WORKSPACE_CLI_OPENAI_API_KEY",
                    "api_key": "sk-openai",
                    "models": ["gpt-4.1"],
                    "test_model": "gpt-4.1",
                }
            ],
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_default_model"


def test_post_model_settings_test_reports_success(tmp_path: Path) -> None:
    client, _store, _runtime, _settings_root = build_client_with_settings_root(tmp_path)

    class FakeChatModel:
        async def ainvoke(self, prompt: str) -> str:
            assert "Reply with OK only." in prompt
            return "OK"

    with patch(
        "apps.webapp.settings_models.create_model",
        return_value=SimpleNamespace(model=FakeChatModel()),
        create=True,
    ):
        response = client.post(
            "/api/settings/models/test",
            json={
                "provider": {
                    "key": "openai",
                    "label": "OpenAI",
                    "enabled": True,
                    "provider_kind": "native",
                    "base_url": None,
                    "api_key_env": "CODE2WORKSPACE_CLI_OPENAI_API_KEY",
                    "api_key": "sk-openai",
                    "models": ["gpt-5.4"],
                    "test_model": "gpt-5.4",
                },
                "model": "gpt-5.4",
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "provider_key": "openai",
        "model": "gpt-5.4",
        "message": "Connection test succeeded.",
    }


def test_appearance_settings_endpoints_validate_supported_theme_modes(
    tmp_path: Path,
) -> None:
    client, _store, _runtime, _settings_root = build_client_with_settings_root(tmp_path)

    response = client.get("/api/settings/appearance")

    assert response.status_code == 200
    assert response.json() == {
        "theme_modes": ["light", "dark", "system"],
        "storage": "browser",
        "default_theme": "system",
    }

    update_response = client.put("/api/settings/appearance", json={"theme": "dark"})

    assert update_response.status_code == 200
    assert update_response.json() == {
        "theme": "dark",
        "storage": "browser",
    }


def test_langgraph_proxy_route_forwards_requests(tmp_path: Path) -> None:
    client, _store, _runtime = build_client(tmp_path)

    async def fake_forward(request, path):  # noqa: ANN001
        return api.JSONResponse({"proxied": path, "method": request.method})

    with patch("apps.webapp.api.forward_langgraph_request", new=fake_forward):
        response = client.get("/langgraph/info")

    assert response.status_code == 200
    assert response.json() == {"proxied": "info", "method": "GET"}


def test_langgraph_runs_stream_fallback_emits_values_and_finishes(tmp_path: Path) -> None:
    class FakeRemoteAgent:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            pass

        async def astream(self, *args, **kwargs):  # noqa: ANN002, ANN003
            yield (), "updates", {"node": {"messages": []}}
            yield (), "messages", (
                type(
                    "Msg",
                    (),
                    {
                        "content": "hello",
                        "additional_kwargs": {},
                        "response_metadata": {},
                        "type": "ai",
                        "name": None,
                        "id": "ai-1",
                        "tool_calls": [],
                        "invalid_tool_calls": [],
                        "usage_metadata": None,
                    },
                )(),
                {"meta": "value"},
            )

    app = api.create_app(store=AppStore(tmp_path / "fallback.db"), runtime=FakeRuntime())
    app.state.langgraph_service = type("Svc", (), {"base_url": "http://langgraph.local"})()
    test_client = TestClient(app)

    with (
        patch("apps.webapp.api.RemoteAgent", FakeRemoteAgent),
        patch(
            "apps.webapp.api.message_to_dict",
            return_value={"type": "AIMessageChunk", "data": {"content": "hello", "id": "ai-1"}},
        ),
        patch("apps.webapp.api.uuid.uuid4", return_value="run-1"),
    ):
        response = test_client.post(
            "/langgraph/threads/thread-1/runs/stream",
            json={"assistant_id": "agent", "input": {"messages": [{"type": "human", "content": "hi"}]}},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert 'event: metadata' in response.text
    assert 'event: updates' in response.text
    assert 'event: messages' in response.text
    assert '"content": "hello"' in response.text


def test_langgraph_runs_stream_frontend_submit_shape_forces_subgraphs_and_emits_valid_events(
    tmp_path: Path,
) -> None:
    class FakeRemoteAgent:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            pass

        async def astream(self, *args, **kwargs):  # noqa: ANN002, ANN003
            assert kwargs["subgraphs"] is True
            yield (), "values", {"messages": [{"type": "human", "content": "hi"}]}
            yield (), "updates", {"node": {"messages": []}}
            yield (), "messages", (
                type(
                    "Msg",
                    (),
                    {
                        "content": "hello",
                        "additional_kwargs": {},
                        "response_metadata": {},
                        "type": "ai",
                        "name": None,
                        "id": "ai-1",
                        "tool_calls": [],
                        "invalid_tool_calls": [],
                        "usage_metadata": None,
                    },
                )(),
                {"meta": "value"},
            )

    app = api.create_app(store=AppStore(tmp_path / "frontend-shape.db"), runtime=FakeRuntime())
    app.state.langgraph_service = type("Svc", (), {"base_url": "http://langgraph.local"})()
    test_client = TestClient(app)

    with (
        patch("apps.webapp.api.RemoteAgent", FakeRemoteAgent),
        patch(
            "apps.webapp.api.message_to_dict",
            return_value={"type": "AIMessageChunk", "data": {"content": "hello", "id": "ai-1"}},
        ),
        patch("apps.webapp.api.uuid.uuid4", return_value="run-frontend"),
    ):
        response = test_client.post(
            "/langgraph/threads/thread-1/runs/stream",
            json={
                "assistant_id": "agent",
                "input": {"messages": [{"type": "human", "content": "hi"}]},
                "stream_mode": ["messages", "updates", "values"],
            },
        )

    assert response.status_code == 200
    assert 'event: values' in response.text
    assert 'event: updates' in response.text
    assert 'event: messages' in response.text
    assert "event: {'" not in response.text


def test_langgraph_runs_stream_normalizes_sdk_augmented_stream_modes(
    tmp_path: Path,
) -> None:
    class FakeRemoteAgent:
        seen_kwargs = None

        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            pass

        async def astream(self, *args, **kwargs):  # noqa: ANN002, ANN003
            type(self).seen_kwargs = kwargs
            yield (), "updates", {"node": {"messages": []}}

    app = api.create_app(store=AppStore(tmp_path / "stream-modes.db"), runtime=FakeRuntime())
    app.state.langgraph_service = type("Svc", (), {"base_url": "http://langgraph.local"})()
    test_client = TestClient(app)

    with (
        patch("apps.webapp.api.RemoteAgent", FakeRemoteAgent),
        patch("apps.webapp.api.uuid.uuid4", return_value="run-modes"),
    ):
        response = test_client.post(
            "/langgraph/threads/thread-1/runs/stream",
            json={
                "assistant_id": "agent",
                "input": {"messages": [{"type": "human", "content": "hi"}]},
                "stream_mode": [
                    "updates",
                    "values",
                    "messages-tuple",
                    "tools",
                    "custom",
                    "messages-tuple",
                ],
            },
        )

    assert response.status_code == 200
    assert FakeRemoteAgent.seen_kwargs is not None
    assert FakeRemoteAgent.seen_kwargs["stream_mode"] == ["updates", "values", "messages"]


def test_langgraph_runs_stream_keeps_standard_event_names_for_subgraph_chunks(
    tmp_path: Path,
) -> None:
    class FakeRemoteAgent:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            pass

        async def astream(self, *args, **kwargs):  # noqa: ANN002, ANN003
            yield ("task-1",), "updates", {"node": {"messages": []}}
            yield ("task-1",), "messages", (
                type(
                    "Msg",
                    (),
                    {
                        "content": "tooling",
                        "additional_kwargs": {},
                        "response_metadata": {},
                        "type": "ai",
                        "name": None,
                        "id": "ai-sub-1",
                        "tool_calls": [],
                        "invalid_tool_calls": [],
                        "usage_metadata": None,
                    },
                )(),
                {"langgraph_checkpoint_ns": "task-1"},
            )

    app = api.create_app(store=AppStore(tmp_path / "subgraph-events.db"), runtime=FakeRuntime())
    app.state.langgraph_service = type("Svc", (), {"base_url": "http://langgraph.local"})()
    test_client = TestClient(app)

    with (
        patch("apps.webapp.api.RemoteAgent", FakeRemoteAgent),
        patch(
            "apps.webapp.api.message_to_dict",
            return_value={"type": "AIMessageChunk", "data": {"content": "tooling", "id": "ai-sub-1"}},
        ),
        patch("apps.webapp.api.uuid.uuid4", return_value="run-subgraph"),
    ):
        response = test_client.post(
            "/langgraph/threads/thread-1/runs/stream",
            json={
                "assistant_id": "agent",
                "input": {"messages": [{"type": "human", "content": "hi"}]},
                "stream_mode": ["messages", "updates"],
            },
        )

    assert response.status_code == 200
    assert "event: updates\n" in response.text
    assert "event: messages\n" in response.text
    assert "event: updates|task-1" not in response.text
    assert "event: messages|task-1" not in response.text


def test_langgraph_runs_stream_flattens_nested_message_dicts_for_browser_clients(
    tmp_path: Path,
) -> None:
    class FakeRemoteAgent:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            pass

        async def astream(self, *args, **kwargs):  # noqa: ANN002, ANN003
            yield (), "messages", (
                type(
                    "Msg",
                    (),
                    {
                        "content": '{"status_code": 200}',
                        "additional_kwargs": {},
                        "response_metadata": {},
                        "type": "tool",
                        "name": "fetch_url",
                        "id": "tool-1",
                        "tool_call_id": "call-1",
                        "artifact": None,
                        "status": "success",
                    },
                )(),
                {"meta": "value"},
            )

    app = api.create_app(store=AppStore(tmp_path / "flatten.db"), runtime=FakeRuntime())
    app.state.langgraph_service = type("Svc", (), {"base_url": "http://langgraph.local"})()
    test_client = TestClient(app)

    with (
        patch("apps.webapp.api.RemoteAgent", FakeRemoteAgent),
        patch(
            "apps.webapp.api.message_to_dict",
            return_value={
                "type": "tool",
                "data": {
                    "content": '{"status_code": 200}',
                    "name": "fetch_url",
                    "id": "tool-1",
                    "tool_call_id": "call-1",
                    "status": "success",
                },
            },
        ),
        patch("apps.webapp.api.uuid.uuid4", return_value="run-flat"),
    ):
        response = test_client.post(
            "/langgraph/threads/thread-1/runs/stream",
            json={
                "assistant_id": "agent",
                "input": {"messages": [{"type": "human", "content": "hi"}]},
                "stream_mode": ["messages", "updates", "values"],
            },
        )

    assert response.status_code == 200
    assert '"tool_call_id": "call-1"' in response.text
    assert '"id": "tool-1"' in response.text
    assert '"data": {' not in response.text


def test_langgraph_runs_stream_emits_structured_error_event_for_provider_failures(
    tmp_path: Path,
) -> None:
    from langgraph_sdk.errors import InternalServerError

    class FakeRemoteAgent:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            pass

        async def astream(self, *args, **kwargs):  # noqa: ANN002, ANN003
            response = httpx.Response(
                500,
                request=httpx.Request("POST", "http://langgraph.local/threads/thread-1/runs/stream"),
            )
            raise InternalServerError(
                "graph run failed",
                response=response,
                body={
                    "error": {
                        "message": "Insufficient credit balance for claude-sonnet-4-6.",
                        "type": "insufficient_quota",
                    }
                },
            )
            yield  # pragma: no cover

    app = api.create_app(store=AppStore(tmp_path / "error-stream.db"), runtime=FakeRuntime())
    app.state.langgraph_service = type("Svc", (), {"base_url": "http://langgraph.local"})()
    test_client = TestClient(app)

    with patch("apps.webapp.api.RemoteAgent", FakeRemoteAgent):
        response = test_client.post(
            "/langgraph/threads/thread-1/runs/stream",
            json={
                "assistant_id": "agent",
                "input": {"messages": [{"type": "human", "content": "hi"}]},
                "stream_mode": ["messages", "updates", "values"],
            },
        )

    assert response.status_code == 200
    assert "event: error" in response.text
    assert '"message": "Insufficient credit balance for claude-sonnet-4-6."' in response.text
    assert '"status_code": 500' in response.text


def test_extract_nested_error_message_parses_stringified_error_payloads() -> None:
    raw = "{'error': 'BadRequestError', 'message': 'Insufficient credit balance for claude-sonnet-4-6.'}"

    assert (
        api._extract_nested_error_message(raw)
        == "Insufficient credit balance for claude-sonnet-4-6."
    )


def test_langgraph_history_falls_back_to_state_when_upstream_history_returns_404(tmp_path: Path) -> None:
    class FakeResponse:
        def __init__(self, status_code: int, payload: object):
            self.status_code = status_code
            self._payload = payload
            self.headers: dict[str, str] = {"content-type": "application/json"}

        def json(self):
            return self._payload

    class FakeProxyClient:
        def build_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("build_request should not be used for history fallback")

        async def send(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("send should not be used for history fallback")

        async def request(self, method, url, headers=None, content=None):  # noqa: ANN001, ANN003
            if method == "POST" and url.endswith("/threads/thread-1/history"):
                return FakeResponse(404, {"detail": "thread not found"})
            if method == "GET" and url.endswith("/threads/thread-1/state"):
                return FakeResponse(
                    200,
                    {
                        "values": {
                            "messages": [{"type": "ai", "content": "hello"}],
                        },
                        "next": [],
                        "tasks": [],
                    },
                )
            raise AssertionError(f"unexpected call: {method} {url}")

    app = api.create_app(store=AppStore(tmp_path / "fallback.db"), runtime=FakeRuntime())
    app.state.proxy_client = FakeProxyClient()
    app.state.langgraph_service = type("Svc", (), {"base_url": "http://langgraph.local"})()
    test_client = TestClient(app)

    response = test_client.post("/langgraph/threads/thread-1/history", json={"limit": 10})

    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, list)
    assert payload[0]["values"]["messages"] == [{"type": "ai", "content": "hello"}]


def test_serve_frontend_prefers_route_specific_static_html(tmp_path: Path) -> None:
    frontend_dir = tmp_path / "frontend-out"
    frontend_dir.mkdir(parents=True)
    (frontend_dir / "index.html").write_text("<html><body>root</body></html>", encoding="utf-8")
    (frontend_dir / "settings.html").write_text(
        "<html><body>settings page</body></html>",
        encoding="utf-8",
    )
    client = TestClient(
        api.create_app(
            store=AppStore(tmp_path / "frontend-route.db"),
            runtime=FakeRuntime(),
            frontend_dir=frontend_dir,
        )
    )

    response = client.get("/settings")

    assert response.status_code == 200
    assert "settings page" in response.text
