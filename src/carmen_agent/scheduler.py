from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from carmen_agent.apps.automation import (
    DEFAULT_DAILY_HOUR,
    DEFAULT_DAILY_MINUTE,
    DEFAULT_REMINDER_MINUTES,
)
from carmen_agent.paths import PROJECT_ROOT, WORK_DIR

DEFAULT_STATE = WORK_DIR / "portable-scheduler-state.json"


@dataclass(frozen=True)
class SchedulerConfig:
    hour: int = DEFAULT_DAILY_HOUR
    minute: int = DEFAULT_DAILY_MINUTE
    reminder_minutes: int = DEFAULT_REMINDER_MINUTES
    reminders_enabled: bool = True

    def __post_init__(self) -> None:
        if not 0 <= self.hour <= 23 or not 0 <= self.minute <= 59:
            raise ValueError("hour must be 0–23 and minute must be 0–59")
        if self.reminders_enabled and not 5 <= self.reminder_minutes <= 1_440:
            raise ValueError("reminder interval must be between 5 and 1440 minutes")


@dataclass(frozen=True)
class SchedulerTick:
    daily_ran: bool
    reminders_ran: bool


def _load_state(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(key): str(item) for key, item in value.items()} if isinstance(value, dict) else {}


def _write_state(path: Path, state: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def run_tick(
    *,
    config: SchedulerConfig,
    state_path: Path = DEFAULT_STATE,
    now: datetime | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    python_executable: str | None = None,
) -> SchedulerTick:
    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    state = _load_state(state_path)
    python = python_executable or sys.executable
    daily_due = current.time().replace(tzinfo=None) >= datetime(
        current.year, current.month, current.day, config.hour, config.minute
    ).time()
    daily_ran = False
    if daily_due and state.get("daily_date") != current.date().isoformat():
        result = runner(
            [python, "-m", "carmen_agent.skills.cli"],
            cwd=PROJECT_ROOT,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Daily agent failed with exit code {result.returncode}.")
        state["daily_date"] = current.date().isoformat()
        daily_ran = True

    last_reminder = ""
    raw_last = state.get("last_reminder_check", "")
    if raw_last:
        try:
            last_reminder = datetime.fromisoformat(raw_last.replace("Z", "+00:00"))
        except ValueError:
            last_reminder = ""
    reminder_due = config.reminders_enabled and (not isinstance(last_reminder, datetime) or (
        current - last_reminder.astimezone(current.tzinfo)
        >= timedelta(minutes=config.reminder_minutes)
    ))
    reminders_ran = False
    if reminder_due:
        result = runner(
            [python, "-m", "carmen_agent.reminders", "--notify"],
            cwd=PROJECT_ROOT,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Reminder check failed with exit code {result.returncode}.")
        state["last_reminder_check"] = current.isoformat()
        reminders_ran = True
    _write_state(state_path, state)
    return SchedulerTick(daily_ran=daily_ran, reminders_ran=reminders_ran)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Carmen's platform-neutral daily and reminder scheduler."
    )
    parser.add_argument("--hour", type=int, default=DEFAULT_DAILY_HOUR)
    parser.add_argument("--minute", type=int, default=DEFAULT_DAILY_MINUTE)
    parser.add_argument("--reminder-minutes", type=int, default=DEFAULT_REMINDER_MINUTES)
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-reminders", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        config = SchedulerConfig(
            args.hour, args.minute, args.reminder_minutes, not args.no_reminders
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    while True:
        try:
            tick = run_tick(config=config, state_path=args.state)
        except RuntimeError as exc:
            print(f"Scheduler warning: {exc}", file=sys.stderr, flush=True)
        else:
            if tick.daily_ran or tick.reminders_ran:
                print(
                    f"Scheduler tick: daily={tick.daily_ran}, reminders={tick.reminders_ran}",
                    flush=True,
                )
        if args.once:
            return
        time.sleep(max(10, args.poll_seconds))


if __name__ == "__main__":
    main()
