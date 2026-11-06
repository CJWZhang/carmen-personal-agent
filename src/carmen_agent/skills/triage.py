from __future__ import annotations

import json
import re
from typing import Protocol

from carmen_agent.skills.schemas import InboxItem, TodoItem, Urgency
from carmen_agent.infra.context import ContextEngine
from carmen_agent.configuration import config_value

VALID_URGENCY: set[Urgency] = {"now", "today", "this_week", "fyi"}

TRIAGE_SYSTEM_TEMPLATE = (
    "You triage a single inbox item and return ONE JSON object, nothing else. "
    "Schema: {\"title\": str, \"why\": str, \"urgency\": \"now|today|this_week|fyi\", "
    "\"needs_reply\": bool, \"due\": str, \"people\": [str], \"confidence\": number}. "
    "title: <=80 chars, imperative. why: <=140 chars explaining the ask. "
    "urgency: now if hard deadline <24h or explicit blocker, today if same-day ask, "
    "this_week if soft ask, fyi if informational. "
    "needs_reply: true only if the sender expects a written response. "
    "due: an ISO date/datetime ONLY if the item text literally quotes that date or "
    "an unambiguous deadline word tied to a date; if you'd have to infer, return \"\". "
    "Never invent dates. people: only people named in the item. "
    "confidence: how sure you are that {user_name} must personally act on this item before "
    "end of day. NOT confidence in your classification. If the next action clearly "
    "belongs to someone else (author must fix findings, reviewer must approve, PM must "
    "decide) OR you're unsure {user_name} even owns this, use <=0.3. Use 0.7+ only when "
    "{user_name} is unambiguously the sole owner of the next step. "
    "For GitHub, identify whether the next action is review, respond to feedback, fix CI, "
    "resolve a conflict, or follow up on an assigned issue. "
    "For calendar, create a task only when preparation or a decision is actually needed."
)


def triage_system_prompt() -> str:
    user_name = config_value("CARMEN_USER_NAME", "user_name", "the user")
    return TRIAGE_SYSTEM_TEMPLATE.replace("{user_name}", user_name)

# Hard cap applied when the collector marked the item as waiting on someone else.
# The model doesn't see status (we strip it from the schema to avoid it flipping
# open→waiting from a snippet), so we enforce the semantic in code.
_WAITING_CONFIDENCE_CAP = 0.4

_GITHUB_ACTION_PREFIXES = {
    "merge": "Merge PR",
    "review": "Review PR",
    "address_review": "Address review feedback",
    "fix_ci": "Fix CI",
    "resolve_conflict": "Resolve merge conflict",
    "follow_up": "Follow up on PR",
}

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_TRIAGE_KEYS = {"title", "why", "urgency", "needs_reply", "due", "people", "confidence"}
MAX_MODEL_OUTPUT_CHARS = 8_192


def _validated_due(raw_due: str, item: InboxItem) -> str:
    """Keep the model's due date only when it literally appears in the item text.

    The local 4B model tends to hallucinate deadlines out of vague phrases like
    "before Thursday" or "soon". If we can't ground the date in the source, drop it.
    """
    match = _ISO_DATE_RE.search(raw_due or "")
    if not match:
        return ""
    date = match.group(1)
    haystack = f"{item.subject}\n{item.body}"
    return date if date in haystack else ""


class _Session(Protocol):
    def generate(self, messages: list[dict[str, str]], **opts: object) -> str: ...


def _user_prompt(item: InboxItem) -> str:
    header = f"[{item.source}] from {item.sender}"
    if item.kind:
        header += f" ({item.kind})"
    if item.channel:
        header += f" in {item.channel}"
    if item.subject:
        header += f" — {item.subject}"
    body = item.body.strip()
    if len(body) > 1200:
        body = body[:1200] + "…"
    return f"{header}\n\n{body}"


