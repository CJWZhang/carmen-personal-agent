from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from carmen_agent.actions.schemas import ActionEvidence, ActionItem, ActionStatus
from carmen_agent.hosted.security import hash_oauth_state
from carmen_agent.infra.runtime import RunContext, attributed_user_id


@dataclass(frozen=True)
class Installation:
    team_id: str
    team_name: str
    enterprise_id: str
    bot_user_id: str
    bot_token_encrypted: str
    user_token_encrypted: str
    installing_user_id: str
    scopes: str


@dataclass(frozen=True)
class HostedUser:
    team_id: str
    user_id: str
    user_token_encrypted: str = ""
    scopes: str = ""
    timezone: str = "UTC"
    daily_hour: int = 5
    enabled: bool = True


@dataclass(frozen=True)
class Job:
    id: str
    run_id: str
    team_id: str
    user_id: str
    kind: str
    payload: dict[str, Any]
    attempts: int


class HostedStore(Protocol):
    def save_oauth_state(self, state: str) -> None: ...
    def consume_oauth_state(self, state: str) -> bool: ...
    def save_installation(self, installation: Installation) -> None: ...
    def get_installation(self, team_id: str) -> Installation | None: ...
    def delete_installation(self, team_id: str) -> None: ...
    def save_user(self, user: HostedUser) -> None: ...
    def get_user(self, team_id: str, user_id: str) -> HostedUser | None: ...
    def list_users(self) -> list[HostedUser]: ...
    def record_event_once(self, event_id: str, team_id: str) -> bool: ...
    def save_authored_message(
        self,
        *,
        team_id: str,
        user_id: str,
        message_id: str,
        channel_id: str,
        text: str,
        posted_at: str,
    ) -> None: ...
    def recent_authored_messages(
        self,
        *,
        team_id: str,
        user_id: str,
        limit: int = 20,
    ) -> list[str]: ...
    def enqueue_job(
        self,
        *,
        team_id: str,
        user_id: str,
        kind: str,
        payload: dict[str, Any],
    ) -> str: ...
    def enqueue_daily_digest_once(
        self,
        *,
        team_id: str,
        user_id: str,
        local_date: str,
    ) -> bool: ...
    def claim_job(self) -> Job | None: ...
    def heartbeat_job(self, job_id: str) -> None: ...
    def finish_job(
        self, job_id: str, *, error: str = "", retry: bool = False
    ) -> None: ...
    def save_draft(
        self,
        *,
        team_id: str,
        user_id: str,
        channel_id: str,
        body: str,
        draft_id: str = "",
    ) -> str: ...
    def record_draft_feedback(
        self, *, team_id: str, user_id: str, draft_id: str, outcome: str,
        edited_body: str = ""
    ) -> bool: ...
    def save_action(self, action: ActionItem, evidence: ActionEvidence) -> None: ...
    def list_actions(
        self, *, team_id: str, user_id: str, include_candidates: bool = True
    ) -> list[ActionItem]: ...
    def get_action(
        self, *, team_id: str, user_id: str, selector: str
    ) -> ActionItem | None: ...
    def latest_action_evidence(self, action_id: str) -> ActionEvidence | None: ...
    def transition_action(
        self,
        *,
        action_id: str,
        status: ActionStatus,
        reason: str,
        snoozed_until: str = "",
        evidence_source_id: str = "",
    ) -> bool: ...
    def record_action_feedback(
        self, *, action_id: str, outcome: str, detail: str = ""
    ) -> None: ...
    def set_source_cursor(
        self, *, team_id: str, user_id: str, source: str, cursor: str
    ) -> None: ...
    def get_source_cursor(
        self, *, team_id: str, user_id: str, source: str
    ) -> str: ...
    def claim_notification(
        self, *, action_id: str, channel: str, policy_key: str
    ) -> bool: ...
    def notification_was_delivered(
        self, *, action_id: str, channel: str, policy_key: str
    ) -> bool: ...
    def start_agent_run(
        self, *, context: RunContext, agent_name: str, agent_version: int,
        config: dict[str, Any]
    ) -> None: ...
    def finish_agent_run(
        self, run_id: str, *, error: str = "", retrying: bool = False
    ) -> None: ...
    def get_completed_run_step(self, run_id: str, step_name: str) -> Any | None: ...
    def start_run_step(
        self, *, run_id: str, step_name: str, idempotency_key: str
    ) -> None: ...
    def finish_run_step(
        self, *, run_id: str, step_name: str, output: Any = None, error: str = ""
    ) -> None: ...
    def start_tool_call(
        self, *, call_id: str, run_id: str, step_name: str, tool_name: str,
        idempotency_key: str, policy: dict[str, Any], arguments: Any
    ) -> None: ...
    def finish_tool_call(
        self, call_id: str, *, output: Any = None, error: str = ""
    ) -> None: ...
    def get_completed_tool_call(self, idempotency_key: str) -> Any | None: ...


