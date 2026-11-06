from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

from carmen_agent.skills.schemas import (
    MAX_SNAPSHOT_BYTES,
    MAX_TODOS,
    InboxItem,
    SchemaValidationError,
    Source,
    TodoItem,
    todo_item_from_dict,
)

_URGENCY_ORDER = {"now": 0, "today": 1, "this_week": 2, "fyi": 3}
# If a task hasn't been re-observed in the source in this many days, drop it from the
# brief. Keeps stale carryover (merged PRs, completed threads) from accreting forever.
DEFAULT_CARRYOVER_MAX_DAYS = 3
_GITHUB_URL_RE = re.compile(
    r"https?://github\.com/([^/\s]+)/([^/\s]+)/(pull|issues)/(\d+)",
    re.IGNORECASE,
)
_GITHUB_REF_RE = re.compile(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)\b")


@dataclass
class RunProgress:
    new: list[TodoItem]
    updated: list[TodoItem]
    carried: list[TodoItem]
    completed: list[TodoItem]
    aged_out: list[TodoItem] = field(default_factory=list)


def _unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = value.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


def _normalized_url(url: str) -> str:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    if not parts.scheme or not parts.netloc:
        return url.strip().lower()
    # Gmail message ids live in URL fragments and Calendar event ids live in
    # query parameters. Dropping either would merge unrelated work simply
    # because it came from the same product page.
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/"),
            parts.query,
            parts.fragment,
        )
    )


def _correlation_keys(item: InboxItem, todo: TodoItem) -> set[str]:
    keys = {f"explicit:{key.strip().lower()}" for key in item.related_keys if key.strip()}
    keys.update(f"explicit:{key.strip().lower()}" for key in todo.related_keys if key.strip())
    if item.thread_id:
        keys.add(f"thread:{item.source}:{item.thread_id}")
    if item.url:
        keys.add(f"url:{_normalized_url(item.url)}")

    text = "\n".join((item.subject, item.body, item.url))
    for owner, repo, kind, number in _GITHUB_URL_RE.findall(text):
        normalized_kind = "pr" if kind.lower() == "pull" else "issue"
        keys.add(f"github:{owner.lower()}/{repo.lower()}:{normalized_kind}:{number}")
        keys.add(f"github:{owner.lower()}/{repo.lower()}:number:{number}")
    for repo, number in _GITHUB_REF_RE.findall(text):
        keys.add(f"github:{repo.lower()}:number:{number}")

    repository = str(
        item.metadata.get("repository")
        or item.metadata.get("repo")
        or ""
    ).strip()
    # Schema now normalises number to a string; treat empty as absent so we don't emit
    # `github:foo/bar:number:` correlation keys for non-GitHub items with the field present.
    number = str(item.metadata.get("number") or "").strip()
    if repository and number:
        keys.add(f"github:{repository.lower()}:number:{number}")
    return keys


def _stable_group_id(source_ids: list[str], keys: set[str]) -> str:
    seed = min(keys) if keys else min(source_ids)
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    return f"work-{digest}"


