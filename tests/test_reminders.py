from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from carmen_agent.reminders import (
    build_reminder_events,
    deliver_reminders,
    pending_reminders,
)
from carmen_agent.skills.schemas import TodoItem


def _task(**overrides: object) -> TodoItem:
    values: dict[str, object] = {
        "source": "manual",
        "id": "manual-1",
        "title": "Prepare launch notes",
        "why": "Added manually",
        "urgency": "this_week",
        "needs_reply": False,
        "sources": ["manual"],
    }
    values.update(overrides)
    return TodoItem(**values)  # type: ignore[arg-type]


def test_explicit_reminder_fires_only_after_its_time() -> None:
    task = _task(remind_at="2026-07-24T09:30+00:00")
    before = build_reminder_events(
        [task],
        now=datetime(2026, 7, 24, 9, 29, tzinfo=timezone.utc),
    )
    after = build_reminder_events(
        [task],
        now=datetime(2026, 7, 24, 9, 30, tzinfo=timezone.utc),
    )
    assert before == []
    assert after[0].task_id == "manual-1"
    assert after[0].key.endswith("2026-07-24T09:30+00:00")


def test_today_and_overdue_tasks_create_daily_reminders() -> None:
    events = build_reminder_events(
        [
            _task(id="today", due="2026-07-24"),
            _task(id="overdue", due="2026-07-23"),
            _task(id="priority", urgency="today"),
        ],
        now=datetime(2026, 7, 24, 12, tzinfo=timezone.utc),
    )
    assert {event.task_id for event in events} == {"today", "overdue", "priority"}
    assert any(event.reason == "Due today" for event in events)
    assert any(event.reason.startswith("Overdue") for event in events)


def test_deliveries_are_deduplicated(tmp_path: Path) -> None:
    state = tmp_path / "reminders.json"
    event = build_reminder_events(
        [_task(urgency="today")],
        now=datetime(2026, 7, 24, 12, tzinfo=timezone.utc),
    )[0]
    seen: list[str] = []
    delivered = deliver_reminders(
        [event],
        state_path=state,
        notifier=lambda current: seen.append(current.key),
        now=datetime(2026, 7, 24, 12, tzinfo=timezone.utc),
    )
    assert delivered == 1
    assert seen == [event.key]
    assert pending_reminders([event], state_path=state) == []
    raw = json.loads(state.read_text(encoding="utf-8"))
    assert event.key in raw["deliveries"]
