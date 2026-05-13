"""Runtime bridge for the Code2Workspace web workbench."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.types import Command, Interrupt

from apps.webapp.models import EventRecord
from apps.webapp.store import AppStore
from code2workspace_cli.config import build_stream_config
from code2workspace_cli.remote_client import RemoteAgent
from code2workspace_cli.server import ServerProcess
from code2workspace_cli.server_manager import start_server_and_get_agent

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _ActiveTurnSession:
    thread_id: str
    turn_id: str
    assistant_id: str
    cwd: str
    agent: RemoteAgent
    server_proc: ServerProcess
    task: asyncio.Task[None]
    pending_decisions: asyncio.Future[dict[str, dict[str, list[dict[str, str]]]]] | None = None
    status: str = "running"


class EventBus:
    """In-memory fanout for persisted event records."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[EventRecord | None]]] = {}
        self._lock = asyncio.Lock()

    async def publish(self, event: EventRecord) -> None:
        """Publish an event to all live subscribers."""
        async with self._lock:
            queues = list(self._subscribers.get(event.thread_id, set()))
        for queue in queues:
            await queue.put(event)

    async def close(self) -> None:
        """Close all active subscriptions."""
        async with self._lock:
            subscribers = self._subscribers
            self._subscribers = {}
        for queues in subscribers.values():
            for queue in queues:
                await queue.put(None)

    async def stream(self, thread_id: str, store: AppStore, after_event_id: int | None = None):
        """Yield backlog and live events for a thread."""
        for event in store.list_events(thread_id, after_event_id=after_event_id):
            yield event

        queue: asyncio.Queue[EventRecord | None] = asyncio.Queue()
        async with self._lock:
            self._subscribers.setdefault(thread_id, set()).add(queue)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    yield None
                    continue
                if event is None:
                    return
                yield event.to_dict()
        finally:
            async with self._lock:
                self._subscribers.get(thread_id, set()).discard(queue)


