from __future__ import annotations

import re
from typing import Literal

QueryIntent = Literal[
    "draft", "focus", "blocked", "plan", "status", "task", "workspace",
]

_DRAFT = re.compile(r"\b(draft|reply|respond|write|send)\b", re.I)
_FOCUS = re.compile(r"\b(focus|priority|priorities|next|urgent|important)\b", re.I)
_BLOCKED = re.compile(r"\b(blocked|blocking|waiting|stuck|dependenc(?:y|ies))\b", re.I)
_PLAN = re.compile(r"\b(plan|schedule|today|day|hours?|afternoon|morning)\b", re.I)
_STATUS = re.compile(r"\b(status|changed|update|progress|done|complete|open loops?)\b", re.I)


def route_query(message: str, *, has_selected_task: bool = False) -> QueryIntent:
    text = " ".join(message.split())
    if _DRAFT.search(text):
        return "draft"
    if _BLOCKED.search(text):
        return "blocked"
    if _FOCUS.search(text):
        return "focus"
    if _PLAN.search(text):
        return "plan"
    if _STATUS.search(text):
        return "status"
    if has_selected_task:
        return "task"
    return "workspace"


__all__ = ["QueryIntent", "route_query"]
