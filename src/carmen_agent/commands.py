from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from carmen_agent.paths import (
    ACTION_RECEIPTS_PATH as DEFAULT_ACTION_RECEIPTS,
    BRIEF_PATH as DEFAULT_BRIEF,
    HABITS_PATH as DEFAULT_HABITS_PATH,
    PROJECT_ROOT,
    STATE_PATH as DEFAULT_STATE,
    TODOS_PATH as DEFAULT_TODOS,
)
from carmen_agent.actions.contracts import require_connector_action
from carmen_agent.actions.receipts import (
    ActionReceipt,
    append_receipt,
    validate_receipt_store,
)
from carmen_agent.infra.local_tools import ExecuteLocalActionAdapter
from carmen_agent.infra.runtime import RunContext, ToolRuntime, UserActor
from carmen_agent.infra.tools import ToolExecutor, stable_idempotency_key
from carmen_agent.skills.habits import record_dismissal
from carmen_agent.skills.schemas import (
    MAX_SNAPSHOT_BYTES,
    MAX_TODOS,
    TodoItem,
    dump_todos,
    todo_item_from_dict,
)
from carmen_agent.skills.synthesis import RunProgress, reconcile_state
from carmen_agent.skills.todo import write_morning_brief


class CommandError(RuntimeError):
    pass


MAX_ACTION_RESPONSE_CHARS = 16_384
_ACTION_RESPONSE_KEYS = {"status", "message", "external_id"}
_ACTION_STATUSES = {"failed", "submitted", "pending", "completed", "verified"}


def _todo_from_dict(raw: Any, *, path: str = "task") -> TodoItem:
    try:
        return todo_item_from_dict(raw, path=path)
    except (TypeError, ValueError) as exc:
        raise CommandError(f"Invalid task data: {exc}") from exc


def load_tasks(path: Path = DEFAULT_TODOS) -> list[TodoItem]:
    if not path.is_file():
        return []
    if path.stat().st_size > MAX_SNAPSHOT_BYTES:
        raise CommandError(f"Task file exceeds the {MAX_SNAPSHOT_BYTES}-byte limit.")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CommandError(f"Could not read tasks from {path}: {exc}") from exc
    if not isinstance(raw, list):
        raise CommandError(f"Expected a JSON array in {path}.")
    if len(raw) > MAX_TODOS:
        raise CommandError(f"Task file exceeds the {MAX_TODOS}-task limit.")
    return [
        _todo_from_dict(entry, path=f"tasks[{index}]")
        for index, entry in enumerate(raw)
    ]


def resolve_task(tasks: list[TodoItem], selector: str) -> TodoItem:
    cleaned = selector.strip()
    if cleaned.isdigit():
        index = int(cleaned) - 1
        if 0 <= index < len(tasks):
            return tasks[index]
        raise CommandError(f"Task number {cleaned} does not exist.")
    exact = [task for task in tasks if task.id == cleaned]
    if exact:
        return exact[0]
    prefix = [task for task in tasks if task.id.startswith(cleaned)]
    if len(prefix) == 1:
        return prefix[0]
    if len(prefix) > 1:
        raise CommandError(f"Task id prefix {cleaned!r} is ambiguous.")
    raise CommandError(f"No task matches {cleaned!r}.")


def format_task_list(tasks: list[TodoItem]) -> str:
    if not tasks:
        return "No open tasks. Run `carmen-daily` after refreshing the source snapshots."
    lines: list[str] = []
    for index, task in enumerate(tasks, start=1):
        sources = "+".join(task.sources or [task.source])
        reply = " · reply" if task.needs_reply else ""
        due = f" · due {task.due}" if task.due else ""
        reminder = f" · remind {task.remind_at}" if task.remind_at else ""
        lines.append(
            f"{index}. [{task.urgency}] {task.title} ({sources})"
            f"{due}{reminder}{reply} — {task.id}"
        )
    return "\n".join(lines)


