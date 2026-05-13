"""Shared data shapes for the Code2Workspace web workbench."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True, frozen=True)
class WebThreadRecord:
    """Thread metadata persisted by the web bridge."""

    thread_id: str
    assistant_id: str
    cwd: str
    active_status: str
    created_at: str
    updated_at: str
    title: str | None = None
    model_spec: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dict."""
        return asdict(self)


@dataclass(slots=True, frozen=True)
class ThreadSummary:
    """Merged thread summary exposed by the web API."""

    thread_id: str
    assistant_id: str
    cwd: str | None
    active_status: str
    created_at: str | None
    updated_at: str | None
    message_count: int
    initial_prompt: str | None
    title: str | None = None
    model_spec: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dict."""
        return asdict(self)


@dataclass(slots=True, frozen=True)
class TurnRecord:
    """One persisted turn submitted from the web UI."""

    turn_id: str
    thread_id: str
    prompt: str
    status: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dict."""
        return asdict(self)


@dataclass(slots=True, frozen=True)
class EventRecord:
    """One persisted runtime event for a thread turn."""

    event_id: int
    thread_id: str
    turn_id: str | None
    kind: str
    payload: dict[str, Any]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dict."""
        return asdict(self)
