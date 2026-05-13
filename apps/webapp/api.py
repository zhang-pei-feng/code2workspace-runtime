"""ASGI app for the Code2Workspace web workbench."""

from __future__ import annotations

import ast
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
import uuid
import zipfile
from xml.etree import ElementTree

import httpx
from langchain_core.messages import message_to_dict
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from apps.webapp.langgraph_service import SharedLangGraphService
from apps.webapp.models import ThreadSummary
from apps.webapp.runtime import WebRuntimeBridge
from apps.webapp.settings_models import ModelSettingsStore, SettingsValidationError
from apps.webapp.store import AppStore
from code2workspace_cli.session_workspace import prepare_session_cwd
from code2workspace_cli.remote_client import RemoteAgent
from code2workspace_cli.sessions import (
    delete_thread,
    generate_thread_id,
    get_thread_agent,
    get_thread_cwd,
    list_threads,
    populate_thread_checkpoint_details,
)
from code2workspace_cli.thread_history import (
    ThreadHistoryEntry,
    ThreadHistoryPayload,
    ThreadHistoryType,
    fetch_persisted_thread_history_payload,
)


def create_app(
    *,
    store: AppStore | None = None,
    runtime: WebRuntimeBridge | Any | None = None,
    frontend_dir: Path | None = None,
    settings_root: Path | None = None,
) -> Starlette:
    """Create the web workbench application."""
    store = store or AppStore()
    runtime = runtime or WebRuntimeBridge(store)
    frontend_dir = frontend_dir or Path(__file__).resolve().parent / "frontend" / "out"
    settings_store = ModelSettingsStore(settings_root)
    langgraph_service = SharedLangGraphService(cwd=Path.cwd())
    proxy_client = httpx.AsyncClient(timeout=None, follow_redirects=True)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        app.state.store = store
        app.state.runtime = runtime
        app.state.frontend_dir = frontend_dir
        app.state.langgraph_service = langgraph_service
        app.state.proxy_client = proxy_client
        app.state.settings_store = settings_store
        await langgraph_service.start()
        yield
        await proxy_client.aclose()
        await langgraph_service.stop()
        shutdown = getattr(runtime, "shutdown", None)
        if callable(shutdown):
            await shutdown()

    app = Starlette(debug=True, routes=_build_routes(), lifespan=lifespan)
    app.state.store = store
    app.state.runtime = runtime
    app.state.frontend_dir = frontend_dir
    app.state.langgraph_service = langgraph_service
    app.state.proxy_client = proxy_client
    app.state.settings_store = settings_store
    return app


async def health(_: Request) -> JSONResponse:
    """Return a simple health payload."""
    return JSONResponse({"ok": True})


async def list_models(request: Request) -> JSONResponse:
    """Return currently configured web-selectable model specs."""
    settings_store: ModelSettingsStore = request.app.state.settings_store
    return JSONResponse(settings_store.load_model_selector_payload())


async def get_model_settings(request: Request) -> JSONResponse:
    """Return the editable shared model settings payload."""
    settings_store: ModelSettingsStore = request.app.state.settings_store
    return JSONResponse(settings_store.load_settings_payload())


async def put_model_settings(request: Request) -> JSONResponse:
    """Persist the editable shared model settings payload."""
    settings_store: ModelSettingsStore = request.app.state.settings_store
    try:
        payload = await request.json()
        saved = settings_store.save_settings_payload(payload)
    except SettingsValidationError as exc:
        return JSONResponse({"error": exc.code, "detail": exc.message}, status_code=400)
    return JSONResponse(saved)


async def test_model_settings(request: Request) -> JSONResponse:
    """Run a lightweight connectivity probe for one provider draft."""
    settings_store: ModelSettingsStore = request.app.state.settings_store
    payload = await request.json()
    provider = payload.get("provider")
    model = str(payload.get("model") or "").strip()
    if not isinstance(provider, dict) or not model:
        return JSONResponse({"error": "provider_and_model_required"}, status_code=400)
    try:
        result = await settings_store.test_provider(provider, model)
    except SettingsValidationError as exc:
        return JSONResponse({"error": exc.code, "detail": exc.message}, status_code=400)
    return JSONResponse(result)


async def get_appearance_settings(request: Request) -> JSONResponse:
    """Return supported appearance settings."""
    settings_store: ModelSettingsStore = request.app.state.settings_store
    return JSONResponse(settings_store.load_appearance_payload())


async def put_appearance_settings(request: Request) -> JSONResponse:
    """Validate one appearance choice and echo it back to the browser."""
    settings_store: ModelSettingsStore = request.app.state.settings_store
    try:
        payload = await request.json()
        result = settings_store.save_appearance_payload(payload)
    except SettingsValidationError as exc:
        return JSONResponse({"error": exc.code, "detail": exc.message}, status_code=400)
    return JSONResponse(result)


