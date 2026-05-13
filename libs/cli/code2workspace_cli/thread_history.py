"""Shared helpers for reading persisted thread history."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

logger = logging.getLogger(__name__)

ChannelValues = dict[str, object]


class SupportsThreadState(Protocol):
    """Protocol for remote/local agents that can read checkpointed state."""

    async def aget_state(self, config: dict[str, object]) -> object:
        """Return thread state."""


ReadFallback = Callable[[str], Awaitable[ChannelValues]]


class ThreadHistoryType(StrEnum):
    """Normalized history entry types exposed to non-TUI surfaces."""

    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SKILL = "skill"


@dataclass(slots=True)
class ThreadHistoryEntry:
    """One normalized history entry."""

    type: ThreadHistoryType
    content: str
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_status: str | None = None
    tool_output: str | None = None
    skill_name: str | None = None
    skill_description: str | None = None
    skill_source: str | None = None
    skill_args: str | None = None
    skill_body: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dict."""
        return {
            "type": self.type.value,
            "content": self.content,
            "tool_name": self.tool_name,
            "tool_args": self.tool_args,
            "tool_status": self.tool_status,
            "tool_output": self.tool_output,
            "skill_name": self.skill_name,
            "skill_description": self.skill_description,
            "skill_source": self.skill_source,
            "skill_args": self.skill_args,
            "skill_body": self.skill_body,
        }


@dataclass(slots=True)
class ThreadHistoryPayload:
    """Persisted thread history plus cached token metadata."""

    entries: list[ThreadHistoryEntry] = field(default_factory=list)
    context_tokens: int = 0


def convert_messages_to_history_entries(
    messages: list[object],
) -> list[ThreadHistoryEntry]:
    """Convert LangChain messages into normalized thread-history entries.

    Returns:
        Ordered history entries suitable for web/TUI reconstruction.
    """
    result: list[ThreadHistoryEntry] = []
    pending_tool_indices: dict[str, int] = {}

    for msg in messages:
        if isinstance(msg, HumanMessage):
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            if content.startswith("[SYSTEM]"):
                continue

            skill_meta = (msg.additional_kwargs or {}).get("__skill")
            if isinstance(skill_meta, dict) and skill_meta.get("name"):
                result.append(
                    ThreadHistoryEntry(
                        type=ThreadHistoryType.SKILL,
                        content="",
                        skill_name=str(skill_meta["name"]),
                        skill_description=str(skill_meta.get("description", "")),
                        skill_source=str(skill_meta.get("source", "")),
                        skill_args=str(skill_meta.get("args", "")),
                        skill_body=content,
                    )
                )
            else:
                result.append(
                    ThreadHistoryEntry(type=ThreadHistoryType.USER, content=content)
                )

        elif isinstance(msg, AIMessage):
            text = _coerce_ai_text(msg.content)
            if text:
                result.append(
                    ThreadHistoryEntry(
                        type=ThreadHistoryType.ASSISTANT,
                        content=text,
                    )
                )

            for tool_call in getattr(msg, "tool_calls", []):
                tool_id = tool_call.get("id")
                entry = ThreadHistoryEntry(
                    type=ThreadHistoryType.TOOL,
                    content="",
                    tool_name=tool_call.get("name", "unknown"),
                    tool_args=_coerce_tool_args(tool_call.get("args", {})),
                    tool_status="pending",
                )
                result.append(entry)
                if tool_id:
                    pending_tool_indices[tool_id] = len(result) - 1
                else:
                    entry.tool_status = "rejected"

        elif isinstance(msg, ToolMessage):
            tool_id = getattr(msg, "tool_call_id", None)
            if tool_id and tool_id in pending_tool_indices:
                entry = result[pending_tool_indices.pop(tool_id)]
                status = getattr(msg, "status", "success")
                entry.tool_status = "success" if status == "success" else "error"
                entry.tool_output = (
                    msg.content if isinstance(msg.content, str) else str(msg.content)
                )
            else:
                logger.debug(
                    "ToolMessage with tool_call_id=%r could not be matched "
                    "to a pending tool call",
                    tool_id,
                )

        else:
            logger.debug(
                "Skipping unsupported message type %s during history conversion",
                type(msg).__name__,
            )

    for idx in pending_tool_indices.values():
        result[idx].tool_status = "rejected"

    return result


def is_remote_agent(agent: object | None) -> bool:
    """Return whether the current agent should use checkpoint fallback."""
    if agent is None:
        return False

    try:
        from code2workspace_cli.remote_client import RemoteAgent
    except ImportError:
        return False

    return isinstance(agent, RemoteAgent)


async def get_thread_state_values(
    agent: SupportsThreadState | None,
    thread_id: str,
) -> ChannelValues:
    """Fetch thread state values, with direct-checkpointer fallback.

    Returns:
        Channel values recovered from the live agent state or local checkpointer.
    """
    return await _get_thread_state_values(agent, thread_id)


