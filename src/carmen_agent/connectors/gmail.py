from __future__ import annotations

from typing import Any

from carmen_agent.connectors.base import SourceEvent


def normalize_gmail_record(
    record: dict[str, Any], *, tenant_id: str, target_user_id: str
) -> SourceEvent | None:
    message_id = str(record.get("id", ""))
    body = str(record.get("body", "")).strip()
    subject = str(record.get("subject", "")).strip()
    if not message_id or not (body or subject):
        return None
    text = f"Subject: {subject}\n{body}".strip()
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    return SourceEvent(
        source="gmail",
        tenant_id=tenant_id,
        target_user_id=target_user_id,
        source_id=message_id,
        actor_id=str(record.get("sender", "")),
        text=text[:4_000],
        thread_id=str(record.get("thread_id") or metadata.get("thread_id") or message_id),
        permalink=str(record.get("url", "")),
        occurred_at=str(record.get("ts", "")),
        direct=True,
    )


def gmail_resolved(record: dict[str, Any]) -> bool:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    labels = {str(value).upper() for value in metadata.get("labels", [])}
    return bool(metadata.get("user_replied")) or "SENT" in labels