async def list_thread_summaries(request: Request) -> JSONResponse:
    """Return merged checkpoint-backed and web-only thread summaries."""
    store: AppStore = request.app.state.store
    runtime = request.app.state.runtime
    page = _parse_positive_int(request.query_params.get("page"), default=1)
    page_size = _parse_positive_int(request.query_params.get("page_size"), default=50)
    page_size = min(page_size, 100)
    threads = await _load_thread_summaries(store, runtime)
    total = len(threads)
    start = (page - 1) * page_size
    end = start + page_size
    return JSONResponse(
        {
            "threads": [thread.to_dict() for thread in threads[start:end]],
            "page": page,
            "page_size": page_size,
            "total": total,
        }
    )


async def create_thread(request: Request) -> JSONResponse:
    """Create a new web thread draft."""
    store: AppStore = request.app.state.store
    raw = await request.body()
    payload = json.loads(raw) if raw else {}
    assistant_id = str(payload.get("assistant_id") or "agent")
    cwd = prepare_session_cwd(Path.cwd(), mode="isolated")
    thread = store.create_thread(
        thread_id=generate_thread_id(),
        assistant_id=assistant_id,
        cwd=str(cwd),
        title=payload.get("title"),
    )
    return JSONResponse({"thread": _draft_to_summary(thread, active_status="idle").to_dict()}, status_code=201)


async def get_thread(request: Request) -> JSONResponse:
    """Return one thread summary."""
    store: AppStore = request.app.state.store
    runtime = request.app.state.runtime
    thread_id = request.path_params["thread_id"]
    summary = await _get_thread_summary(thread_id, store, runtime)
    if summary is None:
        return JSONResponse({"error": "thread_not_found"}, status_code=404)
    return JSONResponse({"thread": summary.to_dict()})


async def patch_thread_route(request: Request) -> JSONResponse:
    """Update web-thread persisted fields such as title or model."""
    store: AppStore = request.app.state.store
    runtime = request.app.state.runtime
    thread_id = request.path_params["thread_id"]
    existing = store.get_thread(thread_id)
    if existing is None:
        return JSONResponse({"error": "thread_not_found"}, status_code=404)

    payload = await request.json()
    updated = store.update_thread_fields(
        existing.thread_id,
        title=payload["title"] if "title" in payload else ...,
        model_spec=payload["model_spec"] if "model_spec" in payload else ...,
    )
    if updated is None:
        return JSONResponse({"error": "thread_not_found"}, status_code=404)
    summary = _draft_to_summary(updated, active_status=runtime.get_active_status(thread_id))
    return JSONResponse({"thread": summary.to_dict()})


async def delete_thread_route(request: Request) -> JSONResponse:
    """Delete one thread from both checkpoint and web stores."""
    store: AppStore = request.app.state.store
    runtime = request.app.state.runtime
    thread_id = request.path_params["thread_id"]
    interrupt = getattr(runtime, "interrupt_thread", None)
    if callable(interrupt):
        await interrupt(thread_id)
    deleted = await delete_thread(thread_id)
    store_deleted = store.delete_thread(thread_id)
    if not (deleted or store_deleted):
        return JSONResponse({"error": "thread_not_found"}, status_code=404)
    return JSONResponse({"deleted": True})


async def get_thread_history(request: Request) -> JSONResponse:
    """Return normalized persisted history for one thread."""
    store: AppStore = request.app.state.store
    thread_id = request.path_params["thread_id"]
    if await _resolve_thread_meta(thread_id, store) is None:
        return JSONResponse({"error": "thread_not_found"}, status_code=404)
    payload = await fetch_persisted_thread_history_payload(thread_id)
    return JSONResponse(
        {
            "entries": [entry.to_dict() for entry in payload.entries],
            "context_tokens": payload.context_tokens,
        }
    )


async def create_message(request: Request) -> JSONResponse:
    """Create and start one web turn."""
    store: AppStore = request.app.state.store
    runtime = request.app.state.runtime
    thread_id = request.path_params["thread_id"]
    meta = await _resolve_thread_meta(thread_id, store)
    if meta is None:
        return JSONResponse({"error": "thread_not_found"}, status_code=404)

    payload = await request.json()
    prompt = str(payload.get("content", "")).strip()
    if not prompt:
        return JSONResponse({"error": "content_required"}, status_code=400)

    store.upsert_thread(
        thread_id=thread_id,
        assistant_id=meta["assistant_id"],
        cwd=meta["cwd"],
        title=meta["title"],
        active_status="queued",
    )
    turn = store.create_turn(thread_id, prompt)
    try:
        await runtime.start_turn(
            thread_id=thread_id,
            turn_id=turn.turn_id,
            assistant_id=meta["assistant_id"],
            cwd=meta["cwd"],
            prompt=prompt,
        )
    except RuntimeError as exc:
        return JSONResponse({"error": "thread_busy", "detail": str(exc)}, status_code=409)
    return JSONResponse({"turn": turn.to_dict()}, status_code=202)


