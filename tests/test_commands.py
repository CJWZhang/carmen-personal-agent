from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from carmen_agent.commands import (
    CommandError,
    add_task,
    execute_task,
    format_task_list,
    load_tasks,
    mark_complete,
    resolve_task,
    set_task_reminder,
    snooze_task,
    update_task,
)
from carmen_agent.skills.schemas import TodoItem, dump_todos


def _tasks() -> list[TodoItem]:
    return [
        TodoItem(
            source="github",
            id="work-abc123",
            title="Review checkout PR",
            why="Review requested",
            urgency="today",
            needs_reply=False,
            sources=["github"],
            source_ids=["github:org/repo#42"],
            links=["https://github.com/org/repo/pull/42"],
        ),
        TodoItem(
            source="slack",
            id="work-def456",
            title="Reply to Alice",
            why="Alice asked for a decision",
            urgency="this_week",
            needs_reply=True,
            sources=["slack"],
            source_ids=["slack:123"],
        ),
    ]


def test_task_commands_list_and_resolve(tmp_path: Path) -> None:
    todos_path = tmp_path / "todos.json"
    dump_todos(_tasks(), todos_path)
    loaded = load_tasks(todos_path)
    listing = format_task_list(loaded)
    assert "1. [today] Review checkout PR" in listing
    assert resolve_task(loaded, "2").id == "work-def456"
    assert resolve_task(loaded, "work-abc").title == "Review checkout PR"


def test_load_tasks_rejects_unknown_fields_and_wrong_runtime_types(tmp_path: Path) -> None:
    path = tmp_path / "todos.json"
    raw = {
        "source": "slack",
        "id": "bad",
        "title": "Reply",
        "why": "Asked",
        "urgency": "today",
        "needs_reply": "false",
        "unknown": "field",
    }
    path.write_text(json.dumps([raw]), encoding="utf-8")
    with pytest.raises(CommandError, match="unknown fields"):
        load_tasks(path)
    raw.pop("unknown")
    path.write_text(json.dumps([raw]), encoding="utf-8")
    with pytest.raises(CommandError, match="boolean fields"):
        load_tasks(path)


def test_mark_complete_updates_open_tasks_state_and_brief(tmp_path: Path) -> None:
    todos_path = tmp_path / "todos.json"
    state_path = tmp_path / "state.json"
    brief_path = tmp_path / "TODO.md"
    habits_path = tmp_path / "habits.json"
    tasks = _tasks()
    dump_todos(tasks, todos_path)
    result = mark_complete(
        tasks,
        tasks[0],
        todos_path=todos_path,
        state_path=state_path,
        brief_path=brief_path,
        habits_path=habits_path,
    )
    assert "Completed work-abc123" in result
    assert [task.id for task in load_tasks(todos_path)] == ["work-def456"]
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["tasks"][0]["status"] == "done"
    assert "Review checkout PR" in brief_path.read_text(encoding="utf-8")
    assert not habits_path.exists()


def test_mark_complete_records_dismissal_to_habits(tmp_path: Path) -> None:
    from carmen_agent.skills.habits import format_suppression_hints

    todos_path = tmp_path / "todos.json"
    state_path = tmp_path / "state.json"
    brief_path = tmp_path / "TODO.md"
    habits_path = tmp_path / "habits.json"
    now = datetime(2026, 7, 23, 12, tzinfo=timezone.utc)
    tasks = [
        TodoItem(
            source="gmail",
            id=f"work-vanta-{i}",
            title="Vanta vendor risk update",
            why="Vanta wants risk review",
            urgency="fyi",
            needs_reply=False,
            people=["Vanta", "Carmen"],
            first_seen=now.isoformat(),
        )
        for i in range(2)
    ]
    dump_todos(tasks, todos_path)
    for task in tasks:
        mark_complete(
            list(tasks),
            task,
            todos_path=todos_path,
            state_path=state_path,
            brief_path=brief_path,
            habits_path=habits_path,
            learn_dismissal=True,
        )
    hints = format_suppression_hints(now=now, path=habits_path, min_count=2)
    assert "gmail items from vanta" in hints.lower()
    assert "dismissed 2 time(s)" in hints


