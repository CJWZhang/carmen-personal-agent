from __future__ import annotations

import hashlib
import hmac
import io
import json
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from carmen_agent.hosted.app import HostedApplication
from carmen_agent.hosted.config import HostedSettings
from carmen_agent.hosted.security import verify_slack_signature
from carmen_agent.hosted.slack_api import OAuthResult
from carmen_agent.hosted.storage import HostedUser, Installation, SQLiteStore
from carmen_agent.hosted.worker import HostedWorker, enqueue_due_daily_digests


class FakeCipher:
    def encrypt(self, value: str) -> str:
        return "encrypted:" + value

    def decrypt(self, value: str) -> str:
        assert value.startswith("encrypted:")
        return value.removeprefix("encrypted:")


class FakeSlack:
    def __init__(self) -> None:
        self.dms: list[tuple[str, str, str]] = []

    def exchange_oauth_code(self, **_: str) -> OAuthResult:
        return OAuthResult(
            team_id="T1",
            team_name="Test",
            enterprise_id="",
            bot_user_id="B1",
            bot_token="xoxb-secret",
            user_id="U1",
            user_token="xoxp-secret",
            scopes="commands,chat:write",
        )

    def recent_context(self, **_: object) -> list[dict[str, str]]:
        return [{"user": "U2", "text": "can you review this?", "ts": "1.0"}]

    def post_dm(
        self, *, token: str, user_id: str, text: str, client_msg_id: str = ""
    ) -> None:
        self.dms.append((token, user_id, text))


class FakeProvider:
    def generate(self, messages, **_):
        assert messages
        return "yep, taking a look now!"


def _settings(database_url: str) -> HostedSettings:
    return HostedSettings(
        public_base_url="https://assistant.example",
        database_url=database_url,
        slack_client_id="client",
        slack_client_secret="client-secret",
        slack_signing_secret="signing-secret",
        token_encryption_key="unused-in-test",
        bot_scopes=("commands", "chat:write", "im:write"),
        user_scopes=("channels:history",),
    )


def _sign(body: bytes, timestamp: str, secret: str = "signing-secret") -> str:
    base = b"v0:" + timestamp.encode("ascii") + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def _request(
    app: HostedApplication,
    *,
    method: str,
    path: str,
    body: bytes = b"",
    query: str = "",
    signed: bool = False,
) -> tuple[str, dict[str, str], bytes]:
    timestamp = str(int(time.time()))
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body),
    }
    if signed:
        environ["HTTP_X_SLACK_REQUEST_TIMESTAMP"] = timestamp
        environ["HTTP_X_SLACK_SIGNATURE"] = _sign(body, timestamp)
    captured: dict[str, object] = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = dict(headers)

    output = b"".join(app(environ, start_response))
    return (
        str(captured["status"]),
        captured["headers"],  # type: ignore[return-value]
        output,
    )


def _installed_store(path: Path) -> SQLiteStore:
    store = SQLiteStore(path)
    store.save_installation(
        Installation(
            team_id="T1",
            team_name="Test",
            enterprise_id="",
            bot_user_id="B1",
            bot_token_encrypted="encrypted:xoxb-secret",
            user_token_encrypted="",
            installing_user_id="U1",
            scopes="commands",
        )
    )
    store.save_user(
        HostedUser(
            team_id="T1",
            user_id="U1",
            user_token_encrypted="encrypted:xoxp-secret",
        )
    )
    return store


def test_slack_signature_rejects_tampering_and_stale_requests() -> None:
    body = b"command=%2Fdraft"
    timestamp = "1000"
    signature = _sign(body, timestamp)
    assert verify_slack_signature(
        signing_secret="signing-secret",
        timestamp=timestamp,
        signature=signature,
        body=body,
        now=1000,
    )
    assert not verify_slack_signature(
        signing_secret="signing-secret",
        timestamp=timestamp,
        signature=signature,
        body=body + b"x",
        now=1000,
    )
    assert not verify_slack_signature(
        signing_secret="signing-secret",
        timestamp=timestamp,
        signature=signature,
        body=body,
        now=1400,
    )


def test_migrate_command_only_requires_database_url(monkeypatch) -> None:
    from carmen_agent.hosted import cli

    for name in (
        "CARMEN_PUBLIC_BASE_URL",
        "SLACK_CLIENT_ID",
        "SLACK_CLIENT_SECRET",
        "SLACK_SIGNING_SECRET",
        "CARMEN_TOKEN_ENCRYPTION_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://migration-only/test")
    captured: list[str] = []
    monkeypatch.setattr(cli, "migrate", captured.append)
    monkeypatch.setattr("sys.argv", ["carmen-hosted", "migrate"])

    cli.main()

    assert captured == ["postgresql://migration-only/test"]