def _parse(raw: str) -> dict:
    if not isinstance(raw, str):
        raise ValueError("model output must be text")
    if len(raw) > MAX_MODEL_OUTPUT_CHARS:
        raise ValueError(
            f"model output exceeds the {MAX_MODEL_OUTPUT_CHARS}-character limit"
        )
    match = _JSON_RE.search(raw)
    if not match:
        raise ValueError(f"no JSON object in model output: {raw!r}")
    data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("triage output must be one JSON object")
    unknown = set(data) - _TRIAGE_KEYS
    if unknown:
        raise ValueError(f"triage output has unknown fields: {', '.join(sorted(unknown))}")
    for name, maximum in (("title", 120), ("why", 500), ("due", 128)):
        value = data.get(name, "")
        if not isinstance(value, str):
            raise ValueError(f"triage {name} must be a string")
        if len(value) > maximum:
            raise ValueError(f"triage {name} exceeds {maximum} characters")
    urgency = data.get("urgency", "fyi")
    if not isinstance(urgency, str):
        raise ValueError("triage urgency must be a string")
    needs_reply = data.get("needs_reply", False)
    if type(needs_reply) is not bool:
        raise ValueError("triage needs_reply must be a boolean")
    people = data.get("people", [])
    if not isinstance(people, list) or len(people) > 20:
        raise ValueError("triage people must be an array of at most 20 strings")
    if any(not isinstance(person, str) or len(person) > 256 for person in people):
        raise ValueError("triage people must contain strings of at most 256 characters")
    confidence = data.get("confidence", 0.7)
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("triage confidence must be a number")
    if not 0 <= float(confidence) <= 1:
        raise ValueError("triage confidence must be between 0 and 1")
    return data


def triage_item(
    session: _Session,
    item: InboxItem,
    *,
    suppression_hints: str = "",
) -> TodoItem:
    system_prompt = triage_system_prompt()
    if suppression_hints:
        system_prompt = f"{system_prompt}\n\n{suppression_hints}"
    messages = ContextEngine().assemble_prompt(
        system_prompt=system_prompt,
        current_user_message=_user_prompt(item),
    )
    raw = session.generate(
        messages,
        max_tokens=220,
        temperature=0.2,
        top_p=0.9,
        sanitize_emoji=False,
    )
    data = _parse(raw)
    urgency = data.get("urgency", "fyi")
    if urgency not in VALID_URGENCY:
        urgency = "fyi"
    # Trust the collector's status. It has full source context (thread history, reactions,
    # completion evidence) while the local 4B model only sees a truncated snippet, so
    # letting the model flip open→waiting caused false blockers in the brief.
    status = item.status
    raw_people = data.get("people") or item.participants
    people = [person.strip() for person in raw_people if person.strip()]
    if item.sender and item.sender not in people:
        people.insert(0, item.sender)
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.7))))
    except (TypeError, ValueError):
        confidence = 0.5
    if status == "waiting":
        confidence = min(confidence, _WAITING_CONFIDENCE_CAP)
    next_action = str(item.metadata.get("next_action") or "").strip().lower()
    title = str(data.get("title", item.subject or "(no subject)"))
    why = str(data.get("why", ""))
    needs_reply = data.get("needs_reply", False) and status != "done"
    due = str(item.metadata.get("deadline_at") or "").strip()
    if not due:
        due = _validated_due(str(data.get("due", "")), item)
    if item.source == "github":
        if next_action == "await_review":
            status = "waiting"
            confidence = min(confidence, _WAITING_CONFIDENCE_CAP)
        elif next_action in _GITHUB_ACTION_PREFIXES:
            title = f"{_GITHUB_ACTION_PREFIXES[next_action]}: {item.subject or title}"
            confidence = max(confidence, 0.75)
        needs_reply = False
        reason = str(item.metadata.get("reason") or "").strip()
        if reason:
            why = reason
    elif next_action == "reply":
        needs_reply = True
    links = [item.url] if item.url else []
    return TodoItem(
        source=item.source,
        id=item.id,
        title=title[:120],
        why=why[:500],
        urgency=urgency,  # type: ignore[arg-type]
        needs_reply=needs_reply,
        due=due,
        links=links,
        sources=[item.source],
        source_ids=[f"{item.source}:{item.id}"],
        people=people,
        confidence=confidence,
        status=status,
        related_keys=list(item.related_keys),
    )


def triage_all(
    session: _Session,
    items: list[InboxItem],
    *,
    suppression_hints: str = "",
) -> list[TodoItem]:
    todos: list[TodoItem] = []
    for item in items:
        try:
            todos.append(triage_item(session, item, suppression_hints=suppression_hints))
        except (ValueError, json.JSONDecodeError) as exc:
            todos.append(
                TodoItem(
                    source=item.source,
                    id=item.id,
                    title=(item.subject or item.body[:80] or "(untitled)")[:120],
                    why=f"triage-parse-error: {exc}",
                    urgency="fyi",
                    needs_reply=False,
                    links=[item.url] if item.url else [],
                    sources=[item.source],
                    source_ids=[f"{item.source}:{item.id}"],
                    people=[item.sender] if item.sender else [],
                    confidence=0.2,
                    status=item.status,
                    related_keys=list(item.related_keys),
                )
            )
    return todos