SQLITE_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS oauth_states (
    state_hash TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS slack_installations (
    team_id TEXT PRIMARY KEY,
    team_name TEXT NOT NULL,
    enterprise_id TEXT NOT NULL DEFAULT '',
    bot_user_id TEXT NOT NULL DEFAULT '',
    bot_token_encrypted TEXT NOT NULL,
    user_token_encrypted TEXT NOT NULL DEFAULT '',
    installing_user_id TEXT NOT NULL,
    scopes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    team_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    user_token_encrypted TEXT NOT NULL DEFAULT '',
    scopes TEXT NOT NULL DEFAULT '',
    timezone TEXT NOT NULL DEFAULT 'UTC',
    daily_hour INTEGER NOT NULL DEFAULT 5,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (team_id, user_id),
    FOREIGN KEY (team_id) REFERENCES slack_installations(team_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS authored_messages (
    team_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    text TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (team_id, message_id),
    FOREIGN KEY (team_id, user_id) REFERENCES users(team_id, user_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS authored_messages_user_time
ON authored_messages(team_id, user_id, posted_at DESC);

CREATE TABLE IF NOT EXISTS processed_events (
    event_id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    processed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    team_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    run_after TEXT NOT NULL,
    locked_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS jobs_ready
ON jobs(status, run_after, created_at);

CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT '',
    actor_kind TEXT NOT NULL,
    actor_id TEXT NOT NULL DEFAULT '',
    surface TEXT NOT NULL,
    thread_id TEXT NOT NULL DEFAULT '',
    agent_name TEXT NOT NULL,
    agent_version INTEGER NOT NULL,
    config TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    attempts INTEGER NOT NULL DEFAULT 1,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_steps (
    run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    step_name TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    attempts INTEGER NOT NULL DEFAULT 1,
    output TEXT NOT NULL DEFAULT 'null',
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, step_name),
    UNIQUE(idempotency_key)
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    step_name TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    policy TEXT NOT NULL,
    arguments TEXT NOT NULL,
    output TEXT NOT NULL DEFAULT 'null',
    status TEXT NOT NULL DEFAULT 'running',
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_deliveries (
    team_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    local_date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (team_id, user_id, local_date)
);

CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'suggested',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS draft_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id TEXT NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    outcome TEXT NOT NULL,
    edited_body TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    confidence REAL NOT NULL DEFAULT 0.5,
    priority_score REAL NOT NULL DEFAULT 0,
    requester_id TEXT NOT NULL DEFAULT '',
    explicit_ask INTEGER NOT NULL DEFAULT 0,
    due_at TEXT NOT NULL DEFAULT '',
    channel_id TEXT NOT NULL DEFAULT '',
    thread_id TEXT NOT NULL DEFAULT '',
    permalink TEXT NOT NULL DEFAULT '',
    snoozed_until TEXT NOT NULL DEFAULT '',
    resolution_reason TEXT NOT NULL DEFAULT '',
    last_checked_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(team_id, user_id, source, source_id, action_type),
    FOREIGN KEY (team_id, user_id) REFERENCES users(team_id, user_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS actions_open
ON actions(team_id, user_id, status, due_at, updated_at DESC);

CREATE TABLE IF NOT EXISTS action_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT NOT NULL REFERENCES actions(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    permalink TEXT NOT NULL DEFAULT '',
    actor_id TEXT NOT NULL DEFAULT '',
    observed_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(action_id, source, source_id)
);

CREATE TABLE IF NOT EXISTS action_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT NOT NULL REFERENCES actions(id) ON DELETE CASCADE,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    evidence_source_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS action_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT NOT NULL REFERENCES actions(id) ON DELETE CASCADE,
    outcome TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_cursors (
    team_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    source TEXT NOT NULL,
    cursor TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (team_id, user_id, source),
    FOREIGN KEY (team_id, user_id) REFERENCES users(team_id, user_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS notification_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT NOT NULL REFERENCES actions(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    policy_key TEXT NOT NULL,
    delivered_at TEXT NOT NULL,
    UNIQUE(action_id, channel, policy_key)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _installation_from_row(row: Any) -> Installation:
    return Installation(
        team_id=row["team_id"],
        team_name=row["team_name"],
        enterprise_id=row["enterprise_id"],
        bot_user_id=row["bot_user_id"],
        bot_token_encrypted=row["bot_token_encrypted"],
        user_token_encrypted=row["user_token_encrypted"],
        installing_user_id=row["installing_user_id"],
        scopes=row["scopes"],
    )


def _user_from_row(row: Any) -> HostedUser:
    return HostedUser(
        team_id=row["team_id"],
        user_id=row["user_id"],
        user_token_encrypted=row["user_token_encrypted"],
        scopes=row["scopes"],
        timezone=row["timezone"],
        daily_hour=int(row["daily_hour"]),
        enabled=bool(row["enabled"]),
    )


def _action_from_row(row: Any) -> ActionItem:
    return ActionItem(
        id=str(row["id"]),
        team_id=str(row["team_id"]),
        user_id=str(row["user_id"]),
        source=str(row["source"]),  # type: ignore[arg-type]
        source_id=str(row["source_id"]),
        action_type=str(row["action_type"]),  # type: ignore[arg-type]
        summary=str(row["summary"]),
        status=str(row["status"]),  # type: ignore[arg-type]
        confidence=float(row["confidence"]),
        priority_score=float(row["priority_score"]),
        requester_id=str(row["requester_id"]),
        explicit_ask=bool(row["explicit_ask"]),
        due_at=str(row["due_at"] or ""),
        channel_id=str(row["channel_id"]),
        thread_id=str(row["thread_id"]),
        permalink=str(row["permalink"]),
        snoozed_until=str(row["snoozed_until"] or ""),
        resolution_reason=str(row["resolution_reason"]),
        last_checked_at=str(row["last_checked_at"] or ""),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _evidence_from_row(row: Any) -> ActionEvidence:
    return ActionEvidence(
        source=str(row["source"]),  # type: ignore[arg-type]
        source_id=str(row["source_id"]),
        excerpt=str(row["excerpt"]),
        permalink=str(row["permalink"]),
        actor_id=str(row["actor_id"]),
        observed_at=str(row["observed_at"] or ""),
    )


class SQLiteStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SQLITE_SCHEMA)
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "run_id" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN run_id TEXT NOT NULL DEFAULT ''")
                connection.execute("UPDATE jobs SET run_id=id WHERE run_id='' OR run_id IS NULL")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def save_oauth_state(self, state: str) -> None:
        expires = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO oauth_states(state_hash, expires_at) VALUES (?, ?)",
                (hash_oauth_state(state), expires),
            )

    def consume_oauth_state(self, state: str) -> bool:
        stamp = _now()
        state_hash = hash_oauth_state(state)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT expires_at FROM oauth_states WHERE state_hash = ?",
                (state_hash,),
            ).fetchone()
            connection.execute(
                "DELETE FROM oauth_states WHERE state_hash = ? OR expires_at < ?",
                (state_hash, stamp),
            )
        return row is not None and str(row["expires_at"]) >= stamp

    def save_installation(self, installation: Installation) -> None:
        stamp = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO slack_installations(
                    team_id, team_name, enterprise_id, bot_user_id,
                    bot_token_encrypted, user_token_encrypted,
                    installing_user_id, scopes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(team_id) DO UPDATE SET
                    team_name=excluded.team_name,
                    enterprise_id=excluded.enterprise_id,
                    bot_user_id=excluded.bot_user_id,
                    bot_token_encrypted=excluded.bot_token_encrypted,
                    user_token_encrypted=excluded.user_token_encrypted,
                    installing_user_id=excluded.installing_user_id,
                    scopes=excluded.scopes,
                    updated_at=excluded.updated_at
                """,
                (
                    installation.team_id,
                    installation.team_name,
                    installation.enterprise_id,
                    installation.bot_user_id,
                    installation.bot_token_encrypted,
                    installation.user_token_encrypted,
                    installation.installing_user_id,
                    installation.scopes,
                    stamp,
                    stamp,
                ),
            )

    def get_installation(self, team_id: str) -> Installation | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM slack_installations WHERE team_id = ?",
                (team_id,),
            ).fetchone()
        return _installation_from_row(row) if row is not None else None

    def delete_installation(self, team_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM slack_installations WHERE team_id = ?",
                (team_id,),
            )

    def save_user(self, user: HostedUser) -> None:
        stamp = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO users(
                    team_id, user_id, user_token_encrypted, scopes,
                    timezone, daily_hour, enabled,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(team_id, user_id) DO UPDATE SET
                    user_token_encrypted=excluded.user_token_encrypted,
                    scopes=excluded.scopes,
                    timezone=excluded.timezone,
                    daily_hour=excluded.daily_hour,
                    enabled=excluded.enabled,
                    updated_at=excluded.updated_at
                """,
                (
                    user.team_id,
                    user.user_id,
                    user.user_token_encrypted,
                    user.scopes,
                    user.timezone,
                    user.daily_hour,
                    int(user.enabled),
                    stamp,
                    stamp,
                ),
            )

    def get_user(self, team_id: str, user_id: str) -> HostedUser | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE team_id = ? AND user_id = ?",
                (team_id, user_id),
            ).fetchone()
        return _user_from_row(row) if row is not None else None

    def list_users(self) -> list[HostedUser]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM users WHERE enabled = 1 ORDER BY team_id, user_id"
            ).fetchall()
        return [_user_from_row(row) for row in rows]

    def record_event_once(self, event_id: str, team_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO processed_events(event_id, team_id, processed_at)
                VALUES (?, ?, ?)
                """,
                (event_id, team_id, _now()),
            )
        return cursor.rowcount == 1

    def save_authored_message(
        self,
        *,
        team_id: str,
        user_id: str,
        message_id: str,
        channel_id: str,
        text: str,
        posted_at: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO authored_messages(
                    team_id, user_id, message_id, channel_id, text, posted_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(team_id, message_id) DO UPDATE SET
                    text=excluded.text,
                    posted_at=excluded.posted_at
                """,
                (team_id, user_id, message_id, channel_id, text, posted_at, _now()),
            )

    def recent_authored_messages(
        self,
        *,
        team_id: str,
        user_id: str,
        limit: int = 20,
    ) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT text FROM authored_messages
                WHERE team_id = ? AND user_id = ?
                ORDER BY posted_at DESC LIMIT ?
                """,
                (team_id, user_id, limit),
            ).fetchall()
        return [str(row["text"]) for row in reversed(rows)]

    def enqueue_job(
        self,
        *,
        team_id: str,
        user_id: str,
        kind: str,
        payload: dict[str, Any],
    ) -> str:
        job_id = uuid4().hex
        run_id = uuid4().hex
        stamp = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs(
                    id, run_id, team_id, user_id, kind, payload, status,
                    attempts, run_after, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?)
                """,
                (
                    job_id,
                    run_id,
                    team_id,
                    user_id,
                    kind,
                    json.dumps(payload, ensure_ascii=False),
                    stamp,
                    stamp,
                    stamp,
                ),
            )
        return job_id

    def claim_job(self) -> Job | None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE (status = 'queued' AND run_after <= ?)
                   OR (status = 'running' AND locked_at < ?)
                ORDER BY created_at LIMIT 1
                """,
                (
                    _now(),
                    (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
                ),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                """
                UPDATE jobs SET status='running', attempts=attempts+1,
                    locked_at=?, updated_at=?
                WHERE id=?
                """,
                (_now(), _now(), row["id"]),
            )
            connection.commit()
            return Job(
                id=row["id"],
                run_id=row["run_id"] or row["id"],
                team_id=row["team_id"],
                user_id=row["user_id"],
                kind=row["kind"],
                payload=json.loads(row["payload"]),
                attempts=int(row["attempts"]) + 1,
            )
        finally:
            connection.close()

    def heartbeat_job(self, job_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs SET locked_at=?, updated_at=?
                WHERE id=? AND status='running'
                """,
                (_now(), _now(), job_id),
            )

    def enqueue_daily_digest_once(
        self,
        *,
        team_id: str,
        user_id: str,
        local_date: str,
    ) -> bool:
        job_id = uuid4().hex
        run_id = uuid4().hex
        stamp = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO daily_deliveries(
                    team_id, user_id, local_date, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (team_id, user_id, local_date, stamp),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                """
                INSERT INTO jobs(
                    id, run_id, team_id, user_id, kind, payload, status,
                    attempts, run_after, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'daily_digest', '{}', 'queued', 0, ?, ?, ?)
                """,
                (job_id, run_id, team_id, user_id, stamp, stamp, stamp),
            )
        return True

    def finish_job(
        self, job_id: str, *, error: str = "", retry: bool = False
    ) -> None:
        status = "queued" if error and retry else ("failed" if error else "done")
        run_after = (
            datetime.now(timezone.utc) + timedelta(seconds=30)
        ).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs SET status=?,
                    payload=CASE WHEN ? THEN payload ELSE '{}' END,
                    last_error=?, run_after=?, locked_at=NULL, updated_at=? WHERE id=?
                """,
                (status, int(retry), error[:500], run_after, _now(), job_id),
            )

    def save_draft(
        self,
        *,
        team_id: str,
        user_id: str,
        channel_id: str,
        body: str,
        draft_id: str = "",
    ) -> str:
        draft_id = draft_id or uuid4().hex
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO drafts(id, team_id, user_id, channel_id, body, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET body=excluded.body
                """,
                (draft_id, team_id, user_id, channel_id, body, _now()),
            )
        return draft_id

    def record_draft_feedback(
        self, *, team_id: str, user_id: str, draft_id: str, outcome: str,
        edited_body: str = ""
    ) -> bool:
        if outcome not in {"accepted", "modified", "rejected"}:
            return False
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM drafts WHERE id=? AND team_id=? AND user_id=?",
                (draft_id, team_id, user_id),
            ).fetchone()
            if exists is None:
                return False
            connection.execute(
                """
                INSERT INTO draft_feedback(draft_id, outcome, edited_body, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (draft_id, outcome, edited_body[:4_000], _now()),
            )
            connection.execute(
                "UPDATE drafts SET status=? WHERE id=?", (outcome, draft_id)
            )
        return True

    def save_action(self, action: ActionItem, evidence: ActionEvidence) -> None:
        stamp = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO actions(
                    id, team_id, user_id, source, source_id, action_type, summary,
                    status, confidence, priority_score, requester_id, explicit_ask,
                    due_at, channel_id, thread_id, permalink, snoozed_until,
                    resolution_reason, last_checked_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    summary=excluded.summary,
                    confidence=excluded.confidence,
                    requester_id=excluded.requester_id,
                    explicit_ask=excluded.explicit_ask,
                    due_at=CASE WHEN excluded.due_at != '' THEN excluded.due_at ELSE actions.due_at END,
                    channel_id=excluded.channel_id,
                    thread_id=excluded.thread_id,
                    permalink=CASE WHEN excluded.permalink != '' THEN excluded.permalink ELSE actions.permalink END,
                    updated_at=excluded.updated_at
                """,
                (
                    action.id, action.team_id, action.user_id, action.source,
                    action.source_id, action.action_type, action.summary, action.status,
                    action.confidence, action.priority_score, action.requester_id,
                    int(action.explicit_ask), action.due_at, action.channel_id,
                    action.thread_id, action.permalink, action.snoozed_until,
                    action.resolution_reason, action.last_checked_at,
                    action.created_at or stamp, stamp,
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO action_evidence(
                    action_id, source, source_id, excerpt, permalink, actor_id,
                    observed_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action.id, evidence.source, evidence.source_id, evidence.excerpt,
                    evidence.permalink, evidence.actor_id, evidence.observed_at, stamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO action_events(action_id, from_status, to_status, reason, created_at)
                SELECT ?, '', ?, 'extracted', ?
                WHERE NOT EXISTS (SELECT 1 FROM action_events WHERE action_id=?)
                """,
                (action.id, action.status, stamp, action.id),
            )

    def list_actions(
        self, *, team_id: str, user_id: str, include_candidates: bool = True
    ) -> list[ActionItem]:
        statuses = ("candidate", "open", "waiting") if include_candidates else ("open", "waiting")
        placeholders = ",".join("?" for _ in statuses)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM actions
                WHERE team_id=? AND user_id=? AND status IN ({placeholders})
                ORDER BY priority_score DESC, due_at != '' DESC, due_at, updated_at DESC
                """,
                (team_id, user_id, *statuses),
            ).fetchall()
        return [_action_from_row(row) for row in rows]

    def get_action(
        self, *, team_id: str, user_id: str, selector: str
    ) -> ActionItem | None:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM actions
                WHERE team_id=? AND user_id=? AND (id=? OR id LIKE ?)
                ORDER BY CASE WHEN id=? THEN 0 ELSE 1 END LIMIT 2
                """,
                (team_id, user_id, selector, selector + "%", selector),
            ).fetchall()
        if not rows or (len(rows) > 1 and rows[0]["id"] != selector):
            return None
        return _action_from_row(rows[0])

    def latest_action_evidence(self, action_id: str) -> ActionEvidence | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM action_evidence WHERE action_id=?
                ORDER BY id DESC LIMIT 1
                """,
                (action_id,),
            ).fetchone()
        return _evidence_from_row(row) if row is not None else None

    def transition_action(
        self,
        *,
        action_id: str,
        status: ActionStatus,
        reason: str,
        snoozed_until: str = "",
        evidence_source_id: str = "",
    ) -> bool:
        stamp = _now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM actions WHERE id=?", (action_id,)
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                """
                UPDATE actions SET status=?, resolution_reason=?, snoozed_until=?,
                    last_checked_at=?, updated_at=? WHERE id=?
                """,
                (status, reason[:500], snoozed_until, stamp, stamp, action_id),
            )
            connection.execute(
                """
                INSERT INTO action_events(
                    action_id, from_status, to_status, reason, evidence_source_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (action_id, row["status"], status, reason[:500], evidence_source_id, stamp),
            )
        return True

    def record_action_feedback(
        self, *, action_id: str, outcome: str, detail: str = ""
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO action_feedback(action_id, outcome, detail, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (action_id, outcome[:40], detail[:500], _now()),
            )

    def set_source_cursor(
        self, *, team_id: str, user_id: str, source: str, cursor: str
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO source_cursors(team_id, user_id, source, cursor, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(team_id, user_id, source) DO UPDATE SET
                    cursor=excluded.cursor, updated_at=excluded.updated_at
                """,
                (team_id, user_id, source, cursor, _now()),
            )

    def get_source_cursor(
        self, *, team_id: str, user_id: str, source: str
    ) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT cursor FROM source_cursors WHERE team_id=? AND user_id=? AND source=?",
                (team_id, user_id, source),
            ).fetchone()
        return str(row["cursor"]) if row else ""

    def claim_notification(
        self, *, action_id: str, channel: str, policy_key: str
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO notification_deliveries(
                    action_id, channel, policy_key, delivered_at
                ) VALUES (?, ?, ?, ?)
                """,
                (action_id, channel, policy_key, _now()),
            )
        return cursor.rowcount == 1

    def notification_was_delivered(
        self, *, action_id: str, channel: str, policy_key: str
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM notification_deliveries
                WHERE action_id=? AND channel=? AND policy_key=?
                """,
                (action_id, channel, policy_key),
            ).fetchone()
        return row is not None

    def start_agent_run(
        self,
        *,
        context: RunContext,
        agent_name: str,
        agent_version: int,
        config: dict[str, Any],
    ) -> None:
        stamp = _now()
        actor_id = attributed_user_id(context.actor) or getattr(
            context.actor, "service", getattr(context.actor, "component", "")
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_runs(
                    id, team_id, user_id, actor_kind, actor_id, surface,
                    thread_id, agent_name, agent_version, config, status,
                    attempts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', 1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status='running', attempts=agent_runs.attempts+1,
                    config=excluded.config, updated_at=excluded.updated_at
                """,
                (
                    context.run_id, context.tenant_id, context.user_id or "",
                    context.actor.kind, actor_id, context.surface,
                    context.thread_id or "", agent_name, agent_version,
                    json.dumps(config, ensure_ascii=False, sort_keys=True), stamp, stamp,
                ),
            )

    def finish_agent_run(
        self, run_id: str, *, error: str = "", retrying: bool = False
    ) -> None:
        status = "retrying" if retrying else ("failed" if error else "completed")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE agent_runs SET status=?, last_error=?, updated_at=? WHERE id=?
                """,
                (status, error[:500], _now(), run_id),
            )

    def get_completed_run_step(self, run_id: str, step_name: str) -> Any | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT output FROM run_steps
                WHERE run_id=? AND step_name=? AND status='completed'
                """,
                (run_id, step_name),
            ).fetchone()
        return json.loads(row["output"]) if row else None

    def start_run_step(
        self, *, run_id: str, step_name: str, idempotency_key: str
    ) -> None:
        stamp = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO run_steps(
                    run_id, step_name, idempotency_key, status, attempts,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'running', 1, ?, ?)
                ON CONFLICT(run_id, step_name) DO UPDATE SET
                    status='running', attempts=run_steps.attempts+1,
                    last_error='', updated_at=excluded.updated_at
                """,
                (run_id, step_name, idempotency_key, stamp, stamp),
            )

    def finish_run_step(
        self,
        *,
        run_id: str,
        step_name: str,
        output: Any = None,
        error: str = "",
    ) -> None:
        status = "failed" if error else "completed"
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE run_steps SET status=?, output=?, last_error=?, updated_at=?
                WHERE run_id=? AND step_name=?
                """,
                (
                    status, json.dumps(output, ensure_ascii=False, default=str),
                    error[:500], _now(), run_id, step_name,
                ),
            )

    def start_tool_call(
        self,
        *,
        call_id: str,
        run_id: str,
        step_name: str,
        tool_name: str,
        idempotency_key: str,
        policy: dict[str, Any],
        arguments: Any,
    ) -> None:
        stamp = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tool_calls(
                    id, run_id, step_name, tool_name, idempotency_key,
                    policy, arguments, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status='running', last_error='', updated_at=excluded.updated_at
                """,
                (
                    call_id, run_id, step_name, tool_name, idempotency_key,
                    json.dumps(policy, sort_keys=True),
                    json.dumps(arguments, ensure_ascii=False, sort_keys=True), stamp, stamp,
                ),
            )

    def finish_tool_call(
        self, call_id: str, *, output: Any = None, error: str = ""
    ) -> None:
        status = "failed" if error else "completed"
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE tool_calls SET status=?, output=?, last_error=?, updated_at=?
                WHERE id=?
                """,
                (
                    status, json.dumps(output, ensure_ascii=False, default=str),
                    error[:500], _now(), call_id,
                ),
            )

    def get_completed_tool_call(self, idempotency_key: str) -> Any | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT output FROM tool_calls
                WHERE idempotency_key=? AND status='completed'
                """,
                (idempotency_key,),
            ).fetchone()
        return json.loads(row["output"]) if row else None