async def _get_thread_state_values(
    agent: SupportsThreadState | None,
    thread_id: str,
    *,
    read_fallback: ReadFallback | None = None,
) -> ChannelValues:
    """Fetch thread state values, with injectable fallback reader.

    Returns:
        Channel values recovered from the live agent state or fallback reader.
    """
    if agent is None:
        return {}

    config = {"configurable": {"thread_id": thread_id}}
    state = await agent.aget_state(config)

    values: ChannelValues = {}
    if state and getattr(state, "values", None):
        values = dict(state.values)

    messages = values.get("messages")
    if isinstance(messages, list) and messages:
        return values
    if not is_remote_agent(agent):
        return values

    logger.debug(
        "Remote state empty for thread %s; falling back to local checkpointer",
        thread_id,
    )
    reader = read_fallback or read_channel_values_from_checkpointer
    fallback_values = await reader(thread_id)
    fallback_messages = fallback_values.get("messages")
    if isinstance(fallback_messages, list) and fallback_messages:
        values["messages"] = fallback_messages
    if (
        values.get("_summarization_event") is None
        and "_summarization_event" in fallback_values
    ):
        values["_summarization_event"] = fallback_values["_summarization_event"]
    if values.get("_context_tokens") is None and "_context_tokens" in fallback_values:
        values["_context_tokens"] = fallback_values["_context_tokens"]
    return values


async def fetch_thread_history_payload(
    agent: SupportsThreadState | None,
    thread_id: str,
) -> ThreadHistoryPayload:
    """Read and normalize persisted history for one thread.

    Returns:
        Normalized history payload including cached context-token count.
    """
    state_values = await _get_thread_state_values(agent, thread_id)
    raw_tokens = state_values.get("_context_tokens")
    context_tokens = (
        raw_tokens if isinstance(raw_tokens, int) and raw_tokens >= 0 else 0
    )
    messages = state_values.get("messages", [])
    if not messages:
        return ThreadHistoryPayload(context_tokens=context_tokens)

    if messages and isinstance(messages[0], dict):
        from langchain_core.messages.utils import convert_to_messages

        messages = convert_to_messages(messages)

    entries = await asyncio.to_thread(convert_messages_to_history_entries, messages)
    return ThreadHistoryPayload(entries=entries, context_tokens=context_tokens)


async def fetch_persisted_thread_history_payload(
    thread_id: str,
) -> ThreadHistoryPayload:
    """Read history directly from the local checkpointer without an active agent.

    Returns:
        Normalized history payload including cached context-token count.
    """
    state_values = await read_channel_values_from_checkpointer(thread_id)
    raw_tokens = state_values.get("_context_tokens")
    context_tokens = (
        raw_tokens if isinstance(raw_tokens, int) and raw_tokens >= 0 else 0
    )
    messages = state_values.get("messages", [])
    if not messages:
        return ThreadHistoryPayload(context_tokens=context_tokens)

    if messages and isinstance(messages[0], dict):
        from langchain_core.messages.utils import convert_to_messages

        messages = convert_to_messages(messages)

    entries = await asyncio.to_thread(convert_messages_to_history_entries, messages)
    return ThreadHistoryPayload(entries=entries, context_tokens=context_tokens)


async def read_channel_values_from_checkpointer(thread_id: str) -> ChannelValues:
    """Read checkpoint channel values directly from the SQLite checkpointer.

    Returns:
        Channel values from the latest checkpoint row, or an empty dict.
    """
    try:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        from code2workspace_cli.sessions import get_db_path

        db_path = str(get_db_path())
        config = {"configurable": {"thread_id": thread_id}}
        async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
            tup = await saver.aget_tuple(config)
            if tup and tup.checkpoint:
                channel_values = tup.checkpoint.get("channel_values", {})
                if isinstance(channel_values, dict):
                    return dict(channel_values)
    except (ImportError, OSError) as exc:
        logger.warning(
            "Failed to read checkpointer directly for %s: %s",
            thread_id,
            exc,
        )
    except Exception:
        logger.warning(
            "Unexpected error reading checkpointer for %s",
            thread_id,
            exc_info=True,
        )
    return {}


def _coerce_ai_text(content: object) -> str:
    """Extract displayable text from AI message content.

    Returns:
        Concatenated text suitable for user-facing transcript rendering.
    """
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts).strip()
    return str(content).strip() if content is not None else ""


def _coerce_tool_args(value: object) -> dict[str, object]:
    """Normalize tool-call args into a dict.

    Returns:
        A dictionary form for the stored tool-call arguments.
    """
    if isinstance(value, dict):
        return value
    return {"value": value}
