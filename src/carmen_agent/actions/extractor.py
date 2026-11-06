from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import datetime

from carmen_agent.actions.schemas import ActionEvidence, ActionItem, ExtractionResult
from carmen_agent.connectors.base import SourceEvent
from carmen_agent.providers import ModelProvider
from carmen_agent.infra.context import ContextEngine

_ASK_RE = re.compile(
    r"\b(?:can|could|would|will) you\b|\bplease\b|\bneed you to\b|\bwhen can you\b|\?\s*$",
    re.IGNORECASE,
)
_COMMIT_RE = re.compile(
    r"\b(?:i(?:'|’)ll|i will|i can|let me|i(?:'|’)m going to|i'll)\b",
    re.IGNORECASE,
)
_TYPE_PATTERNS = (
    ("review", re.compile(r"\b(?:review|take a look|look over|approve|pr)\b", re.I)),
    ("decision", re.compile(r"\b(?:decide|decision|choose|sign off)\b", re.I)),
    ("meeting_prep", re.compile(r"\b(?:prep|agenda|meeting|sync)\b", re.I)),
    ("deliverable", re.compile(r"\b(?:send|ship|deliver|write|prepare|build|fix)\b", re.I)),
    ("reply", re.compile(r"\b(?:reply|respond|get back|answer)\b", re.I)),
)
_EXTRACTION_KEYS = {
    "is_actionable", "action_type", "assigned_to_user", "explicit_ask", "summary",
    "requester_id", "deadline_text", "due_at", "confidence", "rationale",
}
_ACTION_TYPES = {"reply", "review", "decision", "deliverable", "meeting_prep", "follow_up"}
MAX_EXTRACTION_OUTPUT_CHARS = 8_192


def _action_type(text: str) -> str:
    for action_type, pattern in _TYPE_PATTERNS:
        if pattern.search(text):
            return action_type
    return "follow_up"


def heuristic_extract(event: SourceEvent) -> ExtractionResult:
    self_authored = event.actor_id == event.target_user_id
    mentioned = f"<@{event.target_user_id}>" in event.text
    incoming_ask = not self_authored and (event.direct or mentioned) and bool(_ASK_RE.search(event.text))
    commitment = self_authored and bool(_COMMIT_RE.search(event.text))
    actionable = incoming_ask or commitment
    summary = re.sub(r"<@[^>]+>", "", event.text).strip()
    return ExtractionResult(
        is_actionable=actionable,
        action_type=_action_type(event.text),  # type: ignore[arg-type]
        assigned_to_user=actionable,
        explicit_ask=incoming_ask,
        summary=summary[:240] if actionable else "",
        requester_id=event.actor_id if incoming_ask else "",
        confidence=0.82 if incoming_ask else (0.76 if commitment else 0.05),
        rationale="direct ask" if incoming_ask else ("authored commitment" if commitment else "not explicitly assigned"),
    )


def _json_object(raw: str) -> dict[str, object] | None:
    if not isinstance(raw, str) or len(raw) > MAX_EXTRACTION_OUTPUT_CHARS:
        return None
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.S)
        if not match:
            return None
        try:
            value = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(value, dict) or set(value) - _EXTRACTION_KEYS:
        return None
    if any(type(value.get(name, False)) is not bool for name in (
        "is_actionable", "assigned_to_user", "explicit_ask"
    )):
        return None
    if value.get("action_type", "follow_up") not in _ACTION_TYPES:
        return None
    for name, maximum in (
        ("summary", 240), ("requester_id", 512), ("deadline_text", 100),
        ("due_at", 128), ("rationale", 240),
    ):
        field = value.get(name, "")
        if not isinstance(field, str) or len(field) > maximum:
            return None
    confidence = value.get("confidence", 0.0)
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return None
    if not 0 <= float(confidence) <= 1:
        return None
    return value


def _boolean(value: object) -> bool:
    return value is True


def _iso_or_empty(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return text[:40]


def extract_action(provider: ModelProvider, event: SourceEvent) -> ExtractionResult:
    fallback = heuristic_extract(event)
    messages = ContextEngine().assemble_prompt(
        system_prompt=(
                "Classify one work message for a personal follow-through assistant. "
                "An incoming item is assigned only when it directly asks or mentions the target user; "
                "an outgoing item is actionable only when the target explicitly commits. "
                "Return JSON only with: is_actionable, action_type (reply|review|decision|deliverable|meeting_prep|follow_up), "
                "assigned_to_user, explicit_ask, summary, requester_id, deadline_text, due_at (ISO or empty), "
                "confidence (0..1), rationale. Do not invent a deadline."
        ),
        current_user_message=json.dumps(
                {
                    "target_user_id": event.target_user_id,
                    "actor_id": event.actor_id,
                    "direct_message": event.direct,
                    "text": event.text,
                    "context": [asdict(item) for item in event.context[-8:]],
                },
                ensure_ascii=False,
        ),
    )
    try:
        parsed = _json_object(provider.generate(messages, max_tokens=300, temperature=0.0))
    except Exception:
        return fallback
    if parsed is None:
        return fallback
    action_type = str(parsed.get("action_type", fallback.action_type))
    if action_type not in _ACTION_TYPES:
        action_type = fallback.action_type
    try:
        confidence = max(0.0, min(1.0, float(parsed.get("confidence", fallback.confidence))))
    except (TypeError, ValueError):
        confidence = fallback.confidence
    actionable = _boolean(parsed.get("is_actionable")) and _boolean(parsed.get("assigned_to_user"))
    # A model cannot promote an ambient incoming channel message without assignment evidence.
    if event.actor_id != event.target_user_id and not event.direct and f"<@{event.target_user_id}>" not in event.text:
        actionable = False
    if event.actor_id == event.target_user_id and not fallback.is_actionable:
        actionable = False
    return ExtractionResult(
        is_actionable=actionable,
        action_type=action_type,  # type: ignore[arg-type]
        assigned_to_user=_boolean(parsed.get("assigned_to_user")),
        explicit_ask=_boolean(parsed.get("explicit_ask")),
        summary=str(parsed.get("summary") or fallback.summary)[:240],
        requester_id=str(parsed.get("requester_id") or fallback.requester_id),
        deadline_text=str(parsed.get("deadline_text") or "")[:100],
        due_at=_iso_or_empty(parsed.get("due_at")),
        confidence=confidence,
        rationale=str(parsed.get("rationale") or "")[:240],
    )


def action_from_extraction(
    event: SourceEvent, result: ExtractionResult
) -> tuple[ActionItem, ActionEvidence] | None:
    if not result.is_actionable or not result.assigned_to_user or not result.summary:
        return None
    action = ActionItem.create(
        team_id=event.tenant_id,
        user_id=event.target_user_id,
        source=event.source,
        source_id=event.source_id,
        action_type=result.action_type,
        summary=result.summary,
        status="open" if result.confidence >= 0.7 else "candidate",
        confidence=result.confidence,
        requester_id=result.requester_id,
        explicit_ask=result.explicit_ask,
        due_at=result.due_at,
        channel_id=event.channel_id,
        thread_id=event.thread_id,
        permalink=event.permalink,
    )
    evidence = ActionEvidence(
        source=event.source,
        source_id=event.source_id,
        excerpt=event.text[:500],
        permalink=event.permalink,
        actor_id=event.actor_id,
        observed_at=event.occurred_at,
    )
    return action, evidence
