from __future__ import annotations

import argparse
from collections import deque
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import asdict
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

from carmen_agent.commands import (
    CommandError,
    add_task,
    execute_task,
    load_tasks,
    mark_complete,
    resolve_task,
    set_task_reminder,
    snooze_task,
    update_task,
)
from carmen_agent.actions.contracts import connector_actions_for
from carmen_agent.apps.automation import (
    DEFAULT_DAILY_HOUR,
    DEFAULT_DAILY_MINUTE,
    DEFAULT_REMINDER_MINUTES,
    AutomationSettings,
    AutomationSettingsError,
    load_automation_settings,
    parse_automation_preferences,
)
from carmen_agent.configuration import config_value, save_user_config
from carmen_agent.context.packet import build_context_packet
from carmen_agent.infra.context import ContextEngine
from carmen_agent.paths import (
    ACTION_RECEIPTS_PATH,
    BRIEF_PATH,
    CALENDAR_PREFERENCES_PATH,
    DEFAULT_ADAPTER_DIR,
    DEFAULT_MODEL_NAME,
    DRAFTS_DIR,
    HABITS_PATH,
    INBOX_DIR,
    PROJECT_ROOT,
    STATE_PATH,
    STYLE_PROFILE_PATH,
    TODOS_PATH,
)
from carmen_agent.personalization import personalization_status
from carmen_agent.observability.telemetry import TelemetryStore
from carmen_agent.source_status import (
    authoritative_sources,
    dashboard_source_health,
    fresh_sources,
    inspect_source_snapshots,
)
from carmen_agent.source_rules import partition_source_items
from carmen_agent.response_quality import response_is_usable
from carmen_agent.ranking.personalized import rank_task
from carmen_agent.skills.draft import _safe_fallback
from carmen_agent.skills.model import default_local_provider, load_session
from carmen_agent.setup import diagnostics
from carmen_agent.skills.schemas import (
    InboxItem,
    TodoItem,
    dump_todos,
    load_inbox,
    parse_stamp,
    todo_item_from_dict,
)
from carmen_agent.skills.synthesis import reconcile_state
from carmen_agent.skills.calendar_preferences import (
    CalendarPreferenceError,
    personalize_calendar_tasks,
    record_calendar_preference,
)
from carmen_agent.skills.todo import write_morning_brief

ASSET_PACKAGE = "carmen_agent.ui.assets"
TASK_ROUTE = re.compile(
    r"^/api/tasks/(?P<task_id>[^/]+)(?:/(?P<action>complete|start|wait|reopen|verify-context|usually-skip|usually-attend|snooze|remind|edit|execute|accept|not-a-task|not-mine|already-handled))?$"
)
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
_SAVED_SNAPSHOT_PREFIX = "Saved source snapshot · "
_DRAFT_INTENT_RE = re.compile(r"\b(draft|reply|respond|response)\b", re.IGNORECASE)
_GREETING_RE = re.compile(r"^(?:hi|hello|hey|hiya|yo)[!?.]*$", re.IGNORECASE)
_SOURCE_LABELS = {
    "slack": "Slack",
    "gmail": "Gmail",
    "calendar": "Calendar",
    "github": "GitHub",
}


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.status = status


