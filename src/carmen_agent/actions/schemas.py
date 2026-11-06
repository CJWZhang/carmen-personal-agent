from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

ActionType = Literal[
    "reply",
    "review",
    "decision",
    "deliverable",
    "meeting_prep",
    "follow_up",
]
ActionStatus = Literal[
    "candidate",
    "open",
    "waiting",
    "responded",
    "completed",
    "dismissed",
    "stale",
]
SourceName = Literal["slack", "gmail", "calendar", "github", "manual"]

OPEN_STATUSES: tuple[ActionStatus, ...] = ("candidate", "open", "waiting")
TERMINAL_STATUSES: tuple[ActionStatus, ...] = (
    "responded",
    "completed",
    "dismissed",
    "stale",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_action_id(
    *, team_id: str, user_id: str, source: str, source_id: str, action_type: str
) -> str:
    material = "\x1f".join((team_id, user_id, source, source_id, action_type))
    return "act-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class ActionEvidence:
    source: SourceName
    source_id: str
    excerpt: str
    permalink: str = ""
    actor_id: str = ""
    observed_at: str = ""


@dataclass(frozen=True)
class ExtractionResult:
    is_actionable: bool
    action_type: ActionType = "follow_up"
    assigned_to_user: bool = False
    explicit_ask: bool = False
    summary: str = ""
    requester_id: str = ""
    deadline_text: str = ""
    due_at: str = ""
    confidence: float = 0.0
    rationale: str = ""


@dataclass(frozen=True)
class ActionItem:
    id: str
    team_id: str
    user_id: str
    source: SourceName
    source_id: str
    action_type: ActionType
    summary: str
    status: ActionStatus = "open"
    confidence: float = 0.5
    priority_score: float = 0.0
    requester_id: str = ""
    explicit_ask: bool = False
    due_at: str = ""
    channel_id: str = ""
    thread_id: str = ""
    permalink: str = ""
    snoozed_until: str = ""
    resolution_reason: str = ""
    last_checked_at: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        *,
        team_id: str,
        user_id: str,
        source: SourceName,
        source_id: str,
        action_type: ActionType,
        summary: str,
        **values: object,
    ) -> ActionItem:
        return cls(
            id=stable_action_id(
                team_id=team_id,
                user_id=user_id,
                source=source,
                source_id=source_id,
                action_type=action_type,
            ),
            team_id=team_id,
            user_id=user_id,
            source=source,
            source_id=source_id,
            action_type=action_type,
            summary=summary,
            **values,  # type: ignore[arg-type]
        )
