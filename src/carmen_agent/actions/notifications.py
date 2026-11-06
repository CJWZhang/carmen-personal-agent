from __future__ import annotations

from carmen_agent.actions.ranking import rank_actions
from carmen_agent.actions.schemas import ActionEvidence, ActionItem


def render_action(action: ActionItem, evidence: ActionEvidence | None = None) -> str:
    label = action.action_type.replace("_", " ").title()
    lines = [f"*{label}* · `{action.id}`", action.summary]
    if action.due_at:
        lines.append(f"Due: {action.due_at}")
    if evidence is not None and evidence.excerpt:
        lines.append(f"> {evidence.excerpt[:240]}")
    link = action.permalink or (evidence.permalink if evidence else "")
    if link:
        lines.append(f"<{link}|Open source>")
    return "\n".join(lines)


def render_digest(
    actions: list[ActionItem],
    evidence_by_action: dict[str, ActionEvidence] | None = None,
    *,
    limit: int = 8,
) -> str:
    visible = [item for item in rank_actions(actions) if item.priority_score > 0][:limit]
    if not visible:
        return "No open asks or commitments with enough evidence right now."
    evidence_by_action = evidence_by_action or {}
    return "\n\n".join(
        f"{index}. {render_action(action, evidence_by_action.get(action.id))}"
        for index, action in enumerate(visible, 1)
    )