class DashboardService:
    """Application boundary shared by the HTTP UI and unit tests."""

    def __init__(
        self,
        *,
        todos_path: Path = TODOS_PATH,
        state_path: Path = STATE_PATH,
        brief_path: Path = BRIEF_PATH,
        habits_path: Path = HABITS_PATH,
        receipts_path: Path = ACTION_RECEIPTS_PATH,
        calendar_preferences_path: Path = CALENDAR_PREFERENCES_PATH,
        inbox_dir: Path = INBOX_DIR,
        drafts_dir: Path = DRAFTS_DIR,
        session_factory: Callable[..., Any] = load_session,
        model_provider: str | None = None,
        model_name: str | None = None,
        base_only: bool = False,
        daily_runner: Callable[[], Any] | None = None,
        maintenance_runner: Callable[[list[str]], Any] | None = None,
        session_factory_uses_settings: bool = False,
        telemetry: TelemetryStore | None = None,
    ) -> None:
        self.todos_path = todos_path
        self.state_path = state_path
        self.brief_path = brief_path
        self.habits_path = habits_path
        self.receipts_path = receipts_path
        self.calendar_preferences_path = calendar_preferences_path
        self.inbox_dir = inbox_dir
        self.drafts_dir = drafts_dir
        self.session_factory = session_factory
        self.model_provider = model_provider or default_local_provider()
        self.model_name = model_name or config_value(
            "CARMEN_MODEL_NAME",
            "model_name",
            DEFAULT_MODEL_NAME if self.model_provider == "mlx" else "qwen3:4b",
        )
        self.base_only = base_only
        self.daily_runner = daily_runner or self._default_daily_runner
        self.maintenance_runner = maintenance_runner or self._default_command_runner
        self.session_factory_uses_settings = session_factory_uses_settings
        self.telemetry = telemetry or TelemetryStore(
            self.state_path.parent / "quality-telemetry.jsonl"
        )
        self._session: Any | None = None
        self._session_uses_adapter: bool | None = None
        self._model_lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._refresh_status: dict[str, str] = {
            "state": "idle",
            "started_at": "",
            "finished_at": "",
            "message": "",
            "detail": "",
        }
        self._maintenance_status: dict[str, str] = {
            "state": "idle",
            "action": "",
            "started_at": "",
            "finished_at": "",
            "message": "",
            "detail": "",
        }

    @staticmethod
    def _default_daily_runner() -> dict[str, Any]:
        # The dashboard button is a data refresh, not a model-training or
        # drafting action. Keep it bounded and import the normalized snapshots
        # deterministically in `_run_refresh`; local inference remains available
        # through chat and the explicit daily/maintenance workflows.
        return DashboardService._run_bounded_command(
            [
                sys.executable,
                "-m",
                "carmen_agent.collector",
                "--sources",
                "calendar,gmail,github,slack",
                "--mode",
                "auto",
                "--timeout",
                "480",
                "--max-source-seconds",
                "120",
                "--progressive",
            ],
            timeout=510,
        )

    @staticmethod
    def _default_command_runner(command: list[str]) -> dict[str, Any]:
        return DashboardService._run_bounded_command(command, timeout=7_200)

    @staticmethod
    def _run_bounded_command(command: list[str], *, timeout: int) -> dict[str, Any]:
        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        chunks: deque[str] = deque(maxlen=16)

        def drain() -> None:
            if process.stdout is None:
                return
            while chunk := process.stdout.read(4_096):
                chunks.append(chunk)

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            process.wait()
        reader.join(timeout=5)
        output = "".join(chunks)[-4_000:]
        if timed_out:
            output = f"Operation timed out after {timeout} seconds.\n{output}"[-4_000:]
        return {
            "returncode": 124 if timed_out else int(process.returncode or 0),
            "output": output,
        }

    @staticmethod
    def _runner_result(value: Any) -> tuple[int, str]:
        if isinstance(value, dict):
            returncode = value.get("returncode", 1)
            output = value.get("output", "")
            if type(returncode) is not int or not isinstance(output, str):
                return 1, "Operation returned an invalid result."
            return returncode, output[-4_000:]
        if type(value) is int:
            return value, ""
        return 1, "Operation returned an invalid result."

    def _run_refresh(self) -> None:
        try:
            returncode, _raw_detail = self._runner_result(self.daily_runner())
            # Treat normalized connector snapshots as the source of truth. The
            # model may enrich or rank these records, but a malformed or partial
            # model response must never make a successfully collected task vanish.
            self.seed_saved_snapshot({})
            state = "succeeded" if returncode == 0 else "failed"
            if returncode == 0:
                message = "Sources and tasks refreshed."
                detail = ""
            else:
                failed = [
                    _SOURCE_LABELS.get(record.source, record.source.title())
                    for record in self._source_health()
                    if record.status in {"error", "unavailable"} or not record.fresh
                ]
                labels = ", ".join(dict.fromkeys(failed))
                message = (
                    f"Could not refresh {labels}. Last good data was preserved."
                    if labels
                    else "The source refresh did not finish. Last good data was preserved."
                )
                detail = "Open Settings → Connections to see what needs attention."
        except Exception as exc:
            print(f"Dashboard refresh failed: {exc}", flush=True)
            state = "failed"
            message = "The source refresh did not finish. Last good data was preserved."
            detail = "Open Settings → Connections to see what needs attention."
        with self._refresh_lock:
            self._refresh_status = {
                **self._refresh_status,
                "state": state,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "message": message,
                "detail": detail,
            }

    def start_refresh(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._validate_keys(payload, set(), "refresh request")
        with self._refresh_lock:
            if self._refresh_status["state"] == "running":
                return {
                    "message": "A source refresh is already running.",
                    "overview": self.overview(),
                }
            if self._maintenance_status["state"] == "running":
                raise ApiError("Wait for the current background operation to finish.")
            self._refresh_status = {
                "state": "running",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": "",
                "message": "Refreshing Slack, Gmail, Calendar, and GitHub…",
                "detail": "",
            }
        threading.Thread(target=self._run_refresh, daemon=True).start()
        return {"message": "Source refresh started.", "overview": self.overview()}

    def operation_status(self) -> dict[str, Any]:
        """Return lightweight background state without rebuilding task context."""
        source_health = dashboard_source_health(
            self._source_health(),
            task_generated_at=self._task_generated_at(),
        )
        with self._refresh_lock:
            return {
                "refresh": dict(self._refresh_status),
                "maintenance": dict(self._maintenance_status),
                "source_health": source_health,
            }

    def settings(self) -> dict[str, Any]:
        automation = self._automation_settings()
        return {
            "user_name": config_value("CARMEN_USER_NAME", "user_name", ""),
            "provider": self.model_provider,
            "model": self.model_name,
            "base_url": config_value(
                "CARMEN_MODEL_BASE_URL",
                "model_base_url",
                "http://127.0.0.1:11434/v1",
            ),
            "action_command": config_value(
                "CARMEN_ACTION_COMMAND", "action_command", ""
            ),
            "automation": automation.to_api_dict(),
            "personalization": personalization_status().public_dict(),
        }

    @staticmethod
    def _automation_settings() -> AutomationSettings:
        return load_automation_settings(reader=config_value)

    def save_automation(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            automation = parse_automation_preferences(payload)
        except AutomationSettingsError as exc:
            raise ApiError(str(exc)) from exc
        with self._refresh_lock:
            if (
                self._refresh_status["state"] == "running"
                or self._maintenance_status["state"] == "running"
            ):
                raise ApiError("Wait for the current background operation to finish.")
        save_user_config(automation.config_values())
        result = self.start_maintenance(
            {
                "action": "schedule",
                "hour": automation.hour,
                "minute": automation.minute,
                "reminder_minutes": automation.reminder_minutes,
            }
        )
        result["message"] = "Automation saved. Repairing the background schedule…"
        result["overview"] = self.overview()
        return result

    def save_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {"user_name", "provider", "model", "base_url", "action_command"}
        self._validate_keys(payload, allowed, "settings")
        if set(payload) != allowed:
            raise ApiError("Settings must include name, provider, model, base URL, and action adapter.")
        if any(not isinstance(payload[name], str) for name in allowed):
            raise ApiError("All settings fields must be text.")
        provider = payload["provider"].strip().lower()
        if provider not in {"mlx", "ollama", "openai-compatible"}:
            raise ApiError("Model provider is invalid.")
        user_name = " ".join(payload["user_name"].split())
        model = payload["model"].strip()
        base_url = payload["base_url"].strip()
        action_command = payload["action_command"].strip()
        if not user_name or len(user_name) > 120:
            raise ApiError("Name is required and must be 120 characters or fewer.")
        if not model or len(model) > 512:
            raise ApiError("Model is required and must be 512 characters or fewer.")
        parsed_url = urlsplit(base_url)
        if base_url and parsed_url.scheme not in {"http", "https"}:
            raise ApiError("Model base URL must use http or https.")
        if len(base_url) > 2_048 or len(action_command) > 4_096:
            raise ApiError("A settings value is too long.")
        if action_command:
            executable = Path(action_command).expanduser()
            if (
                not executable.is_absolute()
                or not executable.is_file()
                or not os.access(executable, os.X_OK)
            ):
                raise ApiError(
                    "Action adapter must be an existing executable absolute file path."
                )
            action_command = str(executable)
        save_user_config(
            {
                "user_name": user_name,
                "model_provider": provider,
                "model_name": model,
                "model_base_url": base_url,
                "action_command": action_command,
            }
        )
        self.model_provider = provider
        self.model_name = model
        with self._model_lock:
            self._session = None
            self._session_uses_adapter = None
        return {"message": "Settings saved.", "overview": self.overview()}

    def run_diagnostics(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._validate_keys(payload, set(), "diagnostics request")
        items = []
        for item in diagnostics():
            rendered = asdict(item)
            # The shared diagnostic function also serves the CLI and can include
            # command-line recovery hints. Keep the dashboard self-contained.
            if item.name == "MLX runtime" and item.level == "error":
                rendered["detail"] = "Not included in this app installation."
            elif item.name == "Scheduler install" and item.level != "ok":
                rendered["detail"] = "Automatic scheduling is unavailable on this device."
            elif item.name == "Connected-source collector" and item.level != "ok":
                rendered["detail"] = "Connected-source access is unavailable in this app installation."
            items.append(rendered)
        return {
            "diagnostics": items,
            "settings": self.settings(),
        }

    def _run_maintenance(self, action: str, command: list[str]) -> None:
        try:
            returncode, detail = self._runner_result(self.maintenance_runner(command))
            state = "succeeded" if returncode == 0 else "failed"
            message = (
                f"{action.replace('_', ' ').title()} finished."
                if returncode == 0
                else f"{action.replace('_', ' ').title()} failed. Review the details below."
            )
            if action == "personalize_voice":
                message = (
                    "Your personalized voice is ready."
                    if returncode == 0
                    else "Personalization could not finish. Your last working voice was preserved."
                )
            if returncode == 0 and action in {"personalize_voice", "train_voice", "prepare_style"}:
                with self._model_lock:
                    self._session = None
                    self._session_uses_adapter = None
        except Exception as exc:
            print(f"Dashboard operation {action} failed: {exc}", flush=True)
            state = "failed"
            message = (
                "Personalization could not finish. Your last working voice was preserved."
                if action == "personalize_voice"
                else f"{action.replace('_', ' ').title()} failed. Review the details below."
            )
            detail = str(exc)[-4_000:]
        with self._refresh_lock:
            self._maintenance_status = {
                **self._maintenance_status,
                "state": state,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "message": message,
                "detail": detail,
            }

    @staticmethod
    def _schedule_command(
        *,
        hour: int = DEFAULT_DAILY_HOUR,
        minute: int = DEFAULT_DAILY_MINUTE,
        reminder_minutes: int = DEFAULT_REMINDER_MINUTES,
    ) -> list[str]:
        return [
            sys.executable,
            "-m",
            "carmen_agent.skills.schedule",
            "--install",
            "--hour",
            str(hour),
            "--minute",
            str(minute),
            "--reminder-minutes",
            str(reminder_minutes),
        ]

    def _daily_refresh_due(self, *, now: datetime | None = None) -> bool:
        current = now or datetime.now().astimezone()
        if current.tzinfo is None:
            current = current.astimezone()
        automation = self._automation_settings()
        scheduled = current.replace(
            hour=automation.hour,
            minute=automation.minute,
            second=0,
            microsecond=0,
        )
        if current < scheduled:
            return False
        health = self._source_health()
        return any(
            (attempt := parse_stamp(record.last_attempt_at)) is None
            or attempt.astimezone(current.tzinfo).date() != current.date()
            for record in health
        )

    def _run_auto_schedule(self) -> None:
        automation = self._automation_settings()
        self._run_maintenance(
            "schedule",
            self._schedule_command(
                hour=automation.hour,
                minute=automation.minute,
                reminder_minutes=automation.reminder_minutes,
            ),
        )
        if self._daily_refresh_due():
            try:
                self.start_refresh({})
            except ApiError as exc:
                print(f"Automatic catch-up refresh skipped: {exc}", flush=True)

    def ensure_daily_schedule(self) -> bool:
        """Install or repair the saved schedule without blocking app startup."""
        if os.environ.get("CARMEN_DISABLE_AUTO_SCHEDULE", "").strip() == "1":
            return False
        with self._refresh_lock:
            if (
                self._refresh_status["state"] == "running"
                or self._maintenance_status["state"] == "running"
            ):
                return False
            automation = self._automation_settings()
            self._maintenance_status = {
                "state": "running",
                "action": "schedule",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": "",
                "message": (
                    f"Ensuring the automatic {automation.daily_refresh_label} "
                    "refresh is ready…"
                ),
                "detail": "",
            }
        threading.Thread(
            target=self._run_auto_schedule,
            daemon=True,
        ).start()
        return True

    def start_maintenance(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = payload.get("action")
        if action not in {
            "schedule", "personalize_voice", "prepare_style", "train_voice",
            "evaluate", "notify",
        }:
            raise ApiError("Unknown maintenance operation.")
        expected = (
            {"action", "hour", "minute", "reminder_minutes"}
            if action == "schedule"
            else {"action", "mode", "refresh"}
            if action == "personalize_voice"
            else {"action", "mode"}
            if action == "train_voice"
            else {"action"}
        )
        self._validate_keys(payload, expected, "operation")
        command: list[str]
        if action == "schedule":
            automation = self._automation_settings()
            hour = payload.get("hour", automation.hour)
            minute = payload.get("minute", automation.minute)
            reminder_minutes = payload.get(
                "reminder_minutes", automation.reminder_minutes
            )
            if any(type(value) is not int for value in (hour, minute, reminder_minutes)):
                raise ApiError("Schedule values must be integers.")
            if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                raise ApiError("Schedule time is invalid.")
            if not 5 <= reminder_minutes <= 1_440:
                raise ApiError("Reminder interval must be between 5 and 1,440 minutes.")
            command = self._schedule_command(
                hour=hour,
                minute=minute,
                reminder_minutes=reminder_minutes,
            )
        elif action == "prepare_style":
            command = [sys.executable, "-m", "carmen_agent.prepare"]
        elif action == "personalize_voice":
            mode = payload.get("mode", "profile")
            refresh = payload.get("refresh", False)
            if mode not in {"profile", "activate", "smoke", "full"}:
                raise ApiError(
                    "Personalization mode must be profile, activate, smoke, or full."
                )
            if type(refresh) is not bool:
                raise ApiError("Personalization refresh must be true or false.")
            command = [
                sys.executable, "-m", "carmen_agent.personalization", "--mode", mode,
            ]
            if refresh:
                command.append("--refresh")
        elif action == "evaluate":
            command = [sys.executable, "-m", "carmen_agent.evals.runner"]
        elif action == "notify":
            command = [sys.executable, "-m", "carmen_agent.reminders", "--notify"]
        else:
            mode = payload.get("mode", "full")
            if mode not in {"smoke", "full"}:
                raise ApiError("Training mode must be smoke or full.")
            command = [str(PROJECT_ROOT / "run-train.sh")]
            if mode == "smoke":
                command.extend(["--iters", "20"])
        with self._refresh_lock:
            if self._refresh_status["state"] == "running" or self._maintenance_status["state"] == "running":
                raise ApiError("Another background operation is already running.")
            self._maintenance_status = {
                "state": "running",
                "action": str(action),
                "started_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": "",
                "message": (
                    "Learning from your authored writing…"
                    if action == "personalize_voice"
                    else f"{str(action).replace('_', ' ').title()} started…"
                ),
                "detail": "",
            }
            started_message = self._maintenance_status["message"]
        threading.Thread(
            target=self._run_maintenance,
            args=(str(action), command),
            daemon=True,
        ).start()
        return {"message": started_message, "overview": self.overview()}

    def _tasks(self) -> list[TodoItem]:
        return load_tasks(self.todos_path)

    def _task_generated_at(self) -> datetime | None:
        if not self.todos_path.is_file():
            return None
        return datetime.fromtimestamp(
            self.todos_path.stat().st_mtime, tz=timezone.utc
        )

    def _adapter_label(self) -> str:
        voice_mode = config_value("CARMEN_VOICE_MODE", "voice_mode", "profile").lower()
        adapter_ready = (DEFAULT_ADAPTER_DIR / "adapters.safetensors").is_file()
        if self.model_provider == "mlx" and voice_mode == "lora" and adapter_ready:
            return "Carmen LoRA for drafts"
        if STYLE_PROFILE_PATH.is_file():
            return "Carmen voice profile"
        return "Default voice"

    def _source_health(self):
        return inspect_source_snapshots(self.inbox_dir)

    def _task_is_verified(self, task: TodoItem, health_by_source: dict[str, Any]) -> bool:
        # Saved-snapshot tasks are deliberately visible, but are labeled everywhere
        # as historical until a successful source run replaces them.
        if self._saved_snapshot_at(task):
            return True
        sources = set(task.sources or [task.source]) - {"manual"}
        if not sources:
            return True
        observed = parse_stamp(task.last_seen)
        for source in sources:
            record = health_by_source.get(source)
            if record is None or not record.fresh:
                return False
            refreshed = parse_stamp(record.last_success_at)
            if refreshed is not None and (
                observed is None
                or observed.astimezone(timezone.utc) < refreshed.astimezone(timezone.utc)
            ):
                return False
        return True

    def _verified_tasks(self) -> tuple[list[TodoItem], int, list[Any]]:
        tasks = self._tasks()
        health = self._source_health()
        by_source = {record.source: record for record in health}
        stale_count = sum(
            not self._task_is_verified(task, by_source) for task in tasks
        )
        # Stale data is a display state, not permission to erase work. Keep every
        # open loop visible and counted while making its freshness explicit.
        return tasks, stale_count, health

    def _completed_tasks(self) -> list[TodoItem]:
        if not self.state_path.is_file():
            return []
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        entries = raw.get("tasks", []) if isinstance(raw, dict) else []
        completed: list[TodoItem] = []
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict) or entry.get("status") != "done":
                continue
            try:
                completed.append(todo_item_from_dict(entry))
            except (TypeError, ValueError):
                continue
        return sorted(completed, key=lambda task: task.last_seen, reverse=True)[:30]

    def _safe_draft(self, task: TodoItem) -> str:
        roots = [self.drafts_dir.resolve()]
        bodies: list[str] = []
        raw_paths = task.draft_paths or ([task.draft_path] if task.draft_path else [])
        for raw in raw_paths:
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                candidate = self.drafts_dir / candidate
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if not any(resolved == root or root in resolved.parents for root in roots):
                continue
            try:
                body = resolved.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if body:
                bodies.append(body)
        return "\n\n".join(bodies)

    def _source_items(self, task: TodoItem) -> list[InboxItem]:
        if not self.inbox_dir.is_dir():
            return []
        try:
            snapshot_status, _ = self._snapshot_state(task)
            sources = (
                {"slack", "gmail", "calendar", "github"}
                if snapshot_status in {"saved", "cached", "stale"}
                else fresh_sources(self._source_health())
            )
            items = load_inbox(self.inbox_dir, sources=sources)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return []
        bundle = ContextEngine().task_context(task=task, items=items)
        by_id = {(item.source, item.id): item for item in items}
        return [
            by_id[(record.source, record.id)]
            for record in bundle.evidence
            if (record.source, record.id) in by_id
        ]

    def _source_item(self, task: TodoItem) -> InboxItem | None:
        items = self._source_items(task)
        return items[0] if items else None

    def _serialize_task(
        self,
        task: TodoItem,
        *,
        include_context: bool = True,
        focus_now: bool = False,
    ) -> dict[str, Any]:
        result = asdict(task)
        result["bucket"] = self._task_bucket(task)
        result["needs_review"] = self._task_needs_review(task)
        result["focus_now"] = focus_now
        result["recommendation_reason"] = self._recommendation_reason(task)
        result["verified_at"] = self._verified_at(task)
        result["confidence_label"] = (
            "Personalized recommendation"
            if task.preference_reason
            else
            "Confirmed by you"
            if task.review_state == "accepted"
            else "High confidence"
            if task.confidence >= 0.75
            else "Needs your review"
            if self._task_needs_review(task)
            else "Source-derived"
        )
        snapshot_status, snapshot_at = self._snapshot_state(task)
        personalized_rank = rank_task(task, snapshot_status=snapshot_status)
        result["personalized_score"] = personalized_rank.score
        result["ranking_reasons"] = list(personalized_rank.reasons)
        result["snapshot_status"] = snapshot_status
        result["snapshot_at"] = snapshot_at
        result.pop("draft_path", None)
        result.pop("draft_paths", None)
        result["draft"] = self._safe_draft(task)
        result["connector_actions"] = [
            action.public_dict()
            for action in connector_actions_for(task, has_draft=bool(result["draft"]))
        ]
        result["links"] = [
            link for link in task.links if urlsplit(link).scheme in {"http", "https"}
        ]
        if include_context:
            source_items = self._source_items(task)
            source = source_items[0] if source_items else None
            result["context"] = (
                {
                    "sender": source.sender,
                    "subject": source.subject,
                    "body": source.body[:4_000],
                    "channel": source.channel,
                    "timestamp": source.ts,
                    "url": source.url
                    if urlsplit(source.url).scheme in {"http", "https"}
                    else "",
                }
                if source is not None
                else None
            )
            result["context_bundle"] = ContextEngine().task_context(
                task=task,
                items=source_items,
            ).as_dict()
        return result

    def _snapshot_state(self, task: TodoItem) -> tuple[str, str]:
        snapshot_at = self._saved_snapshot_at(task)
        health_by_source = {record.source: record for record in self._source_health()}
        task_sources = set(task.sources or [task.source]) - {"manual"}
        cached_records = [
            health_by_source[source]
            for source in task_sources
            if source in health_by_source
            and health_by_source[source].status in {"error", "unavailable"}
        ]
        if snapshot_at:
            return "saved", snapshot_at
        elif cached_records:
            return "cached", max(
                (record.last_success_at for record in cached_records), default=""
            )
        elif not self._task_is_verified(task, health_by_source):
            return "stale", max(
                (
                    health_by_source[source].last_success_at
                    for source in task_sources
                    if source in health_by_source
                ),
                default="",
            )
        return "current", ""

    def _task_needs_review(self, task: TodoItem) -> bool:
        if task.source == "manual" or task.review_state == "accepted":
            return False
        return bool(self._saved_snapshot_at(task)) or task.confidence < 0.5

    def _task_bucket(self, task: TodoItem) -> str:
        """Assign every active task to exactly one dashboard bucket."""
        if self._task_needs_review(task):
            return "review"
        if task.status == "waiting":
            return "waiting"
        if task.source == "calendar":
            return "meetings"
        if task.needs_reply:
            return "replies"
        return "focus"

    def _recommendation_reason(self, task: TodoItem) -> str:
        if task.preference_reason:
            return task.preference_reason
        if self._task_needs_review(task):
            return "Carmen found a possible action, but the evidence is not strong enough to add it to your plan without you."
        if task.lifecycle_stage == "verifying":
            return "The connector accepted an action, but the source has not verified the resulting state yet."
        if task.status == "waiting":
            return "Someone else owns the next move; keep this visible for follow-up."
        if task.urgency == "now":
            return "Marked as urgent and ready for your attention now."
        if task.due:
            try:
                days = (datetime.fromisoformat(task.due).date() - datetime.now().astimezone().date()).days
            except ValueError:
                days = 99
            if days < 0:
                return "The due date has passed, so this needs a decision or correction."
            if days == 0:
                return "Due today."
            if days == 1:
                return "Due tomorrow."
        if task.needs_reply:
            return "A conversation appears to be waiting for your response."
        if task.source == "calendar":
            return "An upcoming meeting appears to need preparation or a decision."
        return "This is an open commitment assigned to you."

    def _verified_at(self, task: TodoItem) -> str:
        if task.source == "manual":
            return task.last_seen or task.first_seen or ""
        records = {record.source: record for record in self._source_health()}
        stamps = [
            records[source].last_success_at
            for source in set(task.sources or [task.source])
            if source in records and records[source].last_success_at
        ]
        return max(stamps, default=task.last_seen or task.first_seen or "")

    def _rank_key(self, task: TodoItem) -> tuple[Any, ...]:
        urgency = {"now": 0, "today": 1, "this_week": 2, "fyi": 3}
        due = task.due if re.fullmatch(r"\d{4}-\d{2}-\d{2}", task.due or "") else "9999-12-31"
        score = rank_task(
            task,
            snapshot_status=self._snapshot_state(task)[0],
        ).score
        return (
            1 if self._task_needs_review(task) else 0,
            1 if task.status == "waiting" else 0,
            -score,
            urgency.get(task.urgency, 4),
            -task.priority_adjustment,
            due,
            task.title.casefold(),
        )

    def overview(self) -> dict[str, Any]:
        active, stale_count, health = self._verified_tasks()
        completed = self._completed_tasks()
        now = datetime.now().astimezone()
        source_counts: dict[str, int] = {}
        for task in active:
            source_counts[task.source] = source_counts.get(task.source, 0) + 1
        modified = max(
            (
                path.stat().st_mtime
                for path in (self.todos_path, self.state_path)
                if path.is_file()
            ),
            default=0,
        )
        saved_count = sum(bool(self._saved_snapshot_at(task)) for task in active)
        source_health = dashboard_source_health(
            health,
            task_generated_at=self._task_generated_at(),
            stale_task_count=stale_count,
        )
        available_saved = self._available_saved_snapshot_count()
        if saved_count:
            source_health["message"] += (
                f"; showing {saved_count} clearly labeled saved snapshot item(s)"
            )
        source_health["saved_snapshot_count"] = saved_count
        source_health["saved_available_count"] = available_saved
        active = sorted(active, key=self._rank_key)
        bucket_counts = {"focus": 0, "replies": 0, "meetings": 0, "waiting": 0, "review": 0}
        for task in active:
            bucket_counts[self._task_bucket(task)] += 1
        focus_ids = {
            task.id
            for task in [
                item
                for item in active
                if not self._task_needs_review(item) and item.status != "waiting"
            ][:3]
        }
        return {
            "user": {
                "name": config_value("CARMEN_USER_NAME", "user_name", "there"),
                "provider": self.model_provider,
                "model": self.model_name,
                "adapter": self._adapter_label(),
            },
            "personalization": personalization_status().public_dict(),
            "date": f"{now:%A, %B} {now.day}",
            "last_updated": (
                datetime.fromtimestamp(modified).astimezone().isoformat(timespec="minutes")
                if modified
                else ""
            ),
            "counts": {
                "open": len(active),
                **bucket_counts,
                "completed": len(completed),
            },
            "source_counts": source_counts,
            "refresh": dict(self._refresh_status),
            "maintenance": dict(self._maintenance_status),
            "capabilities": {
                "task_execution": bool(
                    config_value("CARMEN_ACTION_COMMAND", "action_command", "")
                ),
            },
            "source_health": source_health,
            "quality": self.telemetry.summary(),
            "tasks": [
                self._serialize_task(
                    task,
                    focus_now=task.id in focus_ids,
                )
                for task in active
            ],
            "completed": [self._serialize_task(task, include_context=False) for task in completed],
        }

    def _saved_snapshot_candidates(self) -> list[InboxItem]:
        actionable, _ = self._saved_snapshot_partition()
        return actionable

    def _saved_snapshot_partition(self) -> tuple[list[InboxItem], list[TodoItem]]:
        try:
            items = load_inbox(
                self.inbox_dir,
                sources={"slack", "gmail", "calendar", "github"},
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return [], []
        return partition_source_items(items)

    @staticmethod
    def _saved_snapshot_at(task: TodoItem) -> str:
        if task.completion_evidence.startswith(_SAVED_SNAPSHOT_PREFIX):
            return task.completion_evidence[len(_SAVED_SNAPSHOT_PREFIX):].strip()
        return ""

    def _available_saved_snapshot_count(self) -> int:
        existing_ids = {
            source_id
            for task in self._tasks()
            for source_id in (task.source_ids or [f"{task.source}:{task.id}"])
        }
        return sum(
            f"{item.source}:{item.id}" not in existing_ids
            for item in self._saved_snapshot_candidates()
        )

    def seed_saved_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._validate_keys(payload, set(), "saved snapshot request")
        health = self._source_health()
        authoritative = authoritative_sources(health)
        source_refreshes = {
            record.source: refreshed
            for record in health
            if (refreshed := parse_stamp(record.last_success_at)) is not None
        }
        # GitHub and Calendar collectors return exhaustive inventories, so their
        # successful snapshots can replace prior tasks. Slack and Gmail collection
        # is deliberately bounded; absence there is never evidence that an open
        # loop disappeared or was completed.
        candidates, deterministic_done = self._saved_snapshot_partition()
        resolved_source_ids = {
            source_id
            for task in deterministic_done
            for source_id in (task.source_ids or [f"{task.source}:{task.id}"])
        }
        current_candidate_ids = {
            f"{item.source}:{item.id}"
            for item in candidates
            if item.source in authoritative
        }
        replaceable_sources = authoritative & {"github", "calendar"}
        tasks = [
            task
            for task in self._tasks()
            if not (set(task.sources or [task.source]) & replaceable_sources)
            and not (
                set(task.source_ids or [f"{task.source}:{task.id}"])
                & resolved_source_ids
            )
            and not (
                set(task.source_ids or [f"{task.source}:{task.id}"])
                & current_candidate_ids
            )
        ]
        tasks.extend(deterministic_done)
        existing_ids = {
            source_id
            for task in tasks
            for source_id in (task.source_ids or [f"{task.source}:{task.id}"])
        }
        added = 0
        current_added = 0
        for item in candidates:
            source_id = f"{item.source}:{item.id}"
            if source_id in existing_ids:
                continue
            is_current = item.source in authoritative
            digest = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:12]
            reason = str(item.metadata.get("reason") or item.body).strip()[:500]
            reply_kinds = {
                "direct_ask", "dm_follow_up", "event_response", "mention", "reply",
            }
            due = str(item.metadata.get("deadline_at") or "").strip()
            if item.source == "calendar":
                event_at = parse_stamp(item.ts)
                if event_at is not None:
                    due = event_at.astimezone().date().isoformat()
            if not due:
                match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", item.body)
                due = match.group(1) if match else ""
            urgency = "this_week"
            if due:
                try:
                    days_until = (
                        datetime.fromisoformat(due).date()
                        - datetime.now().astimezone().date()
                    ).days
                except ValueError:
                    days_until = 7
                if days_until <= 1:
                    urgency = "today"
            next_action = str(item.metadata.get("next_action") or "").strip().lower()
            action_prefixes = {
                "merge": "Merge PR",
                "review": "Review PR",
                "address_review": "Address review feedback",
                "fix_ci": "Fix CI",
                "resolve_conflict": "Resolve merge conflict",
                "follow_up": "Follow up",
                "prepare": "Prepare for",
                "respond": "Respond to",
                "reply": "Reply to",
            }
            title = item.subject or item.body or "Saved work item"
            if prefix := action_prefixes.get(next_action):
                title = f"{prefix}: {title}"
            tasks.append(
                TodoItem(
                    source=item.source,
                    id=f"{'work' if is_current else 'saved'}-{digest}",
                    title=title[:120],
                    why=reason,
                    urgency=urgency,
                    needs_reply=(
                        item.kind in reply_kinds or next_action in {"reply", "respond"}
                    ),
                    due=due,
                    links=[item.url] if item.url else [],
                    sources=[item.source],
                    source_ids=[source_id],
                    people=list(item.participants) or ([item.sender] if item.sender else []),
                    confidence=0.75 if is_current else 0.5,
                    status=item.status,
                    related_keys=list(item.related_keys),
                    first_seen=item.ts,
                    last_seen=item.ts,
                    carryover=not is_current,
                    completion_evidence=(
                        "" if is_current else f"{_SAVED_SNAPSHOT_PREFIX}{item.ts}"
                    ),
                )
            )
            existing_ids.add(source_id)
            added += 1
            current_added += int(is_current)
        personalize_calendar_tasks(
            tasks,
            candidates,
            path=self.calendar_preferences_path,
        )
        active, progress = reconcile_state(
            tasks,
            self.state_path,
            # Missing from an exhaustive GitHub/Calendar inventory is meaningful.
            # Missing from a bounded Slack/Gmail search is not completion evidence.
            authoritative_sources=authoritative & {"github", "calendar"},
            source_last_success=source_refreshes,
        )
        dump_todos(active, self.todos_path)
        write_morning_brief(
            active,
            progress,
            self.brief_path,
            coverage=fresh_sources(health),
        )
        if current_added:
            message = f"Imported {current_added} current task(s) from refreshed sources."
            historical = added - current_added
            if historical:
                message += f" Also restored {historical} labeled saved task(s)."
        elif added:
            message = f"Imported {added} saved task(s). They remain labeled as historical."
        else:
            message = "All actionable source tasks are already shown."
        return {"message": message, "overview": self.overview()}

    def add(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._validate_keys(payload, {"title", "urgency", "due", "remind_at"}, "task")
        for field in ("title", "urgency", "due", "remind_at"):
            if field in payload and not isinstance(payload[field], str):
                raise ApiError(f"Task {field} must be text.")
        tasks = self._tasks()
        message = add_task(
            tasks,
            str(payload.get("title", "")),
            urgency=str(payload.get("urgency", "this_week")),
            due=str(payload.get("due", "")),
            remind_at=str(payload.get("remind_at", "")),
            todos_path=self.todos_path,
            state_path=self.state_path,
            brief_path=self.brief_path,
        )
        return {"message": message, "overview": self.overview()}

    def mutate_task(
        self, task_id: str, action: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        expected = (
            {"minutes"}
            if action == "snooze"
            else {"at"}
            if action == "remind"
            else {"title", "why", "urgency", "due", "remind_at"}
            if action == "edit"
            else {"action_id"}
            if action == "execute"
            else set()
        )
        self._validate_keys(payload, expected, "task action")
        if action == "snooze" and (
            "minutes" in payload
            and (type(payload["minutes"]) is not int)
        ):
            raise ApiError("Snooze minutes must be an integer.")
        if action == "remind" and not isinstance(payload.get("at", ""), str):
            raise ApiError("Reminder time must be text.")
        if action == "edit":
            if not payload:
                raise ApiError("At least one editable task field is required.")
            for field, value in payload.items():
                if not isinstance(value, str):
                    raise ApiError(f"Task {field} must be text.")
        if action == "execute" and (
            not isinstance(payload.get("action_id", ""), str)
            or len(payload.get("action_id", "")) > 128
        ):
            raise ApiError("Connector action id must be text up to 128 characters.")
        tasks = self._tasks()
        task = resolve_task(tasks, task_id)
        if action == "verify-context":
            result = self.start_refresh({})
            sources = ", ".join(
                _SOURCE_LABELS.get(source, source.title())
                for source in (task.sources or [task.source])
            )
            result["message"] = (
                f"Refreshing connected sources to verify {sources} context."
            )
            return result
        if action in {"usually-skip", "usually-attend"}:
            if task.source != "calendar":
                raise ApiError("Meeting preferences can only be set on Calendar tasks.")
            source_item = self._source_item(task)
            organizer = (
                str(source_item.metadata.get("organizer") or source_item.sender).strip()
                if source_item is not None
                else ""
            )
            disposition = "usually_skip" if action == "usually-skip" else "usually_attend"
            try:
                record_calendar_preference(
                    source_item.subject if source_item is not None else task.title,
                    organizer=organizer,
                    disposition=disposition,
                    path=self.calendar_preferences_path,
                )
            except CalendarPreferenceError as exc:
                raise ApiError(f"Could not save meeting preference: {exc}") from exc
            if source_item is not None:
                personalize_calendar_tasks(
                    [task], [source_item], path=self.calendar_preferences_path
                )
            elif disposition == "usually_skip":
                task.priority_adjustment = -45
                if "urgency" not in task.edited_fields:
                    task.urgency = "fyi"
                task.confidence = min(task.confidence, 0.35)
                task.preference_reason = (
                    "Personalized as optional: you marked this as a meeting you usually skip."
                )
            else:
                task.priority_adjustment = 10
                task.preference_reason = (
                    "Personalized from your feedback: you usually attend this meeting."
                )
            task.review_state = "accepted"
            active, progress = reconcile_state(tasks, self.state_path)
            dump_todos(active, self.todos_path)
            write_morning_brief(active, progress, self.brief_path)
            label = "optional" if disposition == "usually_skip" else "usually attended"
            return {
                "message": f"Saved this meeting series as {label}.",
                "overview": self.overview(),
            }
        if action == "accept":
            task.review_state = "accepted"
            task.confidence = max(task.confidence, 0.75)
            active, progress = reconcile_state(tasks, self.state_path)
            dump_todos(active, self.todos_path)
            write_morning_brief(active, progress, self.brief_path)
            message = f"Added to your plan: {task.title}"
        elif action in {"not-a-task", "not-mine", "already-handled"}:
            evidence = {
                "not-a-task": "user correction: not a task",
                "not-mine": "user correction: not mine",
                "already-handled": "user correction: already handled",
            }[action]
            mark_complete(
                tasks,
                task,
                todos_path=self.todos_path,
                state_path=self.state_path,
                brief_path=self.brief_path,
                habits_path=self.habits_path,
                evidence=evidence,
                resolution_mode="dismissed",
                learn_dismissal=action in {"not-a-task", "not-mine"},
            )
            message = {
                "not-a-task": "Removed. Carmen will use this correction when ranking similar items.",
                "not-mine": "Removed from your plan as someone else's responsibility.",
                "already-handled": "Marked as already handled.",
            }[action]
        elif action == "complete":
            message = mark_complete(
                tasks,
                task,
                todos_path=self.todos_path,
                state_path=self.state_path,
                brief_path=self.brief_path,
                habits_path=self.habits_path,
                evidence="User marked the task done locally in the app.",
                resolution_mode="local",
            )
        elif action in {"start", "wait", "reopen"}:
            if action == "start":
                task.status = "open"
                task.lifecycle_stage = "in_progress"
                message = f"Started: {task.title}"
            elif action == "wait":
                task.status = "waiting"
                task.lifecycle_stage = "waiting"
                message = f"Moved to waiting: {task.title}"
            else:
                task.status = "open"
                task.lifecycle_stage = "open"
                task.resolution_mode = ""
                message = f"Reopened: {task.title}"
            task.last_seen = datetime.now(timezone.utc).isoformat()
            active, progress = reconcile_state(tasks, self.state_path)
            dump_todos(active, self.todos_path)
            write_morning_brief(active, progress, self.brief_path)
        elif action == "snooze":
            try:
                minutes = int(payload.get("minutes", 60))
            except (TypeError, ValueError) as exc:
                raise CommandError("Snooze minutes must be a number.") from exc
            message = snooze_task(
                tasks,
                task,
                minutes=minutes,
                todos_path=self.todos_path,
                state_path=self.state_path,
                brief_path=self.brief_path,
            )
        elif action == "remind":
            message = set_task_reminder(
                tasks,
                task,
                str(payload.get("at", "")),
                todos_path=self.todos_path,
                state_path=self.state_path,
                brief_path=self.brief_path,
            )
        elif action == "edit":
            message = update_task(
                tasks,
                task,
                title=payload.get("title"),
                why=payload.get("why"),
                urgency=payload.get("urgency"),
                due=payload.get("due"),
                remind_at=payload.get("remind_at"),
                todos_path=self.todos_path,
                state_path=self.state_path,
                brief_path=self.brief_path,
            )
        elif action == "execute":
            action_command = config_value(
                "CARMEN_ACTION_COMMAND", "action_command", ""
            )
            if not action_command:
                raise ApiError(
                    "Configure an action adapter in Settings before running tasks."
                )
            message = execute_task(
                tasks,
                task,
                executor_command=action_command,
                todos_path=self.todos_path,
                state_path=self.state_path,
                brief_path=self.brief_path,
                habits_path=self.habits_path,
                receipts_path=self.receipts_path,
                action_id=str(payload.get("action_id", "")).strip(),
            )
        else:
            raise ApiError("Unknown task action.", HTTPStatus.NOT_FOUND)
        return {"message": message, "overview": self.overview()}

    def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        chat_started = time.monotonic()
        self._validate_keys(payload, {"message", "task_id", "history"}, "chat request")
        raw_message = payload.get("message", "")
        if not isinstance(raw_message, str):
            raise ApiError("Message must be text.")
        message = " ".join(raw_message.split())
        if not message:
            raise ApiError("Message cannot be empty.")
        if len(message) > 4_000:
            raise ApiError("Message must be 4,000 characters or fewer.")

        context_record: dict[str, Any] = {}
        task: TodoItem | None = None
        source: InboxItem | None = None
        source_items: list[InboxItem] = []
        workspace_tasks, _, _ = self._verified_tasks()
        workspace_context = []
        for item in workspace_tasks[:30]:
            snapshot_status, snapshot_at = self._snapshot_state(item)
            workspace_context.append({
                "id": item.id,
                "title": item.title,
                "why": item.why,
                "source": item.source,
                "urgency": item.urgency,
                "status": item.status,
                "needs_reply": item.needs_reply,
                "due": item.due,
                "people": item.people[:5],
                "priority_adjustment": item.priority_adjustment,
                "preference_reason": item.preference_reason,
                "last_seen": item.last_seen,
                "snapshot_status": snapshot_status,
                "snapshot_at": snapshot_at,
            })
        raw_task_id = payload.get("task_id", "")
        if not isinstance(raw_task_id, str) or len(raw_task_id) > 512:
            raise ApiError("Task id must be text no longer than 512 characters.")
        task_id = raw_task_id.strip()
        if task_id:
            task = resolve_task(workspace_tasks, task_id)
            source_items = self._source_items(task)
            source = source_items[0] if source_items else None
            snapshot_status, snapshot_at = self._snapshot_state(task)
            task_context = ContextEngine().task_context(
                task=task,
                items=source_items,
            ).as_dict()
            context_record = {
                "title": task.title,
                "why": task.why,
                "source": task.source,
                "people": task.people,
                "due": task.due,
                "snapshot_status": snapshot_status,
                "snapshot_at": snapshot_at,
                "evidence_bundle": task_context,
            }

        history: list[dict[str, str]] = []
        raw_history = payload.get("history", [])
        if not isinstance(raw_history, list) or len(raw_history) > 12:
            raise ApiError("Chat history must be an array of at most 12 messages.")
        for index, entry in enumerate(raw_history):
            if not isinstance(entry, dict) or set(entry) != {"role", "content"}:
                raise ApiError(f"Chat history item {index + 1} has an invalid shape.")
            if entry["role"] not in {"user", "assistant"}:
                raise ApiError(f"Chat history item {index + 1} has an invalid role.")
            if not isinstance(entry["content"], str) or len(entry["content"]) > 4_000:
                raise ApiError(
                    f"Chat history item {index + 1} content must be text up to 4,000 characters."
                )
            content = entry["content"].strip()
            if content:
                history.append({"role": entry["role"], "content": content})

        draft_requested = task is not None and bool(_DRAFT_INTENT_RE.search(message))
        if task is None and not history and _GREETING_RE.fullmatch(message):
            return self._chat_result(
                reply="hey! what can I help you with?",
                task=None,
                source=None,
                evidence_count=0,
                workspace_task_count=len(workspace_context),
                fallback_used=False,
            )

        if task is not None and source is None and draft_requested:
            reply = (
                f"“{task.title}” is a personal task, not a message, so there isn't "
                "a response to draft. I can help break it into concrete next steps instead."
                if task.source == "manual"
                else (
                    "I don't have the original source message, so I can't draft a "
                    "grounded response yet. Refresh the connected source first."
                )
            )
            return self._chat_result(
                reply=reply,
                task=task,
                source=source,
                evidence_count=len(source_items),
                workspace_task_count=len(workspace_context),
                fallback_used=True,
            )

        selected_prompt_task = next(
            (
                record for record in workspace_context
                if task is not None and record["id"] == task.id
            ),
            None,
        )
        evidence_records = (
            context_record.get("evidence_bundle", {}).get("evidence", [])
            if context_record
            else []
        )
        packet = build_context_packet(
            message=message,
            workspace_tasks=workspace_context,
            selected_task=selected_prompt_task,
            evidence=evidence_records,
            max_tokens=3_000,
        )

        try:
            with self._model_lock:
                voice_mode = config_value(
                    "CARMEN_VOICE_MODE", "voice_mode", "profile"
                ).lower()
                use_voice_adapter = bool(
                    draft_requested
                    and self.model_provider == "mlx"
                    and voice_mode == "lora"
                    and (DEFAULT_ADAPTER_DIR / "adapters.safetensors").is_file()
                )
                if (
                    self._session is None
                    or self._session_uses_adapter != use_voice_adapter
                ):
                    self._session = (
                        self.session_factory(
                            provider_name=self.model_provider,
                            model_name=self.model_name,
                            # LoRA is isolated to drafting. General planning stays on the
                            # instruction-following base model so style tuning cannot weaken
                            # task reasoning or factual grounding.
                            base_only=(not use_voice_adapter)
                            if self.model_provider == "mlx"
                            else self.base_only,
                        )
                        if self.session_factory_uses_settings
                        else self.session_factory()
                    )
                    self._session_uses_adapter = use_voice_adapter
                if draft_requested and source is not None:
                    system_prompt = (
                        self._session.system_prompt
                        + " You are drafting inside a private local work dashboard. "
                        "Use the style profile only for tone. Answer the source message "
                        "directly and never invent facts, actions, dates, or commitments. "
                        "Return only a coherent message the user could send."
                    )
                else:
                    system_prompt = (
                        "You are Carmen's private local work copilot. Help her reason, "
                        "prioritize, summarize, and plan from the supplied workspace context. "
                        "Be concise, warm, and direct, but prioritize a correct useful answer "
                        "over imitating Slack phrases. Never claim an external action was "
                        "performed. Respond in clear English unless Carmen asks for another "
                        "language. Ground task-specific answers only in supplied records. "
                        "If context is missing, say exactly what is missing. Return a coherent, "
                        "complete response without control tokens or metadata."
                    )
                session_context = [packet.text]
                messages = ContextEngine().assemble_prompt(
                    system_prompt=system_prompt,
                    session_context=session_context,
                    transcript=history,
                    current_user_message=message,
                )
                reply = ""
                fallback_used = False
                input_text = (
                    message
                    + packet.text
                )
                for attempt in range(2):
                    attempt_messages = list(messages)
                    if attempt:
                        attempt_messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "The prior output was malformed. Answer again in coherent "
                                    "English using only the supplied context."
                                ),
                            }
                        )
                    reply = self._session.generate(
                        attempt_messages,
                        max_tokens=500 if draft_requested else 800,
                        temperature=0.55 if attempt == 0 else 0.15,
                        top_p=0.9 if attempt == 0 else 0.8,
                        sanitize_emoji=True,
                    ).strip()
                    if self._response_is_usable(reply, input_text):
                        break
                if not self._response_is_usable(reply, input_text):
                    fallback_used = True
                    if task is not None and any(
                        word in message.lower() for word in ("draft", "reply", "respond")
                    ):
                        reply = _safe_fallback(source) if source is not None else (
                            "I don't have the original source message, so I can't draft a "
                            "grounded response yet. Refresh the connected source first."
                        )
                    elif task is not None:
                        reply = (
                            f"I found the task “{task.title},” but the local model returned an "
                            "invalid answer. Refresh its source context or try the question again."
                        )
                    else:
                        reply = (
                            "I couldn't produce a reliable answer from the local model. Your "
                            "work data was not changed; please try again."
                        )
        except (Exception, SystemExit) as exc:
            self.telemetry.record(
                "chat",
                status="error",
                intent=packet.intent,
                duration_ms=round((time.monotonic() - chat_started) * 1_000, 1),
                input_tokens=packet.estimated_tokens,
                output_chars=0,
                fallback_used=True,
                truncated=packet.truncated,
            )
            raise ApiError(
                f"The configured local model is unavailable: {exc}",
                HTTPStatus.SERVICE_UNAVAILABLE,
            ) from exc
        self.telemetry.record(
            "chat",
            status="fallback" if fallback_used else "ok",
            intent=packet.intent,
            duration_ms=round((time.monotonic() - chat_started) * 1_000, 1),
            input_tokens=packet.estimated_tokens,
            output_chars=len(reply),
            fallback_used=fallback_used,
            truncated=packet.truncated,
        )
        result = self._chat_result(
            reply=reply,
            task=task,
            source=source,
            evidence_count=len(source_items),
            workspace_task_count=len(workspace_context),
            fallback_used=fallback_used,
        )
        result["meta"]["context_packet"] = {
            "intent": packet.intent,
            "estimated_tokens": packet.estimated_tokens,
            "task_count": len(packet.task_ids),
            "evidence_count": len(packet.evidence_ids),
            "truncated": packet.truncated,
        }
        return result

    def _chat_result(
        self,
        *,
        reply: str,
        task: TodoItem | None,
        source: InboxItem | None,
        evidence_count: int,
        workspace_task_count: int,
        fallback_used: bool,
    ) -> dict[str, Any]:
        return {
            "reply": reply,
            "meta": {
                "provider": self.model_provider,
                "model": self.model_name,
                "adapter": self._adapter_label(),
                "context": (
                    {
                        "task": task.title,
                        "source": task.source,
                        "source_message_loaded": source is not None,
                        "evidence_count": evidence_count,
                        "workspace_task_count": workspace_task_count,
                    }
                    if task is not None
                    else {
                        "task": "",
                        "source": "workspace",
                        "source_message_loaded": False,
                        "evidence_count": 0,
                        "workspace_task_count": workspace_task_count,
                    }
                ),
                "quality_fallback": fallback_used,
            },
        }

    @staticmethod
    def _validate_keys(payload: dict[str, Any], allowed: set[str], label: str) -> None:
        unknown = set(payload) - allowed
        if unknown:
            raise ApiError(
                f"Unknown {label} field(s): {', '.join(sorted(unknown))}."
            )

    @staticmethod
    def _response_is_usable(reply: str, input_text: str) -> bool:
        return response_is_usable(reply, input_text, max_chars=8_000)


def _asset_bytes(name: str, token: str) -> tuple[bytes, str]:
    resource = files(ASSET_PACKAGE).joinpath(name)
    content = resource.read_bytes()
    if name == "index.html":
        content = content.replace(b"__CARMEN_SESSION_TOKEN__", token.encode("ascii"))
    content_type = {
        "index.html": "text/html; charset=utf-8",
        "app.css": "text/css; charset=utf-8",
        "app.js": "text/javascript; charset=utf-8",
        "manifest.webmanifest": "application/manifest+json; charset=utf-8",
        "favicon.svg": "image/svg+xml",
    }[name]
    return content, content_type


def build_handler(
    service: DashboardService,
    *,
    session_token: str,
    verbose: bool = False,
) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "CarmenLocal/0.1"
        sys_version = ""

        def log_message(self, format: str, *args: object) -> None:
            if verbose:
                super().log_message(format, *args)

        def _host_is_local(self) -> bool:
            raw = self.headers.get("Host", "")
            host = raw
            if raw.startswith("[") and "]" in raw:
                host = raw[: raw.index("]") + 1]
            elif ":" in raw:
                host = raw.rsplit(":", 1)[0]
            return host.lower() in ALLOWED_HOSTS

        def _headers(self, status: int, content_type: str, length: int) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'self'",
            )
            self.end_headers()

        def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
            self._headers(status, content_type, len(body))
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, value: Any, status: int = 200) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self._send(body, "application/json; charset=utf-8", status)

        def _error(self, error: Exception) -> None:
            status = error.status if isinstance(error, ApiError) else HTTPStatus.BAD_REQUEST
            self._json({"error": str(error)}, int(status))

        def _internal_error(self) -> None:
            self._json(
                {
                    "error": (
                        "The local app hit an unexpected error. Open Settings, run System "
                        "checks, and try again."
                    )
                },
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

        def _payload(self) -> dict[str, Any]:
            raw_length = self.headers.get("Content-Length", "0")
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise ApiError("Invalid request length.") from exc
            if length < 0 or length > 65_536:
                raise ApiError("Request is too large.", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            raw = self.rfile.read(length)
            if not raw:
                return {}
            try:
                payload = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ApiError("Request body must be valid JSON.") from exc
            if not isinstance(payload, dict):
                raise ApiError("Request body must be a JSON object.")
            return payload

        def _authorized_write(self) -> bool:
            return secrets.compare_digest(
                self.headers.get("X-Carmen-Session", ""), session_token
            )

        def _dispatch_get(self) -> None:
            path = urlsplit(self.path).path
            if path == "/api/overview":
                self._json(service.overview())
                return
            if path == "/api/status":
                self._json(service.operation_status())
                return
            assets = {
                "/": "index.html",
                "/index.html": "index.html",
                "/assets/app.css": "app.css",
                "/assets/app.js": "app.js",
                "/manifest.webmanifest": "manifest.webmanifest",
                "/favicon.svg": "favicon.svg",
            }
            name = assets.get(path)
            if name is None:
                raise ApiError("Not found.", HTTPStatus.NOT_FOUND)
            body, content_type = _asset_bytes(name, session_token)
            self._send(body, content_type)

        def do_HEAD(self) -> None:
            self.do_GET()

        def do_GET(self) -> None:
            if not self._host_is_local():
                self._json({"error": "Localhost access only."}, HTTPStatus.FORBIDDEN)
                return
            try:
                self._dispatch_get()
            except (ApiError, CommandError, OSError, json.JSONDecodeError) as exc:
                self._error(exc)
            except Exception:
                self._internal_error()

        def do_POST(self) -> None:
            if not self._host_is_local():
                self._json({"error": "Localhost access only."}, HTTPStatus.FORBIDDEN)
                return
            if not self._authorized_write():
                self._json({"error": "Invalid local session."}, HTTPStatus.FORBIDDEN)
                return
            try:
                payload = self._payload()
                path = urlsplit(self.path).path
                if path == "/api/tasks":
                    self._json(service.add(payload), HTTPStatus.CREATED)
                    return
                if path == "/api/chat":
                    self._json(service.chat(payload))
                    return
                if path == "/api/refresh":
                    self._json(service.start_refresh(payload), HTTPStatus.ACCEPTED)
                    return
                if path == "/api/seed-snapshot":
                    self._json(service.seed_saved_snapshot(payload), HTTPStatus.CREATED)
                    return
                if path == "/api/settings":
                    self._json(service.save_settings(payload))
                    return
                if path == "/api/automation":
                    self._json(service.save_automation(payload), HTTPStatus.ACCEPTED)
                    return
                if path == "/api/diagnostics":
                    self._json(service.run_diagnostics(payload))
                    return
                if path == "/api/operations":
                    self._json(service.start_maintenance(payload), HTTPStatus.ACCEPTED)
                    return
                match = TASK_ROUTE.fullmatch(path)
                if match and match.group("action"):
                    self._json(
                        service.mutate_task(
                            unquote(match.group("task_id")),
                            str(match.group("action")),
                            payload,
                        )
                    )
                    return
                raise ApiError("Not found.", HTTPStatus.NOT_FOUND)
            except (ApiError, CommandError, OSError, json.JSONDecodeError) as exc:
                self._error(exc)
            except Exception:
                self._internal_error()

        def do_OPTIONS(self) -> None:
            self._json({"error": "Cross-origin requests are not supported."}, HTTPStatus.METHOD_NOT_ALLOWED)

    return DashboardHandler


def build_server(
    service: DashboardService | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    session_token: str | None = None,
    verbose: bool = False,
) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("The local UI may only bind to a loopback address.")
    token = session_token or secrets.token_urlsafe(32)
    handler = build_handler(
        service or DashboardService(), session_token=token, verbose=verbose
    )
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Open the private Carmen Assistant local dashboard."
    )
    parser.add_argument("--host", choices=("127.0.0.1", "localhost"), default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--provider",
        choices=("auto", "mlx", "ollama", "openai-compatible"),
        default="auto",
        help="Override the configured inference backend for dashboard chat.",
    )
    parser.add_argument("--model", default=None, help="Override the configured model.")
    parser.add_argument(
        "--base-only",
        action="store_true",
        help="Use the MLX base model without a LoRA adapter.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.port < 0 or args.port > 65_535:
        raise SystemExit("--port must be between 0 and 65535.")
    service = DashboardService(
        session_factory=load_session,
        model_provider=(
            default_local_provider() if args.provider == "auto" else args.provider
        ),
        model_name=args.model,
        base_only=args.base_only,
        session_factory_uses_settings=True,
    )
    try:
        server = build_server(
            service,
            host=args.host,
            port=args.port,
            verbose=args.verbose,
        )
    except OSError as exc:
        raise SystemExit(f"Could not start the local app: {exc}") from exc
    address = server.server_address
    rendered_host = f"[{address[0]}]" if ":" in address[0] else address[0]
    url = f"http://{rendered_host}:{address[1]}/"
    print(f"Carmen Assistant is running locally at {url}", flush=True)
    service.ensure_daily_schedule()
    print("Press Ctrl+C to stop it.", flush=True)
    if not args.no_browser:
        threading.Timer(0.35, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Carmen Assistant.", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