def format_task(task: TodoItem) -> str:
    return json.dumps(asdict(task), indent=2, ensure_ascii=False)


def read_drafts(task: TodoItem) -> str:
    paths = task.draft_paths or ([task.draft_path] if task.draft_path else [])
    if not paths:
        raise CommandError(f"Task {task.id} does not have a reply draft.")
    sections: list[str] = []
    for raw_path in paths:
        path = Path(raw_path)
        try:
            body = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise CommandError(f"Could not read draft {path}: {exc}") from exc
        sections.append(f"--- {path.name} ---\n{body}")
    return "\n\n".join(sections)


def normalize_due(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        return ""
    try:
        return date.fromisoformat(cleaned).isoformat()
    except ValueError as exc:
        raise CommandError("Due dates must use YYYY-MM-DD.") from exc


def normalize_reminder(
    value: str,
    *,
    local_tz: timezone | Any | None = None,
) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise CommandError(
            "Reminder time is required. Use YYYY-MM-DD HH:MM, including a UTC offset when needed."
        )
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        reminder = datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise CommandError(
            "Reminder time must use YYYY-MM-DD HH:MM or an ISO-8601 timestamp."
        ) from exc
    if "T" not in cleaned and " " not in cleaned:
        raise CommandError("Reminder time must include an hour and minute.")
    timezone_for_naive = local_tz or datetime.now().astimezone().tzinfo or timezone.utc
    if reminder.tzinfo is None:
        reminder = reminder.replace(tzinfo=timezone_for_naive)
    return reminder.isoformat(timespec="minutes")


def _persist_open_tasks(
    tasks: list[TodoItem],
    *,
    todos_path: Path,
    state_path: Path,
    brief_path: Path,
) -> list[TodoItem]:
    active, progress = reconcile_state(tasks, state_path)
    dump_todos(active, todos_path)
    write_morning_brief(active, progress, brief_path)
    return active


def add_task(
    tasks: list[TodoItem],
    title: str,
    *,
    urgency: str = "this_week",
    due: str = "",
    remind_at: str = "",
    todos_path: Path = DEFAULT_TODOS,
    state_path: Path = DEFAULT_STATE,
    brief_path: Path = DEFAULT_BRIEF,
) -> str:
    cleaned_title = " ".join(title.split())
    if not cleaned_title:
        raise CommandError("Task title cannot be empty.")
    if urgency not in {"now", "today", "this_week", "fyi"}:
        raise CommandError("Urgency must be now, today, this_week, or fyi.")
    stamp = datetime.now(timezone.utc).isoformat()
    task_id = f"manual-{uuid4().hex[:12]}"
    task = TodoItem(
        source="manual",
        id=task_id,
        title=cleaned_title[:120],
        why="Added manually",
        urgency=urgency,  # type: ignore[arg-type]
        needs_reply=False,
        due=normalize_due(due),
        remind_at=normalize_reminder(remind_at) if remind_at else "",
        sources=["manual"],
        source_ids=[f"manual:{task_id}"],
        confidence=1.0,
        first_seen=stamp,
        last_seen=stamp,
    )
    active = _persist_open_tasks(
        [*tasks, task],
        todos_path=todos_path,
        state_path=state_path,
        brief_path=brief_path,
    )
    number = next(
        (index for index, candidate in enumerate(active, start=1) if candidate.id == task.id),
        len(active),
    )
    reminder = f" · remind {task.remind_at}" if task.remind_at else ""
    due_text = f" · due {task.due}" if task.due else ""
    return f"Added task {number}: {task.title}{due_text}{reminder} — {task.id}"


def update_task(
    tasks: list[TodoItem],
    task: TodoItem,
    *,
    title: str | None = None,
    why: str | None = None,
    urgency: str | None = None,
    due: str | None = None,
    remind_at: str | None = None,
    todos_path: Path = DEFAULT_TODOS,
    state_path: Path = DEFAULT_STATE,
    brief_path: Path = DEFAULT_BRIEF,
) -> str:
    """Persist validated user-owned overrides for an open task."""
    changed: list[str] = []
    if title is not None:
        cleaned = " ".join(title.split())
        if not cleaned:
            raise CommandError("Task title cannot be empty.")
        if len(cleaned) > 120:
            raise CommandError("Task title must be 120 characters or fewer.")
        if cleaned != task.title:
            task.title = cleaned
            changed.append("title")
    if why is not None:
        cleaned_why = why.strip()
        if len(cleaned_why) > 500:
            raise CommandError("Task notes must be 500 characters or fewer.")
        if cleaned_why != task.why:
            task.why = cleaned_why
            changed.append("why")
    if urgency is not None:
        if urgency not in {"now", "today", "this_week", "fyi"}:
            raise CommandError("Urgency must be now, today, this_week, or fyi.")
        if urgency != task.urgency:
            task.urgency = urgency  # type: ignore[assignment]
            changed.append("urgency")
    if due is not None:
        normalized_due = normalize_due(due)
        if normalized_due != task.due:
            task.due = normalized_due
            changed.append("due")
    if remind_at is not None:
        normalized_reminder = normalize_reminder(remind_at) if remind_at.strip() else ""
        if normalized_reminder != task.remind_at:
            task.remind_at = normalized_reminder
            changed.append("remind_at")
    if not changed:
        raise CommandError("No task changes were provided.")
    task.edited_fields = list(dict.fromkeys([*task.edited_fields, *changed]))
    task.last_seen = datetime.now(timezone.utc).isoformat()
    _persist_open_tasks(
        tasks,
        todos_path=todos_path,
        state_path=state_path,
        brief_path=brief_path,
    )
    return f"Updated {task.id}: {task.title}"


def set_task_reminder(
    tasks: list[TodoItem],
    task: TodoItem,
    reminder: str,
    *,
    todos_path: Path = DEFAULT_TODOS,
    state_path: Path = DEFAULT_STATE,
    brief_path: Path = DEFAULT_BRIEF,
) -> str:
    task.remind_at = normalize_reminder(reminder)
    _persist_open_tasks(
        tasks,
        todos_path=todos_path,
        state_path=state_path,
        brief_path=brief_path,
    )
    return f"Reminder set for {task.remind_at}: {task.title}"


def snooze_task(
    tasks: list[TodoItem],
    task: TodoItem,
    *,
    minutes: int = 60,
    now: datetime | None = None,
    todos_path: Path = DEFAULT_TODOS,
    state_path: Path = DEFAULT_STATE,
    brief_path: Path = DEFAULT_BRIEF,
) -> str:
    if minutes < 1 or minutes > 10_080:
        raise CommandError("Snooze must be between 1 minute and 7 days.")
    current = now or datetime.now().astimezone()
    task.remind_at = (current + timedelta(minutes=minutes)).isoformat(timespec="minutes")
    _persist_open_tasks(
        tasks,
        todos_path=todos_path,
        state_path=state_path,
        brief_path=brief_path,
    )
    return f"Snoozed until {task.remind_at}: {task.title}"


def _mark_state_complete(
    task: TodoItem,
    state_path: Path,
    evidence: str,
    *,
    completed_at: str,
) -> None:
    raw: dict[str, Any] = {"version": 1, "last_run": completed_at, "tasks": []}
    if state_path.is_file():
        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except (OSError, json.JSONDecodeError):
            pass
    entries = raw.get("tasks", [])
    if not isinstance(entries, list):
        entries = []
    found = False
    for entry in entries:
        if isinstance(entry, dict) and entry.get("id") == task.id:
            entry["status"] = "done"
            entry["last_seen"] = completed_at
            entry["completion_evidence"] = evidence
            entry["lifecycle_stage"] = task.lifecycle_stage
            entry["resolution_mode"] = task.resolution_mode
            entry["last_verified_at"] = task.last_verified_at
            entry["resolution_receipt_id"] = task.resolution_receipt_id
            found = True
    if not found:
        completed = asdict(task)
        completed["status"] = "done"
        completed["last_seen"] = completed_at
        completed["completion_evidence"] = evidence
        entries.append(completed)
    raw["last_run"] = completed_at
    raw["tasks"] = entries
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")


def mark_complete(
    tasks: list[TodoItem],
    task: TodoItem,
    *,
    todos_path: Path = DEFAULT_TODOS,
    state_path: Path = DEFAULT_STATE,
    brief_path: Path = DEFAULT_BRIEF,
    habits_path: Path = DEFAULT_HABITS_PATH,
    evidence: str = "explicit terminal command",
    resolution_mode: str = "local",
    receipt_id: str = "",
    verified_at: str = "",
    learn_dismissal: bool = False,
) -> str:
    now = datetime.now(timezone.utc)
    completed_at = now.isoformat()
    task.status = "done"
    task.lifecycle_stage = "done"
    task.resolution_mode = resolution_mode  # type: ignore[assignment]
    task.resolution_receipt_id = receipt_id
    task.last_verified_at = verified_at
    task.last_seen = completed_at
    task.completion_evidence = evidence
    remaining = [candidate for candidate in tasks if candidate.id != task.id]
    dump_todos(remaining, todos_path)
    _mark_state_complete(task, state_path, evidence, completed_at=completed_at)
    # Only explicit negative feedback is a suppression signal. Ordinary local or
    # source-verified completion must not teach the system that similar work is noise.
    if learn_dismissal:
        record_dismissal(task, now=now, evidence=evidence, path=habits_path)
    progress = RunProgress(new=[], updated=[], carried=[], completed=[task])
    write_morning_brief(remaining, progress, brief_path)
    return f"Completed {task.id}: {task.title}"


def execute_task(
    tasks: list[TodoItem],
    task: TodoItem,
    *,
    executor_command: str,
    todos_path: Path = DEFAULT_TODOS,
    state_path: Path = DEFAULT_STATE,
    brief_path: Path = DEFAULT_BRIEF,
    habits_path: Path = DEFAULT_HABITS_PATH,
    receipts_path: Path = DEFAULT_ACTION_RECEIPTS,
    action_id: str = "",
) -> str:
    if not executor_command.strip():
        raise CommandError(
            "No action adapter is configured. Set CARMEN_ACTION_COMMAND or pass "
            "`--with-command /path/to/your-action-adapter`."
        )
    draft = ""
    if task.draft_path or task.draft_paths:
        draft = read_drafts(task)
    connector_action = None
    if action_id:
        try:
            connector_action = require_connector_action(
                task, action_id, has_draft=bool(draft)
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        try:
            validate_receipt_store(receipts_path)
        except ValueError as exc:
            raise CommandError(
                f"Action receipt history is invalid; no external action was run: {exc}"
            ) from exc
    payload = {
        "version": 2,
        "request": "execute_task",
        "task": asdict(task),
        "draft": draft,
        "connector_action": (
            connector_action.public_dict() if connector_action is not None else None
        ),
        "safety": {
            "user_explicitly_requested_execution": True,
            "do_not_expand_beyond_this_task": True,
        },
    }

    def fail_adapter(message: str) -> None:
        if connector_action is not None:
            append_receipt(
                ActionReceipt.create(
                    task_id=task.id,
                    source=task.source,
                    action_id=action_id,
                    status="failed",
                    message=message,
                ),
                receipts_path,
            )
        raise CommandError(message)

    run_id = "local-" + uuid4().hex
    context = RunContext(
        run_id=run_id,
        tenant_id="local",
        user_id="local-user",
        actor=UserActor(user_id="local-user"),
        surface="execute_task",
        thread_id=task.id,
        permissions=frozenset({"actions.execute"}),
    )
    key = stable_idempotency_key(run_id, "execute", ExecuteLocalActionAdapter.name)
    try:
        result = ToolExecutor().execute(
            ExecuteLocalActionAdapter(
                command=executor_command,
                payload_json=json.dumps(payload, ensure_ascii=False),
                working_directory=str(PROJECT_ROOT),
            ),
            ToolRuntime(
                context=context,
                tool_call_id="call-" + key[:24],
                idempotency_key=key,
                approved=True,
                services={"subprocess_run": subprocess.run},
            ),
        )
    except subprocess.TimeoutExpired as exc:
        try:
            fail_adapter("Action adapter timed out after 120 seconds.")
        except CommandError as error:
            raise error from exc
    except OSError as exc:
        try:
            fail_adapter(f"Could not run action adapter: {exc}")
        except CommandError as error:
            raise error from exc
    returncode = int(result["returncode"])
    stdout = str(result["stdout"])
    stderr = str(result["stderr"])
    if len(stdout) > MAX_ACTION_RESPONSE_CHARS or len(stderr) > MAX_ACTION_RESPONSE_CHARS:
        fail_adapter(
            f"Action adapter output exceeds {MAX_ACTION_RESPONSE_CHARS} characters."
        )
    if returncode != 0:
        detail = stderr.strip() or stdout.strip() or "unknown error"
        if connector_action is not None:
            append_receipt(
                ActionReceipt.create(
                    task_id=task.id, source=task.source, action_id=action_id,
                    status="failed", message=detail,
                ),
                receipts_path,
            )
        raise CommandError(f"Action adapter failed ({returncode}): {detail}")

    output = stdout.strip()
    try:
        response = json.loads(output) if output else {}
    except json.JSONDecodeError as exc:
        if connector_action is not None:
            try:
                fail_adapter("Action adapter must return one JSON object.")
            except CommandError as error:
                raise error from exc
        response = {"status": "unknown", "message": output}
    if connector_action is not None:
        if not isinstance(response, dict):
            fail_adapter("Action adapter must return one JSON object.")
        unknown = set(response) - _ACTION_RESPONSE_KEYS
        if unknown:
            fail_adapter(
                "Action adapter returned unknown field(s): " + ", ".join(sorted(unknown))
            )
        if any(not isinstance(response.get(key, ""), str) for key in _ACTION_RESPONSE_KEYS):
            fail_adapter("Action adapter status, message, and external_id must be text.")
    status = str(response.get("status", "unknown"))
    message = str(response.get("message", "")).strip()
    external_id = str(response.get("external_id", "")).strip()
    if connector_action is not None:
        if status not in _ACTION_STATUSES:
            fail_adapter("Action adapter returned an unsupported status.")
        if len(message) > 1_000 or len(external_id) > 512:
            fail_adapter("Action adapter response fields exceed their size limits.")
        if status == "verified" and not external_id:
            fail_adapter("A verified action requires a non-empty external_id receipt.")
    verified = status == "verified" and bool(external_id)
    receipt = None
    if connector_action is not None:
        receipt = ActionReceipt.create(
            task_id=task.id,
            source=task.source,
            action_id=action_id,
            status="verified" if verified else status,
            external_id=external_id,
            message=message,
            verified=verified,
        )
        append_receipt(receipt, receipts_path)
    if verified and receipt is not None:
        completion = mark_complete(
            tasks,
            task,
            todos_path=todos_path,
            state_path=state_path,
            brief_path=brief_path,
            habits_path=habits_path,
            evidence=message or f"Source verified {action_id} as complete.",
            resolution_mode="source_verified",
            receipt_id=receipt.id,
            verified_at=receipt.completed_at,
        )
        return f"{completion}\n{message}".rstrip()
    if connector_action is not None and status in {"completed", "submitted", "pending"}:
        # A successful write is not necessarily task completion. Keep the task
        # visible until a connector reread supplies affirmative source evidence.
        task.status = "waiting"
        task.lifecycle_stage = "verifying"
        task.resolution_receipt_id = receipt.id if receipt is not None else ""
        _persist_open_tasks(
            tasks,
            todos_path=todos_path,
            state_path=state_path,
            brief_path=brief_path,
        )
        return message or "Action submitted. Carmen is waiting for source verification."
    if status == "completed":
        completion = mark_complete(
            tasks,
            task,
            todos_path=todos_path,
            state_path=state_path,
            brief_path=brief_path,
            habits_path=habits_path,
            evidence=message or "action adapter reported completion",
            resolution_mode="local",
        )
        return f"{completion}\n{message}".rstrip()
    return message or f"Action adapter returned status: {status}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect and explicitly act on tasks produced by carmen-daily."
    )
    parser.add_argument("--todos", type=Path, default=DEFAULT_TODOS)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--brief", type=Path, default=DEFAULT_BRIEF)
    parser.add_argument("--habits", type=Path, default=DEFAULT_HABITS_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="List open tasks with stable ids.")
    add = subparsers.add_parser("add", help="Add a personal task.")
    add.add_argument("title", nargs="+")
    add.add_argument(
        "--urgency",
        choices=("now", "today", "this_week", "fyi"),
        default="this_week",
    )
    add.add_argument("--due", default="", help="Optional due date: YYYY-MM-DD.")
    add.add_argument(
        "--remind-at",
        default="",
        help='Optional local reminder time, for example "2026-07-24 15:00".',
    )
    for name in ("show", "draft", "complete"):
        command = subparsers.add_parser(name)
        command.add_argument("task")
    remind = subparsers.add_parser("remind", help="Set an exact reminder on a task.")
    remind.add_argument("task")
    remind.add_argument(
        "--at",
        required=True,
        help='Local reminder time, for example "2026-07-24 15:00".',
    )
    snooze = subparsers.add_parser("snooze", help="Move a task reminder into the future.")
    snooze.add_argument("task")
    snooze.add_argument("--minutes", type=int, default=60)
    execute = subparsers.add_parser(
        "execute",
        help="Execute exactly one task through the configured action adapter.",
    )
    execute.add_argument("task")
    execute.add_argument(
        "--with-command",
        default=os.environ.get("CARMEN_ACTION_COMMAND", ""),
        help="Action-adapter executable. It receives a versioned JSON request on stdin.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        tasks = load_tasks(args.todos)
        if args.command == "list":
            print(format_task_list(tasks))
            return
        if args.command == "add":
            print(
                add_task(
                    tasks,
                    " ".join(args.title),
                    urgency=args.urgency,
                    due=args.due,
                    remind_at=args.remind_at,
                    todos_path=args.todos,
                    state_path=args.state,
                    brief_path=args.brief,
                )
            )
            return
        task = resolve_task(tasks, args.task)
        if args.command == "show":
            print(format_task(task))
        elif args.command == "draft":
            print(read_drafts(task))
        elif args.command == "complete":
            print(
                mark_complete(
                    tasks,
                    task,
                    todos_path=args.todos,
                    state_path=args.state,
                    brief_path=args.brief,
                    habits_path=args.habits,
                )
            )
        elif args.command == "remind":
            print(
                set_task_reminder(
                    tasks,
                    task,
                    args.at,
                    todos_path=args.todos,
                    state_path=args.state,
                    brief_path=args.brief,
                )
            )
        elif args.command == "snooze":
            print(
                snooze_task(
                    tasks,
                    task,
                    minutes=args.minutes,
                    todos_path=args.todos,
                    state_path=args.state,
                    brief_path=args.brief,
                )
            )
        elif args.command == "execute":
            print(
                execute_task(
                    tasks,
                    task,
                    executor_command=args.with_command,
                    todos_path=args.todos,
                    state_path=args.state,
                    brief_path=args.brief,
                    habits_path=args.habits,
                )
            )
    except CommandError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
