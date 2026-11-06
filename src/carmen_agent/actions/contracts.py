"""Typed, user-approved connector actions exposed by the local task UI."""
from __future__ import annotations

from dataclasses import asdict, dataclass

from carmen_agent.skills.schemas import TodoItem


@dataclass(frozen=True)
class ConnectorAction:
    id: str
    source: str
    label: str
    description: str
    requires_draft: bool = False
    may_resolve_task: bool = False

    def public_dict(self) -> dict[str, object]:
        return asdict(self)


_ACTIONS = {
    "slack.send_reply": ConnectorAction(
        "slack.send_reply", "slack", "Send Slack reply",
        "Send the reviewed response in the original Slack thread.", True, True,
    ),
    "gmail.send_reply": ConnectorAction(
        "gmail.send_reply", "gmail", "Send email reply",
        "Send the reviewed response in the original Gmail thread.", True, True,
    ),
    "calendar.accept": ConnectorAction(
        "calendar.accept", "calendar", "Accept invitation",
        "Accept the source calendar invitation.", False, True,
    ),
    "calendar.tentative": ConnectorAction(
        "calendar.tentative", "calendar", "Mark tentative",
        "Set the source calendar invitation to tentative.", False, True,
    ),
    "calendar.decline": ConnectorAction(
        "calendar.decline", "calendar", "Decline invitation",
        "Decline the source calendar invitation.", False, True,
    ),
    "github.comment": ConnectorAction(
        "github.comment", "github", "Post GitHub comment",
        "Post the reviewed draft on the linked pull request or issue.", True, False,
    ),
    "github.submit_review": ConnectorAction(
        "github.submit_review", "github", "Submit GitHub review",
        "Submit the reviewed text as a pull-request review.", True, True,
    ),
    "github.merge": ConnectorAction(
        "github.merge", "github", "Merge pull request",
        "Merge the linked pull request after source-side checks pass.", False, True,
    ),
}


def connector_actions_for(task: TodoItem, *, has_draft: bool) -> list[ConnectorAction]:
    actions: list[ConnectorAction] = []
    if task.source == "slack" and task.needs_reply:
        actions.append(_ACTIONS["slack.send_reply"])
    elif task.source == "gmail" and task.needs_reply:
        actions.append(_ACTIONS["gmail.send_reply"])
    elif task.source == "calendar" and task.needs_reply:
        actions.extend(
            _ACTIONS[action]
            for action in ("calendar.accept", "calendar.tentative", "calendar.decline")
        )
    elif task.source == "github":
        lowered = f"{task.title} {task.why}".lower()
        actions.append(_ACTIONS["github.comment"])
        if "review" in lowered:
            actions.append(_ACTIONS["github.submit_review"])
        if "merge" in lowered and "conflict" not in lowered:
            actions.append(_ACTIONS["github.merge"])
    return [action for action in actions if has_draft or not action.requires_draft]


def require_connector_action(
    task: TodoItem, action_id: str, *, has_draft: bool
) -> ConnectorAction:
    allowed = {action.id: action for action in connector_actions_for(task, has_draft=has_draft)}
    try:
        return allowed[action_id]
    except KeyError as exc:
        raise ValueError("This connector action is not available for the selected task.") from exc
