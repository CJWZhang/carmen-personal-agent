"""Passive learning from the user's explicit negative feedback.

When the user says an item is not a task or not theirs, we log bounded surface
features (source, first person, title snippet) plus how long it survived before
dismissal. Ordinary completion and source-verified resolution are deliberately
excluded: finishing work is not evidence that similar work is noise. Repeated
explicit dismissals become a prompt hint for future triage.

Storage is a single JSON file (`work/habits.json`), append-mostly, deterministic,
and inspectable — no black-box embedding store.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from carmen_agent.paths import HABITS_PATH as DEFAULT_HABITS_PATH
from carmen_agent.skills.schemas import Source, TodoItem, from_dict, parse_stamp
from carmen_agent.configuration import config_value

# Only patterns dismissed at least this many times within the lookback window will
# be surfaced to the triage prompt. Prevents one-off completes from teaching bad rules.
SUPPRESSION_MIN_COUNT = 2
SUPPRESSION_LOOKBACK_DAYS = 30
MAX_HINTS = 8


@dataclass
class Dismissal:
    source: Source
    person: str
    title: str
    dismissed_at: str
    time_to_dismiss_seconds: int | None = None
    evidence: str = ""
    feedback_kind: str = ""

    def key(self) -> tuple[str, str]:
        # Group by (source, first person). Coarse but captures the intuition:
        # "you keep dismissing gmail items from Vanta". Title varies too much to
        # group on directly; we surface a representative title separately.
        return (self.source, self.person.strip().lower())


@dataclass
class Habits:
    version: int = 1
    dismissals: list[Dismissal] = field(default_factory=list)


def _first_person(todo: TodoItem) -> str:
    configured_user = config_value("CARMEN_USER_NAME", "user_name", "").casefold()
    self_names = {"unknown", "carmen", "carmen zhang"}
    if configured_user:
        self_names.add(configured_user)
    for person in todo.people:
        cleaned = person.strip()
        if cleaned and cleaned.casefold() not in self_names:
            return cleaned
    return ""


def load_habits(path: Path = DEFAULT_HABITS_PATH) -> Habits:
    if not path.is_file():
        return Habits()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Habits()
    if not isinstance(raw, dict):
        return Habits()
    entries = raw.get("dismissals", [])
    dismissals: list[Dismissal] = []
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                dismissals.append(from_dict(Dismissal, entry))
            except (TypeError, ValueError):
                continue
    return Habits(version=int(raw.get("version", 1) or 1), dismissals=dismissals)


def _save(habits: Habits, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"version": habits.version, "dismissals": [asdict(d) for d in habits.dismissals]},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def record_dismissal(
    todo: TodoItem,
    *,
    now: datetime | None = None,
    evidence: str = "",
    path: Path = DEFAULT_HABITS_PATH,
) -> Dismissal:
    """Log a completed task so future triage learns what patterns the user ignores."""
    dismissed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    first_seen = parse_stamp(todo.first_seen)
    ttl = int((dismissed_at - first_seen).total_seconds()) if first_seen else None
    dismissal = Dismissal(
        source=todo.source,
        person=_first_person(todo),
        title=todo.title[:120],
        dismissed_at=dismissed_at.isoformat(),
        time_to_dismiss_seconds=ttl,
        evidence=evidence,
        feedback_kind="explicit_dismissal",
    )
    habits = load_habits(path)
    habits.dismissals.append(dismissal)
    _save(habits, path)
    return dismissal


def format_suppression_hints(
    *,
    now: datetime | None = None,
    path: Path = DEFAULT_HABITS_PATH,
    lookback_days: int = SUPPRESSION_LOOKBACK_DAYS,
    min_count: int = SUPPRESSION_MIN_COUNT,
    max_hints: int = MAX_HINTS,
) -> str:
    """Return a prompt-ready hint string, or '' if no signal is strong enough yet."""
    habits = load_habits(path)
    if not habits.dismissals:
        return ""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=lookback_days)
    recent: list[Dismissal] = []
    for dismissal in habits.dismissals:
        stamp = parse_stamp(dismissal.dismissed_at)
        reliable = dismissal.feedback_kind == "explicit_dismissal" or any(
            dismissal.evidence.startswith(prefix)
            for prefix in (
                "user correction: not a task",
                "user correction: not mine",
            )
        )
        if reliable and stamp is not None and stamp >= cutoff:
            recent.append(dismissal)
    if not recent:
        return ""

    counts: Counter[tuple[str, str]] = Counter(d.key() for d in recent)
    example_titles: dict[tuple[str, str], str] = {}
    for dismissal in recent:
        example_titles.setdefault(dismissal.key(), dismissal.title)

    lines: list[str] = []
    for key, count in counts.most_common():
        if count < min_count:
            continue
        source, person = key
        person_label = person or "(no named sender)"
        example = example_titles.get(key, "")
        example_clause = f' — e.g. "{example}"' if example else ""
        lines.append(
            f"- {source} items from {person_label} (dismissed {count} time(s){example_clause})"
        )
        if len(lines) >= max_hints:
            break
    if not lines:
        return ""
    user_name = config_value("CARMEN_USER_NAME", "user_name", "the user")
    return (
        f"Recent dismissal patterns ({user_name} resolved these without acting; treat similar "
        "items as low priority and cap confidence at 0.3 unless the current item has "
        "a new explicit ask):\n" + "\n".join(lines)
    )