class WebRuntimeBridge:
    """Drive active web turns against short-lived LangGraph server sessions."""

    def __init__(self, store: AppStore, *, assistant_id: str = "agent") -> None:
        self._store = store
        self._assistant_id = assistant_id
        self._active: dict[str, _ActiveTurnSession] = {}
        self._lock = asyncio.Lock()
        self.events = EventBus()

    def get_active_status(self, thread_id: str) -> str:
        """Return the current active status for a thread."""
        session = self._active.get(thread_id)
        if session is None:
            thread = self._store.get_thread(thread_id)
            return thread.active_status if thread is not None else "idle"
        return session.status

    async def shutdown(self) -> None:
        """Stop active turn sessions and close subscriptions."""
        async with self._lock:
            sessions = list(self._active.values())
            self._active.clear()
        for session in sessions:
            session.task.cancel()
            session.server_proc.stop()
        await self.events.close()

    async def start_turn(
        self,
        *,
        thread_id: str,
        turn_id: str,
        assistant_id: str,
        cwd: str,
        prompt: str,
    ) -> None:
        """Start one active turn."""
        async with self._lock:
            if thread_id in self._active:
                msg = f"thread {thread_id} already has an active turn"
                raise RuntimeError(msg)
            agent, server_proc, _ = await start_server_and_get_agent(
                assistant_id=assistant_id,
                auto_approve=True,
                interactive=True,
                cwd=Path(cwd),
                no_mcp=True,
            )
            session = _ActiveTurnSession(
                thread_id=thread_id,
                turn_id=turn_id,
                assistant_id=assistant_id,
                cwd=cwd,
                agent=agent,
                server_proc=server_proc,
                task=asyncio.create_task(
                    self._run_turn(thread_id=thread_id, turn_id=turn_id, prompt=prompt)
                ),
                status="running",
            )
            self._active[thread_id] = session

    async def interrupt_thread(self, thread_id: str) -> bool:
        """Interrupt one active turn by cancelling its task and stopping its server."""
        session = self._active.get(thread_id)
        if session is None:
            return False
        session.status = "interrupted"
        session.task.cancel()
        self._store.complete_turn(session.turn_id, status="interrupted", error="Interrupted from web UI.")
        await self._persist_event(
            thread_id,
            session.turn_id,
            "turn.interrupted",
            {"message": "Interrupted from web UI."},
        )
        session.server_proc.stop()
        async with self._lock:
            self._active.pop(thread_id, None)
        return True

    async def submit_decisions(
        self,
        thread_id: str,
        decisions: list[dict[str, str]],
    ) -> bool:
        """Resume one waiting turn with approval decisions."""
        session = self._active.get(thread_id)
        if session is None or session.pending_decisions is None:
            return False

        payload: dict[str, dict[str, list[dict[str, str]]]] = {}
        for item in decisions:
            interrupt_id = item["interrupt_id"]
            decision = {"type": item["decision"]}
            if item.get("message"):
                decision["message"] = item["message"]
            payload.setdefault(interrupt_id, {"decisions": []})["decisions"].append(decision)

        await self._persist_event(
            thread_id,
            session.turn_id,
            "decision.submitted",
            {"decisions": decisions},
        )
        session.status = "running"
        self._store.mark_turn_running(session.turn_id)
        session.pending_decisions.set_result(payload)
        session.pending_decisions = None
        return True

    async def _run_turn(self, *, thread_id: str, turn_id: str, prompt: str) -> None:
        """Drive one streamed turn to completion."""
        session = self._active[thread_id]
        config = build_stream_config(thread_id, session.assistant_id, cwd=session.cwd)
        await session.agent.aensure_thread(config)
        self._store.mark_turn_running(turn_id)
        await self._persist_event(thread_id, turn_id, "user.message", {"text": prompt})
        await self._persist_event(thread_id, turn_id, "turn.started", {"status": "running"})

        stream_input: dict[str, Any] | Command = {
            "messages": [HumanMessage(content=prompt)]
        }
        pending_text = ""
        tool_buffers: dict[str | int, dict[str, Any]] = {}
        displayed_tool_ids: set[str] = set()
        try:
            while True:
                pending_interrupts: dict[str, Any] = {}
                async for namespace, stream_mode, data in session.agent.astream(
                    stream_input,
                    stream_mode=["messages", "updates"],
                    subgraphs=True,
                    config=config,
                    durability="exit",
                ):
                    if namespace:
                        continue
                    if stream_mode == "updates" and isinstance(data, dict):
                        pending_interrupts.update(
                            await self._handle_update_chunk(thread_id, turn_id, data)
                        )
                        continue
                    if stream_mode == "messages":
                        pending_text = await self._handle_message_chunk(
                            thread_id,
                            turn_id,
                            data,
                            pending_text,
                            tool_buffers,
                            displayed_tool_ids,
                        )
                if pending_interrupts:
                    session.status = "awaiting_decision"
                    self._store.mark_turn_waiting(turn_id)
                    session.pending_decisions = asyncio.get_running_loop().create_future()
                    decisions = await session.pending_decisions
                    stream_input = Command(resume=decisions)
                    continue
                break
        except asyncio.CancelledError:
            logger.debug("Web turn cancelled for %s", thread_id)
            raise
        except Exception as exc:
            logger.exception("Web turn failed for %s", thread_id)
            self._store.complete_turn(turn_id, status="failed", error=str(exc))
            await self._persist_event(
                thread_id,
                turn_id,
                "turn.failed",
                {"error": f"{type(exc).__name__}: {exc}"},
            )
        else:
            self._store.complete_turn(turn_id, status="succeeded")
            if pending_text:
                await self._persist_event(
                    thread_id,
                    turn_id,
                    "assistant.completed",
                    {"text": pending_text},
                )
            await self._persist_event(
                thread_id,
                turn_id,
                "turn.completed",
                {"status": "succeeded"},
            )
        finally:
            session.server_proc.stop()
            async with self._lock:
                self._active.pop(thread_id, None)

    async def _handle_update_chunk(
        self,
        thread_id: str,
        turn_id: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle one updates-mode chunk."""
        pending_interrupts: dict[str, Any] = {}
        interrupts = data.get("__interrupt__", [])
        if not isinstance(interrupts, list):
            return pending_interrupts
        for interrupt in interrupts:
            if not isinstance(interrupt, Interrupt):
                continue
            pending_interrupts[interrupt.id] = interrupt.value
            await self._persist_event(
                thread_id,
                turn_id,
                "approval.required",
                {"interrupt_id": interrupt.id, "request": interrupt.value},
            )
        return pending_interrupts

    async def _handle_message_chunk(
        self,
        thread_id: str,
        turn_id: str,
        data: Any,
        pending_text: str,
        tool_buffers: dict[str | int, dict[str, Any]],
        displayed_tool_ids: set[str],
    ) -> str:
        """Handle one messages-mode chunk."""
        if not isinstance(data, tuple) or len(data) != 2:
            return pending_text
        message, _metadata = data
        if isinstance(message, ToolMessage):
            tool_id = getattr(message, "tool_call_id", None)
            await self._persist_event(
                thread_id,
                turn_id,
                "tool.completed",
                {
                    "tool_call_id": tool_id,
                    "status": getattr(message, "status", "success"),
                    "output": message.content if isinstance(message.content, str) else str(message.content),
                },
            )
            return pending_text

        if not hasattr(message, "content_blocks"):
            return pending_text

        for block in message.content_blocks:
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text", "")
                if text:
                    pending_text += text
                    await self._persist_event(
                        thread_id,
                        turn_id,
                        "assistant.delta",
                        {"text": text},
                    )
            elif block_type in {"tool_call", "tool_call_chunk"}:
                buffer_key = block.get("index")
                if buffer_key is None:
                    buffer_key = block.get("id") or f"buffer-{len(tool_buffers)}"
                buffer = tool_buffers.setdefault(
                    buffer_key,
                    {"name": None, "id": None, "args": None, "args_parts": []},
                )
                if block.get("name"):
                    buffer["name"] = block["name"]
                if block.get("id"):
                    buffer["id"] = block["id"]
                args = block.get("args")
                if isinstance(args, dict):
                    buffer["args"] = args
                    buffer["args_parts"] = []
                elif isinstance(args, str):
                    if args:
                        parts = buffer.setdefault("args_parts", [])
                        parts.append(args)
                        buffer["args"] = "".join(parts)
                if buffer.get("name") and buffer.get("id") and buffer["id"] not in displayed_tool_ids:
                    parsed_args = _parse_tool_args(buffer.get("args"))
                    if parsed_args is None:
                        continue
                    displayed_tool_ids.add(buffer["id"])
                    await self._persist_event(
                        thread_id,
                        turn_id,
                        "tool.called",
                        {
                            "tool_call_id": buffer["id"],
                            "name": buffer["name"],
                            "args": parsed_args,
                        },
                    )
                    tool_buffers.pop(buffer_key, None)
        return pending_text

    async def _persist_event(
        self,
        thread_id: str,
        turn_id: str | None,
        kind: str,
        payload: dict[str, Any],
    ) -> EventRecord:
        """Persist and publish one event."""
        event = self._store.append_event(thread_id, turn_id, kind, payload)
        await self.events.publish(event)
        return event


def _parse_tool_args(value: Any) -> dict[str, Any] | None:
    """Best-effort parse of tool-call args."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        if not value:
            return None
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    if value is None:
        return None
    return {"value": value}
