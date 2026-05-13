from __future__ import annotations

from pathlib import Path

from apps.webapp.store import AppStore


def test_thread_turn_and_event_lifecycle(tmp_path: Path) -> None:
    store = AppStore(tmp_path / "webapp.db")
    thread = store.create_thread(thread_id="web-1", assistant_id="agent", cwd=str(tmp_path))
    turn = store.create_turn(thread.thread_id, "inspect repository")
    store.mark_turn_running(turn.turn_id)
    event = store.append_event(
        thread.thread_id,
        turn.turn_id,
        "turn.started",
        {"prompt": "inspect repository"},
    )
    store.complete_turn(turn.turn_id, status="succeeded")

    loaded_thread = store.get_thread(thread.thread_id)
    assert loaded_thread is not None
    assert loaded_thread.thread_id == thread.thread_id
    assert store.get_turn(turn.turn_id)["status"] == "succeeded"
    assert store.list_turns(thread.thread_id)[0]["turn_id"] == turn.turn_id
    assert store.list_events(thread.thread_id)[0]["event_id"] == event.event_id


def test_delete_thread_removes_turns_and_events(tmp_path: Path) -> None:
    store = AppStore(tmp_path / "webapp.db")
    thread = store.create_thread(thread_id="web-1", assistant_id="agent", cwd=str(tmp_path))
    turn = store.create_turn(thread.thread_id, "inspect repository")
    store.append_event(thread.thread_id, turn.turn_id, "turn.started", {"prompt": "inspect repository"})

    deleted = store.delete_thread(thread.thread_id)

    assert deleted is True
    assert store.get_thread(thread.thread_id) is None
    assert store.list_turns(thread.thread_id) == []
    assert store.list_events(thread.thread_id) == []


def test_list_events_after_cursor(tmp_path: Path) -> None:
    store = AppStore(tmp_path / "webapp.db")
    thread = store.create_thread(thread_id="web-1", assistant_id="agent", cwd=str(tmp_path))
    turn = store.create_turn(thread.thread_id, "hello")
    first = store.append_event(thread.thread_id, turn.turn_id, "turn.started", {"prompt": "hello"})
    second = store.append_event(thread.thread_id, turn.turn_id, "assistant.delta", {"text": "ok"})

    events = store.list_events(thread.thread_id, after_event_id=first.event_id)

    assert [event["event_id"] for event in events] == [second.event_id]


def test_thread_model_spec_persists_across_store_roundtrip(tmp_path: Path) -> None:
    store = AppStore(tmp_path / "webapp.db")
    store.create_thread(
        thread_id="web-1",
        assistant_id="agent",
        cwd=str(tmp_path),
        model_spec="anthropic:claude-sonnet-4-6",
    )

    loaded = store.get_thread("web-1")

    assert loaded is not None
    assert loaded.model_spec == "anthropic:claude-sonnet-4-6"