def test_execute_task_sends_versioned_payload_and_completes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    todos_path = tmp_path / "todos.json"
    state_path = tmp_path / "state.json"
    brief_path = tmp_path / "TODO.md"
    habits_path = tmp_path / "habits.json"
    tasks = _tasks()
    dump_todos(tasks, todos_path)
    captured: dict[str, str] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command[0]
        captured["input"] = kwargs["input"]
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout='{"status":"completed","message":"PR review submitted"}',
            stderr="",
        )

    monkeypatch.setattr("carmen_agent.commands.subprocess.run", fake_run)
    result = execute_task(
        tasks,
        tasks[0],
        executor_command="/tmp/carmen-action-adapter",
        todos_path=todos_path,
        state_path=state_path,
        brief_path=brief_path,
        habits_path=habits_path,
    )
    payload = json.loads(captured["input"])
    assert captured["command"] == "/tmp/carmen-action-adapter"
    assert payload["version"] == 2
    assert payload["connector_action"] is None
    assert payload["task"]["id"] == "work-abc123"
    assert payload["safety"]["user_explicitly_requested_execution"] is True
    assert "PR review submitted" in result
    assert [task.id for task in load_tasks(todos_path)] == ["work-def456"]


def test_typed_connector_action_stays_visible_until_source_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    todos_path = tmp_path / "todos.json"
    state_path = tmp_path / "state.json"
    brief_path = tmp_path / "TODO.md"
    receipts_path = tmp_path / "receipts.json"
    task = TodoItem(
        source="calendar", id="event-1", title="Respond to launch review",
        why="RSVP is still required", urgency="today", needs_reply=True,
        sources=["calendar"], source_ids=["calendar:event-1"],
    )

    def fake_run(command, **kwargs):
        payload = json.loads(kwargs["input"])
        assert payload["connector_action"]["id"] == "calendar.accept"
        return subprocess.CompletedProcess(
            command, 0,
            stdout='{"status":"submitted","message":"RSVP sent","external_id":"evt-1"}',
            stderr="",
        )

    monkeypatch.setattr("carmen_agent.commands.subprocess.run", fake_run)
    result = execute_task(
        [task], task, executor_command="/tmp/adapter",
        todos_path=todos_path, state_path=state_path, brief_path=brief_path,
        receipts_path=receipts_path, action_id="calendar.accept",
    )

    active = load_tasks(todos_path)
    assert result == "RSVP sent"
    assert active[0].status == "waiting"
    assert active[0].lifecycle_stage == "verifying"
    receipts = json.loads(receipts_path.read_text(encoding="utf-8"))
    assert receipts[0]["verified"] is False
    assert active[0].resolution_receipt_id == receipts[0]["id"]


def test_typed_connector_action_completes_only_with_verified_external_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    todos_path = tmp_path / "todos.json"
    state_path = tmp_path / "state.json"
    brief_path = tmp_path / "TODO.md"
    receipts_path = tmp_path / "receipts.json"
    task = TodoItem(
        source="calendar", id="event-1", title="Respond to launch review",
        why="RSVP is still required", urgency="today", needs_reply=True,
        sources=["calendar"], source_ids=["calendar:event-1"],
    )

    monkeypatch.setattr(
        "carmen_agent.commands.subprocess.run",
        lambda command, **_: subprocess.CompletedProcess(
            command, 0,
            stdout='{"status":"verified","message":"Calendar shows accepted","external_id":"evt-1"}',
            stderr="",
        ),
    )
    execute_task(
        [task], task, executor_command="/tmp/adapter",
        todos_path=todos_path, state_path=state_path, brief_path=brief_path,
        receipts_path=receipts_path, action_id="calendar.accept",
    )

    assert load_tasks(todos_path) == []
    state = json.loads(state_path.read_text(encoding="utf-8"))["tasks"][0]
    assert state["resolution_mode"] == "source_verified"
    assert state["last_verified_at"]
    assert state["resolution_receipt_id"]


def test_typed_connector_action_rejects_unbounded_or_unknown_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = TodoItem(
        source="calendar", id="event-1", title="Respond to launch review",
        why="RSVP is still required", urgency="today", needs_reply=True,
        sources=["calendar"], source_ids=["calendar:event-1"],
    )
    receipts_path = tmp_path / "receipts.json"
    monkeypatch.setattr(
        "carmen_agent.commands.subprocess.run",
        lambda command, **_: subprocess.CompletedProcess(
            command, 0,
            stdout='{"status":"submitted","message":"sent","extra":"unsafe"}',
            stderr="",
        ),
    )

    with pytest.raises(CommandError, match="unknown field"):
        execute_task(
            [task], task, executor_command="/tmp/adapter",
            todos_path=tmp_path / "todos.json",
            state_path=tmp_path / "state.json",
            brief_path=tmp_path / "TODO.md",
            receipts_path=receipts_path,
            action_id="calendar.accept",
        )

    receipts = json.loads(receipts_path.read_text(encoding="utf-8"))
    assert receipts[-1]["status"] == "failed"
    assert task.lifecycle_stage == "open"