def synthesize_todos(
    items: list[InboxItem],
    todos: list[TodoItem],
) -> list[TodoItem]:
    """Deduplicate items that share stable evidence, without fuzzy guessing."""
    item_by_source_id = {f"{item.source}:{item.id}": item for item in items}
    count = len(todos)
    parents = list(range(count))
    keys_by_index: list[set[str]] = []

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    key_owner: dict[str, int] = {}
    for index, todo in enumerate(todos):
        source_id = f"{todo.source}:{todo.id}"
        item = item_by_source_id.get(source_id)
        keys = _correlation_keys(item, todo) if item else set(todo.related_keys)
        keys_by_index.append(keys)
        for key in keys:
            if key in key_owner:
                union(index, key_owner[key])
            else:
                key_owner[key] = index

    groups: dict[int, list[int]] = {}
    for index in range(count):
        groups.setdefault(find(index), []).append(index)

    merged: list[TodoItem] = []
    for indices in groups.values():
        group = [todos[index] for index in indices]
        group.sort(key=lambda todo: (_URGENCY_ORDER[todo.urgency], todo.status == "done"))
        primary = group[0]
        source_ids = _unique(
            source_id
            for todo in group
            for source_id in (todo.source_ids or [f"{todo.source}:{todo.id}"])
        )
        sources = _unique(
            source
            for todo in group
            for source in (todo.sources or [todo.source])
        )
        all_keys = set().union(*(keys_by_index[index] for index in indices))
        status = (
            "done"
            if all(todo.status == "done" for todo in group)
            else "waiting"
            if all(todo.status in {"waiting", "done"} for todo in group)
            else "open"
        )
        draft_paths = _unique(
            path
            for todo in group
            for path in ([todo.draft_path] if todo.draft_path else []) + todo.draft_paths
        )
        why = " · ".join(_unique(todo.why for todo in group if todo.why))[:500]
        completion_evidence = " · ".join(
            _unique(todo.completion_evidence for todo in group if todo.completion_evidence)
        )[:500]
        lifecycle_stage = (
            "done" if status == "done" else
            "waiting" if status == "waiting" else
            primary.lifecycle_stage
            if primary.lifecycle_stage in {"candidate", "in_progress", "verifying"}
            else "open"
        )
        merged.append(
            TodoItem(
                source=primary.source,
                id=_stable_group_id(source_ids, all_keys),
                title=primary.title,
                why=why,
                urgency=min(group, key=lambda todo: _URGENCY_ORDER[todo.urgency]).urgency,
                needs_reply=any(todo.needs_reply for todo in group if todo.status != "done"),
                due=next((todo.due for todo in group if todo.due), ""),
                links=_unique(link for todo in group for link in todo.links),
                draft_path=draft_paths[0] if draft_paths else "",
                sources=sources,  # type: ignore[arg-type]
                source_ids=source_ids,
                people=_unique(person for todo in group for person in todo.people),
                confidence=min(todo.confidence for todo in group),
                status=status,  # type: ignore[arg-type]
                related_keys=sorted(all_keys),
                draft_paths=draft_paths,
                completion_evidence=completion_evidence,
                priority_adjustment=(
                    primary.priority_adjustment if primary.source == "calendar" else 0
                ),
                preference_reason=(
                    primary.preference_reason if primary.source == "calendar" else ""
                ),
                lifecycle_stage=lifecycle_stage,  # type: ignore[arg-type]
                resolution_mode=next(
                    (todo.resolution_mode for todo in group if todo.resolution_mode), ""
                ),
                last_verified_at=max(
                    (todo.last_verified_at for todo in group if todo.last_verified_at),
                    default="",
                ),
                resolution_receipt_id=next(
                    (todo.resolution_receipt_id for todo in group if todo.resolution_receipt_id),
                    "",
                ),
            )
        )
    return sorted(merged, key=lambda todo: (_URGENCY_ORDER[todo.urgency], todo.title.lower()))


def _todo_from_dict(raw: dict[str, Any]) -> TodoItem | None:
    try:
        return todo_item_from_dict(raw)
    except (TypeError, ValueError):
        return None