async def stream_events(request: Request) -> Response:
    """Stream persisted and live events for a thread."""
    store: AppStore = request.app.state.store
    runtime = request.app.state.runtime
    thread_id = request.path_params["thread_id"]
    if await _resolve_thread_meta(thread_id, store) is None:
        return JSONResponse({"error": "thread_not_found"}, status_code=404)

    after_raw = request.query_params.get("after")
    after_event_id = int(after_raw) if after_raw else None

    async def event_stream():
        async for event in runtime.events.stream(thread_id, store, after_event_id):
            if event is None:
                yield ": keep-alive\n\n"
                continue
            if isinstance(event, dict):
                event_dict = event
            else:
                event_dict = event.to_dict()
            yield _format_sse_event(event_dict)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def interrupt_thread_route(request: Request) -> JSONResponse:
    """Interrupt one active thread turn."""
    runtime = request.app.state.runtime
    thread_id = request.path_params["thread_id"]
    interrupted = await runtime.interrupt_thread(thread_id)
    if not interrupted:
        return JSONResponse({"error": "thread_not_running"}, status_code=409)
    return JSONResponse({"interrupted": True})


async def submit_decisions_route(request: Request) -> JSONResponse:
    """Resume a waiting thread with approval decisions."""
    runtime = request.app.state.runtime
    payload = await request.json()
    decisions = payload.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        return JSONResponse({"error": "decisions_required"}, status_code=400)
    resumed = await runtime.submit_decisions(
        request.path_params["thread_id"],
        decisions,
    )
    if not resumed:
        return JSONResponse({"error": "thread_not_waiting"}, status_code=409)
    return JSONResponse({"resumed": True})


async def get_workspace_tree(request: Request) -> JSONResponse:
    """Return directory entries under the thread workspace."""
    store: AppStore = request.app.state.store
    thread_id = request.path_params["thread_id"]
    meta = await _resolve_thread_meta(thread_id, store)
    if meta is None:
        return JSONResponse({"error": "thread_not_found"}, status_code=404)

    try:
        root, target = _resolve_workspace_target(meta["cwd"], request.query_params.get("path", ""))
    except ValueError:
        return JSONResponse({"error": "invalid_workspace_path"}, status_code=400)
    if not target.exists():
        return JSONResponse({"error": "workspace_path_not_found"}, status_code=404)
    if not target.is_dir():
        return JSONResponse({"error": "workspace_path_not_directory"}, status_code=400)

    entries = []
    for item in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        relative = "" if item == root else str(item.relative_to(root))
        entries.append(
            {
                "name": item.name,
                "path": relative,
                "type": "directory" if item.is_dir() else "file",
                "size": item.stat().st_size if item.is_file() else None,
            }
        )
    return JSONResponse(
        {
            "cwd": str(root),
            "path": "" if target == root else str(target.relative_to(root)),
            "entries": entries,
        }
    )


async def get_workspace_file(request: Request) -> JSONResponse:
    """Return text content for one workspace file."""
    store: AppStore = request.app.state.store
    thread_id = request.path_params["thread_id"]
    meta = await _resolve_thread_meta(thread_id, store)
    if meta is None:
        return JSONResponse({"error": "thread_not_found"}, status_code=404)

    try:
        root, target = _resolve_workspace_target(meta["cwd"], request.query_params.get("path", ""))
    except ValueError:
        return JSONResponse({"error": "invalid_workspace_path"}, status_code=400)
    if not target.exists():
        return JSONResponse({"error": "workspace_path_not_found"}, status_code=404)
    if not target.is_file():
        return JSONResponse({"error": "workspace_path_not_file"}, status_code=400)

    raw = _read_workspace_file_preview(target)
    truncated = len(raw) > 100_000
    return JSONResponse(
        {
            "cwd": str(root),
            "path": str(target.relative_to(root)),
            "content": raw[:100_000],
            "truncated": truncated,
        }
    )


