from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from carmen_agent.skills.schemas import TodoItem, parse_stamp


@dataclass(frozen=True)
class PersonalizedRank:
    score: float
    reasons: tuple[str, ...]


def rank_task(
    task: TodoItem,
    *,
    snapshot_status: str = "current",
    now: datetime | None = None,
) -> PersonalizedRank:
    """Rank current obligations while keeping preferences inspectable."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    score = float(task.confidence) * 20
    reasons: list[str] = []
    urgency = {"now": 45, "today": 32, "this_week": 12, "fyi": 0}.get(
        task.urgency, 0
    )
    score += urgency
    if urgency:
        reasons.append(task.urgency)
    if task.needs_reply:
        score += 12
        reasons.append("reply expected")
    if task.lifecycle_stage == "in_progress":
        score += 8
        reasons.append("in progress")
    if task.status == "waiting":
        score -= 18
        reasons.append("waiting on someone else")
    if task.review_state == "accepted":
        score += 4
    due = parse_stamp(task.due)
    if due is not None:
        hours = (due.astimezone(timezone.utc) - current).total_seconds() / 3600
        if hours <= 0:
            score += 28
            reasons.append("overdue")
        elif hours <= 24:
            score += 22
            reasons.append("due within 24 hours")
        elif hours <= 72:
            score += 10
            reasons.append("due soon")
    adjustment = max(-100, min(100, task.priority_adjustment))
    score += adjustment
    if adjustment:
        reasons.append(task.preference_reason or "personalized preference")
    if snapshot_status in {"saved", "cached", "stale"}:
        score -= 12
        reasons.append("needs source verification")
    if task.lifecycle_stage in {"done", "dismissed", "superseded"} or task.status == "done":
        score = 0
    return PersonalizedRank(
        score=round(max(0.0, min(100.0, score)), 2),
        reasons=tuple(reasons),
    )


__all__ = ["PersonalizedRank", "rank_task"]