def test_oauth_state_is_one_time_and_install_stores_encrypted_user_token(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "hosted.db")
    app = HostedApplication(
        settings=_settings("sqlite:///unused"),
        store=store,
        cipher=FakeCipher(),
        slack=FakeSlack(),  # type: ignore[arg-type]
    )
    status, headers, _ = _request(app, method="GET", path="/slack/install")
    assert status == "302 Found"
    state = urllib.parse.parse_qs(
        urllib.parse.urlsplit(headers["Location"]).query
    )["state"][0]
    query = urllib.parse.urlencode({"code": "temporary", "state": state})
    status, _, _ = _request(
        app,
        method="GET",
        path="/slack/oauth/callback",
        query=query,
    )
    assert status == "200 OK"
    user = store.get_user("T1", "U1")
    assert user is not None
    assert user.user_token_encrypted == "encrypted:xoxp-secret"
    replay_status, _, _ = _request(
        app,
        method="GET",
        path="/slack/oauth/callback",
        query=query,
    )
    assert replay_status == "400 Bad Request"


def test_events_store_only_connected_users_own_messages(tmp_path: Path) -> None:
    store = _installed_store(tmp_path / "hosted.db")
    app = HostedApplication(
        settings=_settings("sqlite:///unused"),
        store=store,
        cipher=FakeCipher(),
        slack=FakeSlack(),  # type: ignore[arg-type]
    )
    for event_id, user_id, text in (
        ("E1", "U2", "coworker private text"),
        ("E2", "U1", "i'll send the review tomorrow"),
    ):
        body = json.dumps(
            {
                "type": "event_callback",
                "team_id": "T1",
                "event_id": event_id,
                "event": {
                    "type": "message",
                    "user": user_id,
                    "channel": "C1",
                    "ts": event_id,
                    "text": text,
                },
            }
        ).encode()
        status, _, _ = _request(
            app,
            method="POST",
            path="/slack/events",
            body=body,
            signed=True,
        )
        assert status == "200 OK"
    messages = store.recent_authored_messages(team_id="T1", user_id="U1")
    assert messages == ["i'll send the review tomorrow"]
    assert "coworker" not in "\n".join(messages)


def test_draft_command_queues_private_worker_job(tmp_path: Path) -> None:
    store = _installed_store(tmp_path / "hosted.db")
    slack = FakeSlack()
    app = HostedApplication(
        settings=_settings("sqlite:///unused"),
        store=store,
        cipher=FakeCipher(),
        slack=slack,  # type: ignore[arg-type]
    )
    body = urllib.parse.urlencode(
        {
            "command": "/draft",
            "team_id": "T1",
            "user_id": "U1",
            "channel_id": "C1",
            "text": "say I can review it",
        }
    ).encode()
    status, _, response = _request(
        app,
        method="POST",
        path="/slack/commands",
        body=body,
        signed=True,
    )
    assert status == "200 OK"
    assert "Drafting" in response.decode()
    worker = HostedWorker(
        store=store,
        cipher=FakeCipher(),
        slack=slack,  # type: ignore[arg-type]
        provider=FakeProvider(),  # type: ignore[arg-type]
    )
    assert worker.run_once()
    assert slack.dms[0][0] == "xoxb-secret"
    assert "yep, taking a look now!" in slack.dms[0][2]
    with store._connect() as connection:
        run = connection.execute("SELECT status FROM agent_runs").fetchone()
        calls = connection.execute(
            "SELECT tool_name, status, arguments FROM tool_calls ORDER BY tool_name"
        ).fetchall()
    assert run is not None and run["status"] == "completed"
    assert [row["tool_name"] for row in calls] == [
        "slack_post_assistant_dm",
        "slack_recent_context",
    ]
    assert all(row["status"] == "completed" for row in calls)
    assert "xox" not in "\n".join(str(row["arguments"]) for row in calls)