async def upload_workspace_files(request: Request) -> JSONResponse:
    """Write uploaded files into one workspace directory."""
    store: AppStore = request.app.state.store
    thread_id = request.path_params["thread_id"]
    meta = await _resolve_thread_meta(thread_id, store)
    if meta is None:
        return JSONResponse({"error": "thread_not_found"}, status_code=404)

    form = await request.form()
    try:
        root, target = _require_workspace_directory(
            meta["cwd"],
            str(form.get("path") or ""),
        )
    except ValueError:
        return JSONResponse({"error": "invalid_workspace_path"}, status_code=400)
    except FileNotFoundError:
        return JSONResponse({"error": "workspace_path_not_found"}, status_code=404)
    except NotADirectoryError:
        return JSONResponse({"error": "workspace_path_not_directory"}, status_code=400)

    files = [item for item in form.getlist("files") if isinstance(item, UploadFile)]
    if not files:
        return JSONResponse({"error": "files_required"}, status_code=400)

    written_files: list[dict[str, str | int]] = []
    for upload in files:
        destination = target / Path(upload.filename or "upload.bin").name
        content = await upload.read()
        destination.write_bytes(content)
        written_files.append(
            {
                "name": destination.name,
                "path": str(destination.relative_to(root)),
                "size": len(content),
            }
        )
        await upload.close()

    return JSONResponse(
        {
            "cwd": str(root),
            "path": "" if target == root else str(target.relative_to(root)),
            "files": written_files,
        }
    )


async def download_workspace_path(request: Request) -> Response:
    """Download one workspace file or zip one workspace directory."""
    store: AppStore = request.app.state.store
    thread_id = request.path_params["thread_id"]
    meta = await _resolve_thread_meta(thread_id, store)
    if meta is None:
      return JSONResponse({"error": "thread_not_found"}, status_code=404)

    try:
        root, target = _resolve_workspace_target(
            meta["cwd"],
            request.query_params.get("path", ""),
        )
    except ValueError:
        return JSONResponse({"error": "invalid_workspace_path"}, status_code=400)
    if not target.exists():
        return JSONResponse({"error": "workspace_path_not_found"}, status_code=404)

    if target.is_file():
        return FileResponse(
            target,
            filename=target.name,
            media_type="application/octet-stream",
        )

    temp_dir = Path(tempfile.mkdtemp(prefix="code2workspace-webapp-zip-"))
    zip_path = temp_dir / _workspace_archive_name(root, target, thread_id)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in target.rglob("*"):
            if item.is_dir():
                continue
            archive.write(item, arcname=str(item.relative_to(root)))

    return FileResponse(
        zip_path,
        filename=zip_path.name,
        media_type="application/zip",
        background=BackgroundTask(_cleanup_temp_path, temp_dir),
    )


async def delete_workspace_path(request: Request) -> JSONResponse:
    """Delete one workspace file or directory."""
    store: AppStore = request.app.state.store
    thread_id = request.path_params["thread_id"]
    meta = await _resolve_thread_meta(thread_id, store)
    if meta is None:
        return JSONResponse({"error": "thread_not_found"}, status_code=404)

    raw_path = request.query_params.get("path", "")
    if raw_path == "":
        return JSONResponse({"error": "workspace_root_delete_forbidden"}, status_code=400)
    try:
        root, target = _resolve_workspace_target(meta["cwd"], raw_path)
    except ValueError:
        return JSONResponse({"error": "invalid_workspace_path"}, status_code=400)
    if not target.exists():
        return JSONResponse({"error": "workspace_path_not_found"}, status_code=404)

    _delete_workspace_target(target)
    return JSONResponse(
        {
            "deleted": True,
            "cwd": str(root),
            "path": raw_path,
        }
    )


async def list_runs(request: Request) -> JSONResponse:
    """Return all turns for one thread."""
    store: AppStore = request.app.state.store
    thread_id = request.path_params["thread_id"]
    if await _resolve_thread_meta(thread_id, store) is None:
        return JSONResponse({"error": "thread_not_found"}, status_code=404)
    return JSONResponse({"turns": store.list_turns(thread_id)})


async def get_run(request: Request) -> JSONResponse:
    """Return one turn plus its persisted event timeline."""
    store: AppStore = request.app.state.store
    thread_id = request.path_params["thread_id"]
    turn_id = request.path_params["turn_id"]
    if await _resolve_thread_meta(thread_id, store) is None:
        return JSONResponse({"error": "thread_not_found"}, status_code=404)
    turn = store.get_turn(turn_id)
    if turn is None or turn["thread_id"] != thread_id:
        return JSONResponse({"error": "turn_not_found"}, status_code=404)
    return JSONResponse({"turn": turn, "events": store.list_events(thread_id, turn_id=turn_id)})


async def clear_thread_events_route(request: Request) -> JSONResponse:
    """Clear web-only event logs for one thread."""
    store: AppStore = request.app.state.store
    thread_id = request.path_params["thread_id"]
    if await _resolve_thread_meta(thread_id, store) is None:
        return JSONResponse({"error": "thread_not_found"}, status_code=404)
    cleared = store.clear_thread_events(thread_id)
    return JSONResponse({"cleared": cleared})