class PostgresStore:
    """Postgres implementation used by multi-user hosted deployments."""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError(
                "Postgres storage requires the `hosted` dependency extra."
            ) from exc
        self._psycopg = psycopg
        self._dict_row = dict_row

    def _connect(self):
        return self._psycopg.connect(self.database_url, row_factory=self._dict_row)

    def save_oauth_state(self, state: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO oauth_states(state_hash, expires_at)
                VALUES (%s, NOW() + INTERVAL '10 minutes')
                ON CONFLICT(state_hash) DO UPDATE SET expires_at=excluded.expires_at
                """,
                (hash_oauth_state(state),),
            )

    def consume_oauth_state(self, state: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                DELETE FROM oauth_states
                WHERE state_hash=%s AND expires_at >= NOW()
                RETURNING state_hash
                """,
                (hash_oauth_state(state),),
            ).fetchone()
            connection.execute("DELETE FROM oauth_states WHERE expires_at < NOW()")
        return row is not None

    def save_installation(self, installation: Installation) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO slack_installations(
                    team_id, team_name, enterprise_id, bot_user_id,
                    bot_token_encrypted, user_token_encrypted,
                    installing_user_id, scopes
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(team_id) DO UPDATE SET
                    team_name=excluded.team_name,
                    enterprise_id=excluded.enterprise_id,
                    bot_user_id=excluded.bot_user_id,
                    bot_token_encrypted=excluded.bot_token_encrypted,
                    user_token_encrypted=excluded.user_token_encrypted,
                    installing_user_id=excluded.installing_user_id,
                    scopes=excluded.scopes,
                    updated_at=NOW()
                """,
                (
                    installation.team_id,
                    installation.team_name,
                    installation.enterprise_id,
                    installation.bot_user_id,
                    installation.bot_token_encrypted,
                    installation.user_token_encrypted,
                    installation.installing_user_id,
                    installation.scopes,
                ),
            )

    def get_installation(self, team_id: str) -> Installation | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM slack_installations WHERE team_id=%s",
                (team_id,),
            ).fetchone()
        return _installation_from_row(row) if row else None

    def delete_installation(self, team_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM slack_installations WHERE team_id=%s",
                (team_id,),
            )

    def save_user(self, user: HostedUser) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO users(
                    team_id, user_id, user_token_encrypted, scopes,
                    timezone, daily_hour, enabled
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(team_id, user_id) DO UPDATE SET
                    user_token_encrypted=excluded.user_token_encrypted,
                    scopes=excluded.scopes,
                    timezone=excluded.timezone,
                    daily_hour=excluded.daily_hour,
                    enabled=excluded.enabled,
                    updated_at=NOW()
                """,
                (
                    user.team_id,
                    user.user_id,
                    user.user_token_encrypted,
                    user.scopes,
                    user.timezone,
                    user.daily_hour,
                    user.enabled,
                ),
            )

    def get_user(self, team_id: str, user_id: str) -> HostedUser | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE team_id=%s AND user_id=%s",
                (team_id, user_id),
            ).fetchone()
        return _user_from_row(row) if row else None

    def list_users(self) -> list[HostedUser]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM users WHERE enabled=TRUE ORDER BY team_id, user_id"
            ).fetchall()
        return [_user_from_row(row) for row in rows]

    def record_event_once(self, event_id: str, team_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                INSERT INTO processed_events(event_id, team_id)
                VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING event_id
                """,
                (event_id, team_id),
            ).fetchone()
        return row is not None

    def save_authored_message(
        self,
        *,
        team_id: str,
        user_id: str,
        message_id: str,
        channel_id: str,
        text: str,
        posted_at: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO authored_messages(
                    team_id, user_id, message_id, channel_id, text, posted_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT(team_id, message_id) DO UPDATE SET
                    text=excluded.text, posted_at=excluded.posted_at
                """,
                (team_id, user_id, message_id, channel_id, text, posted_at),
            )

    def recent_authored_messages(
        self,
        *,
        team_id: str,
        user_id: str,
        limit: int = 20,
    ) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT text FROM authored_messages
                WHERE team_id=%s AND user_id=%s
                ORDER BY posted_at DESC LIMIT %s
                """,
                (team_id, user_id, limit),
            ).fetchall()
        return [str(row["text"]) for row in reversed(rows)]

    def enqueue_job(
        self,
        *,
        team_id: str,
        user_id: str,
        kind: str,
        payload: dict[str, Any],
    ) -> str:
        job_id = uuid4().hex
        run_id = uuid4().hex
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs(id, run_id, team_id, user_id, kind, payload)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                """,
                (job_id, run_id, team_id, user_id, kind, json.dumps(payload)),
            )
        return job_id

    def claim_job(self) -> Job | None:
        with self._connect() as connection:
            with connection.transaction():
                row = connection.execute(
                    """
                    SELECT * FROM jobs
                    WHERE (status='queued' AND run_after <= NOW())
                       OR (status='running' AND locked_at < NOW() - INTERVAL '10 minutes')
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED LIMIT 1
                    """
                ).fetchone()
                if row is None:
                    return None
                connection.execute(
                    """
                    UPDATE jobs SET status='running', attempts=attempts+1,
                        locked_at=NOW(), updated_at=NOW() WHERE id=%s
                    """,
                    (row["id"],),
                )
        payload = row["payload"]
        return Job(
            id=row["id"],
            run_id=row.get("run_id") or row["id"],
            team_id=row["team_id"],
            user_id=row["user_id"],
            kind=row["kind"],
            payload=payload if isinstance(payload, dict) else json.loads(payload),
            attempts=int(row["attempts"]) + 1,
        )

    def heartbeat_job(self, job_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs SET locked_at=NOW(), updated_at=NOW()
                WHERE id=%s AND status='running'
                """,
                (job_id,),
            )

    def enqueue_daily_digest_once(
        self,
        *,
        team_id: str,
        user_id: str,
        local_date: str,
    ) -> bool:
        job_id = uuid4().hex
        run_id = uuid4().hex
        with self._connect() as connection:
            with connection.transaction():
                inserted = connection.execute(
                    """
                    INSERT INTO daily_deliveries(team_id, user_id, local_date)
                    VALUES (%s, %s, %s)
                    ON CONFLICT DO NOTHING RETURNING local_date
                    """,
                    (team_id, user_id, local_date),
                ).fetchone()
                if inserted is None:
                    return False
                connection.execute(
                    """
                    INSERT INTO jobs(id, run_id, team_id, user_id, kind, payload)
                    VALUES (%s, %s, %s, %s, 'daily_digest', '{}'::jsonb)
                    """,
                    (job_id, run_id, team_id, user_id),
                )
        return True

    def finish_job(
        self, job_id: str, *, error: str = "", retry: bool = False
    ) -> None:
        status = "queued" if error and retry else ("failed" if error else "done")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs SET status=%s,
                    payload=CASE WHEN %s THEN payload ELSE '{}'::jsonb END,
                    last_error=%s, run_after=NOW() + INTERVAL '30 seconds',
                    locked_at=NULL, updated_at=NOW() WHERE id=%s
                """,
                (status, retry, error[:500], job_id),
            )

    def save_draft(
        self,
        *,
        team_id: str,
        user_id: str,
        channel_id: str,
        body: str,
        draft_id: str = "",
    ) -> str:
        draft_id = draft_id or uuid4().hex
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO drafts(id, team_id, user_id, channel_id, body)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT(id) DO UPDATE SET body=excluded.body
                """,
                (draft_id, team_id, user_id, channel_id, body),
            )
        return draft_id

    def record_draft_feedback(
        self, *, team_id: str, user_id: str, draft_id: str, outcome: str,
        edited_body: str = ""
    ) -> bool:
        if outcome not in {"accepted", "modified", "rejected"}:
            return False
        with self._connect() as connection:
            row = connection.execute(
                """
                INSERT INTO draft_feedback(draft_id, outcome, edited_body)
                SELECT id, %s, %s FROM drafts
                WHERE id=%s AND team_id=%s AND user_id=%s
                RETURNING draft_id
                """,
                (outcome, edited_body[:4_000], draft_id, team_id, user_id),
            ).fetchone()
            if row is not None:
                connection.execute(
                    "UPDATE drafts SET status=%s WHERE id=%s", (outcome, draft_id)
                )
        return row is not None

    def save_action(self, action: ActionItem, evidence: ActionEvidence) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO actions(
                    id, team_id, user_id, source, source_id, action_type, summary,
                    status, confidence, priority_score, requester_id, explicit_ask,
                    due_at, channel_id, thread_id, permalink, snoozed_until,
                    resolution_reason, last_checked_at, created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    NULLIF(%s, '')::timestamptz, %s, %s, %s,
                    NULLIF(%s, '')::timestamptz, %s,
                    NULLIF(%s, '')::timestamptz, %s::timestamptz, NOW()
                )
                ON CONFLICT(id) DO UPDATE SET
                    summary=excluded.summary,
                    confidence=excluded.confidence,
                    requester_id=excluded.requester_id,
                    explicit_ask=excluded.explicit_ask,
                    due_at=COALESCE(excluded.due_at, actions.due_at),
                    channel_id=excluded.channel_id,
                    thread_id=excluded.thread_id,
                    permalink=CASE WHEN excluded.permalink != '' THEN excluded.permalink ELSE actions.permalink END,
                    updated_at=NOW()
                """,
                (
                    action.id, action.team_id, action.user_id, action.source,
                    action.source_id, action.action_type, action.summary, action.status,
                    action.confidence, action.priority_score, action.requester_id,
                    action.explicit_ask, action.due_at, action.channel_id,
                    action.thread_id, action.permalink, action.snoozed_until,
                    action.resolution_reason, action.last_checked_at,
                    action.created_at or _now(),
                ),
            )
            connection.execute(
                """
                INSERT INTO action_evidence(
                    action_id, source, source_id, excerpt, permalink, actor_id, observed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(action_id, source, source_id) DO NOTHING
                """,
                (
                    action.id, evidence.source, evidence.source_id, evidence.excerpt,
                    evidence.permalink, evidence.actor_id, evidence.observed_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO action_events(action_id, from_status, to_status, reason)
                SELECT %s, '', %s, 'extracted'
                WHERE NOT EXISTS (SELECT 1 FROM action_events WHERE action_id=%s)
                """,
                (action.id, action.status, action.id),
            )

    def list_actions(
        self, *, team_id: str, user_id: str, include_candidates: bool = True
    ) -> list[ActionItem]:
        statuses = ["candidate", "open", "waiting"] if include_candidates else ["open", "waiting"]
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM actions
                WHERE team_id=%s AND user_id=%s AND status=ANY(%s)
                ORDER BY priority_score DESC, due_at NULLS LAST, updated_at DESC
                """,
                (team_id, user_id, statuses),
            ).fetchall()
        return [_action_from_row(row) for row in rows]

    def get_action(
        self, *, team_id: str, user_id: str, selector: str
    ) -> ActionItem | None:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM actions
                WHERE team_id=%s AND user_id=%s AND (id=%s OR id LIKE %s)
                ORDER BY CASE WHEN id=%s THEN 0 ELSE 1 END LIMIT 2
                """,
                (team_id, user_id, selector, selector + "%", selector),
            ).fetchall()
        if not rows or (len(rows) > 1 and rows[0]["id"] != selector):
            return None
        return _action_from_row(rows[0])

    def latest_action_evidence(self, action_id: str) -> ActionEvidence | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM action_evidence WHERE action_id=%s
                ORDER BY id DESC LIMIT 1
                """,
                (action_id,),
            ).fetchone()
        return _evidence_from_row(row) if row else None

    def transition_action(
        self,
        *,
        action_id: str,
        status: ActionStatus,
        reason: str,
        snoozed_until: str = "",
        evidence_source_id: str = "",
    ) -> bool:
        with self._connect() as connection:
            with connection.transaction():
                row = connection.execute(
                    "SELECT status FROM actions WHERE id=%s FOR UPDATE", (action_id,)
                ).fetchone()
                if row is None:
                    return False
                connection.execute(
                    """
                    UPDATE actions SET status=%s, resolution_reason=%s,
                        snoozed_until=NULLIF(%s, '')::timestamptz,
                        last_checked_at=NOW(), updated_at=NOW() WHERE id=%s
                    """,
                    (status, reason[:500], snoozed_until, action_id),
                )
                connection.execute(
                    """
                    INSERT INTO action_events(
                        action_id, from_status, to_status, reason, evidence_source_id
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (action_id, row["status"], status, reason[:500], evidence_source_id),
                )
        return True

    def record_action_feedback(
        self, *, action_id: str, outcome: str, detail: str = ""
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO action_feedback(action_id, outcome, detail)
                VALUES (%s, %s, %s)
                """,
                (action_id, outcome[:40], detail[:500]),
            )

    def set_source_cursor(
        self, *, team_id: str, user_id: str, source: str, cursor: str
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO source_cursors(team_id, user_id, source, cursor)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(team_id, user_id, source) DO UPDATE SET
                    cursor=excluded.cursor, updated_at=NOW()
                """,
                (team_id, user_id, source, cursor),
            )

    def get_source_cursor(
        self, *, team_id: str, user_id: str, source: str
    ) -> str:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT cursor FROM source_cursors
                WHERE team_id=%s AND user_id=%s AND source=%s
                """,
                (team_id, user_id, source),
            ).fetchone()
        return str(row["cursor"]) if row else ""

    def claim_notification(
        self, *, action_id: str, channel: str, policy_key: str
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                INSERT INTO notification_deliveries(action_id, channel, policy_key)
                VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING RETURNING id
                """,
                (action_id, channel, policy_key),
            ).fetchone()
        return row is not None

    def notification_was_delivered(
        self, *, action_id: str, channel: str, policy_key: str
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM notification_deliveries
                WHERE action_id=%s AND channel=%s AND policy_key=%s
                """,
                (action_id, channel, policy_key),
            ).fetchone()
        return row is not None

    def start_agent_run(
        self,
        *,
        context: RunContext,
        agent_name: str,
        agent_version: int,
        config: dict[str, Any],
    ) -> None:
        actor_id = attributed_user_id(context.actor) or getattr(
            context.actor, "service", getattr(context.actor, "component", "")
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_runs(
                    id, team_id, user_id, actor_kind, actor_id, surface,
                    thread_id, agent_name, agent_version, config
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT(id) DO UPDATE SET
                    status='running', attempts=agent_runs.attempts+1,
                    config=excluded.config, updated_at=NOW()
                """,
                (
                    context.run_id, context.tenant_id, context.user_id or "",
                    context.actor.kind, actor_id, context.surface,
                    context.thread_id or "", agent_name, agent_version,
                    json.dumps(config),
                ),
            )

    def finish_agent_run(
        self, run_id: str, *, error: str = "", retrying: bool = False
    ) -> None:
        status = "retrying" if retrying else ("failed" if error else "completed")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE agent_runs SET status=%s, last_error=%s, updated_at=NOW()
                WHERE id=%s
                """,
                (status, error[:500], run_id),
            )

    def get_completed_run_step(self, run_id: str, step_name: str) -> Any | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT output FROM run_steps
                WHERE run_id=%s AND step_name=%s AND status='completed'
                """,
                (run_id, step_name),
            ).fetchone()
        if not row:
            return None
        output = row["output"]
        return output if not isinstance(output, str) else json.loads(output)

    def start_run_step(
        self, *, run_id: str, step_name: str, idempotency_key: str
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO run_steps(run_id, step_name, idempotency_key)
                VALUES (%s, %s, %s)
                ON CONFLICT(run_id, step_name) DO UPDATE SET
                    status='running', attempts=run_steps.attempts+1,
                    last_error='', updated_at=NOW()
                """,
                (run_id, step_name, idempotency_key),
            )

    def finish_run_step(
        self,
        *,
        run_id: str,
        step_name: str,
        output: Any = None,
        error: str = "",
    ) -> None:
        status = "failed" if error else "completed"
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE run_steps SET status=%s, output=%s::jsonb,
                    last_error=%s, updated_at=NOW()
                WHERE run_id=%s AND step_name=%s
                """,
                (status, json.dumps(output, default=str), error[:500], run_id, step_name),
            )

    def start_tool_call(
        self,
        *,
        call_id: str,
        run_id: str,
        step_name: str,
        tool_name: str,
        idempotency_key: str,
        policy: dict[str, Any],
        arguments: Any,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tool_calls(
                    id, run_id, step_name, tool_name, idempotency_key,
                    policy, arguments, status
                ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, 'running')
                ON CONFLICT(id) DO UPDATE SET
                    status='running', last_error='', updated_at=NOW()
                """,
                (
                    call_id, run_id, step_name, tool_name, idempotency_key,
                    json.dumps(policy), json.dumps(arguments),
                ),
            )

    def finish_tool_call(
        self, call_id: str, *, output: Any = None, error: str = ""
    ) -> None:
        status = "failed" if error else "completed"
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE tool_calls SET status=%s, output=%s::jsonb,
                    last_error=%s, updated_at=NOW() WHERE id=%s
                """,
                (status, json.dumps(output, default=str), error[:500], call_id),
            )

    def get_completed_tool_call(self, idempotency_key: str) -> Any | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT output FROM tool_calls
                WHERE idempotency_key=%s AND status='completed'
                """,
                (idempotency_key,),
            ).fetchone()
        if not row:
            return None
        output = row["output"]
        return output if not isinstance(output, str) else json.loads(output)


def build_store(database_url: str) -> HostedStore:
    if database_url.startswith("sqlite:///"):
        return SQLiteStore(Path(database_url.removeprefix("sqlite:///")))
    if database_url.startswith(("postgresql://", "postgres://")):
        return PostgresStore(database_url)
    raise ValueError("DATABASE_URL must use sqlite:/// or postgresql://.")