def test_corrupt_receipt_history_blocks_external_write_before_adapter_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = TodoItem(
        source="calendar", id="event-1", title="Respond to launch review",
        why="RSVP is still required", urgency="today", needs_reply=True,
        sources=["calendar"], source_ids=["calendar:event-1"],
    )
    receipts_path = tmp_path / "receipts.json"
    receipts_path.write_text("not json", encoding="utf-8")
    called = False

    def fake_run(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        return subprocess.CompletedProcess([], 0, "{}", "")

    monkeypatch.setattr("carmen_agent.commands.subprocess.run", fake_run)
    with pytest.raises(CommandError, match="no external action was run"):
        execute_task(
            [task], task, executor_command="/tmp/adapter",
            receipts_path=receipts_path, action_id="calendar.accept",
        )
    assert called is False
    assert receipts_path.read_text(encoding="utf-8") == "not json"


def test_add_manual_task_with_due_date_and_reminder(tmp_path: Path) -> None:
    todos_path = tmp_path / "todos.json"
    state_path = tmp_path / "state.json"
    brief_path = tmp_path / "TODO.md"
    result = add_task(
        [],
        "Prepare launch notes",
        urgency="today",
        due="2026-07-24",
        remind_at="2026-07-24T15:00-04:00",
        todos_path=todos_path,
        state_path=state_path,
        brief_path=brief_path,
    )
    tasks = load_tasks(todos_path)
    assert "Added task" in result
    assert len(tasks) == 1
    assert tasks[0].source == "manual"
    assert tasks[0].due == "2026-07-24"
    assert tasks[0].remind_at == "2026-07-24T15:00-04:00"
    assert "Prepare launch notes" in brief_path.read_text(encoding="utf-8")


def test_update_task_persists_validated_user_overrides(tmp_path: Path) -> None:
    todos_path = tmp_path / "todos.json"
    state_path = tmp_path / "state.json"
    brief_path = tmp_path / "TODO.md"
    tasks = _tasks()
    dump_todos(tasks, todos_path)
    result = update_task(
        tasks,
        tasks[0],
        title="Review checkout migration PR",
        why="Focus on the bridge boundary.",
        urgency="now",
        due="2026-08-14",
        remind_at="2026-08-13 09:30+00:00",
        todos_path=todos_path,
        state_path=state_path,
        brief_path=brief_path,
    )
    loaded = load_tasks(todos_path)[0]
    assert result.startswith("Updated work-abc123")
    assert loaded.title == "Review checkout migration PR"
    assert loaded.why == "Focus on the bridge boundary."
    assert loaded.urgency == "now"
    assert loaded.due == "2026-08-14"
    assert loaded.remind_at == "2026-08-13T09:30+00:00"
    assert set(loaded.edited_fields) == {
        "title", "why", "urgency", "due", "remind_at"
    }
    assert "Review checkout migration PR" in brief_path.read_text(encoding="utf-8")


def test_set_and_snooze_task_reminder(tmp_path: Path) -> None:
    todos_path = tmp_path / "todos.json"
    state_path = tmp_path / "state.json"
    brief_path = tmp_path / "TODO.md"
    tasks = _tasks()
    dump_todos(tasks, todos_path)
    set_task_reminder(
        tasks,
        tasks[0],
        "2026-07-24T09:30-04:00",
        todos_path=todos_path,
        state_path=state_path,
        brief_path=brief_path,
    )
    assert load_tasks(todos_path)[0].remind_at == "2026-07-24T09:30-04:00"
    snooze_task(
        tasks,
        tasks[0],
        minutes=45,
        now=datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc),
        todos_path=todos_path,
        state_path=state_path,
        brief_path=brief_path,
    )
    assert load_tasks(todos_path)[0].remind_at == "2026-07-24T10:45+00:00"
