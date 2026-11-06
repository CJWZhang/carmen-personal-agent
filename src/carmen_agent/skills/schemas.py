from __future__ import annotations

import json
import tempfile
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, TypeVar
from urllib.parse import urlsplit

Source = Literal["gmail", "slack", "calendar", "github", "manual"]
Urgency = Literal["now", "today", "this_week", "fyi"]
WorkStatus = Literal["open", "waiting", "done"]
ReviewState = Literal["", "accepted"]
LifecycleStage = Literal[
    "candidate", "open", "in_progress", "waiting", "verifying", "done",
    "dismissed", "superseded",
]
ResolutionMode = Literal[
    "", "local", "source_verified", "dismissed", "expired", "superseded",
]

CONNECTED_SOURCES: frozenset[str] = frozenset({"gmail", "slack", "calendar", "github"})
ALL_SOURCES: frozenset[str] = CONNECTED_SOURCES | {"manual"}
VALID_URGENCIES: frozenset[str] = frozenset({"now", "today", "this_week", "fyi"})
VALID_STATUSES: frozenset[str] = frozenset({"open", "waiting", "done"})
VALID_REVIEW_STATES: frozenset[str] = frozenset({"", "accepted"})
VALID_LIFECYCLE_STAGES: frozenset[str] = frozenset({
    "candidate", "open", "in_progress", "waiting", "verifying", "done",
    "dismissed", "superseded",
})
VALID_RESOLUTION_MODES: frozenset[str] = frozenset({
    "", "local", "source_verified", "dismissed", "expired", "superseded",
})
MAX_SNAPSHOT_BYTES = 5 * 1024 * 1024
MAX_ITEMS_PER_SOURCE = 500
MAX_TODOS = 1_000
MAX_METADATA_BYTES = 32 * 1024
_METADATA_STRING_FIELDS = {
    "repository", "repo", "number", "reason", "state", "note",
    "response_status", "organizer", "kind", "deadline_at", "expires_at",
    "latest_activity_at", "next_action", "review_state", "ci_state",
    "mergeable_state", "resolution_evidence", "conversation_id",
    "series_id",
}
_METADATA_BOOLEAN_FIELDS = {
    "cancelled", "review_requested", "assigned", "is_draft",
    "has_unresolved_threads", "ci_passing", "mergeable",
    "user_replied_after_ask", "needs_decision",
    "optional",
}
_METADATA_INTEGER_FIELDS = {
    "historical_instances", "historical_accepted", "historical_declined",
    "historical_tentative", "historical_no_response", "historical_attended",
    "historical_missed",
}


class SchemaValidationError(ValueError):
    """A local or connector record did not match the bounded data contract."""


def _string(value: Any, field_name: str, *, maximum: int, required: bool = False) -> str:
    if not isinstance(value, str):
        raise SchemaValidationError(f"{field_name} must be a string.")
    if required and not value.strip():
        raise SchemaValidationError(f"{field_name} cannot be empty.")
    if len(value) > maximum:
        raise SchemaValidationError(
            f"{field_name} exceeds the {maximum}-character limit."
        )
    return value


def _string_list(
    value: Any,
    field_name: str,
    *,
    maximum_items: int,
    maximum_length: int,
) -> list[str]:
    if not isinstance(value, list):
        raise SchemaValidationError(f"{field_name} must be an array.")
    if len(value) > maximum_items:
        raise SchemaValidationError(
            f"{field_name} exceeds the {maximum_items}-item limit."
        )
    return [
        _string(item, f"{field_name}[{index}]", maximum=maximum_length)
        for index, item in enumerate(value)
    ]


def _bounded_metadata(value: Any, field_name: str = "metadata") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SchemaValidationError(f"{field_name} must be an object.")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise SchemaValidationError(f"{field_name} must contain JSON-safe values.") from exc
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise SchemaValidationError(
            f"{field_name} exceeds the {MAX_METADATA_BYTES}-byte limit."
        )

    unknown = (
        set(value)
        - _METADATA_STRING_FIELDS
        - _METADATA_BOOLEAN_FIELDS
        - _METADATA_INTEGER_FIELDS
    )
    if unknown:
        raise SchemaValidationError(
            f"{field_name} has unknown fields: {', '.join(sorted(unknown))}."
        )
    for key in set(value) & _METADATA_STRING_FIELDS:
        _string(value[key], f"{field_name}.{key}", maximum=4_096)
    for key in set(value) & _METADATA_BOOLEAN_FIELDS:
        if type(value[key]) is not bool:
            raise SchemaValidationError(f"{field_name}.{key} must be a boolean.")
    for key in set(value) & _METADATA_INTEGER_FIELDS:
        if type(value[key]) is not int or not 0 <= value[key] <= 10_000:
            raise SchemaValidationError(
                f"{field_name}.{key} must be an integer between 0 and 10000."
            )
    return value


def _safe_url(value: Any, field_name: str = "url") -> str:
    url = _string(value, field_name, maximum=4_096)
    if url and urlsplit(url).scheme.lower() not in {"http", "https"}:
        raise SchemaValidationError(f"{field_name} must use http or https.")
    return url


