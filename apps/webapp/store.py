"""SQLite-backed persistence for the Code2Workspace web workbench."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from apps.webapp.models import EventRecord, TurnRecord, WebThreadRecord


def utc_now() -> str:
    """Return the current UTC timestamp in ISO 8601 format."""
    return datetime.now(tz=UTC).isoformat()


def default_db_path() -> Path:
    """Return the default sqlite path for the web workbench state."""
    db_dir = Path.home() / ".code2workspace"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "webapp.db"


class AppStore:
    """Store web-only thread metadata, turns, and event timelines."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_db(self) -> None:
        """Create the required tables if they do not already exist."""
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS web_threads (
                    thread_id TEXT PRIMARY KEY,
                    assistant_id TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    title TEXT,
                    model_spec TEXT,
                    active_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS turns (
                    turn_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    error TEXT,
                    FOREIGN KEY(thread_id) REFERENCES web_threads(thread_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL,
                    turn_id TEXT,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(thread_id) REFERENCES web_threads(thread_id) ON DELETE CASCADE,
                    FOREIGN KEY(turn_id) REFERENCES turns(turn_id) ON DELETE CASCADE
                );
                """
            )
            self._ensure_column(conn, "web_threads", "model_spec", "TEXT")

    def _ensure_column(
        self,
        conn: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        """Add one missing column to an existing table."""
        columns = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column in columns:
            return
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def create_thread(
        self,
        *,
        thread_id: str | None = None,
        assistant_id: str,
        cwd: str,
        title: str | None = None,
        model_spec: str | None = None,
    ) -> WebThreadRecord:
        """Create a new web thread metadata row."""
        thread_id = thread_id or str(uuid4())
        now = utc_now()
        title = (title or "").strip() or None
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO web_threads (
                    thread_id, assistant_id, cwd, title, model_spec, active_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (thread_id, assistant_id, cwd, title, model_spec, "idle", now, now),
            )
        return WebThreadRecord(
            thread_id=thread_id,
            assistant_id=assistant_id,
            cwd=cwd,
            title=title,
            model_spec=model_spec,
            active_status="idle",
            created_at=now,
            updated_at=now,
        )

    def upsert_thread(
        self,
        *,
        thread_id: str,
        assistant_id: str,
        cwd: str,
        title: str | None = None,
        model_spec: str | None = None,
        active_status: str = "idle",
    ) -> WebThreadRecord:
        """Insert or update a web thread metadata row."""
        existing = self.get_thread(thread_id)
        now = utc_now()
        created_at = existing.created_at if existing is not None else now
        normalized_title = (title or "").strip() or None
        title = normalized_title if normalized_title is not None else (
            existing.title if existing is not None else None
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO web_threads (
                    thread_id, assistant_id, cwd, title, model_spec, active_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    assistant_id=excluded.assistant_id,
                    cwd=excluded.cwd,
                    title=COALESCE(excluded.title, web_threads.title),
                    model_spec=COALESCE(excluded.model_spec, web_threads.model_spec),
                    active_status=excluded.active_status,
                    updated_at=excluded.updated_at
                """,
                (thread_id, assistant_id, cwd, title, model_spec, active_status, created_at, now),
            )
        return WebThreadRecord(
            thread_id=thread_id,
            assistant_id=assistant_id,
            cwd=cwd,
            title=title,
            model_spec=model_spec if model_spec is not None else (existing.model_spec if existing else None),
            active_status=active_status,
            created_at=created_at,
            updated_at=now,
        )

    def get_thread(self, thread_id: str) -> WebThreadRecord | None:
        """Return one web thread row."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT thread_id, assistant_id, cwd, title, model_spec, active_status, created_at, updated_at
                FROM web_threads
                WHERE thread_id = ?
                """,
                (thread_id,),
            ).fetchone()
        if row is None:
            return None
        return WebThreadRecord(
            thread_id=row["thread_id"],
            assistant_id=row["assistant_id"],
            cwd=row["cwd"],
            title=row["title"],
            model_spec=row["model_spec"],
            active_status=row["active_status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_threads(self) -> list[WebThreadRecord]:
        """Return web-known thread metadata rows ordered by update time."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT thread_id, assistant_id, cwd, title, model_spec, active_status, created_at, updated_at
                FROM web_threads
                ORDER BY updated_at DESC
                """
            ).fetchall()
        return [
            WebThreadRecord(
                thread_id=row["thread_id"],
                assistant_id=row["assistant_id"],
                cwd=row["cwd"],
                title=row["title"],
                model_spec=row["model_spec"],
                active_status=row["active_status"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def set_thread_status(self, thread_id: str, status: str) -> None:
        """Update one thread's active status."""
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE web_threads SET active_status = ?, updated_at = ? WHERE thread_id = ?",
                (status, now, thread_id),
            )

    def update_thread_fields(
        self,
        thread_id: str,
        *,
        title: str | None | object = ...,
        model_spec: str | None | object = ...,
    ) -> WebThreadRecord | None:
        """Update selected mutable thread fields and return the latest row."""
        existing = self.get_thread(thread_id)
        if existing is None:
            return None

        next_title = existing.title if title is ... else ((title or "").strip() or None)
        next_model_spec = existing.model_spec if model_spec is ... else (
            str(model_spec).strip() if model_spec not in (..., None) else None
        )
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE web_threads
                SET title = ?, model_spec = ?, updated_at = ?
                WHERE thread_id = ?
                """,
                (next_title, next_model_spec, now, thread_id),
            )
        return self.get_thread(thread_id)

    def create_turn(self, thread_id: str, prompt: str) -> TurnRecord:
        """Create a queued turn row."""
        turn_id = str(uuid4())
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO turns (
                    turn_id, thread_id, prompt, status, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (turn_id, thread_id, prompt, "queued", now),
            )
            conn.execute(
                "UPDATE web_threads SET updated_at = ?, active_status = ? WHERE thread_id = ?",
                (now, "queued", thread_id),
            )
        return TurnRecord(
            turn_id=turn_id,
            thread_id=thread_id,
            prompt=prompt,
            status="queued",
            created_at=now,
            started_at=None,
            finished_at=None,
            error=None,
        )

    def mark_turn_running(self, turn_id: str) -> None:
        """Mark one turn as running."""
        now = utc_now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT thread_id FROM turns WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
            conn.execute(
                "UPDATE turns SET status = ?, started_at = ? WHERE turn_id = ?",
                ("running", now, turn_id),
            )
            if row is not None:
                conn.execute(
                    "UPDATE web_threads SET updated_at = ?, active_status = ? WHERE thread_id = ?",
                    (now, "running", row["thread_id"]),
                )

    def mark_turn_waiting(self, turn_id: str) -> None:
        """Mark one turn as awaiting approval/decision."""
        now = utc_now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT thread_id FROM turns WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
            conn.execute(
                "UPDATE turns SET status = ? WHERE turn_id = ?",
                ("awaiting_decision", turn_id),
            )
            if row is not None:
                conn.execute(
                    "UPDATE web_threads SET updated_at = ?, active_status = ? WHERE thread_id = ?",
                    (now, "awaiting_decision", row["thread_id"]),
                )

    def complete_turn(self, turn_id: str, *, status: str, error: str | None = None) -> None:
        """Complete one turn."""
        now = utc_now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT thread_id FROM turns WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
            conn.execute(
                """
                UPDATE turns
                SET status = ?, finished_at = ?, error = ?
                WHERE turn_id = ?
                """,
                (status, now, error, turn_id),
            )
            if row is not None:
                thread_status = "idle" if status == "succeeded" else status
                conn.execute(
                    "UPDATE web_threads SET updated_at = ?, active_status = ? WHERE thread_id = ?",
                    (now, thread_status, row["thread_id"]),
                )

    def get_turn(self, turn_id: str) -> dict | None:
        """Return one persisted turn."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT turn_id, thread_id, prompt, status, created_at, started_at, finished_at, error
                FROM turns
                WHERE turn_id = ?
                """,
                (turn_id,),
            ).fetchone()
        if row is None:
            return None
        return TurnRecord(
            turn_id=row["turn_id"],
            thread_id=row["thread_id"],
            prompt=row["prompt"],
            status=row["status"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            error=row["error"],
        ).to_dict()

    def list_turns(self, thread_id: str) -> list[dict]:
        """List turns for a thread."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT turn_id, thread_id, prompt, status, created_at, started_at, finished_at, error
                FROM turns
                WHERE thread_id = ?
                ORDER BY created_at DESC
                """,
                (thread_id,),
            ).fetchall()
        return [
            TurnRecord(
                turn_id=row["turn_id"],
                thread_id=row["thread_id"],
                prompt=row["prompt"],
                status=row["status"],
                created_at=row["created_at"],
                started_at=row["started_at"],
                finished_at=row["finished_at"],
                error=row["error"],
            ).to_dict()
            for row in rows
        ]

    def append_event(
        self,
        thread_id: str,
        turn_id: str | None,
        kind: str,
        payload: dict,
    ) -> EventRecord:
        """Persist one event row."""
        now = utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO events (thread_id, turn_id, kind, payload, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (thread_id, turn_id, kind, json.dumps(payload), now),
            )
            conn.execute(
                "UPDATE web_threads SET updated_at = ? WHERE thread_id = ?",
                (now, thread_id),
            )
            event_id = int(cursor.lastrowid)
        return EventRecord(
            event_id=event_id,
            thread_id=thread_id,
            turn_id=turn_id,
            kind=kind,
            payload=payload,
            created_at=now,
        )

    def list_events(
        self,
        thread_id: str,
        *,
        after_event_id: int | None = None,
        turn_id: str | None = None,
    ) -> list[dict]:
        """List events for a thread."""
        clauses = ["thread_id = ?"]
        params: list[object] = [thread_id]
        if after_event_id is not None:
            clauses.append("event_id > ?")
            params.append(after_event_id)
        if turn_id is not None:
            clauses.append("turn_id = ?")
            params.append(turn_id)
        where_sql = " AND ".join(clauses)

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT event_id, thread_id, turn_id, kind, payload, created_at
                FROM events
                WHERE {where_sql}
                ORDER BY event_id ASC
                """,
                params,
            ).fetchall()
        return [
            EventRecord(
                event_id=int(row["event_id"]),
                thread_id=row["thread_id"],
                turn_id=row["turn_id"],
                kind=row["kind"],
                payload=json.loads(row["payload"]),
                created_at=row["created_at"],
            ).to_dict()
            for row in rows
        ]

    def clear_thread_events(self, thread_id: str) -> int:
        """Delete web-only events for one thread."""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM events WHERE thread_id = ?", (thread_id,))
            return cursor.rowcount

    def delete_thread(self, thread_id: str) -> bool:
        """Delete one thread plus its dependent web rows."""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM web_threads WHERE thread_id = ?", (thread_id,))
            return cursor.rowcount > 0