async def langgraph_proxy_route(request: Request) -> Response:
    """Proxy LangGraph SDK traffic to the shared local server."""
    path = request.path_params.get("path", "")
    return await forward_langgraph_request(request, path)


async def serve_frontend(request: Request) -> Response:
    """Serve the built SPA frontend when present."""
    frontend_dir: Path = request.app.state.frontend_dir
    path = request.path_params.get("path", "")
    if str(path).startswith("api/") or str(path).startswith("langgraph/"):
        return PlainTextResponse("Not found", status_code=404)
    if not frontend_dir.exists():
        return PlainTextResponse(
            "Frontend build not found. Run apps/webapp/frontend build first.",
            status_code=404,
        )

    if path:
        candidate = (frontend_dir / path).resolve()
        try:
            candidate.relative_to(frontend_dir.resolve())
        except ValueError:
            return PlainTextResponse("Not found", status_code=404)
        if candidate.exists() and candidate.is_file():
            return Response(candidate.read_bytes(), media_type=_guess_media_type(candidate))
        html_candidate = (frontend_dir / f"{path}.html").resolve()
        try:
            html_candidate.relative_to(frontend_dir.resolve())
        except ValueError:
            return PlainTextResponse("Not found", status_code=404)
        if html_candidate.exists() and html_candidate.is_file():
            return Response(html_candidate.read_bytes(), media_type="text/html")
    index_html = frontend_dir / "index.html"
    if not index_html.exists():
        return PlainTextResponse("Frontend build incomplete.", status_code=404)
    return Response(index_html.read_bytes(), media_type="text/html")


def _build_routes() -> list[Route]:
    """Build application routes."""
    return [
        Route("/api/health", health),
        Route("/api/models", list_models),
        Route("/api/settings/models", get_model_settings, methods=["GET"]),
        Route("/api/settings/models", put_model_settings, methods=["PUT"]),
        Route("/api/settings/models/test", test_model_settings, methods=["POST"]),
        Route("/api/settings/appearance", get_appearance_settings, methods=["GET"]),
        Route("/api/settings/appearance", put_appearance_settings, methods=["PUT"]),
        Route("/api/threads", list_thread_summaries, methods=["GET"]),
        Route("/api/threads", create_thread, methods=["POST"]),
        Route("/api/threads/{thread_id}", get_thread, methods=["GET"]),
        Route("/api/threads/{thread_id}", patch_thread_route, methods=["PATCH"]),
        Route("/api/threads/{thread_id}", delete_thread_route, methods=["DELETE"]),
        Route("/api/threads/{thread_id}/history", get_thread_history, methods=["GET"]),
        Route("/api/threads/{thread_id}/messages", create_message, methods=["POST"]),
        Route("/api/threads/{thread_id}/events", stream_events, methods=["GET"]),
        Route("/api/threads/{thread_id}/interrupt", interrupt_thread_route, methods=["POST"]),
        Route("/api/threads/{thread_id}/decisions", submit_decisions_route, methods=["POST"]),
        Route("/api/threads/{thread_id}/workspace/tree", get_workspace_tree, methods=["GET"]),
        Route("/api/threads/{thread_id}/workspace/file", get_workspace_file, methods=["GET"]),
        Route("/api/threads/{thread_id}/workspace/upload", upload_workspace_files, methods=["POST"]),
        Route("/api/threads/{thread_id}/workspace/download", download_workspace_path, methods=["GET"]),
        Route("/api/threads/{thread_id}/workspace/item", delete_workspace_path, methods=["DELETE"]),
        Route("/api/threads/{thread_id}/runs", list_runs, methods=["GET"]),
        Route("/api/threads/{thread_id}/runs/{turn_id}", get_run, methods=["GET"]),
        Route("/api/threads/{thread_id}/maintenance/clear-events", clear_thread_events_route, methods=["POST"]),
        Route("/langgraph", langgraph_proxy_route, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]),
        Route("/langgraph/{path:path}", langgraph_proxy_route, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]),
        Route("/", serve_frontend, methods=["GET"]),
        Route("/{path:path}", serve_frontend, methods=["GET"]),
    ]


