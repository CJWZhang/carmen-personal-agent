from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from carmen_agent.skills.draft import draft_all, draft_reply
from carmen_agent.source_rules import partition_source_items
from carmen_agent.skills.schedule import build_launch_agent, build_reminder_launch_agent
from carmen_agent.skills.schemas import (
    InboxItem,
    SchemaValidationError,
    TodoItem,
    dump_todos,
    load_inbox,
)
from carmen_agent.skills.synthesis import reconcile_state, synthesize_todos
from carmen_agent.skills.todo import render_morning_brief, render_todo_md
from carmen_agent.skills.triage import triage_all


class FakeSession:
    system_prompt = "test prompt"

    def __init__(self, outputs: list[str]) -> None:
        self._outputs = list(outputs)
        self.calls: list[list[dict[str, str]]] = []

    def generate(self, messages: list[dict[str, str]], **_: object) -> str:
        self.calls.append(messages)
        return self._outputs.pop(0)


def _write_inbox(tmp_path: Path) -> Path:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "gmail.json").write_text(
        json.dumps(
            [
                {
                    "source": "gmail",
                    "id": "m1",
                    "sender": "alice@example.com",
                    "subject": "Deck review",
                    "body": "Can you look at slide 12 before Thursday?",
                    "url": "https://mail/example/m1",
                    "ts": "2026-07-22T14:03:00Z",
                }
            ]
        ),
        encoding="utf-8",
    )
    return inbox


def test_load_inbox_roundtrip(tmp_path: Path) -> None:
    inbox = _write_inbox(tmp_path)
    items = load_inbox(inbox)
    assert len(items) == 1
    assert items[0].id == "m1"
    assert items[0].source == "gmail"


def test_dump_todos_replaces_existing_snapshot_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "todos.json"
    original = TodoItem(
        source="manual",
        id="original",
        title="Original task",
        why="Keep the last complete snapshot readable.",
        urgency="today",
        needs_reply=False,
    )
    replacement = TodoItem(
        source="manual",
        id="replacement",
        title="Replacement task",
        why="Publish the new snapshot in one operation.",
        urgency="today",
        needs_reply=False,
    )
    dump_todos([original], path)
    path_replace = Path.replace

    def checked_replace(source: Path, target: Path) -> Path:
        assert target == path
        visible = json.loads(path.read_text(encoding="utf-8"))
        assert visible[0]["title"] == "Original task"
        return path_replace(source, target)

    monkeypatch.setattr(Path, "replace", checked_replace)
    dump_todos([replacement], path)

    visible = json.loads(path.read_text(encoding="utf-8"))
    assert visible[0]["title"] == "Replacement task"