@dataclass
class InboxItem:
    source: Source
    id: str
    sender: str
    subject: str
    body: str
    url: str = ""
    ts: str = ""
    channel: str = ""
    thread_id: str = ""
    kind: str = ""
    status: WorkStatus = "open"
    participants: list[str] = field(default_factory=list)
    related_keys: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TodoItem:
    source: Source
    id: str
    title: str
    why: str
    urgency: Urgency
    needs_reply: bool
    due: str = ""
    remind_at: str = ""
    links: list[str] = field(default_factory=list)
    draft_path: str = ""
    sources: list[Source] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)
    people: list[str] = field(default_factory=list)
    confidence: float = 0.5
    status: WorkStatus = "open"
    related_keys: list[str] = field(default_factory=list)
    draft_paths: list[str] = field(default_factory=list)
    first_seen: str = ""
    last_seen: str = ""
    carryover: bool = False
    completion_evidence: str = ""
    edited_fields: list[str] = field(default_factory=list)
    review_state: ReviewState = ""
    priority_adjustment: int = 0
    preference_reason: str = ""
    lifecycle_stage: LifecycleStage = "open"
    resolution_mode: ResolutionMode = ""
    last_verified_at: str = ""
    resolution_receipt_id: str = ""


@dataclass
class Draft:
    todo_id: str
    body: str


def load_inbox(
    inbox_dir: Path, *, sources: set[Source] | None = None
) -> list[InboxItem]:
    items: list[InboxItem] = []
    selected_sources = CONNECTED_SOURCES if sources is None else sources
    for source in sorted(selected_sources):
        if source not in CONNECTED_SOURCES:
            continue
        path = inbox_dir / f"{source}.json"
        if not path.is_file():
            continue
        if path.stat().st_size > MAX_SNAPSHOT_BYTES:
            raise SchemaValidationError(
                f"{path.name} exceeds the {MAX_SNAPSHOT_BYTES}-byte snapshot limit."
            )
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise SchemaValidationError(f"{path.name} must contain a JSON array.")
        if len(raw) > MAX_ITEMS_PER_SOURCE:
            raise SchemaValidationError(
                f"{path.name} exceeds the {MAX_ITEMS_PER_SOURCE}-item limit."
            )
        for index, entry in enumerate(raw):
            item = inbox_item_from_dict(entry, path=f"{path.name}[{index}]")
            if item.source != source:
                raise SchemaValidationError(
                    f"{path.name}[{index}].source must match the snapshot filename."
                )
            items.append(item)
    return items


def dump_todos(todos: Iterable[TodoItem], path: Path) -> None:
    validated = [validate_todo_item(todo, path=f"tasks[{index}]") for index, todo in enumerate(todos)]
    if len(validated) > MAX_TODOS:
        raise SchemaValidationError(f"Task list exceeds the {MAX_TODOS}-item limit.")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        [asdict(t) for t in validated], indent=2, ensure_ascii=False
    )
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(payload)
            temporary = Path(handle.name)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def inbox_item_from_dict(raw: Any, *, path: str = "item") -> InboxItem:
    if not isinstance(raw, dict):
        raise SchemaValidationError(f"{path} must be an object.")
    allowed = {field.name for field in fields(InboxItem)}
    unknown = set(raw) - allowed
    if unknown:
        raise SchemaValidationError(f"{path} has unknown fields: {', '.join(sorted(unknown))}.")
    source = raw.get("source")
    if source not in CONNECTED_SOURCES:
        raise SchemaValidationError(f"{path}.source is invalid.")
    status = raw.get("status", "open")
    if status not in VALID_STATUSES:
        raise SchemaValidationError(f"{path}.status is invalid.")
    return InboxItem(
        source=source,
        id=_string(raw.get("id"), f"{path}.id", maximum=512, required=True),
        sender=_string(raw.get("sender", ""), f"{path}.sender", maximum=512),
        subject=_string(raw.get("subject", ""), f"{path}.subject", maximum=2_000),
        body=_string(raw.get("body", ""), f"{path}.body", maximum=20_000),
        url=_safe_url(raw.get("url", ""), f"{path}.url"),
        ts=_string(raw.get("ts", ""), f"{path}.ts", maximum=128),
        channel=_string(raw.get("channel", ""), f"{path}.channel", maximum=512),
        thread_id=_string(raw.get("thread_id", ""), f"{path}.thread_id", maximum=512),
        kind=_string(raw.get("kind", ""), f"{path}.kind", maximum=128),
        status=status,
        participants=_string_list(
            raw.get("participants", []), f"{path}.participants",
            maximum_items=100, maximum_length=256,
        ),
        related_keys=_string_list(
            raw.get("related_keys", []), f"{path}.related_keys",
            maximum_items=100, maximum_length=512,
        ),
        metadata=_bounded_metadata(raw.get("metadata", {}), f"{path}.metadata"),
    )


