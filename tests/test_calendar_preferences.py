from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from carmen_agent.skills.calendar_preferences import (
    CalendarPreferenceError,
    calendar_preference_for,
    load_calendar_preferences,
    personalize_calendar_tasks,
    record_calendar_preference,
)
from carmen_agent.skills.schemas import InboxItem, TodoItem


def _item(**metadata: object) -> InboxItem:
    return InboxItem(
        source="calendar",
        id="office-hours-1",
        sender="AI team",
        subject="Internal AI Office Hour",
        body="Optional weekly office hour.",
        kind="meeting_prep",
        status="open",
        metadata={"organizer": "AI team", **metadata},
    )


def _task() -> TodoItem:
    return TodoItem(
        source="calendar",
        id="office-hours-1",
        title="Prepare for Internal AI Office Hour",
        why="Weekly meeting",
        urgency="today",
        needs_reply=False,
        confidence=0.8,
        sources=["calendar"],
        source_ids=["calendar:office-hours-1"],
    )


def test_explicit_skip_preference_makes_recurring_meeting_optional(tmp_path: Path) -> None:
    path = tmp_path / "calendar-preferences.json"
    record_calendar_preference(
        "Internal AI Office Hour",
        organizer="AI team",
        disposition="usually_skip",
        path=path,
        now=datetime(2026, 8, 13, tzinfo=timezone.utc),
    )
    task = _task()
    personalize_calendar_tasks([task], [_item()], path=path)

    assert task.urgency == "fyi"
    assert task.priority_adjustment == -45
    assert task.confidence == 0.35
    assert "usually skip" in task.preference_reason


def test_historical_response_behavior_downranks_only_optional_unprotected_meetings(
    tmp_path: Path,
) -> None:
    task = _task()
    item = _item(
        optional=True,
        needs_decision=False,
        historical_instances=6,
        historical_accepted=1,
        historical_declined=3,
        historical_tentative=0,
        historical_no_response=2,
        historical_attended=0,
        historical_missed=0,
    )
    result = calendar_preference_for(task, item, path=tmp_path / "missing.json")
    assert result.priority_adjustment == -35
    assert result.urgency_cap == "fyi"
    assert "6 prior invitations" in result.reason

    protected = _item(
        optional=True,
        needs_decision=True,
        historical_instances=6,
        historical_accepted=0,
        historical_declined=6,
        historical_tentative=0,
        historical_no_response=0,
        historical_attended=0,
        historical_missed=0,
    )
    assert calendar_preference_for(
        _task(), protected, path=tmp_path / "missing.json"
    ).priority_adjustment == 0


def test_calendar_preference_profile_fails_closed_on_corruption(tmp_path: Path) -> None:
    path = tmp_path / "calendar-preferences.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(CalendarPreferenceError, match="not valid JSON"):
        load_calendar_preferences(path)
    with pytest.raises(CalendarPreferenceError, match="not valid JSON"):
        record_calendar_preference(
            "Internal AI Office Hour",
            disposition="usually_skip",
            path=path,
        )
    assert path.read_text(encoding="utf-8") == "not json"


def test_calendar_preference_rule_is_bounded_and_inspectable(tmp_path: Path) -> None:
    path = tmp_path / "calendar-preferences.json"
    record_calendar_preference(
        "Internal AI Office Hour",
        disposition="usually_skip",
        path=path,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["rules"][0]["key"] == "internal ai office hour"
    assert payload["rules"][0]["source"] == "user_explicit"