def _load_previous(path: Path) -> list[TodoItem]:
    if not path.is_file():
        return []
    if path.stat().st_size > MAX_SNAPSHOT_BYTES:
        raise SchemaValidationError(
            f"State file exceeds the {MAX_SNAPSHOT_BYTES}-byte limit."
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SchemaValidationError(f"Could not parse state file {path}: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) - {"version", "last_run", "tasks"}:
        raise SchemaValidationError("State file must be a versioned object with a tasks array.")
    if raw.get("version") != 1 or not isinstance(raw.get("last_run", ""), str):
        raise SchemaValidationError("State file version or last_run is invalid.")
    entries = raw.get("tasks", [])
    if not isinstance(entries, list):
        raise SchemaValidationError("State file tasks must be an array.")
    if len(entries) > MAX_TODOS:
        raise SchemaValidationError(f"State file exceeds the {MAX_TODOS}-task limit.")
    parsed: list[TodoItem] = []
    for index, entry in enumerate(entries):
        todo = _todo_from_dict(entry)
        if todo is None:
            raise SchemaValidationError(f"State task {index + 1} is invalid.")
        parsed.append(todo)
    return parsed


def _same_task(current: TodoItem, previous: TodoItem) -> bool:
    if current.id == previous.id:
        return True
    if set(current.source_ids) & set(previous.source_ids):
        return True
    return bool(set(current.related_keys) & set(previous.related_keys))


def _meaningfully_changed(current: TodoItem, previous: TodoItem) -> bool:
    fields_to_compare = (
        "title",
        "why",
        "urgency",
        "needs_reply",
        "due",
        "remind_at",
        "status",
        "links",
        "source_ids",
        "priority_adjustment",
        "preference_reason",
    )
    return any(getattr(current, name) != getattr(previous, name) for name in fields_to_compare)


def _apply_user_edits(current: TodoItem, previous: TodoItem) -> None:
    """Keep explicit user overrides while source-derived fields continue refreshing."""
    editable = {"title", "why", "urgency", "due", "remind_at"}
    incoming = [name for name in current.edited_fields if name in editable]
    preserved = list(
        dict.fromkeys(
            [name for name in previous.edited_fields if name in editable] + incoming
        )
    )
    for name in preserved:
        if name not in incoming:
            setattr(current, name, getattr(previous, name))
    current.edited_fields = preserved
    if previous.review_state == "accepted":
        current.review_state = "accepted"


def reconcile_state(
    current: list[TodoItem],
    state_path: Path,
    *,
    now: datetime | None = None,
    carryover_max_days: int = DEFAULT_CARRYOVER_MAX_DAYS,
    authoritative_sources: set[Source] | None = None,
    source_last_success: dict[Source, datetime] | None = None,
) -> tuple[list[TodoItem], RunProgress]:
    """Carry unfinished work forward and persist metadata-only progress state.

    Missing records close only when an exhaustive source inventory says they are
    gone. Bounded Slack/Gmail absence is never completion evidence: older open
    loops remain carried until the source proves resolution or the user dismisses
    them. ``carryover_max_days`` is retained for API compatibility and stale-age
    reporting, but it no longer removes work.
    """
    run_time = now or datetime.now(timezone.utc)
    stamp = run_time.isoformat()
    # Retained as keyword arguments for callers and older configuration files.
    # Neither can authorize deleting bounded-source commitments.
    _ = carryover_max_days, source_last_success
    previous = _load_previous(state_path)
    unmatched_previous = set(range(len(previous)))
    new: list[TodoItem] = []
    updated: list[TodoItem] = []
    completed: list[TodoItem] = []
    authoritative = authoritative_sources or set()

    for todo in current:
        match_index = next(
            (
                index
                for index in unmatched_previous
                if _same_task(todo, previous[index])
            ),
            None,
        )
        if match_index is None:
            todo.first_seen = stamp
            todo.last_seen = stamp
            if todo.status != "done":
                new.append(todo)
            continue
        prior = previous[match_index]
        unmatched_previous.remove(match_index)
        _apply_user_edits(todo, prior)
        todo.id = prior.id
        todo.first_seen = prior.first_seen or stamp
        todo.last_seen = stamp
        if todo.status == "done" and prior.status != "done":
            todo.lifecycle_stage = "done"
            completed.append(todo)
        elif todo.status != "done" and _meaningfully_changed(todo, prior):
            updated.append(todo)

    carried: list[TodoItem] = []
    aged_out: list[TodoItem] = []
    resolved_missing: list[TodoItem] = []
    for index in sorted(unmatched_previous):
        todo = previous[index]
        if todo.status == "done":
            continue
        task_sources = set(todo.sources or [todo.source])
        if task_sources & authoritative:
            todo.status = "done"
            todo.lifecycle_stage = "done"
            todo.resolution_mode = "source_verified"
            todo.last_verified_at = stamp
            todo.last_seen = stamp
            names = ", ".join(sorted(task_sources & authoritative))
            todo.completion_evidence = (
                f"No longer present in the fresh authoritative {names} snapshot."
            )
            completed.append(todo)
            resolved_missing.append(todo)
            continue
        # Locally entered tasks are commitments, not cached connector observations.
        # Keep them until the user explicitly completes them.
        if todo.source == "manual" or "manual" in task_sources:
            todo.carryover = True
            carried.append(todo)
            continue
        # Preserve the commitment. The collector receives its stable source ref
        # on subsequent runs and must return affirmative resolution evidence
        # before reconciliation can close it.
        todo.carryover = True
        if todo.status == "waiting":
            todo.lifecycle_stage = "waiting"
        carried.append(todo)

    active = [todo for todo in current if todo.status != "done"] + carried
    active.sort(key=lambda todo: (_URGENCY_ORDER[todo.urgency], todo.title.lower()))
    persisted = active + [todo for todo in current if todo.status == "done"] + resolved_missing
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "last_run": stamp,
                "tasks": [asdict(todo) for todo in persisted],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return active, RunProgress(
        new=new,
        updated=updated,
        carried=carried,
        completed=completed,
        aged_out=aged_out,
    )
