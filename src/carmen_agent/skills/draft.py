from __future__ import annotations

from pathlib import Path
from typing import Protocol

from carmen_agent.skills.schemas import Draft, InboxItem, Source, TodoItem
from carmen_agent.infra.context import ContextEngine
from carmen_agent.configuration import config_value
from carmen_agent.response_quality import response_is_usable

# The style adapter was trained on the user's Slack messages. Its drafts for other
# channels (email greetings/sign-offs, calendar RSVPs) come out as gibberish or
# ambiguous single-word fragments that pass simple text checks. Only draft for
# channels where the adapter has genuine coverage; humans handle the rest.
DRAFTABLE_SOURCES: frozenset[Source] = frozenset({"slack", "github"})


class _Session(Protocol):
    system_prompt: str
    def generate(self, messages: list[dict[str, str]], **opts: object) -> str: ...


def _is_usable_draft(text: str, reference_text: str = "") -> bool:
    return response_is_usable(text, reference_text, max_chars=2_000)


def _safe_fallback(item: InboxItem) -> str:
    if item.source == "gmail":
        first_name = item.sender.strip().split()[0] if item.sender.strip() else "there"
        name = config_value("CARMEN_USER_NAME", "user_name", "")
        signoff = f"\n{name}" if name else ""
        return (
            f"Hi {first_name},\n\n"
            "Thanks for the update! Let me confirm and get back to you.\n\n"
            f"Best,{signoff}"
        )
    if item.source == "calendar":
        return "thanks for the invite! let me double check and get back to you"
    if item.source == "github":
        return "thanks for flagging — let me take a look and follow up"
    return "got it! let me check and get back to you"


def _channel_hint(item: InboxItem) -> str:
    if item.source == "gmail":
        return "Reply is an email. Include a short greeting and sign-off with just the first name."
    if item.source == "slack":
        return "Reply is a Slack message. No greeting or sign-off. Keep it under three sentences unless the ask requires more."
    if item.source == "github":
        return "Reply is a GitHub comment. Be concrete and concise; mention code or checks only when present in the context."
    return "Reply is a calendar response. One or two sentences."


def draft_reply(session: _Session, item: InboxItem, todo: TodoItem) -> Draft:
    context = (
        f"Original from {item.sender}"
        + (f" ({item.channel})" if item.channel else "")
        + f":\n{item.body.strip()}\n\n"
        f"What they need: {todo.why or todo.title}\n"
        f"{_channel_hint(item)}\n"
        "Do not invent facts, decisions, dates, work completed, or commitments. "
        "If essential context is missing, ask one concise clarifying question. "
        "Draft the reply now. Return only the reply body."
    )
    messages = ContextEngine().assemble_prompt(
        system_prompt=session.system_prompt,
        current_user_message=context,
    )
    body = ""
    for attempt in range(2):
        attempt_messages = list(messages)
        if attempt:
            attempt_messages.append(
                {
                    "role": "user",
                    "content": (
                        "The prior output was unusable. Return one plain-text reply only. "
                        "Do not include analysis, thinking, tags, tool calls, or metadata."
                    ),
                }
            )
        body = session.generate(
            attempt_messages,
            max_tokens=240,
            temperature=0.65 if attempt == 0 else 0.3,
            top_p=0.9,
            sanitize_emoji=True,
        )
        if _is_usable_draft(body, context):
            break
    if not _is_usable_draft(body, context):
        body = _safe_fallback(item)
    return Draft(todo_id=todo.id, body=body)


def write_draft(draft: Draft, drafts_dir: Path) -> Path:
    drafts_dir.mkdir(parents=True, exist_ok=True)
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in draft.todo_id)
    path = drafts_dir / f"{safe_id}.md"
    path.write_text(draft.body + "\n", encoding="utf-8")
    return path


def draft_all(
    session: _Session,
    items_by_id: dict[str, InboxItem],
    todos: list[TodoItem],
    drafts_dir: Path,
) -> None:
    for todo in todos:
        if not todo.needs_reply:
            continue
        item = items_by_id.get(f"{todo.source}:{todo.id}") or items_by_id.get(todo.id)
        if item is None or item.source not in DRAFTABLE_SOURCES:
            continue
        draft = draft_reply(session, item, todo)
        source_scoped = Draft(todo_id=f"{todo.source}-{draft.todo_id}", body=draft.body)
        path = write_draft(source_scoped, drafts_dir)
        todo.draft_path = str(path)