async def _load_thread_summaries(store: AppStore, runtime: Any) -> list[ThreadSummary]:
    """Return web-owned thread summaries enriched with checkpoint details when available."""
    web_rows = store.list_threads()
    if not web_rows:
        return []

    checkpoint_rows = [
        {
            "thread_id": web_row.thread_id,
            "agent_name": web_row.assistant_id,
            "updated_at": web_row.updated_at,
            "created_at": web_row.created_at,
            "cwd": web_row.cwd,
        }
        for web_row in web_rows
    ]
    checkpoint_rows = await populate_thread_checkpoint_details(
        checkpoint_rows,
        include_message_count=True,
        include_initial_prompt=True,
    )

    checkpoint_by_id = {str(row["thread_id"]): row for row in checkpoint_rows}
    summaries: list[ThreadSummary] = []
    for web_row in web_rows:
        row = checkpoint_by_id.get(web_row.thread_id, {})
        summaries.append(
            ThreadSummary(
                thread_id=web_row.thread_id,
                assistant_id=str(row.get("agent_name") or web_row.assistant_id),
        cwd=row.get("cwd") or web_row.cwd,
        active_status=runtime.get_active_status(web_row.thread_id),
        created_at=row.get("created_at") or web_row.created_at,
        updated_at=row.get("updated_at") or web_row.updated_at,
        message_count=int(row.get("message_count") or 0),
        initial_prompt=row.get("initial_prompt"),
        title=web_row.title,
        model_spec=web_row.model_spec,
    )
        )

    return sorted(summaries, key=lambda item: item.updated_at or "", reverse=True)


async def _get_thread_summary(
    thread_id: str,
    store: AppStore,
    runtime: Any,
) -> ThreadSummary | None:
    """Return one merged thread summary."""
    for thread in await _load_thread_summaries(store, runtime):
        if thread.thread_id == thread_id:
            return thread
    return None


async def _resolve_thread_meta(thread_id: str, store: AppStore) -> dict[str, str | None] | None:
    """Resolve assistant/cwd metadata for a thread from web rows or checkpoints."""
    thread = store.get_thread(thread_id)
    if thread is not None:
        return {"assistant_id": thread.assistant_id, "cwd": thread.cwd, "title": thread.title}

    assistant_id = await get_thread_agent(thread_id)
    cwd = await get_thread_cwd(thread_id)
    if assistant_id is None and cwd is None:
        return None
    return {"assistant_id": assistant_id or "agent", "cwd": cwd or str(Path.cwd()), "title": None}


def _draft_to_summary(web_row, *, active_status: str) -> ThreadSummary:
    """Convert a web-only draft row into an API thread summary."""
    return ThreadSummary(
        thread_id=web_row.thread_id,
        assistant_id=web_row.assistant_id,
        cwd=web_row.cwd,
        active_status=active_status,
        created_at=web_row.created_at,
        updated_at=web_row.updated_at,
        message_count=0,
        initial_prompt=None,
        title=web_row.title,
        model_spec=web_row.model_spec,
    )


def _resolve_workspace_target(cwd: str, relative_path: str) -> tuple[Path, Path]:
    """Resolve one workspace-relative path safely under the thread cwd."""
    root = Path(cwd).expanduser().resolve()
    target = (root / relative_path).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("path escaped workspace root") from exc
    return root, target


def _require_workspace_directory(cwd: str, relative_path: str) -> tuple[Path, Path]:
    """Resolve one workspace-relative directory path and validate it exists."""
    root, target = _resolve_workspace_target(cwd, relative_path)
    if not target.exists():
        raise FileNotFoundError(relative_path)
    if not target.is_dir():
        raise NotADirectoryError(relative_path)
    return root, target


def _parse_positive_int(raw: str | None, *, default: int) -> int:
    """Parse a positive integer query parameter with a fallback."""
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _workspace_archive_name(root: Path, target: Path, thread_id: str) -> str:
    """Return the download archive name for one workspace directory."""
    if target == root:
        return f"{thread_id}-workspace.zip"
    return f"{target.name}.zip"


def _cleanup_temp_path(path: Path) -> None:
    """Best-effort cleanup for one temporary file or directory tree."""
    if path.is_file():
        path.unlink(missing_ok=True)
        return
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_file():
            child.unlink(missing_ok=True)
        else:
            child.rmdir()
    path.rmdir()


def _delete_workspace_target(path: Path) -> None:
    """Delete one file or directory tree under the workspace root."""
    if path.is_file():
        path.unlink(missing_ok=True)
        return
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_file():
            child.unlink(missing_ok=True)
        else:
            child.rmdir()
    path.rmdir()


def _read_workspace_file_preview(path: Path) -> str:
    """Return previewable text for one workspace file."""
    if path.suffix.lower() == ".docx":
        extracted = _extract_docx_text(path)
        if extracted:
            return extracted
    return path.read_text(encoding="utf-8", errors="replace")


def _extract_docx_text(path: Path) -> str:
    """Extract plain text from a `.docx` document for sidebar preview."""
    try:
        with zipfile.ZipFile(path) as archive:
            document_xml = archive.read("word/document.xml")
    except (KeyError, OSError, zipfile.BadZipFile):
        return ""

    try:
        root = ElementTree.fromstring(document_xml)
    except ElementTree.ParseError:
        return ""

    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs: list[str] = []
    for paragraph in root.findall(".//w:p", namespace):
        texts = [
            node.text or ""
            for node in paragraph.findall(".//w:t", namespace)
        ]
        joined = "".join(texts).strip()
        if joined:
            paragraphs.append(joined)
    return "\n\n".join(paragraphs)