def test_load_inbox_rejects_filename_mismatch_and_unsafe_url(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "slack.json").write_text(
        json.dumps(
            [
                {
                    "source": "gmail",
                    "id": "m1",
                    "sender": "Alice",
                    "subject": "Hi",
                    "body": "Please reply",
                    "url": "javascript:alert(1)",
                }
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(SchemaValidationError, match="must use http or https"):
        load_inbox(inbox)


def test_triage_rejects_string_boolean_instead_of_treating_it_as_true(tmp_path: Path) -> None:
    items = load_inbox(_write_inbox(tmp_path))
    session = FakeSession(
        [
            '{"title":"Reply","why":"Asked","urgency":"today",'
            '"needs_reply":"false","due":"","people":[],"confidence":0.8}'
        ]
    )
    todos = triage_all(session, items)
    assert todos[0].needs_reply is False
    assert todos[0].confidence == 0.2
    assert "triage-parse-error" in todos[0].why


def test_triage_all_parses_json(tmp_path: Path) -> None:
    inbox = _write_inbox(tmp_path)
    items = load_inbox(inbox)
    session = FakeSession(
        [
            '{"title": "Review slide 12", "why": "Alice needs feedback before Thursday", '
            '"urgency": "today", "needs_reply": true, "due": "2026-07-23"}'
        ]
    )
    todos = triage_all(session, items)
    assert todos[0].title == "Review slide 12"
    assert todos[0].urgency == "today"
    assert todos[0].needs_reply is True
    assert todos[0].links == ["https://mail/example/m1"]


def test_triage_drops_ungrounded_due(tmp_path: Path) -> None:
    inbox = _write_inbox(tmp_path)
    items = load_inbox(inbox)
    # Body says "before Thursday" — no literal date. Model hallucinates one anyway.
    session = FakeSession(
        [
            '{"title": "Review slide 12", "why": "Alice wants feedback", '
            '"urgency": "today", "needs_reply": true, "due": "2023-10-10"}'
        ]
    )
    todos = triage_all(session, items)
    assert todos[0].due == ""


def test_triage_keeps_due_when_grounded_in_body(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "gmail.json").write_text(
        json.dumps(
            [
                {
                    "source": "gmail",
                    "id": "m2",
                    "sender": "alice@example.com",
                    "subject": "Deck review",
                    "body": "Please send comments by 2026-07-24.",
                    "url": "",
                    "ts": "2026-07-22T14:03:00Z",
                }
            ]
        ),
        encoding="utf-8",
    )
    items = load_inbox(inbox)
    session = FakeSession(
        [
            '{"title": "Send deck comments", "why": "Alice needs comments", '
            '"urgency": "this_week", "needs_reply": true, "due": "2026-07-24"}'
        ]
    )
    todos = triage_all(session, items)
    assert todos[0].due == "2026-07-24"


def test_triage_trusts_collector_status_over_model(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "github.json").write_text(
        json.dumps(
            [
                {
                    "source": "github",
                    "id": "pr-99",
                    "sender": "reviewer",
                    "subject": "PR 99",
                    "body": "Waiting on the author to address findings.",
                    "url": "https://github.com/org/repo/pull/99",
                    "status": "open",
                }
            ]
        ),
        encoding="utf-8",
    )
    items = load_inbox(inbox)
    # Model tries to flip the item to `waiting` based on the snippet.
    session = FakeSession(
        [
            '{"title": "Review PR 99", "why": "Reviewer expected", '
            '"urgency": "today", "needs_reply": false, "due": "", "status": "waiting"}'
        ]
    )
    todos = triage_all(session, items)
    assert todos[0].status == "open"


def test_triage_caps_confidence_when_collector_says_waiting(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "github.json").write_text(
        json.dumps(
            [
                {
                    "source": "github",
                    "id": "pr-337161",
                    "sender": "reviewer",
                    "subject": "Do not approve yet",
                    "body": "Critical finding blocks approval.",
                    "url": "https://github.com/org/repo/pull/337161",
                    "status": "waiting",
                }
            ]
        ),
        encoding="utf-8",
    )
    items = load_inbox(inbox)
    # Model returns high confidence based on the urgent-sounding body.
    session = FakeSession(
        [
            '{"title": "Review PR 337161", "why": "Critical finding blocks approval", '
            '"urgency": "now", "needs_reply": false, "due": "", "confidence": 0.97}'
        ]
    )
    todos = triage_all(session, items)
    assert todos[0].status == "waiting"
    assert todos[0].confidence <= 0.4


def test_habits_records_dismissal_and_produces_hints(tmp_path: Path) -> None:
    from carmen_agent.skills.habits import format_suppression_hints, record_dismissal

    habits_path = tmp_path / "habits.json"
    now = datetime(2026, 7, 23, 12, tzinfo=timezone.utc)
    for _ in range(2):
        record_dismissal(
            TodoItem(
                source="gmail",
                id="ignored",
                title="Vanta vendor risk review",
                why="",
                urgency="fyi",
                needs_reply=False,
                people=["Vanta", "Carmen"],
                first_seen=(now - timedelta(hours=1)).isoformat(),
            ),
            now=now,
            path=habits_path,
        )
    # A single dismissal from a different sender shouldn't reach the min_count threshold.
    record_dismissal(
        TodoItem(
            source="gmail",
            id="ignored-2",
            title="Kat Wu event",
            why="",
            urgency="fyi",
            needs_reply=False,
            people=["Kat Wu"],
            first_seen=now.isoformat(),
        ),
        now=now,
        path=habits_path,
    )
    hints = format_suppression_hints(now=now, path=habits_path, min_count=2)
    assert "gmail items from vanta" in hints.lower()
    assert "dismissed 2 time(s)" in hints
    assert "kat wu" not in hints.lower()


def test_habits_hints_ignore_stale_dismissals(tmp_path: Path) -> None:
    from carmen_agent.skills.habits import format_suppression_hints, record_dismissal

    habits_path = tmp_path / "habits.json"
    long_ago = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    for _ in range(3):
        record_dismissal(
            TodoItem(
                source="slack",
                id="x",
                title="announcements ping",
                why="",
                urgency="fyi",
                needs_reply=False,
                people=["Announcer"],
                first_seen=long_ago.isoformat(),
            ),
            now=long_ago,
            path=habits_path,
        )
    now = datetime(2026, 7, 23, tzinfo=timezone.utc)
    hints = format_suppression_hints(now=now, path=habits_path, lookback_days=30)
    assert hints == ""


def test_habits_do_not_learn_from_legacy_completion_records(tmp_path: Path) -> None:
    from carmen_agent.skills.habits import format_suppression_hints

    now = datetime.now(timezone.utc)
    path = tmp_path / "habits.json"
    path.write_text(
        json.dumps({
            "version": 1,
            "dismissals": [
                {
                    "source": "calendar",
                    "person": "",
                    "title": "Respond to launch review",
                    "dismissed_at": now.isoformat(),
                    "time_to_dismiss_seconds": None,
                    "evidence": "Calendar shows accepted",
                }
                for _ in range(5)
            ],
        }),
        encoding="utf-8",
    )
    assert format_suppression_hints(now=now, path=path, min_count=2) == ""


def test_habits_hints_reach_triage_prompt(tmp_path: Path) -> None:
    from carmen_agent.skills.triage import triage_item

    item = InboxItem(
        source="gmail",
        id="m1",
        sender="alice@example.com",
        subject="Update",
        body="fyi update",
    )
    session = FakeSession(
        [
            '{"title": "Read update", "why": "informational", "urgency": "fyi", '
            '"needs_reply": false, "due": "", "confidence": 0.6}'
        ]
    )
    hint_text = "Recent dismissal patterns: - gmail items from alice (dismissed 3 time(s))"
    triage_item(session, item, suppression_hints=hint_text)
    assert session.calls, "expected model to be invoked"
    system_prompt = session.calls[0][0]["content"]
    assert hint_text in system_prompt


def test_triage_falls_back_on_bad_json(tmp_path: Path) -> None:
    inbox = _write_inbox(tmp_path)
    items = load_inbox(inbox)
    session = FakeSession(["not json at all"])
    todos = triage_all(session, items)
    assert todos[0].urgency == "fyi"
    assert todos[0].needs_reply is False
    assert "triage-parse-error" in todos[0].why


def test_draft_reply_produces_body() -> None:
    item = InboxItem(
        source="slack",
        id="s1",
        sender="bob",
        subject="",
        body="can we push standup 15 min?",
        channel="#team",
    )
    todo = TodoItem(
        source="slack",
        id="s1",
        title="Confirm standup shift",
        why="Bob wants standup moved by 15m",
        urgency="today",
        needs_reply=True,
    )
    session = FakeSession(["yep, 10:15 works — I'll update the invite"])
    draft = draft_reply(session, item, todo)
    assert draft.todo_id == "s1"
    assert "10:15" in draft.body


def test_draft_reply_retries_model_control_artifacts() -> None:
    item = InboxItem(
        source="slack",
        id="s1",
        sender="bob",
        subject="",
        body="can you review this?",
    )
    todo = TodoItem(
        source="slack",
        id="s1",
        title="Review request",
        why="Bob requested a review",
        urgency="today",
        needs_reply=True,
    )
    session = FakeSession(["<think>\nworking", "yep, taking a look now!"])
    draft = draft_reply(session, item, todo)
    assert draft.body == "yep, taking a look now!"
    assert len(session.calls) == 2


def test_draft_reply_uses_safe_fallback_after_two_invalid_outputs() -> None:
    item = InboxItem(
        source="calendar",
        id="c1",
        sender="Calendar invitation",
        subject="Team sync",
        body="Please RSVP.",
    )
    todo = TodoItem(
        source="calendar",
        id="c1",
        title="Respond to team sync",
        why="RSVP requested",
        urgency="today",
        needs_reply=True,
    )
    session = FakeSession(["<tool_call>", "นัก"])
    draft = draft_reply(session, item, todo)
    assert draft.body == "thanks for the invite! let me double check and get back to you"


def test_draft_reply_rejects_mixed_cross_script_corruption() -> None:
    item = InboxItem(
        source="slack", id="s1", sender="Bob", subject="Review", body="Can you review?"
    )
    todo = TodoItem(
        source="slack", id="s1", title="Review", why="Bob asked",
        urgency="today", needs_reply=True,
    )
    session = FakeSession(["สิงหาคม I can't see this", "got it — taking a look"])
    draft = draft_reply(session, item, todo)
    assert draft.body == "got it — taking a look"
    assert len(session.calls) == 2


def test_draft_reply_rejects_accented_single_token_corruption() -> None:
    item = InboxItem(
        source="slack", id="s1", sender="Bob", subject="Review", body="Can you review?"
    )
    todo = TodoItem(
        source="slack", id="s1", title="Review", why="Bob asked",
        urgency="today", needs_reply=True,
    )
    session = FakeSession(["âm", "got it — taking a look"])
    draft = draft_reply(session, item, todo)
    assert draft.body == "got it — taking a look"
    assert len(session.calls) == 2


def test_draft_all_skips_no_reply(tmp_path: Path) -> None:
    item = InboxItem(source="gmail", id="m1", sender="a", subject="fyi", body="just an update")
    todo = TodoItem(source="gmail", id="m1", title="FYI", why="", urgency="fyi", needs_reply=False)
    session = FakeSession([])  # would raise IndexError if called
    draft_all(session, {"m1": item}, [todo], tmp_path / "drafts")
    assert todo.draft_path == ""


def test_draft_all_skips_gmail_and_calendar(tmp_path: Path) -> None:
    gmail_item = InboxItem(
        source="gmail", id="m1", sender="alice@example.com", subject="RSVP",
        body="RSVP by Friday",
    )
    calendar_item = InboxItem(
        source="calendar", id="c1", sender="organizer", subject="Lunch",
        body="Please respond",
    )
    todos = [
        TodoItem(source="gmail", id="m1", title="RSVP", why="", urgency="today", needs_reply=True),
        TodoItem(source="calendar", id="c1", title="Lunch", why="", urgency="today", needs_reply=True),
    ]
    # Empty output list — a call would raise IndexError. Skip means no calls.
    session = FakeSession([])
    draft_all(session, {"gmail:m1": gmail_item, "calendar:c1": calendar_item}, todos, tmp_path / "drafts")
    assert todos[0].draft_path == ""
    assert todos[1].draft_path == ""
    assert session.calls == []


def test_render_todo_md_groups_by_urgency() -> None:
    todos = [
        TodoItem(
            source="gmail", id="1", title="Urgent thing", why="deadline today",
            urgency="now", needs_reply=True, links=["https://x/1"], draft_path="data/drafts/1.md",
        ),
        TodoItem(
            source="slack", id="2", title="Later thing", why="",
            urgency="fyi", needs_reply=False,
        ),
    ]
    md = render_todo_md(todos)
    assert "## Now" in md
    assert "## FYI" in md
    assert md.index("## Now") < md.index("## FYI")
    assert "reply" in md
    assert "data/drafts/1.md" in md


def test_render_todo_md_empty() -> None:
    assert "No open items" in render_todo_md([])


def test_load_inbox_accepts_github_and_ignores_generated_todos(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "github.json").write_text(
        json.dumps(
            [
                {
                    "source": "github",
                    "id": "pr-42",
                    "sender": "reviewer",
                    "subject": "Review requested on org/repo#42",
                    "body": "Please take a look.",
                    "url": "https://github.com/org/repo/pull/42",
                    "kind": "pull_request",
                }
            ]
        ),
        encoding="utf-8",
    )
    (inbox / "todos.json").write_text(json.dumps([{"not": "an inbox item"}]), encoding="utf-8")
    items = load_inbox(inbox)
    assert len(items) == 1
    assert items[0].source == "github"


def test_synthesis_merges_related_slack_and_github_work() -> None:
    items = [
        InboxItem(
            source="slack",
            id="s1",
            sender="Alice",
            subject="Checkout follow-up",
            body="Can you address the feedback in org/repo#42?",
            related_keys=["checkout-launch"],
        ),
        InboxItem(
            source="github",
            id="g1",
            sender="Alice",
            subject="Requested changes on org/repo#42",
            body="One test is still failing.",
            url="https://github.com/org/repo/pull/42",
            related_keys=["checkout-launch"],
        ),
    ]
    todos = [
        TodoItem(
            source="slack",
            id="s1",
            title="Respond to checkout feedback",
            why="Alice is waiting",
            urgency="today",
            needs_reply=True,
            sources=["slack"],
            source_ids=["slack:s1"],
            people=["Alice"],
        ),
        TodoItem(
            source="github",
            id="g1",
            title="Fix PR 42 test",
            why="A required test is failing",
            urgency="now",
            needs_reply=False,
            links=["https://github.com/org/repo/pull/42"],
            sources=["github"],
            source_ids=["github:g1"],
            people=["Alice"],
        ),
    ]
    merged = synthesize_todos(items, todos)
    assert len(merged) == 1
    assert merged[0].urgency == "now"
    assert set(merged[0].sources) == {"slack", "github"}
    assert merged[0].needs_reply is True
    assert "https://github.com/org/repo/pull/42" in merged[0].links


def test_synthesis_does_not_merge_distinct_gmail_message_urls() -> None:
    items = [
        InboxItem(
            source="gmail",
            id="m1",
            sender="Alice",
            subject="RSVP",
            body="Please RSVP.",
            url="https://mail.google.com/mail/#all/m1",
        ),
        InboxItem(
            source="gmail",
            id="m2",
            sender="Vendor",
            subject="Vendor review",
            body="Please review this vendor.",
            url="https://mail.google.com/mail/#all/m2",
        ),
    ]
    todos = [
        TodoItem(
            source=item.source,
            id=item.id,
            title=item.subject,
            why=item.body,
            urgency="today",
            needs_reply=True,
            source_ids=[f"gmail:{item.id}"],
        )
        for item in items
    ]
    merged = synthesize_todos(items, todos)
    assert len(merged) == 2
    assert {todo.title for todo in merged} == {"RSVP", "Vendor review"}


def test_bounded_source_carryover_does_not_disappear_after_max_days(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    seen_five_days_ago = datetime(2026, 7, 18, 12, tzinfo=timezone.utc)
    now = datetime(2026, 7, 23, 12, tzinfo=timezone.utc)
    fresh = TodoItem(
        source="github",
        id="work-fresh",
        title="Review recent PR",
        why="Requested",
        urgency="today",
        needs_reply=False,
        sources=["github"],
        source_ids=["github:100"],
    )
    # Seed state with two items last seen 5 days ago.
    _, _ = reconcile_state([fresh], state, now=seen_five_days_ago)

    # Search absence is not resolution evidence, even after the old carryover
    # window. The commitment remains until a source or the user closes it.
    active, progress = reconcile_state([], state, now=now, carryover_max_days=3)
    assert [task.title for task in active] == ["Review recent PR"]
    assert progress.aged_out == []
    assert [task.title for task in progress.carried] == ["Review recent PR"]


def test_reconciliation_fails_closed_on_malformed_persisted_state(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text('{"version":1,"last_run":"now","tasks":"not-an-array"}', encoding="utf-8")
    with pytest.raises(SchemaValidationError, match="tasks must be an array"):
        reconcile_state([], state)
    assert '"not-an-array"' in state.read_text(encoding="utf-8")


def test_old_source_refresh_does_not_erase_bounded_source_carryover(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    stale_refresh = datetime(2026, 7, 23, tzinfo=timezone.utc)
    inflated = TodoItem(
        source="slack",
        id="stale-thread",
        title="Old Slack task",
        why="Retriaged from stale JSON",
        urgency="today",
        needs_reply=True,
        sources=["slack"],
        source_ids=["slack:old"],
    )
    reconcile_state([inflated], state, now=now)
    active, progress = reconcile_state(
        [],
        state,
        now=now,
        carryover_max_days=3,
        source_last_success={"slack": stale_refresh},
    )
    assert [task.title for task in active] == ["Old Slack task"]
    assert progress.aged_out == []
    assert progress.carried[0].carryover is True


def test_carryover_survives_when_within_window(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    yesterday = datetime(2026, 7, 22, 12, tzinfo=timezone.utc)
    now = datetime(2026, 7, 23, 12, tzinfo=timezone.utc)
    fresh = TodoItem(
        source="github",
        id="work-fresh",
        title="Review recent PR",
        why="Requested",
        urgency="today",
        needs_reply=False,
        sources=["github"],
        source_ids=["github:100"],
    )
    _, _ = reconcile_state([fresh], state, now=yesterday)
    active, progress = reconcile_state([], state, now=now, carryover_max_days=3)
    assert len(active) == 1
    assert active[0].carryover is True
    assert progress.aged_out == []


def test_fresh_authoritative_source_closes_missing_previous_task(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    prior = TodoItem(
        source="github",
        id="pr-42",
        title="Finish PR 42",
        why="Open authored PR",
        urgency="today",
        needs_reply=False,
        sources=["github"],
        source_ids=["github:42"],
    )
    reconcile_state(
        [prior], state, now=datetime(2026, 7, 23, tzinfo=timezone.utc)
    )
    active, progress = reconcile_state(
        [],
        state,
        now=datetime(2026, 7, 24, tzinfo=timezone.utc),
        authoritative_sources={"github"},
    )
    assert active == []
    assert progress.completed[0].completion_evidence.startswith("No longer present")


def test_manual_task_never_ages_out_without_explicit_completion(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    manual = TodoItem(
        source="manual",
        id="personal",
        title="Write strategy",
        why="Personal commitment",
        urgency="this_week",
        needs_reply=False,
        sources=["manual"],
        source_ids=["manual:personal"],
    )
    reconcile_state(
        [manual], state, now=datetime(2026, 7, 1, tzinfo=timezone.utc)
    )
    active, progress = reconcile_state(
        [], state, now=datetime(2026, 8, 1, tzinfo=timezone.utc), carryover_max_days=3
    )
    assert active[0].title == "Write strategy"
    assert progress.aged_out == []


def test_past_calendar_and_merged_github_items_are_resolved_without_model() -> None:
    items = [
        InboxItem(
            source="calendar",
            id="event-1",
            sender="Alice",
            subject="Lunch",
            body="Lunch invite",
            ts="2026-07-23T12:00:00+00:00",
            kind="event_response",
        ),
        InboxItem(
            source="github",
            id="pr-1",
            sender="Carmen",
            subject="Migration PR",
            body="Merged",
            metadata={"state": "merged", "repository": "ramp/core", "number": "42"},
        ),
    ]
    actionable, resolved = partition_source_items(
        items, now=datetime(2026, 8, 12, tzinfo=timezone.utc)
    )
    assert actionable == []
    assert {todo.source for todo in resolved} == {"calendar", "github"}
    assert all(todo.status == "done" for todo in resolved)


def test_accepted_future_meeting_prep_remains_actionable() -> None:
    prep = InboxItem(
        source="calendar",
        id="event-2",
        sender="Carmen",
        subject="Prepare QA session",
        body="Bring examples and decisions.",
        ts="2026-08-13T15:00:00-04:00",
        kind="meeting_prep",
        metadata={"response_status": "accepted", "cancelled": False},
    )

    actionable, resolved = partition_source_items(
        [prep], now=datetime(2026, 8, 12, tzinfo=timezone.utc)
    )

    assert actionable == [prep]
    assert resolved == []


def test_later_substantive_reply_and_expired_decision_resolve_without_model() -> None:
    answered = InboxItem(
        source="slack", id="thread-1", sender="Alice", subject="Decision",
        body="Alice asked for a decision; Carmen later answered it.",
        metadata={
            "user_replied_after_ask": True,
            "resolution_evidence": "Carmen replied after Alice's ask.",
        },
    )
    expired = InboxItem(
        source="gmail", id="order-1", sender="Lunch", subject="Edit lunch",
        body="Edit by 2 PM.", metadata={"expires_at": "2026-08-13T14:00:00+00:00"},
    )
    emoji_only = InboxItem(
        source="slack", id="thread-2", sender="Bob", subject="Please review",
        body="Bob asked for review; Carmen reacted with eyes.",
        metadata={"user_replied_after_ask": False},
    )
    actionable, resolved = partition_source_items(
        [answered, expired, emoji_only],
        now=datetime(2026, 8, 13, 15, tzinfo=timezone.utc),
    )
    assert actionable == [emoji_only]
    assert {item.id for item in resolved} == {"thread-1", "order-1"}
    assert all(item.status == "done" for item in resolved)


def test_github_next_action_overrides_generic_local_model_classification() -> None:
    session = FakeSession([
        '{"title":"Check PR","why":"Open PR","urgency":"this_week",'
        '"needs_reply":true,"due":"","people":[],"confidence":0.4}'
    ])
    item = InboxItem(
        source="github", id="pr-42", sender="Carmen", subject="Improve policy agent",
        body="Approved, CI passing, mergeable; one non-blocking suggestion remains.",
        metadata={
            "next_action": "merge",
            "reason": "Approved, passing, and mergeable.",
        },
    )
    todo = triage_all(session, [item])[0]
    assert todo.title == "Merge PR: Improve policy agent"
    assert todo.why == "Approved, passing, and mergeable."
    assert todo.needs_reply is False
    assert todo.confidence >= 0.75


def test_state_carries_work_and_records_completion(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    first = TodoItem(
        source="github",
        id="work-1",
        title="Review PR 42",
        why="Review requested",
        urgency="today",
        needs_reply=False,
        sources=["github"],
        source_ids=["github:42"],
    )
    active, progress = reconcile_state(
        [first],
        state,
        now=datetime(2026, 7, 23, 12, tzinfo=timezone.utc),
    )
    assert len(active) == 1
    assert progress.new[0].title == "Review PR 42"

    carried, progress = reconcile_state(
        [],
        state,
        now=datetime(2026, 7, 24, 12, tzinfo=timezone.utc),
    )
    assert carried[0].carryover is True
    assert progress.carried[0].title == "Review PR 42"

    done = TodoItem(
        source="github",
        id="new-generated-id",
        title="Review PR 42",
        why="PR was merged",
        urgency="fyi",
        needs_reply=False,
        sources=["github"],
        source_ids=["github:42"],
        status="done",
    )
    active, progress = reconcile_state(
        [done],
        state,
        now=datetime(2026, 7, 25, 12, tzinfo=timezone.utc),
    )
    assert active == []
    assert progress.completed[0].id == "work-1"


def test_source_refresh_preserves_only_explicit_user_edits(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    prior = TodoItem(
        source="slack",
        id="work-1",
        title="My clearer task title",
        why="Old source summary",
        urgency="today",
        needs_reply=True,
        sources=["slack"],
        source_ids=["slack:123"],
        edited_fields=["title"],
    )
    reconcile_state([prior], state, now=datetime(2026, 8, 12, tzinfo=timezone.utc))
    refreshed = TodoItem(
        source="slack",
        id="new-model-id",
        title="Generated title",
        why="Fresh source summary",
        urgency="now",
        needs_reply=True,
        sources=["slack"],
        source_ids=["slack:123"],
    )
    active, _ = reconcile_state(
        [refreshed], state, now=datetime(2026, 8, 13, tzinfo=timezone.utc)
    )
    assert active[0].title == "My clearer task title"
    assert active[0].why == "Fresh source summary"
    assert active[0].urgency == "now"
    assert active[0].edited_fields == ["title"]


def test_source_refresh_preserves_an_explicitly_accepted_suggestion(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    previous = TodoItem(
        source="slack",
        id="same-task",
        title="Reply to Alice",
        why="Alice asked for a decision",
        urgency="today",
        needs_reply=True,
        confidence=0.2,
        review_state="accepted",
    )
    reconcile_state([previous], state_path)
    refreshed = TodoItem(
        source="slack",
        id="same-task",
        title="Reply to Alice",
        why="Alice asked again",
        urgency="today",
        needs_reply=True,
        confidence=0.2,
    )

    active, _ = reconcile_state([refreshed], state_path)

    assert active[0].review_state == "accepted"


def test_morning_brief_includes_reply_and_progress(tmp_path: Path) -> None:
    draft = tmp_path / "draft.md"
    draft.write_text("yep, taking a look now :ramp-love:\n", encoding="utf-8")
    todo = TodoItem(
        source="slack",
        id="s1",
        title="Reply to Alice",
        why="Alice needs a decision today",
        urgency="today",
        needs_reply=True,
        links=["https://slack/example"],
        draft_path=str(draft),
        sources=["slack"],
        source_ids=["slack:s1"],
        people=["Alice"],
        confidence=0.9,
    )
    _, progress = reconcile_state(
        [todo],
        tmp_path / "state.json",
        now=datetime(2026, 7, 23, 12, tzinfo=timezone.utc),
    )
    brief = render_morning_brief(
        [todo],
        progress,
        generated_at=datetime(2026, 7, 23, 8, tzinfo=timezone.utc),
    )
    assert "# Morning Work Brief — 2026-07-23" in brief
    assert "## Reply queue" in brief
    assert "yep, taking a look now :ramp-love:" in brief
    assert "90% confidence" in brief


def test_schedule_runs_daily_agent_at_five(tmp_path: Path) -> None:
    config = build_launch_agent(
        python_executable=tmp_path / "python",
        collector_command="refresh-work-sources --since 24h",
    )
    assert config["StartCalendarInterval"] == {"Hour": 5, "Minute": 0}
    assert "-m" in config["ProgramArguments"]
    assert "carmen_agent.skills.cli" in config["ProgramArguments"]
    assert config["ProgramArguments"][-1] == "refresh-work-sources --since 24h"


def test_schedule_preserves_virtualenv_python_and_codex_path(tmp_path: Path) -> None:
    python = tmp_path / ".venv" / "bin" / "python"
    codex = tmp_path / ".local" / "bin" / "codex"
    config = build_launch_agent(
        python_executable=python,
        codex_executable=codex,
    )
    assert config["ProgramArguments"][0] == str(python)
    path = config["EnvironmentVariables"]["PATH"].split(os.pathsep)
    assert str(python.parent) in path
    assert str(codex.parent) in path


def test_reminder_schedule_checks_every_thirty_minutes(tmp_path: Path) -> None:
    python = tmp_path / ".venv" / "bin" / "python"
    config = build_reminder_launch_agent(
        interval_minutes=30,
        python_executable=python,
    )
    assert config["StartInterval"] == 1800
    assert config["ProgramArguments"] == [
        str(python),
        "-m",
        "carmen_agent.reminders",
        "--notify",
    ]


def test_schedule_injects_workspace_ca_when_explicitly_provided(tmp_path: Path) -> None:
    ca = tmp_path / "koi.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    config = build_launch_agent(
        python_executable=tmp_path / "python",
        ca_bundle=ca,
    )
    env = config["EnvironmentVariables"]
    assert env["SSL_CERT_FILE"] == str(ca)
    assert env["REQUESTS_CA_BUNDLE"] == str(ca)


def test_schedule_omits_ca_bundle_when_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    from carmen_agent.skills import schedule as schedule_module

    monkeypatch.setattr(schedule_module, "WORKSPACE_CA_PATH", tmp_path / "does-not-exist.pem")
    config = build_launch_agent(python_executable=tmp_path / "python")
    env = config["EnvironmentVariables"]
    assert "SSL_CERT_FILE" not in env
    assert "REQUESTS_CA_BUNDLE" not in env


def test_prefer_offline_sets_env_when_cache_present(tmp_path: Path, monkeypatch) -> None:
    from carmen_agent.skills.model import _prefer_offline_when_cache_is_warm

    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    hub = tmp_path / "hub"
    (hub / "models--mlx-community--Qwen3-4B-Instruct-2507-4bit").mkdir(parents=True)
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    _prefer_offline_when_cache_is_warm("mlx-community/Qwen3-4B-Instruct-2507-4bit")
    assert os.environ.get("HF_HUB_OFFLINE") == "1"


def test_prefer_offline_leaves_env_alone_when_cache_missing(tmp_path: Path, monkeypatch) -> None:
    from carmen_agent.skills.model import _prefer_offline_when_cache_is_warm

    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    _prefer_offline_when_cache_is_warm("mlx-community/Qwen3-4B-Instruct-2507-4bit")
    assert "HF_HUB_OFFLINE" not in os.environ
