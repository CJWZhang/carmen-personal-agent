from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from carmen_agent.setup import Diagnostic
from carmen_agent.skills.schemas import TodoItem, dump_todos
from carmen_agent.ui.app import (
    ApiError,
    DashboardService,
    _asset_bytes,
    build_handler,
    build_server,
)


def _task(**overrides: object) -> TodoItem:
    values: dict[str, object] = {
        "source": "slack",
        "id": "work-abc123",
        "title": "Reply to Alice",
        "why": "Alice needs a launch decision",
        "urgency": "today",
        "needs_reply": True,
        "sources": ["slack"],
        "source_ids": ["slack:171234.10"],
        "people": ["Alice"],
        "links": ["https://example.test/thread"],
    }
    values.update(overrides)
    return TodoItem(**values)  # type: ignore[arg-type]


def _service(
    tmp_path: Path,
    *,
    session_factory=None,
    daily_runner=None,
    maintenance_runner=None,
    session_factory_uses_settings: bool = False,
) -> DashboardService:
    inbox = tmp_path / "data" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "slack.json").write_text("[]\n", encoding="utf-8")
    kwargs = {}
    if session_factory is not None:
        kwargs["session_factory"] = session_factory
    if daily_runner is not None:
        kwargs["daily_runner"] = daily_runner
    if maintenance_runner is not None:
        kwargs["maintenance_runner"] = maintenance_runner
    kwargs["session_factory_uses_settings"] = session_factory_uses_settings
    return DashboardService(
        todos_path=inbox / "todos.json",
        state_path=tmp_path / "work" / "state.json",
        brief_path=tmp_path / "TODO.md",
        habits_path=tmp_path / "work" / "habits.json",
        receipts_path=tmp_path / "work" / "action-receipts.json",
        calendar_preferences_path=tmp_path / "work" / "calendar-preferences.json",
        inbox_dir=inbox,
        drafts_dir=tmp_path / "data" / "drafts",
        **kwargs,
    )


def test_dashboard_overview_and_task_actions_use_existing_state_boundary(tmp_path: Path) -> None:
    service = _service(tmp_path)
    dump_todos([_task()], service.todos_path)

    overview = service.overview()
    assert overview["counts"]["open"] == 1
    assert overview["counts"]["replies"] == 1
    assert sum(overview["counts"][name] for name in ("focus", "replies", "meetings", "waiting", "review")) == 1
    assert overview["tasks"][0]["bucket"] == "replies"
    assert overview["tasks"][0]["title"] == "Reply to Alice"

    # Seed prior reconciliation state first; edits must not be wiped by it.
    service.mutate_task("work-abc123", "snooze", {"minutes": 30})
    edited = service.mutate_task(
        "work-abc123",
        "edit",
        {
            "title": "Reply to Alice about launch",
            "why": "Confirm the launch decision.",
            "urgency": "now",
            "due": "2026-08-13",
            "remind_at": "2026-08-13 09:00+00:00",
        },
    )
    changed = edited["overview"]["tasks"][0]
    assert changed["title"] == "Reply to Alice about launch"
    assert changed["urgency"] == "now"
    assert set(changed["edited_fields"]) == {
        "title", "why", "urgency", "due", "remind_at"
    }

    added = service.add({"title": "Prepare launch notes", "urgency": "this_week"})
    assert added["overview"]["counts"]["open"] == 2
    assert added["overview"]["counts"]["focus"] == 1
    assert sum(
        added["overview"]["counts"][name]
        for name in ("focus", "replies", "meetings", "waiting", "review")
    ) == 2

    snoozed = service.mutate_task("work-abc123", "snooze", {"minutes": 45})
    selected = next(task for task in snoozed["overview"]["tasks"] if task["id"] == "work-abc123")
    assert selected["remind_at"]

    completed = service.mutate_task("work-abc123", "complete", {})
    assert completed["overview"]["counts"]["open"] == 1
    assert completed["overview"]["counts"]["completed"] == 1


def test_task_lifecycle_controls_and_context_verification_are_ui_actions(
    tmp_path: Path,
) -> None:
    runs: list[str] = []

    def refresh() -> int:
        runs.append("refresh")
        return 0

    service = _service(tmp_path, daily_runner=refresh)
    dump_todos([_task()], service.todos_path)

    started = service.mutate_task("work-abc123", "start", {})
    assert started["overview"]["tasks"][0]["lifecycle_stage"] == "in_progress"
    waiting = service.mutate_task("work-abc123", "wait", {})
    assert waiting["overview"]["tasks"][0]["lifecycle_stage"] == "waiting"
    reopened = service.mutate_task("work-abc123", "reopen", {})
    assert reopened["overview"]["tasks"][0]["lifecycle_stage"] == "open"

    verifying = service.mutate_task("work-abc123", "verify-context", {})
    assert "verify Slack context" in verifying["message"]
    for _ in range(100):
        if service.operation_status()["refresh"]["state"] != "running":
            break
        time.sleep(0.01)
    assert runs == ["refresh"]


