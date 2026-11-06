"""Deterministic source state that should not be delegated to a model."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone

from carmen_agent.connectors.calendar import calendar_resolved
from carmen_agent.connectors.github import github_resolved
from carmen_agent.skills.schemas import InboxItem, TodoItem, parse_stamp


_PAST_CALENDAR_KINDS = {
    "event",
    "event_conflict",
    "event_decision",
    "event_response",
    "meeting",
    "meeting_prep",
}


def _resolved_todo(item: InboxItem, evidence: str) -> TodoItem:
    return TodoItem(
        source=item.source,
        id=item.id,
        title=item.subject or f"Resolved {item.source} item",
        why=evidence,
        urgency="fyi",
        needs_reply=False,
        links=[item.url] if item.url else [],
        sources=[item.source],
        source_ids=[f"{item.source}:{item.id}"],
        people=list(item.participants) or ([item.sender] if item.sender else []),
        confidence=1.0,
        status="done",
        related_keys=list(item.related_keys),
        completion_evidence=evidence,
        lifecycle_stage="done",
        resolution_mode="source_verified",
        last_verified_at=datetime.now(timezone.utc).isoformat(),
    )


def partition_source_items(
    items: list[InboxItem], *, now: datetime | None = None
) -> tuple[list[InboxItem], list[TodoItem]]:
    """Return model-actionable items and deterministic completion records."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    actionable: list[InboxItem] = []
    resolved: list[TodoItem] = []
    for item in items:
        raw = asdict(item)
        resolution_evidence = str(
            item.metadata.get("resolution_evidence") or ""
        ).strip()
        if item.metadata.get("user_replied_after_ask") is True:
            resolved.append(
                _resolved_todo(item, resolution_evidence or "A later source message shows the user answered the ask.")
            )
            continue
        if resolution_evidence:
            resolved.append(_resolved_todo(item, resolution_evidence))
            continue
        expires_at = parse_stamp(str(item.metadata.get("expires_at") or ""))
        if expires_at is not None and expires_at <= current:
            resolved.append(_resolved_todo(item, "The source action window has expired."))
            continue
        if item.source == "github" and github_resolved(raw):
            resolved.append(_resolved_todo(item, "GitHub reports this item closed or merged."))
            continue
        if item.source == "calendar":
            if calendar_resolved(raw):
                resolved.append(_resolved_todo(item, "Calendar response is complete or the event was cancelled."))
                continue
            event_time = parse_stamp(item.ts)
            kind = item.kind.strip().lower()
            expires_at_start = kind in _PAST_CALENDAR_KINDS or not kind
            if event_time is not None and expires_at_start and event_time <= current:
                resolved.append(_resolved_todo(item, "Calendar event has already passed."))
                continue
        if item.status == "done":
            resolved.append(_resolved_todo(item, "The connected source reports this item complete."))
            continue
        actionable.append(item)
    return actionable, resolved