def todo_item_from_dict(raw: Any, *, path: str = "task") -> TodoItem:
    if not isinstance(raw, dict):
        raise SchemaValidationError(f"{path} must be an object.")
    allowed = {field.name for field in fields(TodoItem)}
    unknown = set(raw) - allowed
    if unknown:
        raise SchemaValidationError(
            f"{path} has unknown fields: {', '.join(sorted(unknown))}."
        )
    try:
        todo = from_dict(TodoItem, raw)
    except (TypeError, ValueError) as exc:
        raise SchemaValidationError(f"{path} is missing required fields: {exc}") from exc
    if "lifecycle_stage" not in raw:
        todo.lifecycle_stage = (
            "done" if todo.status == "done" else
            "waiting" if todo.status == "waiting" else
            "candidate" if todo.confidence < 0.5 and todo.review_state != "accepted" else
            "open"
        )
    return validate_todo_item(todo, path=path)


def validate_todo_item(todo: TodoItem, *, path: str = "task") -> TodoItem:
    if todo.source not in ALL_SOURCES:
        raise SchemaValidationError(f"{path}.source is invalid.")
    if todo.urgency not in VALID_URGENCIES:
        raise SchemaValidationError(f"{path}.urgency is invalid.")
    if todo.status not in VALID_STATUSES:
        raise SchemaValidationError(f"{path}.status is invalid.")
    if todo.review_state not in VALID_REVIEW_STATES:
        raise SchemaValidationError(f"{path}.review_state is invalid.")
    if todo.lifecycle_stage not in VALID_LIFECYCLE_STAGES:
        raise SchemaValidationError(f"{path}.lifecycle_stage is invalid.")
    if todo.resolution_mode not in VALID_RESOLUTION_MODES:
        raise SchemaValidationError(f"{path}.resolution_mode is invalid.")
    if type(todo.needs_reply) is not bool or type(todo.carryover) is not bool:
        raise SchemaValidationError(f"{path} boolean fields must be booleans.")
    if type(todo.priority_adjustment) is not int or not -100 <= todo.priority_adjustment <= 100:
        raise SchemaValidationError(
            f"{path}.priority_adjustment must be an integer between -100 and 100."
        )
    if isinstance(todo.confidence, bool) or not isinstance(todo.confidence, (int, float)):
        raise SchemaValidationError(f"{path}.confidence must be a number.")
    if not 0 <= float(todo.confidence) <= 1:
        raise SchemaValidationError(f"{path}.confidence must be between 0 and 1.")
    _string(todo.id, f"{path}.id", maximum=512, required=True)
    _string(todo.title, f"{path}.title", maximum=120, required=True)
    _string(todo.why, f"{path}.why", maximum=500)
    for name in (
        "due", "remind_at", "draft_path", "first_seen", "last_seen",
        "last_verified_at", "resolution_receipt_id",
        "preference_reason",
    ):
        _string(getattr(todo, name), f"{path}.{name}", maximum=4_096)
    _string(todo.completion_evidence, f"{path}.completion_evidence", maximum=1_000)
    _string_list(todo.links, f"{path}.links", maximum_items=50, maximum_length=4_096)
    for index, link in enumerate(todo.links):
        _safe_url(link, f"{path}.links[{index}]")
    _string_list(todo.sources, f"{path}.sources", maximum_items=5, maximum_length=32)
    if any(source not in ALL_SOURCES for source in todo.sources):
        raise SchemaValidationError(f"{path}.sources contains an invalid source.")
    _string_list(todo.source_ids, f"{path}.source_ids", maximum_items=100, maximum_length=1_024)
    _string_list(todo.people, f"{path}.people", maximum_items=100, maximum_length=256)
    _string_list(todo.related_keys, f"{path}.related_keys", maximum_items=100, maximum_length=512)
    _string_list(todo.draft_paths, f"{path}.draft_paths", maximum_items=20, maximum_length=4_096)
    _string_list(todo.edited_fields, f"{path}.edited_fields", maximum_items=5, maximum_length=32)
    editable = {"title", "why", "urgency", "due", "remind_at"}
    if any(field_name not in editable for field_name in todo.edited_fields):
        raise SchemaValidationError(f"{path}.edited_fields contains an invalid field.")
    return todo


# ---------------------------------------------------------------------------
# Shared serialization helpers.
#
# These used to live scattered across synthesis.py, habits.py, and commands.py
# — same logic, three copies. Consolidated here so the storage/parse contract
# is defined next to the dataclasses it operates on.
# ---------------------------------------------------------------------------

_T = TypeVar("_T")


def from_dict(cls: type[_T], raw: dict[str, Any]) -> _T:
    """Instantiate a dataclass from a dict, silently dropping unknown keys.

    Lets stored JSON survive schema evolution without brittle exact-match errors.
    Raises TypeError/ValueError if required fields are missing or types mismatch.
    """
    allowed = {f.name for f in fields(cls)}
    return cls(**{key: value for key, value in raw.items() if key in allowed})


def parse_stamp(raw: str) -> datetime | None:
    """Parse an ISO-8601 timestamp, defaulting to UTC when naive.

    Returns None on empty or malformed input rather than raising — callers
    typically want to fall back to a default (e.g., "unknown last-seen") instead
    of blowing up on a stray blank string in state.
    """
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