def test_calendar_preference_feedback_personalizes_priority_in_ui(tmp_path: Path) -> None:
    service = _service(tmp_path)
    task = _task(
        source="calendar",
        id="office-hour-task",
        title="Prepare for Internal AI Office Hour",
        needs_reply=False,
        sources=["calendar"],
        source_ids=["calendar:office-hour-event"],
    )
    dump_todos([task], service.todos_path)
    event = {
        "source": "calendar",
        "id": "office-hour-event",
        "sender": "AI team",
        "subject": "Internal AI Office Hour",
        "body": "Optional weekly office hour.",
        "url": "https://example.test/calendar/office-hour",
        "ts": "2026-08-14T17:00:00+00:00",
        "channel": "primary",
        "thread_id": "office-hour-series",
        "kind": "meeting_prep",
        "status": "open",
        "participants": ["Carmen"],
        "related_keys": [],
        "metadata": {"organizer": "AI team", "optional": True},
    }
    (service.inbox_dir / "calendar.json").write_text(
        json.dumps([event]), encoding="utf-8"
    )

    result = service.mutate_task("office-hour-task", "usually-skip", {})
    personalized = result["overview"]["tasks"][0]

    assert personalized["urgency"] == "fyi"
    assert personalized["priority_adjustment"] == -45
    assert "usually skip" in personalized["recommendation_reason"]
    assert personalized["confidence_label"] == "Personalized recommendation"
    profile = json.loads(service.calendar_preferences_path.read_text(encoding="utf-8"))
    assert profile["rules"][0]["key"] == "internal ai office hour"


def test_uncertain_imports_are_reviewable_and_user_corrections_are_explicit(tmp_path: Path) -> None:
    service = _service(tmp_path)
    dump_todos([_task(confidence=0.2)], service.todos_path)

    suggestion = service.overview()["tasks"][0]
    assert suggestion["needs_review"] is True
    assert suggestion["bucket"] == "review"
    assert suggestion["confidence_label"] == "Needs your review"
    assert "evidence is not strong enough" in suggestion["recommendation_reason"]

    accepted = service.mutate_task("work-abc123", "accept", {})["overview"]["tasks"][0]
    assert accepted["needs_review"] is False
    assert accepted["review_state"] == "accepted"
    assert accepted["confidence_label"] == "Confirmed by you"


def test_rejected_suggestion_leaves_the_open_plan_and_records_reason(tmp_path: Path) -> None:
    service = _service(tmp_path)
    dump_todos([_task(confidence=0.2)], service.todos_path)

    result = service.mutate_task("work-abc123", "not-a-task", {})
    assert result["overview"]["counts"]["open"] == 0
    assert "correction" in result["message"]
    state = json.loads(service.state_path.read_text(encoding="utf-8"))
    assert state["tasks"][0]["completion_evidence"] == "user correction: not a task"


