from __future__ import annotations

from typing import Any

from carmen_agent.connectors.base import SourceEvent


def normalize_calendar_record(
    record: dict[str, Any], *, tenant_id: str, target_user_id: str
) -> SourceEvent | None:
    event_id = str(record.get("id", ""))
    title = str(record.get("subject", "")).strip()
    body = str(record.get("body", "")).strip()
    if not event_id or not title:
        return None
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    text = f"Calendar event: {title}\n{body}".strip()
    return SourceEvent(
        source="calendar",
        tenant_id=tenant_id,
        target_user_id=target_user_id,
        source_id=event_id,
        actor_id=str(metadata.get("organizer") or record.get("sender", "")),
        text=text[:4_000],
        permalink=str(record.get("url", "")),
        occurred_at=str(record.get("ts", "")),
        direct=True,
    )


def calendar_resolved(record: dict[str, Any]) -> bool:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    response = str(metadata.get("response_status", "")).lower()
    if bool(metadata.get("cancelled")) or response == "declined":
        return True
    kind = str(record.get("kind") or metadata.get("kind") or "").lower()
    # Accepting an invitation resolves the RSVP, not the work needed to prepare
    # for that meeting. Only response-shaped records are complete on RSVP state.
    return kind == "event_response" and response in {"accepted", "tentative"}
