from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from carmen_agent.skills.schemas import Source, TodoItem, Urgency
from carmen_agent.skills.synthesis import RunProgress

_URGENCY_ORDER: dict[Urgency, int] = {"now": 0, "today": 1, "this_week": 2, "fyi": 3}
_URGENCY_LABEL: dict[Urgency, str] = {
    "now": "Now",
    "today": "Today",
    "this_week": "This week",
    "fyi": "FYI",
}


def render_todo_md(todos: list[TodoItem]) -> str:
    if not todos:
        return "# TODO\n\n_No open items._\n"
    buckets: dict[Urgency, list[TodoItem]] = {u: [] for u in _URGENCY_ORDER}
    for todo in todos:
        buckets[todo.urgency].append(todo)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# TODO", f"_Generated {stamp}_", ""]
    for urgency in _URGENCY_ORDER:
        bucket = buckets[urgency]
        if not bucket:
            continue
        lines.append(f"## {_URGENCY_LABEL[urgency]}")
        lines.append("")
        for todo in bucket:
            reply_mark = " · reply" if todo.needs_reply else ""
            due_mark = f" · due {todo.due}" if todo.due else ""
            lines.append(f"- **[{todo.source}]** {todo.title}{due_mark}{reply_mark}")
            if todo.why:
                lines.append(f"  - {todo.why}")
            for link in todo.links:
                lines.append(f"  - <{link}>")
            if todo.draft_path:
                lines.append(f"  - draft: `{todo.draft_path}`")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_todo_md(todos: list[TodoItem], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_todo_md(todos), encoding="utf-8")


def _source_label(todo: TodoItem) -> str:
    sources = todo.sources or [todo.source]
    return " + ".join(source.title() for source in sources)


def _task_lines(todo: TodoItem, *, include_draft: bool = False) -> list[str]:
    due = f" · due {todo.due}" if todo.due else ""
    reminder = f" · remind {todo.remind_at}" if todo.remind_at else ""
    carryover = " · carryover" if todo.carryover else ""
    confidence = f" · {round(todo.confidence * 100)}% confidence"
    lines = [
        f"- **[{_source_label(todo)}] {todo.title}**{due}{reminder}{carryover}{confidence}"
    ]
    if todo.why:
        lines.append(f"  - Why: {todo.why}")
    if todo.people:
        lines.append(f"  - People: {', '.join(todo.people)}")
    if todo.links:
        lines.append("  - Context: " + " · ".join(f"<{link}>" for link in todo.links))
    draft_paths = todo.draft_paths or ([todo.draft_path] if todo.draft_path else [])
    for draft_path in draft_paths:
        lines.append(f"  - Draft: `{draft_path}`")
        if include_draft:
            try:
                body = Path(draft_path).read_text(encoding="utf-8").strip()
            except OSError:
                body = ""
            if body:
                lines.extend(f"    > {line}" for line in body.splitlines())
    return lines


def render_morning_brief(
    todos: list[TodoItem],
    progress: RunProgress,
    *,
    generated_at: datetime | None = None,
    coverage: set[Source] | None = None,
) -> str:
    now = generated_at or datetime.now().astimezone()
    date_label = now.strftime("%Y-%m-%d")
    calendar_count = sum("calendar" in (todo.sources or [todo.source]) for todo in todos)
    urgent_count = sum(todo.urgency in {"now", "today"} for todo in todos)
    reply_count = sum(todo.needs_reply for todo in todos)
    active = [todo for todo in todos if todo.urgency != "fyi"]
    top = active[:5]
    top_ids = {todo.id for todo in top}

    lines = [
        f"# Morning Work Brief — {date_label}",
        "",
        "## Today at a glance",
        "",
        (
            f"{urgent_count} item(s) need attention today, {reply_count} need a written "
            f"response, and {calendar_count} calendar item(s) require preparation."
        ),
    ]
    if top:
        lines.append(f"Start with **{top[0].title}**.")
    else:
        lines.append("No actionable work was found in the current source snapshot.")

    lines.extend(["", "## Top priorities", ""])
    if not top:
        lines.append("_No active priorities._")
    for todo in top:
        lines.extend(_task_lines(todo))

    replies = [todo for todo in todos if todo.needs_reply][:5]
    lines.extend(["", "## Reply queue", ""])
    if not replies:
        lines.append("_No replies identified._")
    for todo in replies:
        lines.extend(_task_lines(todo, include_draft=True))

    github = [
        todo
        for todo in todos
        if "github" in (todo.sources or [todo.source]) and todo.id not in top_ids
    ][:5]
    lines.extend(["", "## GitHub and review queue", ""])
    if not github:
        lines.append("_No additional GitHub work._")
    for todo in github:
        lines.extend(_task_lines(todo))

    waiting = [todo for todo in todos if todo.status == "waiting"][:5]
    lines.extend(["", "## Waiting and blockers", ""])
    if not waiting:
        lines.append("_Nothing is explicitly waiting on somebody else._")
    for todo in waiting:
        lines.extend(_task_lines(todo))

    lines.extend(["", "## Carryover and progress", ""])
    progress_lines = [
        (progress.completed, "Completed"),
        (progress.new, "New"),
        (progress.updated, "Changed"),
        (progress.carried, "Carried forward"),
        (progress.aged_out, "Aged out (not seen in source)"),
    ]
    if not any(bucket for bucket, _ in progress_lines):
        lines.append("_No changes from the prior run._")
    for bucket, label in progress_lines:
        if bucket:
            titles = "; ".join(todo.title for todo in bucket[:5])
            lines.append(f"- **{label}:** {titles}")

    fyi = [todo for todo in todos if todo.urgency == "fyi"][:5]
    lines.extend(["", "## FYI", ""])
    if coverage is not None:
        expected: set[Source] = {"gmail", "slack", "calendar", "github"}
        present = ", ".join(sorted(source.title() for source in coverage)) or "none"
        missing = expected - coverage
        coverage_line = f"- **Source coverage:** {present}"
        if missing:
            coverage_line += "; missing " + ", ".join(
                sorted(source.title() for source in missing)
            )
        lines.append(coverage_line)
    if not fyi:
        lines.append("_No important FYIs._")
    for todo in fyi:
        lines.extend(_task_lines(todo))
    return "\n".join(lines).rstrip() + "\n"


def write_morning_brief(
    todos: list[TodoItem],
    progress: RunProgress,
    path: Path,
    *,
    coverage: set[Source] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_morning_brief(todos, progress, coverage=coverage),
        encoding="utf-8",
    )
