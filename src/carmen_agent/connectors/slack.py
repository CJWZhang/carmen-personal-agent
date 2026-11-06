from __future__ import annotations

from typing import Any

from carmen_agent.connectors.base import SourceEvent


def target_user_id(payload: dict[str, Any], fallback: str = "") -> str:
    """Resolve the human authorization target without trusting the author field."""
    for authorization in payload.get("authorizations") or []:
        if not isinstance(authorization, dict):
            continue
        candidate = str(authorization.get("user_id", ""))
        if candidate and not authorization.get("is_bot"):
            return candidate
    return fallback


def normalize_slack_event(
    *, payload: dict[str, Any], target_user: str
) -> SourceEvent | None:
    event = payload.get("event") or {}
    if not isinstance(event, dict) or event.get("type") != "message":
        return None
    if event.get("subtype") or event.get("bot_id"):
        return None
    text = str(event.get("text", "")).strip()
    source_id = str(event.get("ts", ""))
    channel_id = str(event.get("channel", ""))
    if not text or not source_id or not channel_id or not target_user:
        return None
    return SourceEvent(
        source="slack",
        tenant_id=str(payload.get("team_id", "")),
        target_user_id=target_user,
        source_id=source_id,
        actor_id=str(event.get("user", "")),
        text=text[:4_000],
        channel_id=channel_id,
        thread_id=str(event.get("thread_ts") or source_id),
        occurred_at=source_id,
        direct=channel_id.startswith("D"),
    )
