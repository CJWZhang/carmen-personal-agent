from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from carmen_agent.skills.schemas import parse_stamp

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_.-]+", re.I)
_STOP = {
    "a", "an", "and", "are", "for", "from", "i", "in", "is", "it", "me",
    "my", "of", "on", "or", "the", "this", "to", "what", "with",
}
_QUERY_EXPANSIONS = {
    "blocked": {"waiting", "dependency", "stuck", "review"},
    "reply": {"respond", "message", "email", "slack"},
    "respond": {"reply", "message", "email", "slack"},
    "meeting": {"calendar", "prepare", "agenda", "attendees"},
    "priority": {"urgent", "deadline", "due", "focus"},
    "review": {"github", "pull", "request", "pr"},
}


def terms(text: str) -> set[str]:
    values = {match.group(0).casefold() for match in _TOKEN_RE.finditer(text)} - _STOP
    expanded = set(values)
    for value in values:
        expanded.update(_QUERY_EXPANSIONS.get(value, set()))
    return expanded


def _field_text(record: dict[str, Any]) -> str:
    fields = (
        record.get("title", ""), record.get("why", ""), record.get("source", ""),
        record.get("status", ""), record.get("urgency", ""),
        " ".join(str(value) for value in record.get("people", [])),
    )
    return " ".join(str(value) for value in fields)


def _temporal_score(record: dict[str, Any], now: datetime) -> float:
    raw = str(
        record.get("latest_activity_at")
        or record.get("last_seen")
        or record.get("due")
        or ""
    )
    parsed = parse_stamp(raw)
    if parsed is None:
        return 0.0
    age_hours = abs((now - parsed.astimezone(timezone.utc)).total_seconds()) / 3600
    return 1.0 / (1.0 + math.log1p(age_hours))


@dataclass(frozen=True)
class RetrievalHit:
    record: dict[str, Any]
    score: float
    reasons: tuple[str, ...]


class HybridRetriever:
    """Exact, lexical, state-aware, and temporal retrieval without network calls."""

    def retrieve(
        self,
        query: str,
        records: Iterable[dict[str, Any]],
        *,
        intent: str,
        selected_id: str = "",
        limit: int = 10,
        now: datetime | None = None,
    ) -> list[RetrievalHit]:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        query_terms = terms(query)
        hits: list[RetrievalHit] = []
        for record in records:
            record_terms = terms(_field_text(record))
            overlap = len(query_terms & record_terms) / max(1, len(query_terms))
            score = overlap * 45
            reasons: list[str] = []
            if overlap:
                reasons.append("query match")
            if selected_id and str(record.get("id")) == selected_id:
                score += 100
                reasons.append("selected task")
            if intent == "blocked" and record.get("status") == "waiting":
                score += 45
                reasons.append("waiting")
            if intent in {"focus", "plan"}:
                score += {"now": 35, "today": 25, "this_week": 8}.get(
                    str(record.get("urgency")), 0
                )
                if record.get("needs_reply"):
                    score += 8
            if intent == "draft" and record.get("needs_reply"):
                score += 35
                reasons.append("reply needed")
            if record.get("snapshot_status") == "current":
                score += 8
            elif record.get("snapshot_status") in {"stale", "cached", "saved"}:
                score -= 10
            preference = record.get("priority_adjustment", 0)
            if type(preference) is int:
                score += max(-30, min(30, preference)) * 0.5
                if preference:
                    reasons.append("personalized")
            temporal = _temporal_score(record, current)
            score += temporal * 12
            if temporal > 0.5:
                reasons.append("recent")
            # General workspace queries still need a deterministic useful order.
            if not query_terms:
                score += {"now": 20, "today": 12, "this_week": 4}.get(
                    str(record.get("urgency")), 0
                )
            hits.append(RetrievalHit(record, round(score, 4), tuple(reasons)))
        return sorted(
            hits,
            key=lambda hit: (
                hit.score,
                str(hit.record.get("last_seen") or ""),
                str(hit.record.get("id") or ""),
            ),
            reverse=True,
        )[: max(1, min(30, limit))]


__all__ = ["HybridRetriever", "RetrievalHit", "terms"]
