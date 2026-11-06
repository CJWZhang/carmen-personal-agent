from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from carmen_agent.actions.schemas import ActionItem


def _parse(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def priority_score(action: ActionItem, *, now: datetime | None = None) -> float:
    current = now or datetime.now(timezone.utc)
    score = action.confidence * 35
    if action.explicit_ask:
        score += 25
    score += {
        "reply": 14,
        "review": 12,
        "decision": 16,
        "deliverable": 14,
        "meeting_prep": 10,
        "follow_up": 8,
    }.get(action.action_type, 0)
    due = _parse(action.due_at)
    if due is not None:
        hours = (due - current).total_seconds() / 3600
        score += 30 if hours <= 0 else (22 if hours <= 24 else (10 if hours <= 72 else 0))
    snoozed = _parse(action.snoozed_until)
    if snoozed is not None and snoozed > current:
        score -= 100
    if action.status == "candidate":
        score -= 20
    if action.status not in {"candidate", "open", "waiting"}:
        score -= 100
    return round(max(0.0, min(100.0, score)), 2)


def rank_actions(actions: list[ActionItem], *, now: datetime | None = None) -> list[ActionItem]:
    scored = [replace(action, priority_score=priority_score(action, now=now)) for action in actions]
    return sorted(scored, key=lambda item: (item.priority_score, item.created_at), reverse=True)


def should_interrupt(action: ActionItem, *, now: datetime | None = None) -> bool:
    """Only high-confidence direct asks or imminent deadlines may interrupt."""
    if action.status != "open" or action.confidence < 0.85:
        return False
    due = _parse(action.due_at)
    imminent = due is not None and (due - (now or datetime.now(timezone.utc))).total_seconds() <= 86_400
    return imminent or (action.explicit_ask and priority_score(action, now=now) >= 75)