def test_dashboard_never_reads_draft_paths_outside_its_private_draft_dir(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.drafts_dir.mkdir(parents=True)
    allowed = service.drafts_dir / "reply.md"
    allowed.write_text("safe local draft", encoding="utf-8")
    outside = tmp_path / "private.txt"
    outside.write_text("must not be exposed", encoding="utf-8")
    dump_todos(
        [
            _task(id="allowed", draft_path=str(allowed)),
            _task(id="outside", draft_path=str(outside)),
        ],
        service.todos_path,
    )
    rendered = {task["id"]: task for task in service.overview()["tasks"]}
    assert rendered["allowed"]["draft"] == "safe local draft"
    assert rendered["outside"]["draft"] == ""
    assert "draft_path" not in rendered["allowed"]


def test_contextual_chat_includes_bounded_source_context_and_reuses_session(tmp_path: Path) -> None:
    calls: list[list[dict[str, str]]] = []
    factory_calls = 0

    class Session:
        system_prompt = "Write naturally."

        def generate(self, messages, **_):
            calls.append(messages)
            return "sounds good — I'll take a look"

    def factory():
        nonlocal factory_calls
        factory_calls += 1
        return Session()

    service = _service(tmp_path, session_factory=factory)
    (service.inbox_dir / "slack.json").write_text(
        json.dumps(
            [
                {
                    "source": "slack",
                    "id": "171234.10",
                    "sender": "Alice",
                    "subject": "Launch decision",
                    "body": "Can you confirm whether we should launch on Thursday?",
                }
            ]
        ),
        encoding="utf-8",
    )
    dump_todos([_task()], service.todos_path)

    first = service.chat({"message": "Draft a reply", "task_id": "work-abc123"})
    second = service.chat({"message": "Make it shorter"})
    assert first["reply"].startswith("sounds good")
    assert second["reply"].startswith("sounds good")
    assert factory_calls == 1
    assembled = "\n".join(message["content"] for message in calls[0])
    assert "untrusted reference data" in assembled
    assert "launch on Thursday" in assembled
    assert first["meta"]["context"]["source_message_loaded"] is True


def test_trained_lora_is_loaded_only_for_grounded_drafting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_options: list[dict[str, object]] = []

    class Session:
        system_prompt = "Write naturally."

        def generate(self, _messages, **_options):
            return "sounds good, i'll take a look"

    def factory(**options):
        session_options.append(options)
        return Session()

    service = _service(
        tmp_path,
        session_factory=factory,
        session_factory_uses_settings=True,
    )
    service.model_provider = "mlx"
    adapter_dir = tmp_path / "adapter"
    adapter_dir.mkdir()
    (adapter_dir / "adapters.safetensors").write_bytes(b"local adapter")
    monkeypatch.setattr("carmen_agent.ui.app.DEFAULT_ADAPTER_DIR", adapter_dir)

    def configured(_environment: str, name: str, default: str = "") -> str:
        return "lora" if name == "voice_mode" else default

    monkeypatch.setattr("carmen_agent.ui.app.config_value", configured)
    service.inbox_dir.joinpath("slack.json").write_text(
        json.dumps(
            [
                {
                    "source": "slack",
                    "id": "171234.10",
                    "sender": "Alice",
                    "subject": "Launch decision",
                    "body": "Can you confirm the launch plan?",
                }
            ]
        ),
        encoding="utf-8",
    )
    dump_todos([_task()], service.todos_path)

    service.chat({"message": "Draft a reply", "task_id": "work-abc123"})
    service.chat({"message": "What should I focus on?"})

    assert [item["base_only"] for item in session_options] == [False, True]


def test_contextual_chat_supplies_every_source_for_a_merged_task(tmp_path: Path) -> None:
    calls: list[list[dict[str, str]]] = []

    class Session:
        system_prompt = "Be accurate."

        def generate(self, messages, **_):
            calls.append(messages)
            return "The Slack ask is backed by an approved, passing PR."

    service = _service(tmp_path, session_factory=Session)
    service.inbox_dir.joinpath("slack.json").write_text(json.dumps([{
        "source": "slack", "id": "s1", "sender": "Alice", "subject": "Launch",
        "body": "Can you merge PR 42 today?", "ts": "2026-08-13T13:00:00+00:00",
    }]), encoding="utf-8")
    service.inbox_dir.joinpath("github.json").write_text(json.dumps([{
        "source": "github", "id": "g1", "sender": "Carmen", "subject": "PR 42",
        "body": "Approved, CI passing, and mergeable.", "ts": "2026-08-13T14:00:00+00:00",
    }]), encoding="utf-8")
    dump_todos([_task(
        sources=["slack", "github"], source_ids=["slack:s1", "github:g1"]
    )], service.todos_path)
    result = service.chat({"message": "What is the actual next step?", "task_id": "work-abc123"})
    assembled = "\n".join(message["content"] for message in calls[0])
    assert "Can you merge PR 42 today?" in assembled
    assert "Approved, CI passing, and mergeable." in assembled
    assert result["meta"]["context"]["evidence_count"] == 2


def test_chat_retries_cross_script_corruption_then_uses_grounded_answer(tmp_path: Path) -> None:
    replies = iter(["สิงหาคม 대부, I can't see this", "got it — I'll confirm and follow up"])

    class Session:
        system_prompt = "Write naturally."

        def generate(self, _messages, **_opts):
            return next(replies)

    service = _service(tmp_path, session_factory=Session)
    service.inbox_dir.joinpath("slack.json").write_text(
        json.dumps([{"source": "slack", "id": "171234.10", "sender": "Alice", "subject": "Decision", "body": "Can you confirm?"}]),
        encoding="utf-8",
    )
    dump_todos([_task()], service.todos_path)
    result = service.chat({"message": "Draft a reply", "task_id": "work-abc123"})
    assert result["reply"] == "got it — I'll confirm and follow up"
    assert result["meta"]["quality_fallback"] is False


def test_chat_retries_a_truncated_answer_and_allows_longer_planning_output(
    tmp_path: Path,
) -> None:
    replies = iter(["Review the --", "Review the agenda before the meeting."])
    token_limits: list[int] = []

    class Session:
        system_prompt = "Be complete."

        def generate(self, _messages, **options):
            token_limits.append(options["max_tokens"])
            return next(replies)

    service = _service(tmp_path, session_factory=Session)
    result = service.chat({"message": "Help me prepare for my meeting"})

    assert result["reply"] == "Review the agenda before the meeting."
    assert token_limits == [800, 800]
    assert result["meta"]["quality_fallback"] is False


def test_chat_greeting_is_deterministic_and_never_loads_lora(tmp_path: Path) -> None:
    def factory():
        raise AssertionError("a greeting must not load the model")

    service = _service(tmp_path, session_factory=factory)
    result = service.chat({"message": "hi"})
    assert result["reply"] == "hey! what can I help you with?"
    assert result["meta"]["quality_fallback"] is False


def test_chat_rejects_accented_single_token_corruption(tmp_path: Path) -> None:
    class Session:
        system_prompt = "Write naturally."

        def generate(self, _messages, **_opts):
            return "âm"

    service = _service(tmp_path, session_factory=Session)
    service.inbox_dir.joinpath("slack.json").write_text(
        json.dumps(
            [
                {
                    "source": "slack",
                    "id": "171234.10",
                    "sender": "Alice",
                    "subject": "Decision",
                    "body": "Can you confirm?",
                }
            ]
        ),
        encoding="utf-8",
    )
    dump_todos([_task()], service.todos_path)

    result = service.chat(
        {"message": "Draft a concise response", "task_id": "work-abc123"}
    )

    assert result["reply"] == "got it! let me check and get back to you"
    assert result["meta"]["quality_fallback"] is True


def test_manual_task_draft_request_never_calls_model(tmp_path: Path) -> None:
    def factory():
        raise AssertionError("manual task without a message must not load the model")

    service = _service(tmp_path, session_factory=factory)
    dump_todos(
        [
            _task(
                source="manual",
                sources=["manual"],
                source_ids=["manual:abc"],
                id="abc",
                title="abc testing",
                why="Added manually",
                needs_reply=False,
                links=[],
            )
        ],
        service.todos_path,
    )

    result = service.chat(
        {
            "message": "Draft a concise response for this task.",
            "task_id": "abc",
        }
    )

    assert "personal task, not a message" in result["reply"]
    assert "concrete next steps" in result["reply"]
    assert result["meta"]["quality_fallback"] is True


def test_dashboard_counts_tasks_backed_by_stale_sources_with_warning(tmp_path: Path) -> None:
    service = _service(tmp_path)
    dump_todos([_task()], service.todos_path)
    old = datetime.now(timezone.utc) - timedelta(days=4)
    os.utime(service.inbox_dir / "slack.json", (old.timestamp(), old.timestamp()))

    overview = service.overview()
    assert len(overview["tasks"]) == 1
    assert overview["tasks"][0]["snapshot_status"] == "stale"
    assert overview["counts"]["open"] == 1
    assert overview["source_health"]["level"] == "stale"
    assert overview["source_health"]["hidden_task_count"] == 0
    assert overview["source_health"]["stale_task_count"] == 1
    assert "shown with stale labels" in overview["source_health"]["message"]


def test_dashboard_labels_last_good_evidence_cached_after_refresh_failure(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    stamp = datetime.now(timezone.utc).isoformat()
    last_success = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    service.inbox_dir.joinpath("_source-status.json").write_text(
        json.dumps({
            "version": 1,
            "updated_at": stamp,
            "sources": {
                "slack": {
                    "status": "error",
                    "detail": "runtime unavailable",
                    "last_attempt_at": stamp,
                    "last_success_at": last_success,
                    "item_count": 0,
                }
            },
        }),
        encoding="utf-8",
    )
    # The visible task list was built from the last good snapshot, then a newer
    # attempt failed. It remains visible, but must be labeled as cached.
    dump_todos([_task()], service.todos_path)
    task = service.overview()["tasks"][0]
    assert task["snapshot_status"] == "cached"
    assert task["snapshot_at"] == last_success


def test_dashboard_refresh_button_runs_daily_pipeline_once_in_background(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()

    def runner() -> int:
        started.set()
        release.wait(timeout=2)
        return 0

    service = _service(tmp_path, daily_runner=runner)
    result = service.start_refresh({})
    assert result["message"] == "Source refresh started."
    assert started.wait(timeout=1)
    assert service.overview()["refresh"]["state"] == "running"
    duplicate = service.start_refresh({})
    assert "already running" in duplicate["message"]
    release.set()
    for _ in range(100):
        if service.overview()["refresh"]["state"] == "succeeded":
            break
        time.sleep(0.01)
    assert service.overview()["refresh"]["state"] == "succeeded"
    with pytest.raises(ApiError, match="Unknown refresh request"):
        service.start_refresh({"command": "anything"})


def test_operation_status_does_not_rebuild_dashboard_context(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service._refresh_status["state"] = "running"
    service._maintenance_status["state"] = "idle"

    status = service.operation_status()
    assert status["refresh"] == service._refresh_status
    assert status["maintenance"] == service._maintenance_status
    assert [item["source"] for item in status["source_health"]["sources"]] == [
        "slack", "gmail", "calendar", "github",
    ]


def test_dashboard_refresh_deterministically_imports_collected_snapshot(
    tmp_path: Path,
) -> None:
    service: DashboardService

    def runner() -> int:
        refreshed = datetime.now(timezone.utc).isoformat()
        item = {
            "source": "slack",
            "id": "fresh-ask",
            "sender": "Alice",
            "subject": "Confirm launch",
            "body": "Confirm the launch decision.",
            "url": "https://example.test/slack/fresh-ask",
            "ts": refreshed,
            "channel": "launch",
            "thread_id": "",
            "kind": "direct_ask",
            "status": "open",
            "participants": ["Alice"],
            "related_keys": [],
            "metadata": {"reason": "Alice needs a decision."},
        }
        (service.inbox_dir / "slack.json").write_text(
            json.dumps([item]), encoding="utf-8"
        )
        (service.inbox_dir / "_source-status.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "updated_at": refreshed,
                    "sources": {
                        "slack": {
                            "status": "ok",
                            "detail": "Current",
                            "last_attempt_at": refreshed,
                            "last_success_at": refreshed,
                            "item_count": 1,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return 0

    service = _service(tmp_path, daily_runner=runner)
    service.start_refresh({})
    for _ in range(100):
        overview = service.overview()
        if overview["refresh"]["state"] == "succeeded":
            break
        time.sleep(0.01)

    assert overview["refresh"]["state"] == "succeeded"
    assert [task["title"] for task in overview["tasks"]] == ["Confirm launch"]
    assert overview["tasks"][0]["snapshot_status"] == "current"


def test_failed_refresh_never_exposes_internal_process_logs(tmp_path: Path) -> None:
    internal = "ERROR invalid_json_schema " * 1_000
    service = _service(
        tmp_path,
        daily_runner=lambda: {"returncode": 1, "output": internal},
    )
    service.start_refresh({})
    for _ in range(100):
        refresh = service.overview()["refresh"]
        if refresh["state"] == "failed":
            break
        time.sleep(0.01)

    assert refresh["state"] == "failed"
    assert "invalid_json_schema" not in refresh["message"]
    assert "invalid_json_schema" not in refresh["detail"]
    assert refresh["detail"] == "Open Settings → Connections to see what needs attention."


def test_saved_snapshot_seed_restores_actionable_items_but_expires_past_calendar(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    old_slack = {
        "source": "slack",
        "id": "171234.10",
        "sender": "Alice",
        "subject": "Follow up on launch",
        "body": "Alice asked for the launch decision.",
        "url": "https://example.test/slack/thread",
        "ts": "2026-07-23T14:00:00+00:00",
        "channel": "launch",
        "thread_id": "171234.00",
        "kind": "direct_ask",
        "status": "open",
        "participants": ["Alice"],
        "related_keys": [],
        "metadata": {"reason": "Reply with the launch decision."},
    }
    past_calendar = {
        **old_slack,
        "source": "calendar",
        "id": "past-event",
        "subject": "Past meeting",
        "url": "https://example.test/calendar/event",
        "ts": "2026-07-23T15:00:00+00:00",
        "kind": "meeting_prep",
    }
    (service.inbox_dir / "slack.json").write_text(
        json.dumps([old_slack]), encoding="utf-8"
    )
    (service.inbox_dir / "calendar.json").write_text(
        json.dumps([past_calendar]), encoding="utf-8"
    )

    before = service.overview()
    assert before["source_health"]["saved_available_count"] == 1
    restored = service.seed_saved_snapshot({})
    assert restored["message"].startswith("Imported 1")
    assert restored["overview"]["counts"]["open"] == 1
    task = restored["overview"]["tasks"][0]
    assert task["title"] == "Follow up on launch"
    assert task["snapshot_status"] == "saved"
    assert task["snapshot_at"] == "2026-07-23T14:00:00+00:00"
    assert task["context"]["body"] == "Alice asked for the launch decision."
    assert restored["overview"]["source_health"]["saved_available_count"] == 0
    assert service.seed_saved_snapshot({})["message"].startswith("All actionable")
    with pytest.raises(ApiError, match="Unknown saved snapshot request"):
        service.seed_saved_snapshot({"source": "slack"})


def test_bounded_slack_refresh_preserves_unseen_open_loops(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    observed_at = datetime.now(timezone.utc)
    due = (observed_at + timedelta(days=1)).date().isoformat()
    dump_todos(
        [
            _task(id="old-task", source_ids=["slack:old-message"]),
            _task(
                id="prior-new-message",
                title="Stale launch wording",
                source_ids=["slack:new-message"],
            ),
            _task(
                source="manual",
                id="manual-task",
                title="Keep my task",
                source_ids=["manual:manual-task"],
                sources=["manual"],
            ),
        ],
        service.todos_path,
    )
    current = {
        "source": "slack",
        "id": "new-message",
        "sender": "Alice",
        "subject": "Make launch decision",
        "body": f"Alice needs the decision by {due}.",
        "url": "https://example.test/slack/new-message",
        "ts": observed_at.isoformat(),
        "channel": "launch",
        "thread_id": "",
        "kind": "direct_ask",
        "status": "open",
        "participants": ["Alice"],
        "related_keys": [],
        "metadata": {"reason": "Reply with the launch decision."},
    }
    (service.inbox_dir / "slack.json").write_text(
        json.dumps([current]), encoding="utf-8"
    )
    refreshed = observed_at.isoformat()
    (service.inbox_dir / "_source-status.json").write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": refreshed,
                "sources": {
                    "slack": {
                        "status": "ok",
                        "detail": "Current",
                        "last_attempt_at": refreshed,
                        "last_success_at": refreshed,
                        "item_count": 1,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    imported = service.seed_saved_snapshot({})

    assert imported["message"].startswith("Imported 1 current")
    titles = {task["title"] for task in imported["overview"]["tasks"]}
    assert titles == {"Keep my task", "Reply to Alice", "Make launch decision"}
    assert "Stale launch wording" not in titles
    source_task = next(
        task
        for task in imported["overview"]["tasks"]
        if task["title"] == "Make launch decision"
    )
    assert source_task["snapshot_status"] == "current"
    assert source_task["urgency"] == "today"
    assert source_task["due"] == due


def test_explicit_source_resolution_closes_bounded_slack_task(tmp_path: Path) -> None:
    service = _service(tmp_path)
    task = _task()
    dump_todos([task], service.todos_path)
    service.mutate_task(task.id, "start", {})
    resolved = {
        "source": "slack",
        "id": "171234.10",
        "sender": "Alice",
        "subject": "Reply to Alice",
        "body": "Carmen answered after Alice's request.",
        "url": "https://example.test/thread",
        "ts": "2026-08-13T13:00:00+00:00",
        "channel": "launch",
        "thread_id": "171234.00",
        "kind": "direct_ask",
        "status": "done",
        "participants": ["Alice"],
        "related_keys": [],
        "metadata": {
            "user_replied_after_ask": True,
            "resolution_evidence": "A later Slack reply answers Alice's request.",
        },
    }
    (service.inbox_dir / "slack.json").write_text(
        json.dumps([resolved]), encoding="utf-8"
    )

    imported = service.seed_saved_snapshot({})

    assert imported["overview"]["counts"]["open"] == 0
    completed = imported["overview"]["completed"][0]
    assert completed["lifecycle_stage"] == "done"
    assert completed["resolution_mode"] == "source_verified"
    assert "later Slack reply" in completed["completion_evidence"]


def test_control_center_runs_only_allowlisted_background_operations(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def runner(command: list[str]) -> int:
        commands.append(command)
        return 0

    service = _service(tmp_path, maintenance_runner=runner)
    result = service.start_maintenance(
        {"action": "schedule", "hour": 8, "minute": 15, "reminder_minutes": 30}
    )
    assert "Schedule started" in result["message"]
    for _ in range(100):
        if service.overview()["maintenance"]["state"] == "succeeded":
            break
        time.sleep(0.01)
    assert commands[0][-7:] == [
        "--install", "--hour", "8", "--minute", "15", "--reminder-minutes", "30"
    ]

    service.start_maintenance({"action": "train_voice", "mode": "smoke"})
    for _ in range(100):
        if len(commands) == 2 and service.overview()["maintenance"]["state"] == "succeeded":
            break
        time.sleep(0.01)
    assert commands[1][-2:] == ["--iters", "20"]
    service.start_maintenance(
        {"action": "personalize_voice", "mode": "profile", "refresh": True}
    )
    for _ in range(100):
        if len(commands) == 3 and service.overview()["maintenance"]["state"] == "succeeded":
            break
        time.sleep(0.01)
    assert commands[2][-5:] == [
        "-m", "carmen_agent.personalization", "--mode", "profile", "--refresh"
    ]
    with pytest.raises(ApiError, match="refresh must be true or false"):
        service.start_maintenance(
            {"action": "personalize_voice", "mode": "profile", "refresh": "yes"}
        )
    with pytest.raises(ApiError, match="Unknown operation field"):
        service.start_maintenance({"action": "evaluate", "mode": "full"})
    with pytest.raises(ApiError, match="Unknown maintenance"):
        service.start_maintenance({"action": "shell"})


def test_control_center_saves_only_preset_automation_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []
    saved: dict[str, object] = {}

    monkeypatch.setattr(
        "carmen_agent.ui.app.save_user_config",
        lambda values: saved.update(values),
    )
    service = _service(
        tmp_path,
        maintenance_runner=lambda command: commands.append(command) or 0,
    )

    result = service.save_automation(
        {"daily_refresh": "07:00", "reminder_minutes": 60}
    )
    assert result["message"].startswith("Automation saved")
    assert saved == {
        "daily_refresh_hour": 7,
        "daily_refresh_minute": 0,
        "reminder_minutes": 60,
    }
    for _ in range(100):
        if commands:
            break
        time.sleep(0.01)
    assert commands[0][-7:] == [
        "--install", "--hour", "7", "--minute", "0", "--reminder-minutes", "60"
    ]

    with pytest.raises(ApiError, match="available daily refresh"):
        service.save_automation(
            {"daily_refresh": "07:30", "reminder_minutes": 60}
        )
    with pytest.raises(ApiError, match="available reminder"):
        service.save_automation(
            {"daily_refresh": "07:00", "reminder_minutes": 45}
        )


def test_saved_automation_drives_startup_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CARMEN_DISABLE_AUTO_SCHEDULE", raising=False)
    configured = {
        "daily_refresh_hour": "8",
        "daily_refresh_minute": "0",
        "reminder_minutes": "120",
    }

    def configured_value(_environment: str, name: str, default: str = "") -> str:
        return configured.get(name, default)

    monkeypatch.setattr("carmen_agent.ui.app.config_value", configured_value)
    commands: list[list[str]] = []
    service = _service(
        tmp_path,
        maintenance_runner=lambda command: commands.append(command) or 0,
        daily_runner=lambda: 0,
    )

    assert service.settings()["automation"]["daily_refresh"] == "08:00"
    assert service.settings()["automation"]["reminder_minutes"] == 120
    assert service.ensure_daily_schedule() is True
    for _ in range(100):
        if commands:
            break
        time.sleep(0.01)
    assert commands[0][-7:] == [
        "--install", "--hour", "8", "--minute", "0", "--reminder-minutes", "120"
    ]
    repair_commands: list[list[str]] = []
    repair_service = _service(
        tmp_path / "repair",
        maintenance_runner=lambda command: repair_commands.append(command) or 0,
    )
    repair_service.start_maintenance({"action": "schedule"})
    for _ in range(100):
        if repair_commands:
            break
        time.sleep(0.01)
    assert repair_commands[0][-7:] == [
        "--install", "--hour", "8", "--minute", "0", "--reminder-minutes", "120"
    ]


def test_app_startup_ensures_five_am_schedule_without_user_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CARMEN_DISABLE_AUTO_SCHEDULE", raising=False)
    commands: list[list[str]] = []

    def runner(command: list[str]) -> int:
        commands.append(command)
        return 0

    service = _service(
        tmp_path,
        maintenance_runner=runner,
        daily_runner=lambda: 0,
    )
    assert service.ensure_daily_schedule() is True
    for _ in range(100):
        if service.overview()["maintenance"]["state"] != "running":
            break
        time.sleep(0.01)

    assert commands[0][-7:] == [
        "--install", "--hour", "5", "--minute", "0", "--reminder-minutes", "30"
    ]


def test_app_catches_up_after_five_when_sources_have_not_run_today(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    before = datetime(2026, 8, 12, 4, 59, tzinfo=timezone.utc)
    after = datetime(2026, 8, 12, 5, 1, tzinfo=timezone.utc)

    assert service._daily_refresh_due(now=before) is False
    assert service._daily_refresh_due(now=after) is True


def test_background_process_output_is_bounded() -> None:
    result = DashboardService._run_bounded_command(
        [sys.executable, "-c", "print('x' * 20000)"], timeout=5
    )
    assert result["returncode"] == 0
    assert len(result["output"]) <= 4_000


def test_dashboard_refresh_runner_is_a_bounded_collection_not_local_inference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def bounded(command: list[str], *, timeout: int) -> dict[str, object]:
        observed.update(command=command, timeout=timeout)
        return {"returncode": 0, "output": ""}

    monkeypatch.setattr(DashboardService, "_run_bounded_command", staticmethod(bounded))
    DashboardService._default_daily_runner()

    command = observed["command"]
    assert isinstance(command, list)
    assert "carmen_agent.collector" in command
    assert "carmen_agent.skills.cli" not in command
    assert command[-9:] == [
        "--sources", "calendar,gmail,github,slack", "--mode", "auto", "--timeout", "480",
        "--max-source-seconds", "120", "--progressive",
    ]
    assert observed["timeout"] == 510


def test_control_center_validates_and_saves_non_secret_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved: dict[str, str] = {}
    session_options: list[dict[str, object]] = []

    class Session:
        system_prompt = "Write naturally."

        def generate(self, _messages, **_opts):
            return "start with the launch review"

    def session_factory(**options):
        session_options.append(options)
        return Session()

    monkeypatch.setattr(
        "carmen_agent.ui.app.save_user_config",
        lambda values: saved.update(values),
    )
    adapter = tmp_path / "adapter"
    adapter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    adapter.chmod(0o700)
    service = _service(
        tmp_path,
        session_factory=session_factory,
        session_factory_uses_settings=True,
    )
    result = service.save_settings(
        {
            "user_name": "Carmen Zhang",
            "provider": "mlx",
            "model": "local-model",
            "base_url": "http://127.0.0.1:11434/v1",
            "action_command": str(adapter),
        }
    )
    assert result["message"] == "Settings saved."
    assert saved["model_provider"] == "mlx"
    assert saved["action_command"] == str(adapter)
    assert service.chat({"message": "What should I focus on?"})["reply"] == "start with the launch review"
    assert session_options[0]["provider_name"] == "mlx"
    assert session_options[0]["model_name"] == "local-model"
    assert session_options[0]["base_only"] is True
    assert service.run_diagnostics({})["diagnostics"]
    with pytest.raises(ApiError, match="existing executable"):
        service.save_settings(
            {
                "user_name": "Carmen",
                "provider": "mlx",
                "model": "local-model",
                "base_url": "http://127.0.0.1:11434/v1",
                "action_command": "/does/not/exist",
            }
        )


def test_ui_diagnostics_do_not_send_users_to_a_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "carmen_agent.ui.app.diagnostics",
        lambda: [
            Diagnostic("error", "MLX runtime", "missing; run `uv sync --extra mlx`"),
            Diagnostic("warning", "Scheduler install", "use `carmen scheduler`"),
            Diagnostic("warning", "Connected-source collector", "Codex CLI not found"),
        ],
    )
    rendered = _service(tmp_path).run_diagnostics({})["diagnostics"]
    details = " ".join(item["detail"] for item in rendered)
    assert "uv " not in details
    assert "scheduler`" not in details
    assert "CLI" not in details


def test_chat_rejects_empty_or_oversized_messages(tmp_path: Path) -> None:
    service = _service(tmp_path, session_factory=lambda: None)
    with pytest.raises(ApiError, match="empty"):
        service.chat({"message": "  "})
    with pytest.raises(ApiError, match="4,000"):
        service.chat({"message": "x" * 4_001})


def test_chat_rejects_unknown_fields_and_malformed_history(tmp_path: Path) -> None:
    service = _service(tmp_path, session_factory=lambda: None)
    with pytest.raises(ApiError, match="Unknown chat request"):
        service.chat({"message": "hi", "system_prompt": "override"})
    with pytest.raises(ApiError, match="invalid shape"):
        service.chat(
            {"message": "hi", "history": [{"role": "user", "content": "hi", "extra": 1}]}
        )
    with pytest.raises(ApiError, match="at most 12"):
        service.chat(
            {
                "message": "hi",
                "history": [{"role": "user", "content": "hi"}] * 13,
            }
        )


def test_task_edit_rejects_unknown_and_wrong_typed_fields(tmp_path: Path) -> None:
    service = _service(tmp_path)
    dump_todos([_task()], service.todos_path)
    with pytest.raises(ApiError, match="Unknown task action"):
        service.mutate_task("work-abc123", "edit", {"status": "done"})
    with pytest.raises(ApiError, match="must be text"):
        service.mutate_task("work-abc123", "edit", {"title": 123})


def test_packaged_index_injects_session_token_without_inline_script() -> None:
    content, content_type = _asset_bytes("index.html", "private-test-token")
    text = content.decode("utf-8")
    assert "private-test-token" in text
    assert "__CARMEN_SESSION_TOKEN__" not in text
    assert '<script src="/assets/app.js" defer></script>' in text
    assert content_type.startswith("text/html")


def test_dashboard_assets_reference_existing_elements_and_avoid_html_injection() -> None:
    html = _asset_bytes("index.html", "token")[0].decode("utf-8")
    javascript = _asset_bytes("app.js", "token")[0].decode("utf-8")
    html_ids = set(re.findall(r'\bid="([^"]+)"', html))
    referenced_ids = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"\)', javascript))
    assert referenced_ids <= html_ids
    assert ".innerHTML" not in javascript
    assert "function renderAssistantMarkdown" in javascript
    assert "document.createTextNode" in javascript
    assert "unsafe-inline" not in html


def test_dashboard_uses_the_calm_ambient_theme() -> None:
    html = _asset_bytes("index.html", "token")[0].decode("utf-8")
    css = _asset_bytes("app.css", "token")[0].decode("utf-8")
    manifest = _asset_bytes("manifest.webmanifest", "token")[0].decode("utf-8")

    assert '--canvas: #fbfbf9' in css
    assert 'font-family: "Helvetica Neue"' in css
    assert "Georgia" not in css
    assert "radial-gradient(ellipse" in css
    assert '<meta name="theme-color" content="#fbfbf9">' in html
    assert '"theme_color": "#fbfbf9"' in manifest


def test_dashboard_prioritizes_next_work_over_debug_and_secondary_controls() -> None:
    html = _asset_bytes("index.html", "token")[0].decode("utf-8")
    javascript = _asset_bytes("app.js", "token")[0].decode("utf-8")
    css = _asset_bytes("app.css", "token")[0].decode("utf-8")

    assert 'id="metric-focus"' in html
    assert 'id="metric-replies"' in html
    assert 'id="metric-meetings"' in html
    assert 'id="metric-waiting"' not in html
    assert 'id="metric-review"' not in html
    assert 'id="more-actions"' in html
    assert ".more-actions-menu { width: 248px;" in css
    assert "left: 0; right: auto;" in css
    assert "Ask Carmen about this task" in html
    assert "Cached · refresh failed" not in javascript
    assert "Using the last successful sync" in javascript
    assert 'id="source-warning"' in html and 'source-warning hidden' in html


def test_chat_uses_collapsible_right_drawer_with_accessible_launcher() -> None:
    html = _asset_bytes("index.html", "token")[0].decode("utf-8")
    javascript = _asset_bytes("app.js", "token")[0].decode("utf-8")
    css = _asset_bytes("app.css", "token")[0].decode("utf-8")

    assert 'id="assistant-drawer"' in html
    assert 'aria-hidden="true"' in html
    assert 'id="chat-launcher"' in html
    assert 'aria-controls="assistant-drawer"' in html
    assert 'aria-expanded="false"' in html
    assert 'id="chat-close"' in html
    assert "assistant-panel panel" not in html
    assert "function setChatOpen" in javascript
    assert 'setAttribute("aria-hidden"' in javascript
    assert 'setAttribute("aria-expanded"' in javascript
    assert ".app-shell.chat-open" in css
    assert ".chat-drawer" in css
    assert 'id="edit-button"' in html
    assert 'id="edit-task-dialog"' in html
    assert 'id="edit-task-form"' in html
    assert 'id="draft-block"' in html
    assert "/edit" in javascript
    assert 'id="source-refresh-button"' in html
    assert 'id="reload-button"' in html
    assert "Sync work" in html
    assert 'id="source-seed-button"' in html
    assert "Refresh now" in html
    assert "Copy refresh command" not in html
    assert 'api("/api/refresh"' in javascript
    assert '$("#reload-button").addEventListener("click", refreshSources)' in javascript
    assert 'api("/api/seed-snapshot"' in javascript
    assert 'id="settings-button"' in html
    assert 'id="settings-dialog"' in html
    assert 'id="settings-form"' in html
    assert 'id="diagnostics-button"' in html
    assert 'id="install-schedule-button"' in html
    assert 'id="save-automation-button"' in html
    assert 'id="daily-refresh-select"' in html
    assert 'id="reminder-interval-select"' in html
    assert 'api("/api/automation"' in javascript
    assert 'id="connector-list"' in html
    assert 'id="connector-guidance"' in html
    assert '"Timed out"' in javascript
    assert "Sync in progress:" in javascript
    assert 'const failed = sources.filter' in javascript
    assert 'source.fresh && ["ok", "empty"].includes(source.status)' in javascript
    assert "three-minute safety limit" in javascript
    assert "Each source refreshes independently" not in html
    assert 'id="settings-refresh-button"' in html
    assert 'id="personalization-banner"' in html
    assert 'id="open-personalization-button"' in html
    assert 'id="personalization-section"' in html
    assert 'id="personalize-button"' in html
    assert 'id="refresh-voice-button"' in html
    assert 'id="fine-tune-button"' in html
    assert 'startOperation("personalize_voice"' in javascript
    assert "function renderPersonalization" in javascript
    assert ".personalization-steps" in css
    assert 'id="evaluate-button"' in html
    assert 'id="execute-button"' in html
    assert 'id="verify-context-button"' in html
    assert 'id="usually-skip-button"' in html
    assert 'id="usually-attend-button"' in html
    assert 'id="primary-action-button"' in html
    assert 'id="fix-date-button"' in html
    assert 'id="detail-backdrop"' in html
    assert 'id="source-health-strip"' in html
    assert 'id="quick-actions-dialog"' in html
    assert 'aria-label="Search tasks and commands"' in html
    assert 'class="icon-button header-search-button"' in html
    assert 'id="quick-action-input"' in html
    assert 'data-filter="review"' in html
    assert 'id="review-actions"' in html
    assert 'id="detail-recommendation"' in html
    assert 'data-task-prompt=' in html
    assert "Advanced settings" in html
    assert "Data stays on this device" not in html
    assert 'taskAction("accept")' in javascript
    assert 'taskAction("not-a-task")' in javascript
    assert 'taskAction("verify-context")' in javascript
    assert 'taskAction("usually-skip")' in javascript
    assert 'taskAction("usually-attend")' in javascript
    assert "function duePresentation" in javascript
    assert "function recommendedAction" in javascript
    assert "function runPrimaryAction" in javascript
    assert "function openQuickActions" in javascript
    assert 'event.key.toLocaleLowerCase() === "k"' in javascript
    assert ".detail-column.sheet-open" in css
    assert ".source-health-strip" in css
    assert ".quick-actions-dialog" in css
    assert 'api("/api/settings"' in javascript
    assert 'api("/api/operations"' in javascript
    assert 'api("/api/diagnostics"' in javascript


def test_http_handler_requires_local_host_and_session_token(tmp_path: Path) -> None:
    handler_type = build_handler(_service(tmp_path), session_token="expected")
    handler = object.__new__(handler_type)
    handler.headers = {"Host": "127.0.0.1:8765", "X-Carmen-Session": "expected"}
    assert handler._host_is_local()
    assert handler._authorized_write()
    handler.headers = {"Host": "attacker.example", "X-Carmen-Session": "wrong"}
    assert not handler._host_is_local()
    assert not handler._authorized_write()


def test_local_ui_refuses_non_loopback_binding() -> None:
    with pytest.raises(ValueError, match="loopback"):
        build_server(host="0.0.0.0", port=0)
