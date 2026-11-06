from __future__ import annotations

from typing import Any

from carmen_agent.connectors.base import SourceEvent


def normalize_github_record(
    record: dict[str, Any], *, tenant_id: str, target_user_id: str
) -> SourceEvent | None:
    item_id = str(record.get("id", ""))
    title = str(record.get("subject", "")).strip()
    body = str(record.get("body", "")).strip()
    if not item_id or not title:
        return None
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    kind = str(record.get("kind") or metadata.get("kind") or "GitHub item")
    return SourceEvent(
        source="github",
        tenant_id=tenant_id,
        target_user_id=target_user_id,
        source_id=item_id,
        actor_id=str(record.get("sender", "")),
        text=f"{kind}: {title}\n{body}".strip()[:4_000],
        permalink=str(record.get("url", "")),
        occurred_at=str(record.get("ts", "")),
        direct=bool(metadata.get("review_requested") or metadata.get("assigned")),
    )


def github_resolved(record: dict[str, Any]) -> bool:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    state = str(metadata.get("state") or record.get("status", "")).lower()
    return state in {"closed", "merged", "approved", "done"} or bool(metadata.get("review_submitted"))
