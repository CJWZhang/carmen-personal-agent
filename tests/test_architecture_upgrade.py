from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from carmen_agent.collector import rebuild_snapshots_from_events, write_snapshots
from carmen_agent.connectors.direct.base import DirectSyncResult
from carmen_agent.connectors.direct.registry import DirectConnectorRegistry
from carmen_agent.context.packet import build_context_packet
from carmen_agent.domain.events import SourceObservation
from carmen_agent.ingestion.sync import DirectSyncCoordinator
from carmen_agent.observability.telemetry import TelemetryStore
from carmen_agent.projections.tasks import TaskProjector
from carmen_agent.ranking.personalized import rank_task
from carmen_agent.skills.schemas import InboxItem, TodoItem
from carmen_agent.storage.event_store import JsonlEventStore


def _item(*, source: str = "slack", item_id: str = "171.1", status: str = "open") -> InboxItem:
    return InboxItem(
        source=source,  # type: ignore[arg-type]
        id=item_id,
        sender="Alice",
        subject="Launch decision",
        body="Can you confirm the launch plan?",
        url="https://example.test/item",
        ts="2026-08-13T14:00:00+00:00",
        channel="launch" if source == "slack" else "",
        thread_id="171.0" if source == "slack" else "",
        kind="message" if source == "slack" else "email",
        status=status,  # type: ignore[arg-type]
        participants=["Alice"],
        related_keys=[f"{source}:launch"],
        metadata={
            "latest_activity_at": "2026-08-13T14:00:00+00:00",
            "user_replied_after_ask": status == "done",
            "note": "legacy placeholder is intentionally omitted",
        },
    )


def _task(**overrides: object) -> TodoItem:
    values: dict[str, object] = {
        "source": "slack",
        "id": "task-launch",
        "title": "Reply about launch",
        "why": "Alice needs the launch decision",
        "urgency": "today",
        "needs_reply": True,
        "sources": ["slack"],
        "source_ids": ["slack:171.1"],
        "confidence": 0.8,
    }
    values.update(overrides)
    return TodoItem(**values)  # type: ignore[arg-type]


def test_sparse_source_observation_is_typed_hashed_and_round_trips() -> None:
    observation = SourceObservation.from_inbox_item(
        _item(), batch_id="sync-1", observed_at="2026-08-13T14:01:00+00:00"
    )

    assert observation.source == "slack"
    assert observation.content["participants"] == ["Alice"]
    assert observation.attributes["user_replied_after_ask"] is False
    assert "note" not in observation.attributes
    assert observation.to_inbox_item().id == "171.1"
    assert SourceObservation.from_dict(observation.as_dict()) == observation

    tampered = observation.as_dict()
    tampered["content"]["body"] = "different"
    with pytest.raises(ValueError, match="payload hash"):
        SourceObservation.from_dict(tampered)


def test_event_store_is_append_only_idempotent_and_replayable(tmp_path: Path) -> None:
    store = JsonlEventStore(tmp_path / "events.jsonl")
    first = SourceObservation.from_inbox_item(
        _item(), batch_id="sync-1", observed_at="2026-08-13T14:01:00+00:00"
    )
    newer = SourceObservation.from_inbox_item(
        replace(
            _item(),
            body="Please confirm the revised launch plan.",
            metadata={"latest_activity_at": "2026-08-13T14:05:00+00:00"},
        ),
        batch_id="sync-2",
        observed_at="2026-08-13T14:06:00+00:00",
    )

    assert store.append([first, first]) == 1
    assert store.append([first, newer]) == 1
    assert len(store.read()) == 2
    assert store.latest_by_entity()[("slack", "slack:launch")] == newer
    assert TaskProjector().source_items(store.read())[0].body.startswith("Please confirm")


def test_task_projector_applies_only_source_verified_terminal_state() -> None:
    completed = replace(
        _item(status="done"),
        metadata={
            "latest_activity_at": "2026-08-13T14:10:00+00:00",
            "resolution_evidence": "Alice confirmed the thread is resolved.",
        },
    )
    projected = TaskProjector().apply_source_truth([_task()], [completed])

    assert projected[0].status == "done"
    assert projected[0].lifecycle_stage == "done"
    assert projected[0].resolution_mode == "source_verified"
    assert "Alice confirmed" in projected[0].completion_evidence


class _FakeConnector:
    def __init__(self, source: str, *, fail: bool = False) -> None:
        self.source = source
        self.fail = fail

    def available(self) -> bool:
        return True

    def sync(self, *, cursor: str = "", hours: int = 36) -> DirectSyncResult:
        if self.fail:
            raise RuntimeError("synthetic provider failure")
        return DirectSyncResult(
            source=self.source,  # type: ignore[arg-type]
            items=(_item(source=self.source, item_id=f"{self.source}-1"),),
            cursor=f"{self.source}-cursor",
            detail="synthetic direct read",
        )


