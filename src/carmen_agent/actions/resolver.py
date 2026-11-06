from __future__ import annotations

import json
import re
from dataclasses import dataclass

from carmen_agent.actions.schemas import ActionItem, ActionStatus
from carmen_agent.connectors.base import SourceMessage
from carmen_agent.providers import ModelProvider
from carmen_agent.infra.context import ContextEngine

_DONE_RE = re.compile(r"\b(?:done|shipped|sent|resolved|fixed|merged|approved|completed)\b", re.I)
_WAIT_RE = re.compile(r"\b(?:waiting on|blocked by|pending|once .* replies)\b", re.I)
_RESOLUTION_KEYS = {"status", "confidence", "reason", "evidence_id"}


@dataclass(frozen=True)
class Resolution:
    status: ActionStatus
    reason: str
    evidence_id: str = ""


def deterministic_resolution(
    action: ActionItem, messages: list[SourceMessage]
) -> Resolution | None:
    later = [item for item in messages if item.id > action.source_id]
    for message in reversed(later):
        if message.actor_id != action.user_id:
            continue
        if action.action_type == "reply":
            return Resolution("responded", "User replied in the source thread.", message.id)
        if _DONE_RE.search(message.text):
            return Resolution("completed", "User reported completion in the source thread.", message.id)
        if _WAIT_RE.search(message.text):
            return Resolution("waiting", "User reported that the work is waiting or blocked.", message.id)
    return None


def semantic_resolution(
    provider: ModelProvider,
    action: ActionItem,
    messages: list[SourceMessage],
) -> Resolution | None:
    """Conservative fallback for completion language not covered by fixed rules."""
    later = [item for item in messages if item.id > action.source_id]
    if not later:
        return None
    prompt = {
        "action": {"type": action.action_type, "summary": action.summary},
        "target_user_id": action.user_id,
        "later_messages": [
            {"id": item.id, "actor_id": item.actor_id, "text": item.text[:500]}
            for item in later[-20:]
        ],
    }
    try:
        messages = ContextEngine().assemble_prompt(
            system_prompt=(
                        "Determine whether the target user's action is resolved. Return JSON only: "
                        '{"status":"none|responded|completed|waiting","confidence":0.0,'
                        '"reason":"...","evidence_id":"observed message id"}. '
                        "Use none when ambiguous. Never infer completion from another person's message."
            ),
            current_user_message=json.dumps(prompt, ensure_ascii=False),
        )
        raw = provider.generate(
            messages,
            max_tokens=180,
            temperature=0.0,
        )
        if not isinstance(raw, str) or len(raw) > 4_096:
            return None
        cleaned = raw.strip().removeprefix("```json").removesuffix("```").strip()
        parsed = json.loads(cleaned)
    except Exception:
        return None
    if not isinstance(parsed, dict) or set(parsed) != _RESOLUTION_KEYS:
        return None
    if not isinstance(parsed["status"], str):
        return None
    status = parsed["status"]
    try:
        raw_confidence = parsed["confidence"]
        if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, (int, float)):
            return None
        confidence = float(raw_confidence)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed["evidence_id"], str) or len(parsed["evidence_id"]) > 512:
        return None
    if not isinstance(parsed["reason"], str) or len(parsed["reason"]) > 500:
        return None
    evidence_id = parsed["evidence_id"]
    evidence = {item.id: item for item in later}.get(evidence_id)
    if status not in {"responded", "completed", "waiting"} or confidence < 0.9:
        return None
    if evidence is None or evidence.actor_id != action.user_id:
        return None
    return Resolution(
        status=status,  # type: ignore[arg-type]
        reason=parsed["reason"] or "Semantic resolution evidence.",
        evidence_id=evidence_id,
    )
