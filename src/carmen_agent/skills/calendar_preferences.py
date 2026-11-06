"""Inspectable, private personalization for recurring Calendar work."""
from __future__ import annotations

import json
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from carmen_agent.paths import CALENDAR_PREFERENCES_PATH
from carmen_agent.skills.schemas import InboxItem, TodoItem

Disposition = Literal["usually_skip", "usually_attend"]
MAX_RULES = 500
MAX_PROFILE_BYTES = 1024 * 1024
_PREFIX_RE = re.compile(
    r"^(?:prepare\s+for|respond\s+to|rsvp\s+for|decide\s+on(?:\s+duplicate)?|"
    r"review)\s*[:\-]?\s+",
    re.IGNORECASE,
)
_URGENCY_ORDER = {"now": 0, "today": 1, "this_week": 2, "fyi": 3}


class CalendarPreferenceError(ValueError):
    pass


@dataclass(frozen=True)
class CalendarPreferenceRule:
    key: str
    title: str
    organizer: str
    disposition: Disposition
    source: str
    observations: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class CalendarPreferenceResult:
    priority_adjustment: int = 0
    urgency_cap: str = ""
    confidence_cap: float = 1.0
    reason: str = ""


def normalize_meeting_key(title: str) -> str:
    cleaned = _PREFIX_RE.sub("", " ".join(title.casefold().split())).strip()
    cleaned = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", cleaned)).strip()
    return cleaned[:160]


def _parse_rule(raw: object, index: int) -> CalendarPreferenceRule:
    if not isinstance(raw, dict):
        raise CalendarPreferenceError(f"Calendar preference {index} must be an object.")
    expected = {
        "key", "title", "organizer", "disposition", "source", "observations",
        "created_at", "updated_at",
    }
    if set(raw) != expected:
        raise CalendarPreferenceError(f"Calendar preference {index} has an invalid shape.")
    text_fields = expected - {"observations"}
    if any(not isinstance(raw[field], str) for field in text_fields):
        raise CalendarPreferenceError(f"Calendar preference {index} has invalid text fields.")
    disposition = raw["disposition"]
    if disposition not in {"usually_skip", "usually_attend"}:
        raise CalendarPreferenceError(f"Calendar preference {index} has an invalid disposition.")
    observations = raw["observations"]
    if type(observations) is not int or not 1 <= observations <= 10_000:
        raise CalendarPreferenceError(f"Calendar preference {index} has invalid observations.")
    return CalendarPreferenceRule(
        key=str(raw["key"])[:160],
        title=str(raw["title"])[:240],
        organizer=str(raw["organizer"])[:240],
        disposition=disposition,
        source=str(raw["source"])[:64],
        observations=observations,
        created_at=str(raw["created_at"])[:128],
        updated_at=str(raw["updated_at"])[:128],
    )


def load_calendar_preferences(
    path: Path = CALENDAR_PREFERENCES_PATH,
) -> list[CalendarPreferenceRule]:
    if not path.is_file():
        return []
    if path.stat().st_size > MAX_PROFILE_BYTES:
        raise CalendarPreferenceError("Calendar preference profile exceeds its size limit.")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CalendarPreferenceError("Calendar preference profile is not valid JSON.") from exc
    if not isinstance(raw, dict) or set(raw) != {"version", "rules"}:
        raise CalendarPreferenceError("Calendar preference profile has an invalid shape.")
    if raw["version"] != 1 or not isinstance(raw["rules"], list):
        raise CalendarPreferenceError("Calendar preference profile version is invalid.")
    if len(raw["rules"]) > MAX_RULES:
        raise CalendarPreferenceError("Calendar preference profile has too many rules.")
    return [_parse_rule(entry, index) for index, entry in enumerate(raw["rules"])]


def _write_preferences(rules: list[CalendarPreferenceRule], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "rules": [asdict(rule) for rule in rules[-MAX_RULES:]]}
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=".calendar-preferences-",
        suffix=".json", delete=False,
    ) as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def record_calendar_preference(
    title: str,
    *,
    organizer: str = "",
    disposition: Disposition,
    source: str = "user_explicit",
    path: Path = CALENDAR_PREFERENCES_PATH,
    now: datetime | None = None,
) -> CalendarPreferenceRule:
    key = normalize_meeting_key(title)
    if not key:
        raise CalendarPreferenceError("Meeting title is required for personalization.")
    rules = load_calendar_preferences(path)
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    existing = next(
        (
            rule for rule in rules
            if rule.key == key and rule.organizer.casefold() == organizer.strip().casefold()
        ),
        None,
    )
    replacement = CalendarPreferenceRule(
        key=key,
        title=" ".join(title.split())[:240],
        organizer=" ".join(organizer.split())[:240],
        disposition=disposition,
        source=source[:64],
        observations=(existing.observations + 1 if existing else 1),
        created_at=existing.created_at if existing else stamp,
        updated_at=stamp,
    )
    rules = [
        rule for rule in rules
        if not (
            rule.key == key
            and rule.organizer.casefold() == replacement.organizer.casefold()
        )
    ]
    rules.append(replacement)
    _write_preferences(rules, path)
    return replacement


