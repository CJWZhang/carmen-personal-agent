from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from carmen_agent.commands import load_tasks
from carmen_agent.paths import (
    REMINDER_STATE_PATH as DEFAULT_REMINDER_STATE,
    TODOS_PATH as DEFAULT_TODOS,
)
from carmen_agent.skills.schemas import TodoItem


class ReminderError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReminderEvent:
    key: str
    task_id: str
    title: str
    reason: str


def _format_local_time(value: datetime) -> str:
    hour = value.strftime("%I").lstrip("0") or "0"
    return f"{value:%b} {value.day} at {hour}:{value:%M %p}"


def _parse_remind_at(raw: str, local_tz: object) -> datetime | None:
    cleaned = raw.strip()
    if not cleaned:
        return None
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_tz)  # type: ignore[arg-type]
    return parsed


def _parse_due(raw: str) -> date | None:
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def build_reminder_events(
    tasks: list[TodoItem],
    *,
    now: datetime | None = None,
) -> list[ReminderEvent]:
    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local_today = current.date()
    events: list[ReminderEvent] = []
    for task in tasks:
        if task.status != "open":
            continue
        if task.remind_at:
            reminder = _parse_remind_at(task.remind_at, current.tzinfo)
            if reminder is not None and reminder <= current:
                events.append(
                    ReminderEvent(
                        key=f"{task.id}:at:{task.remind_at}",
                        task_id=task.id,
                        title=task.title,
                        reason="Scheduled for "
                        + _format_local_time(reminder.astimezone(current.tzinfo)),
                    )
                )
            continue

        due = _parse_due(task.due) if task.due else None
        if due is not None and due <= local_today:
            reason = "Due today" if due == local_today else f"Overdue since {due.isoformat()}"
            events.append(
                ReminderEvent(
                    key=f"{task.id}:due:{local_today.isoformat()}",
                    task_id=task.id,
                    title=task.title,
                    reason=reason,
                )
            )
        elif task.urgency in {"now", "today"}:
            events.append(
                ReminderEvent(
                    key=f"{task.id}:priority:{local_today.isoformat()}",
                    task_id=task.id,
                    title=task.title,
                    reason="Needs attention today",
                )
            )
    return events


def _load_deliveries(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    deliveries = raw.get("deliveries", {}) if isinstance(raw, dict) else {}
    if not isinstance(deliveries, dict):
        return {}
    return {str(key): str(value) for key, value in deliveries.items()}


def pending_reminders(
    events: list[ReminderEvent],
    *,
    state_path: Path = DEFAULT_REMINDER_STATE,
    force: bool = False,
) -> list[ReminderEvent]:
    if force:
        return events
    delivered = _load_deliveries(state_path)
    return [event for event in events if event.key not in delivered]


def _write_deliveries(
    deliveries: dict[str, str],
    path: Path,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            {"version": 1, "deliveries": deliveries},
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def send_macos_notification(event: ReminderEvent) -> None:
    script = (
        f"display notification {json.dumps(event.reason + ': ' + event.title)} "
        f"with title {json.dumps('Carmen Assistant')}"
    )
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise ReminderError(f"Could not invoke macOS notifications: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise ReminderError(f"macOS notification failed: {detail}")


def send_linux_notification(event: ReminderEvent) -> None:
    executable = shutil.which("notify-send")
    if not executable:
        raise ReminderError("Linux desktop notifications require `notify-send`.")
    result = subprocess.run(
        [executable, "Carmen Assistant", f"{event.reason}: {event.title}"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise ReminderError(f"Linux notification failed: {detail}")


def send_windows_notification(event: ReminderEvent) -> None:
    executable = shutil.which("powershell") or shutil.which("pwsh")
    if not executable:
        raise ReminderError("Windows desktop notifications require PowerShell.")
    title = "Carmen Assistant".replace("'", "''")
    body = f"{event.reason}: {event.title}".replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$n=New-Object System.Windows.Forms.NotifyIcon; "
        "$n.Icon=[System.Drawing.SystemIcons]::Information; "
        "$n.Visible=$true; "
        f"$n.ShowBalloonTip(8000,'{title}','{body}',"
        "[System.Windows.Forms.ToolTipIcon]::Info); Start-Sleep -Seconds 9; $n.Dispose()"
    )
    result = subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-Command", script],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise ReminderError(f"Windows notification failed: {detail}")


def send_console_notification(event: ReminderEvent) -> None:
    print(
        f"Carmen Assistant reminder — {event.reason}: {event.title}",
        file=sys.stderr,
        flush=True,
    )


def send_desktop_notification(
    event: ReminderEvent, *, system: str | None = None
) -> None:
    backend = os.environ.get("CARMEN_NOTIFICATION_BACKEND", "auto").strip().lower()
    detected = system or platform.system()
    sender = {
        "darwin": send_macos_notification,
        "linux": send_linux_notification,
        "windows": send_windows_notification,
        "console": send_console_notification,
    }.get(backend)
    if sender is None:
        sender = {
            "Darwin": send_macos_notification,
            "Linux": send_linux_notification,
            "Windows": send_windows_notification,
        }.get(detected, send_console_notification)
    try:
        sender(event)
    except ReminderError:
        if os.environ.get("CARMEN_NOTIFICATION_STRICT") == "1":
            raise
        send_console_notification(event)


def deliver_reminders(
    events: list[ReminderEvent],
    *,
    state_path: Path = DEFAULT_REMINDER_STATE,
    notifier: Callable[[ReminderEvent], None] | None = None,
    now: datetime | None = None,
) -> int:
    delivered = _load_deliveries(state_path)
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    count = 0
    selected_notifier = notifier or send_desktop_notification
    for event in events:
        selected_notifier(event)
        delivered[event.key] = stamp
        count += 1
    if count:
        _write_deliveries(delivered, state_path)
    return count


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Show or deliver reminders for open Carmen Assistant tasks."
    )
    parser.add_argument("--todos", type=Path, default=DEFAULT_TODOS)
    parser.add_argument("--state", type=Path, default=DEFAULT_REMINDER_STATE)
    parser.add_argument(
        "--notify",
        action="store_true",
        help="Deliver pending reminders using the current OS notification backend.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Include reminders already delivered for their current reminder window.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    tasks = load_tasks(args.todos)
    due = build_reminder_events(tasks)
    pending = pending_reminders(due, state_path=args.state, force=args.force)
    if not pending:
        print("No reminders due.")
        return
    if not args.notify:
        for event in pending:
            print(f"- {event.reason}: {event.title} — {event.task_id}")
        return
    try:
        delivered = deliver_reminders(pending, state_path=args.state)
    except ReminderError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Delivered {delivered} reminder(s).")


if __name__ == "__main__":
    main()
