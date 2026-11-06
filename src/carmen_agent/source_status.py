"""Freshness and coverage state for local connected-source snapshots."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from carmen_agent.skills.schemas import Source, parse_stamp


CONNECTED_SOURCES: tuple[Source, ...] = ("slack", "gmail", "calendar", "github")
SOURCE_STATUS_FILENAME = "_source-status.json"
DEFAULT_SOURCE_MAX_AGE_HOURS = 48
MAX_SOURCE_STATUS_BYTES = 256 * 1024
_SOURCE_LABELS = {
    "slack": "Slack",
    "gmail": "Gmail",
    "calendar": "Calendar",
    "github": "GitHub",
}


def _natural_source_list(sources: Iterable[Source]) -> str:
    labels = [_SOURCE_LABELS[source] for source in sources]
    if len(labels) < 2:
        return "".join(labels)
    if len(labels) == 2:
        return " and ".join(labels)
    return ", ".join(labels[:-1]) + ", and " + labels[-1]


def configured_max_age_hours() -> int:
    raw = os.environ.get("CARMEN_SOURCE_MAX_AGE_HOURS", "").strip()
    if not raw:
        return DEFAULT_SOURCE_MAX_AGE_HOURS
    try:
        return max(1, min(168, int(raw)))
    except ValueError:
        return DEFAULT_SOURCE_MAX_AGE_HOURS


@dataclass(frozen=True)
class SourceSnapshotHealth:
    source: Source
    status: str
    detail: str
    snapshot_exists: bool
    last_attempt_at: str
    last_success_at: str
    item_count: int
    transport: str
    duration_ms: float
    age_hours: float | None
    fresh: bool
    authoritative: bool

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        if value["age_hours"] is not None:
            value["age_hours"] = round(float(value["age_hours"]), 1)
        return value


def _load_status(inbox_dir: Path) -> dict[str, Any]:
    path = inbox_dir / SOURCE_STATUS_FILENAME
    if not path.is_file():
        return {}
    if path.stat().st_size > MAX_SOURCE_STATUS_BYTES:
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict) or set(raw) - {"version", "updated_at", "sources"}:
        return {}
    if raw.get("version") != 1 or not isinstance(raw.get("updated_at", ""), str):
        return {}
    sources = raw.get("sources")
    if not isinstance(sources, dict) or set(sources) - set(CONNECTED_SOURCES):
        return {}
    allowed = {
        "status", "detail", "last_attempt_at", "last_success_at", "item_count",
        "transport", "duration_ms",
    }
    for record in sources.values():
        if not isinstance(record, dict) or set(record) - allowed:
            return {}
        if record.get("status") not in {"ok", "empty", "error", "unavailable", "running"}:
            return {}
        if any(
            not isinstance(record.get(name, ""), str)
            for name in ("detail", "last_attempt_at", "last_success_at")
        ):
            return {}
        count = record.get("item_count", 0)
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 500:
            return {}
        duration = record.get("duration_ms", 0)
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration < 0:
            return {}
        if not isinstance(record.get("transport", ""), str):
            return {}
    return raw


def inspect_source_snapshots(
    inbox_dir: Path,
    *,
    now: datetime | None = None,
    max_age_hours: int | None = None,
    expected: Iterable[Source] = CONNECTED_SOURCES,
) -> list[SourceSnapshotHealth]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    maximum = max_age_hours or configured_max_age_hours()
    payload = _load_status(inbox_dir)
    raw_sources = payload.get("sources") if isinstance(payload.get("sources"), dict) else {}
    results: list[SourceSnapshotHealth] = []

    for source in expected:
        snapshot = inbox_dir / f"{source}.json"
        exists = snapshot.is_file()
        record = raw_sources.get(source) if isinstance(raw_sources.get(source), dict) else {}
        status = str(record.get("status") or ("legacy" if exists else "missing"))
        detail = str(record.get("detail") or "")
        last_attempt_at = str(record.get("last_attempt_at") or "")
        last_success_at = str(record.get("last_success_at") or "")
        success = parse_stamp(last_success_at)
        if success is None and exists:
            success = datetime.fromtimestamp(snapshot.stat().st_mtime, tz=timezone.utc)
            last_success_at = success.isoformat()
        age_hours = (
            max(0.0, (current.astimezone(timezone.utc) - success.astimezone(timezone.utc)).total_seconds() / 3600)
            if success is not None
            else None
        )
        fresh = bool(exists and age_hours is not None and age_hours <= maximum)
        authoritative = bool(
            fresh
            and record
            and status in {"ok", "empty"}
            and last_attempt_at
            and last_attempt_at == str(record.get("last_success_at") or "")
        )
        try:
            item_count = int(record.get("item_count", 0))
        except (TypeError, ValueError):
            item_count = 0
        results.append(
            SourceSnapshotHealth(
                source=source,
                status=status,
                detail=detail,
                snapshot_exists=exists,
                last_attempt_at=last_attempt_at,
                last_success_at=last_success_at,
                item_count=max(0, item_count),
                transport=str(record.get("transport") or "legacy"),
                duration_ms=max(0.0, float(record.get("duration_ms", 0))),
                age_hours=age_hours,
                fresh=fresh,
                authoritative=authoritative,
            )
        )
    return results


def fresh_sources(health: Iterable[SourceSnapshotHealth]) -> set[Source]:
    return {record.source for record in health if record.fresh}


def authoritative_sources(health: Iterable[SourceSnapshotHealth]) -> set[Source]:
    return {record.source for record in health if record.authoritative}


def dashboard_source_health(
    health: list[SourceSnapshotHealth],
    *,
    task_generated_at: datetime | None,
    hidden_task_count: int = 0,
    stale_task_count: int = 0,
) -> dict[str, Any]:
    stale = [record.source for record in health if record.snapshot_exists and not record.fresh]
    missing = [record.source for record in health if not record.snapshot_exists]
    degraded = [
        record.source
        for record in health
        if record.fresh and record.status in {"error", "unavailable"}
    ]
    failed = [
        record.source
        for record in health
        if record.status in {"error", "unavailable"}
    ]
    latest_success = max(
        (parsed for record in health if (parsed := parse_stamp(record.last_success_at)) is not None),
        default=None,
    )
    summary_behind = bool(
        latest_success is not None
        and (task_generated_at is None or task_generated_at.astimezone(timezone.utc) < latest_success.astimezone(timezone.utc))
    )
    syncing = [record.source for record in health if record.status == "running"]
    if stale or missing:
        level = "stale"
        title = "Work sources need a refresh"
    elif syncing:
        level = "warning"
        title = "Updating work sources"
    elif summary_behind:
        level = "stale"
        title = "Sources changed after this task list"
    elif degraded:
        level = "warning"
        title = "Some sources could not refresh"
    else:
        level = "ok"
        title = "Work sources are current"

    current = [record.source for record in health if record.authoritative]
    timed_out = [
        record.source
        for record in health
        if record.status == "error" and "timed out" in record.detail.lower()
    ]
    not_connected = [
        record.source for record in health if record.status == "unavailable"
    ]
    other_failed = [source for source in failed if source not in timed_out + not_connected]
    stale_only = [source for source in stale if source not in failed]

    details: list[str] = []
    if current:
        details.append(_natural_source_list(current) + " refreshed")
    if syncing:
        details.append(_natural_source_list(syncing) + " syncing")
    if timed_out:
        details.append(_natural_source_list(timed_out) + " timed out")
    if not_connected:
        details.append(_natural_source_list(not_connected) + " need connection")
    if other_failed:
        details.append(_natural_source_list(other_failed) + " could not refresh")
    if stale_only:
        details.append(_natural_source_list(stale_only) + " saved data is stale")
    if missing and not not_connected:
        details.append(_natural_source_list(missing) + " have no saved data")
    if degraded:
        details.append(_natural_source_list(degraded) + " are using cached data")
    if summary_behind:
        details.append("the task list has not processed the latest snapshots")
    if hidden_task_count:
        details.append(f"{hidden_task_count} unverified task(s) hidden")
    if stale_task_count:
        details.append(f"{stale_task_count} task(s) shown with stale labels")
    message = "; ".join(details) or "Slack, Gmail, Calendar, and GitHub are current."
    return {
        "level": level,
        "title": title,
        "message": message,
        "refresh_command": "uv run carmen today",
        "hidden_task_count": hidden_task_count,
        "stale_task_count": stale_task_count,
        "sources": [record.public_dict() for record in health],
    }
