from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from carmen_agent.actions.extractor import action_from_extraction, extract_action, heuristic_extract
from carmen_agent.actions.notifications import render_digest
from carmen_agent.actions.ranking import priority_score, should_interrupt
from carmen_agent.actions.resolver import deterministic_resolution, semantic_resolution
from carmen_agent.actions.schemas import ActionEvidence, ActionItem
from carmen_agent.connectors.base import SourceEvent, SourceMessage
from carmen_agent.hosted.storage import HostedUser, Installation, SQLiteStore
from carmen_agent.hosted.worker import enqueue_reconciliation


class InvalidJsonProvider:
    def generate(self, messages, **_):
        assert messages
        return "not json"


class ResolutionProvider:
    def generate(self, messages, **_):
        assert messages
        return '{"status":"completed","confidence":0.96,"reason":"User said it is wrapped up.","evidence_id":"2.0"}'


class WrongShapeProvider:
    def generate(self, messages, **_):
        assert messages
        return (
            '{"is_actionable":"true","action_type":"review",'
            '"assigned_to_user":true,"explicit_ask":true,"summary":"Review",'
            '"requester_id":"U2","deadline_text":"","due_at":"",'
            '"confidence":0.99,"rationale":"ask"}'
        )


def _event(**overrides: object) -> SourceEvent:
    values: dict[str, object] = {
        "source": "slack",
        "tenant_id": "T1",
        "target_user_id": "U1",
        "source_id": "100.1",
        "actor_id": "U2",
        "text": "<@U1> can you review the PR by tomorrow?",
        "channel_id": "C1",
        "thread_id": "100.1",
        "occurred_at": "100.1",
    }
    values.update(overrides)
    return SourceEvent(**values)  # type: ignore[arg-type]


def _store(path: Path) -> SQLiteStore:
    store = SQLiteStore(path)
    store.save_installation(
        Installation(
            team_id="T1",
            team_name="Test",
            enterprise_id="",
            bot_user_id="B1",
            bot_token_encrypted="encrypted:bot",
            user_token_encrypted="",
            installing_user_id="U1",
            scopes="commands",
        )
    )
    store.save_user(HostedUser(team_id="T1", user_id="U1"))
    return store


def test_extractor_requires_assignment_evidence_for_incoming_channels() -> None:
    ambient = _event(text="can someone review the PR?", direct=False)
    assert not heuristic_extract(ambient).is_actionable
    result = extract_action(InvalidJsonProvider(), _event())  # type: ignore[arg-type]
    assert result.is_actionable
    assert result.action_type == "review"
    assert result.explicit_ask


def test_extractor_rejects_wrong_json_runtime_types() -> None:
    ambient = _event(text="can someone review the PR?", direct=False)
    result = extract_action(WrongShapeProvider(), ambient)  # type: ignore[arg-type]
    assert result.is_actionable is False


def test_self_authored_commitment_is_actionable() -> None:
    event = _event(actor_id="U1", text="I'll send the revised doc this afternoon")
    result = heuristic_extract(event)
    assert result.is_actionable
    assert not result.explicit_ask
    assert result.action_type == "deliverable"


def test_action_store_preserves_evidence_and_transition_history(tmp_path: Path) -> None:
    store = _store(tmp_path / "hosted.db")
    event = _event()
    extracted = action_from_extraction(event, heuristic_extract(event))
    assert extracted is not None
    action, evidence = extracted
    store.save_action(action, evidence)
    loaded = store.get_action(team_id="T1", user_id="U1", selector=action.id[:10])
    assert loaded is not None
    assert loaded.summary.startswith("can you review")
    assert store.latest_action_evidence(action.id) == evidence
    assert store.transition_action(
        action_id=action.id, status="completed", reason="Marked done by user."
    )
    assert store.list_actions(team_id="T1", user_id="U1") == []


def test_resolution_detects_reply_and_completion() -> None:
    reply = ActionItem.create(
        team_id="T1", user_id="U1", source="slack", source_id="1.0",
        action_type="reply", summary="Reply to Alex",
    )
    result = deterministic_resolution(
        reply, [SourceMessage(id="2.0", actor_id="U1", text="sounds good")]
    )
    assert result is not None and result.status == "responded"
    deliverable = ActionItem.create(
        team_id="T1", user_id="U1", source="slack", source_id="1.0",
        action_type="deliverable", summary="Send launch notes",
    )
    result = deterministic_resolution(
        deliverable, [SourceMessage(id="2.0", actor_id="U1", text="done — sent it")]
    )
    assert result is not None and result.status == "completed"


def test_semantic_resolution_requires_high_confidence_user_evidence() -> None:
    action = ActionItem.create(
        team_id="T1", user_id="U1", source="slack", source_id="1.0",
        action_type="deliverable", summary="Finalize launch plan",
    )
    result = semantic_resolution(
        ResolutionProvider(),  # type: ignore[arg-type]
        action,
        [SourceMessage(id="2.0", actor_id="U1", text="this is all wrapped up")],
    )
    assert result is not None and result.status == "completed"
    assert semantic_resolution(
        ResolutionProvider(),  # type: ignore[arg-type]
        action,
        [SourceMessage(id="2.0", actor_id="U2", text="this is all wrapped up")],
    ) is None


def test_priority_and_digest_are_evidence_backed() -> None:
    due = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    action = ActionItem.create(
        team_id="T1", user_id="U1", source="slack", source_id="1.0",
        action_type="review", summary="Review the launch PR", confidence=0.95,
        explicit_ask=True, due_at=due,
    )
    evidence = ActionEvidence(source="slack", source_id="1.0", excerpt="Can you review this today?")
    assert priority_score(action) >= 75
    assert should_interrupt(action)
    rendered = render_digest([action], {action.id: evidence})
    assert "Review the launch PR" in rendered
    assert "Can you review this today?" in rendered


def test_reconciliation_queue_uses_hourly_cursor(tmp_path: Path) -> None:
    store = _store(tmp_path / "hosted.db")
    now = datetime(2026, 8, 12, 12, 30, tzinfo=timezone.utc)
    assert enqueue_reconciliation(store, now=now) == 1
    assert enqueue_reconciliation(store, now=now) == 0