def _format_sse_event(event: dict[str, Any]) -> str:
    """Serialize one event record as an SSE message."""
    return (
        f"id: {event['event_id']}\n"
        f"event: {event['kind']}\n"
        f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    )


def _guess_media_type(path: Path) -> str:
    """Return a coarse media type for built frontend assets."""
    if path.suffix == ".js":
        return "text/javascript"
    if path.suffix == ".css":
        return "text/css"
    if path.suffix == ".svg":
        return "image/svg+xml"
    if path.suffix == ".json":
        return "application/json"
    return "application/octet-stream"


async def forward_langgraph_request(request: Request, path: str) -> Response:
    """Forward one request to the shared LangGraph server."""
    if request.method == "POST" and _is_thread_run_stream_path(path):
        return await _forward_langgraph_run_stream(request, path)
    if request.method == "POST" and _is_thread_history_path(path):
        return await _forward_langgraph_history_request(request, path)

    service: SharedLangGraphService = request.app.state.langgraph_service
    base_url = service.base_url.rstrip("/")
    target = f"{base_url}/{path.lstrip('/')}" if path else base_url
    if request.url.query:
        target = f"{target}?{request.url.query}"

    client: httpx.AsyncClient = request.app.state.proxy_client
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"host", "content-length", "connection"}
    }
    upstream_request = client.build_request(
        request.method,
        target,
        headers=headers,
        content=await request.body(),
    )
    upstream_response = await client.send(upstream_request, stream=True)
    response_headers = {
        key: value
        for key, value in upstream_response.headers.items()
        if key.lower()
        not in {"content-length", "transfer-encoding", "connection", "content-encoding"}
    }
    return StreamingResponse(
        upstream_response.aiter_raw(),
        status_code=upstream_response.status_code,
        headers=response_headers,
        background=BackgroundTask(upstream_response.aclose),
    )


def _is_thread_run_stream_path(path: str) -> bool:
    """Return whether the proxy target is a thread-scoped `/runs/stream` path."""
    return bool(re.fullmatch(r"threads/[^/]+/runs/stream", path))


def _is_thread_history_path(path: str) -> bool:
    """Return whether the proxy target is a thread-scoped `/history` path."""
    return bool(re.fullmatch(r"threads/[^/]+/history", path))


async def _forward_langgraph_run_stream(request: Request, path: str) -> Response:
    """Stream LangGraph run events through the backend proxy.

    This uses `RemoteAgent.astream(...)` against the shared local LangGraph
    server because that path has been empirically verified to expose real
    message/update chunks for this graph, while the raw browser-facing
    `/runs/stream` endpoint on the local dev server may degrade into
    heartbeat-only responses in this environment.
    """
    service: SharedLangGraphService = request.app.state.langgraph_service
    base_url = service.base_url.rstrip("/")
    thread_id = path.split("/")[1]
    payload = await request.json()
    assistant_id = str(payload["assistant_id"])
    stream_mode = _normalize_proxy_stream_modes(payload.get("stream_mode"))
    remote = RemoteAgent(base_url, graph_name="agent")
    run_id = str(uuid.uuid4())
    config = {
        "configurable": {"thread_id": thread_id},
        "metadata": {
            "run_id": run_id,
            "thread_id": thread_id,
            "assistant_id": assistant_id,
        },
    }

    async def normalized_events():
        yield "metadata", {"run_id": run_id, "thread_id": thread_id}
        async for namespace, mode, data in remote.astream(
            payload.get("input"),
            stream_mode=list(stream_mode),
            # Force subgraph-aware tuple output so the browser-facing proxy
            # always receives stable `(namespace, mode, data)` events.
            subgraphs=True,
            config=config,
            context=payload.get("context"),
            durability=payload.get("durability"),
        ):
            event_name = mode
            if mode == "messages":
                message_obj, metadata = data
                yield event_name, [
                    _normalize_langgraph_message_payload(message_to_dict(message_obj)),
                    metadata,
                ]
            else:
                yield event_name, data

    async def stream():
        try:
            async for event_name, data in normalized_events():
                yield _encode_langgraph_sse(event_name, data)
        except Exception as exc:  # pragma: no cover - exercised through streaming tests
            yield _encode_langgraph_sse("error", _build_stream_error_payload(exc))

    response_headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    response_headers["Content-Location"] = f"/threads/{thread_id}/runs/{run_id}"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers=response_headers,
    )