def test_failed_hosted_job_retries_with_same_run_and_no_duplicate_dm(tmp_path: Path) -> None:
    store = _installed_store(tmp_path / "hosted.db")
    store.enqueue_job(
        team_id="T1",
        user_id="U1",
        kind="draft",
        payload={"channel_id": "C1", "request": "say I can review it"},
    )

    class FailOnceSlack(FakeSlack):
        def __init__(self) -> None:
            super().__init__()
            self.fail = True

        def post_dm(
            self, *, token: str, user_id: str, text: str, client_msg_id: str = ""
        ) -> None:
            if self.fail:
                self.fail = False
                raise RuntimeError("temporary Slack failure")
            super().post_dm(
                token=token, user_id=user_id, text=text, client_msg_id=client_msg_id
            )

    slack = FailOnceSlack()
    worker = HostedWorker(
        store=store,
        cipher=FakeCipher(),
        slack=slack,  # type: ignore[arg-type]
        provider=FakeProvider(),  # type: ignore[arg-type]
    )
    assert worker.run_once()
    with store._connect() as connection:
        failed = connection.execute("SELECT * FROM jobs").fetchone()
        run_id = str(failed["run_id"])
        connection.execute(
            "UPDATE jobs SET run_after='2000-01-01T00:00:00+00:00'"
        )
    assert failed["status"] == "queued"
    assert json.loads(failed["payload"])["request"] == "say I can review it"

    assert worker.run_once()
    with store._connect() as connection:
        completed = connection.execute("SELECT * FROM jobs").fetchone()
        run = connection.execute(
            "SELECT status, attempts FROM agent_runs WHERE id=?", (run_id,)
        ).fetchone()
        drafts = connection.execute("SELECT COUNT(*) AS count FROM drafts").fetchone()
    assert completed["status"] == "done"
    assert completed["payload"] == "{}"
    assert run["status"] == "completed" and run["attempts"] == 2
    assert drafts["count"] == 1
    assert len(slack.dms) == 1


def test_daily_digest_queue_is_timezone_aware_and_deduplicated(tmp_path: Path) -> None:
    store = _installed_store(tmp_path / "hosted.db")
    store.save_user(
        HostedUser(
            team_id="T1",
            user_id="U1",
            user_token_encrypted="encrypted:xoxp-secret",
            timezone="America/New_York",
            daily_hour=8,
        )
    )
    now = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
    assert enqueue_due_daily_digests(store, now=now) == 1
    assert enqueue_due_daily_digests(store, now=now) == 0


def test_incoming_direct_ask_becomes_evidenced_action_without_entering_style_corpus(
    tmp_path: Path,
) -> None:
    store = _installed_store(tmp_path / "hosted.db")
    slack = FakeSlack()
    app = HostedApplication(
        settings=_settings("sqlite:///unused"),
        store=store,
        cipher=FakeCipher(),
        slack=slack,  # type: ignore[arg-type]
    )
    body = json.dumps(
        {
            "type": "event_callback",
            "team_id": "T1",
            "event_id": "E-direct",
            "event": {
                "type": "message",
                "user": "U2",
                "channel": "D1",
                "ts": "10.0",
                "text": "can you review the launch PR today?",
            },
        }
    ).encode()
    status, _, _ = _request(
        app, method="POST", path="/slack/events", body=body, signed=True
    )
    assert status == "200 OK"
    assert store.recent_authored_messages(team_id="T1", user_id="U1") == []
    worker = HostedWorker(
        store=store,
        cipher=FakeCipher(),
        slack=slack,  # type: ignore[arg-type]
        provider=FakeProvider(),  # type: ignore[arg-type]
    )
    assert worker.run_once()
    actions = store.list_actions(team_id="T1", user_id="U1")
    assert len(actions) == 1
    assert actions[0].action_type == "review"
    evidence = store.latest_action_evidence(actions[0].id)
    assert evidence is not None and "launch PR" in evidence.excerpt


def test_done_command_transitions_action_and_records_feedback(tmp_path: Path) -> None:
    store = _installed_store(tmp_path / "hosted.db")
    from carmen_agent.actions.schemas import ActionEvidence, ActionItem

    action = ActionItem.create(
        team_id="T1",
        user_id="U1",
        source="slack",
        source_id="11.0",
        action_type="reply",
        summary="Reply to the thread",
    )
    store.save_action(
        action,
        ActionEvidence(source="slack", source_id="11.0", excerpt="Can you reply?"),
    )
    app = HostedApplication(
        settings=_settings("sqlite:///unused"),
        store=store,
        cipher=FakeCipher(),
        slack=FakeSlack(),  # type: ignore[arg-type]
    )
    body = urllib.parse.urlencode(
        {
            "command": "/done",
            "team_id": "T1",
            "user_id": "U1",
            "channel_id": "C1",
            "text": action.id,
        }
    ).encode()
    status, _, response = _request(
        app, method="POST", path="/slack/commands", body=body, signed=True
    )
    assert status == "200 OK"
    assert "Marked done" in response.decode()
    assert store.list_actions(team_id="T1", user_id="U1") == []