def test_direct_connectors_commit_independent_health_and_latency(tmp_path: Path) -> None:
    inbox = tmp_path / "data" / "inbox"
    registry = DirectConnectorRegistry(
        connectors=(
            _FakeConnector("slack"),  # type: ignore[arg-type]
            _FakeConnector("gmail", fail=True),  # type: ignore[arg-type]
        )
    )
    telemetry = TelemetryStore(tmp_path / "work" / "metrics.jsonl")
    coordinator = DirectSyncCoordinator(
        registry=registry, inbox_dir=inbox, telemetry=telemetry
    )

    counts, coverage, latency = coordinator.sync(["slack", "gmail"])  # type: ignore[list-item]

    assert counts == {"slack": 1, "gmail": 0}
    assert [record["status"] for record in coverage] == ["ok", "error"]
    assert set(latency) == {"slack", "gmail"}
    assert (inbox / "slack.json").is_file()
    assert not (inbox / "gmail.json").exists()
    status = json.loads((inbox / "_source-status.json").read_text(encoding="utf-8"))
    assert status["sources"]["slack"]["status"] == "ok"
    assert status["sources"]["gmail"]["status"] == "error"
    assert (tmp_path / "work" / "source-cursors.json").is_file()
    assert telemetry.summary()["sync"]["samples"] == 2  # type: ignore[index]


def test_query_aware_packet_is_bounded_and_omits_unrelated_draft_work() -> None:
    records = [
        {
            "id": "launch", "title": "Reply about launch", "why": "Decision needed",
            "source": "slack", "status": "open", "urgency": "today",
            "needs_reply": True, "people": ["Alice"], "snapshot_status": "current",
        },
        {
            "id": "dinner", "title": "Book dinner", "why": "Personal errand",
            "source": "manual", "status": "open", "urgency": "today",
            "needs_reply": False, "people": [], "snapshot_status": "current",
        },
    ]

    packet = build_context_packet(
        message="Draft a reply about the launch decision",
        workspace_tasks=records,
        evidence=(),
        max_tokens=350,
    )

    assert packet.intent == "draft"
    assert packet.estimated_tokens <= 350
    assert packet.task_ids == ("launch",)
    assert "Reply about launch" in packet.text
    assert "Book dinner" not in packet.text


def test_personalized_ranking_changes_priority_but_remains_explainable() -> None:
    normal = _task(source="calendar", sources=["calendar"], needs_reply=False)
    optional = replace(
        normal,
        priority_adjustment=-30,
        preference_reason="Usually skipped optional series",
    )

    normal_rank = rank_task(normal)
    optional_rank = rank_task(optional)
    assert optional_rank.score < normal_rank.score
    assert "Usually skipped optional series" in optional_rank.reasons


def test_telemetry_is_aggregate_only_and_reports_quality_latency(tmp_path: Path) -> None:
    store = TelemetryStore(tmp_path / "telemetry.jsonl")
    store.record(
        "chat", status="ok", intent="focus", duration_ms=125.0,
        input_tokens=300, output_chars=120, fallback_used=False, truncated=False,
    )
    with pytest.raises(ValueError, match="forbidden fields"):
        store.record("chat", task_id="private-id")

    summary = store.summary()
    assert summary["chat"]["samples"] == 1  # type: ignore[index]
    assert summary["chat"]["average_duration_ms"] == 125.0  # type: ignore[index]


def test_snapshot_writer_appends_private_event_history(tmp_path: Path) -> None:
    inbox = tmp_path / "data" / "inbox"
    write_snapshots(
        {
            "items": [_item()],
            "coverage": [{"source": "slack", "status": "ok", "detail": "direct"}],
        },
        inbox_dir=inbox,
    )

    events = JsonlEventStore(tmp_path / "data" / "events" / "source-events.jsonl").read()
    assert len(events) == 2
    assert events[0].native_id == "171.1"
    assert events[1].event_type == "checkpoint"


def test_empty_checkpoint_replay_does_not_resurrect_an_old_snapshot(tmp_path: Path) -> None:
    inbox = tmp_path / "data" / "inbox"
    write_snapshots(
        {
            "items": [_item()],
            "coverage": [{"source": "slack", "status": "ok", "detail": "first"}],
        },
        inbox_dir=inbox,
    )
    write_snapshots(
        {
            "items": [],
            "coverage": [{"source": "slack", "status": "empty", "detail": "second"}],
        },
        inbox_dir=inbox,
    )
    (inbox / "slack.json").unlink()

    counts = rebuild_snapshots_from_events(["slack"], inbox_dir=inbox)

    assert counts == {"slack": 0}
    assert json.loads((inbox / "slack.json").read_text(encoding="utf-8")) == []


def test_replay_keeps_unchanged_item_reobserved_in_a_new_batch(tmp_path: Path) -> None:
    inbox = tmp_path / "data" / "inbox"
    payload = {
        "items": [_item()],
        "coverage": [{"source": "slack", "status": "ok", "detail": "current"}],
    }
    write_snapshots(payload, inbox_dir=inbox)
    write_snapshots(payload, inbox_dir=inbox)
    (inbox / "slack.json").unlink()

    assert rebuild_snapshots_from_events(["slack"], inbox_dir=inbox) == {"slack": 1}