def _integer(metadata: dict[str, object], name: str) -> int:
    value = metadata.get(name, 0)
    return value if type(value) is int and value >= 0 else 0


def calendar_preference_for(
    task: TodoItem,
    item: InboxItem,
    *,
    path: Path = CALENDAR_PREFERENCES_PATH,
) -> CalendarPreferenceResult:
    if task.source != "calendar" or item.source != "calendar":
        return CalendarPreferenceResult()
    metadata = item.metadata
    key = normalize_meeting_key(item.subject or task.title)
    organizer = str(metadata.get("organizer") or item.sender).strip()
    try:
        rules = load_calendar_preferences(path)
    except CalendarPreferenceError:
        rules = []
    rule = next(
        (
            candidate for candidate in reversed(rules)
            if candidate.key == key
            and (
                not candidate.organizer
                or candidate.organizer.casefold() == organizer.casefold()
            )
        ),
        None,
    )
    if rule is not None:
        if rule.disposition == "usually_skip":
            return CalendarPreferenceResult(
                priority_adjustment=-45,
                urgency_cap="fyi",
                confidence_cap=0.35,
                reason=(
                    f'Personalized as optional: you marked “{item.subject or task.title}” '
                    "as a meeting you usually skip."
                ),
            )
        return CalendarPreferenceResult(
            priority_adjustment=10,
            reason=(
                f'Personalized from your feedback: you usually attend '
                f'“{item.subject or task.title}”.'
            ),
        )

    instances = _integer(metadata, "historical_instances")
    accepted = _integer(metadata, "historical_accepted")
    declined = _integer(metadata, "historical_declined")
    tentative = _integer(metadata, "historical_tentative")
    no_response = _integer(metadata, "historical_no_response")
    attended = _integer(metadata, "historical_attended")
    missed = _integer(metadata, "historical_missed")
    response_total = max(instances, accepted + declined + tentative + no_response)
    attendance_total = attended + missed
    optional = metadata.get("optional") is True
    protected = metadata.get("needs_decision") is True

    if attendance_total >= 3 and missed / attendance_total >= 0.67 and not protected:
        return CalendarPreferenceResult(
            priority_adjustment=-40,
            urgency_cap="fyi" if optional else "this_week",
            confidence_cap=0.4,
            reason=(
                f"Personalized from {attendance_total} explicit attendance records: "
                "you usually miss this meeting series."
            ),
        )
    if response_total >= 3:
        skip_rate = (declined + no_response) / response_total
        if skip_rate >= 0.67 and optional and not protected:
            return CalendarPreferenceResult(
                priority_adjustment=-35,
                urgency_cap="fyi",
                confidence_cap=0.4,
                reason=(
                    f"Personalized from {response_total} prior invitations: this optional "
                    "series is usually declined or left unanswered."
                ),
            )
        if declined / response_total >= 0.67 and not protected:
            return CalendarPreferenceResult(
                priority_adjustment=-25,
                urgency_cap="this_week",
                confidence_cap=0.5,
                reason=(
                    f"Personalized from {response_total} prior invitations: you usually "
                    "decline this meeting series."
                ),
            )
        if accepted / response_total >= 0.67:
            return CalendarPreferenceResult(
                priority_adjustment=8,
                reason=(
                    f"Personalized from {response_total} prior invitations: you usually "
                    "accept this meeting series."
                ),
            )
    return CalendarPreferenceResult()


def personalize_calendar_tasks(
    tasks: list[TodoItem],
    items: list[InboxItem],
    *,
    path: Path = CALENDAR_PREFERENCES_PATH,
) -> None:
    by_source_id = {f"{item.source}:{item.id}": item for item in items}
    for task in tasks:
        if task.source != "calendar":
            continue
        item = next(
            (
                by_source_id[source_id]
                for source_id in (task.source_ids or [f"{task.source}:{task.id}"])
                if source_id in by_source_id
            ),
            None,
        )
        if item is None:
            continue
        result = calendar_preference_for(task, item, path=path)
        task.priority_adjustment = result.priority_adjustment
        task.preference_reason = result.reason[:500]
        task.confidence = min(task.confidence, result.confidence_cap)
        if (
            result.urgency_cap
            and "urgency" not in task.edited_fields
            and _URGENCY_ORDER[task.urgency] < _URGENCY_ORDER[result.urgency_cap]
        ):
            task.urgency = result.urgency_cap  # type: ignore[assignment]


__all__ = [
    "CalendarPreferenceError",
    "CalendarPreferenceResult",
    "CalendarPreferenceRule",
    "calendar_preference_for",
    "load_calendar_preferences",
    "normalize_meeting_key",
    "personalize_calendar_tasks",
    "record_calendar_preference",
]