def _normalize_proxy_stream_modes(raw_modes: Any) -> list[str]:
    """Normalize browser stream modes to the subset supported by the proxy path."""
    supported = {"messages", "updates", "values", "events", "debug", "tasks", "checkpoints"}
    alias_map = {
        "messages-tuple": "messages",
    }
    normalized: list[str] = []
    items = raw_modes if isinstance(raw_modes, list) else [raw_modes]
    for item in items:
        if not isinstance(item, str):
            continue
        candidate = alias_map.get(item, item)
        if candidate not in supported:
            continue
        if candidate in normalized:
            continue
        normalized.append(candidate)
    if not normalized:
        return ["messages", "updates", "values"]
    return normalized


def _build_stream_error_payload(exc: Exception) -> dict[str, Any]:
    """Convert backend streaming exceptions into browser-structured errors."""
    payload: dict[str, Any] = {
        "message": _extract_error_message(exc),
        "name": exc.__class__.__name__,
    }
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        payload["status_code"] = status_code
    return payload


def _extract_error_message(exc: Exception) -> str:
    """Extract the most user-useful message from nested provider/LangGraph errors."""
    for candidate in (
        getattr(exc, "body", None),
        getattr(exc, "detail", None),
        getattr(exc, "message", None),
        str(exc),
    ):
        message = _extract_nested_error_message(candidate)
        if message:
            return message
    return exc.__class__.__name__


def _extract_nested_error_message(value: Any) -> str | None:
    """Walk common nested API error payloads and return the first meaningful message."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if text.startswith(("{", "[")) and text.endswith(("}", "]")):
            for parser in (json.loads, ast.literal_eval):
                try:
                    parsed = parser(text)
                except (ValueError, SyntaxError, json.JSONDecodeError):
                    continue
                message = _extract_nested_error_message(parsed)
                if message:
                    return message
        return text or None
    if isinstance(value, dict):
        for key in ("message", "detail", "error_description", "description"):
            message = _extract_nested_error_message(value.get(key))
            if message:
                return message
        error_value = value.get("error")
        if error_value is not None:
            message = _extract_nested_error_message(error_value)
            if message:
                return message
        for nested in value.values():
            message = _extract_nested_error_message(nested)
            if message:
                return message
        return None
    if isinstance(value, list):
        for item in value:
            message = _extract_nested_error_message(item)
            if message:
                return message
        return None
    return None


async def _forward_langgraph_history_request(request: Request, path: str) -> Response:
    """Forward thread history requests, with state fallback when history 404s."""
    service: SharedLangGraphService = request.app.state.langgraph_service
    base_url = service.base_url.rstrip("/")
    client: httpx.AsyncClient = request.app.state.proxy_client
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"host", "content-length", "connection"}
    }
    body = await request.body()

    history_response = await client.request(
        "POST",
        f"{base_url}/{path.lstrip('/')}",
        headers=headers,
        content=body,
    )
    if history_response.status_code != 404:
        return JSONResponse(history_response.json(), status_code=history_response.status_code)

    thread_id = path.split("/")[1]
    state_response = await client.request(
        "GET",
        f"{base_url}/threads/{thread_id}/state",
        headers=headers,
    )
    if state_response.status_code >= 400:
        return JSONResponse(state_response.json(), status_code=state_response.status_code)

    state_payload = state_response.json()
    fallback_history = [
        {
            "values": state_payload.get("values", {}),
            "next": state_payload.get("next", []),
            "tasks": state_payload.get("tasks", []),
            "checkpoint": state_payload.get(
                "checkpoint",
                {"thread_id": thread_id, "checkpoint_ns": "", "checkpoint_id": "fallback"},
            ),
            "parent_checkpoint": state_payload.get("parent_checkpoint"),
            "metadata": state_payload.get("metadata", {}),
            "created_at": state_payload.get("created_at"),
        }
    ]
    return JSONResponse(fallback_history)


def _encode_langgraph_sse(event: str, data: dict[str, Any]) -> str:
    """Encode one LangGraph-style SSE event."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _normalize_langgraph_message_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Flatten LangChain's nested `message_to_dict` shape for browser SDK clients."""
    if not isinstance(payload, dict):
        return payload
    nested = payload.get("data")
    if not isinstance(nested, dict):
        return payload
    message_type = payload.get("type")
    if message_type is None:
        return nested
    return {
        **nested,
        "type": message_type,
    }


async def _sleep_short() -> None:
    """Small async sleep helper to keep stream fallback testable."""
    import asyncio

    await asyncio.sleep(0.5)


app = create_app()

__all__ = [
    "ThreadHistoryEntry",
    "ThreadHistoryPayload",
    "ThreadHistoryType",
    "app",
    "create_app",
]
