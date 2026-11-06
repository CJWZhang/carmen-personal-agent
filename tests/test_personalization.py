from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from carmen_agent.connectors.direct.slack import SlackDirectConnector
from carmen_agent.personalization import (
    PersonalizationError,
    _collect_bridge,
    _validate_messages,
    personalization_status,
    personalize,
)


class SlackTransport:
    def __init__(self) -> None:
        self.queries: list[dict[str, object]] = []

    def request(self, url: str, **kwargs):
        if url.endswith("/auth.test"):
            return {"ok": True, "user_id": "U-CARMEN"}
        query = kwargs.get("query") or {}
        self.queries.append(query)
        return {
            "ok": True,
            "messages": {
                "matches": [
                    {
                        "ts": "1784823275.092429",
                        "user_id": "U-CARMEN",
                        "text": "perfect, let's ship this :party_blob:",
                        "channel": {"id": "C1", "name": "launch"},
                    },
                    {
                        "ts": "1784823276.092429",
                        "user_id": "U-SOMEONE-ELSE",
                        "text": "This was not written by Carmen.",
                        "channel": {"id": "C1", "name": "launch"},
                    },
                ],
                "paging": {"pages": 1},
            },
        }


def test_direct_slack_style_collection_is_bounded_to_authenticated_author(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CARMEN_SLACK_USER_TOKEN", "xoxp-private")
    transport = SlackTransport()

    messages = SlackDirectConnector(transport=transport).collect_authored_messages(days=90)

    assert messages == [
        {
            "id": "1784823275.092429",
            "channel_type": "channel",
            "timestamp": "2026-07-23T16:14:35.092429+00:00",
            "text": "perfect, let's ship this :party_blob:",
        }
    ]
    assert transport.queries[0]["query"].startswith("from:me after:")
    assert transport.queries[0]["count"] == 100


def test_style_corpus_shape_rejects_unbounded_or_extra_data() -> None:
    with pytest.raises(PersonalizationError, match="invalid shape"):
        _validate_messages(
            [
                {
                    "id": "1",
                    "channel_type": "dm",
                    "timestamp": "2026-08-13T12:00:00Z",
                    "text": "hello",
                    "sender": "someone else",
                }
            ]
        )
    with pytest.raises(PersonalizationError, match="invalid size"):
        _validate_messages([] * 0 + [
            {"id": str(index), "channel_type": "dm", "timestamp": "", "text": "hello"}
            for index in range(501)
        ])


def test_style_corpus_accepts_authenticated_sent_email() -> None:
    assert _validate_messages(
        [
            {
                "id": "gmail:message-1",
                "channel_type": "email",
                "timestamp": "2026-08-13T12:00:00Z",
                "text": "Thanks for the thoughtful update. I will follow up tomorrow.",
            }
        ]
    )[0]["channel_type"] == "email"


def test_bridge_requests_authored_gmail_sent_bodies_without_quoted_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema = tmp_path / "style-schema.json"
    schema.write_text("{}\n", encoding="utf-8")
    observed: dict[str, object] = {}

    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        observed["command"] = command
        observed["timeout"] = kwargs["timeout"]
        output = Path(command[command.index("--output-last-message") + 1])
        output.write_text(
            json.dumps(
                {
                    "messages": [
                        {
                            "id": "gmail:message-1",
                            "channel_type": "email",
                            "timestamp": "2026-08-13T12:00:00Z",
                            "text": "Thanks for the update. I will review this tomorrow.",
                        }
                    ],
                    "detail": "Collected one authenticated sent email.",
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("carmen_agent.personalization.STYLE_SCHEMA_PATH", schema)
    monkeypatch.setattr("carmen_agent.personalization.shutil.which", lambda _name: "/usr/bin/codex")
    monkeypatch.setattr("carmen_agent.personalization.subprocess.run", run)

    messages = _collect_bridge(180, timeout=10)

    assert messages[0]["channel_type"] == "email"
    command = observed["command"]
    assert isinstance(command, list)
    prompt = command[-1]
    assert "in:sent" in prompt
    assert "authenticated account" in prompt
    assert "quoted thread history" in prompt
    assert observed["timeout"] == 10


def test_profile_personalization_sanitizes_locally_and_activates_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = tmp_path / "data" / "raw" / "slack_messages.jsonl"
    processed = tmp_path / "data" / "processed"
    raw.parent.mkdir(parents=True)
    raw.write_text(
        "".join(
            json.dumps(
                {
                    "id": str(index),
                    "channel_type": "dm" if index % 2 else "channel",
                    "timestamp": f"2026-08-13T12:{index:02d}:00Z",
                    "text": f"quick update number {index}: email me at carmen@example.com :party_blob:",
                }
            )
            + "\n"
            for index in range(55)
        ),
        encoding="utf-8",
    )
    saved: dict[str, object] = {}
    monkeypatch.setattr("carmen_agent.personalization.RAW_STYLE_PATH", raw)
    monkeypatch.setattr("carmen_agent.personalization.PROCESSED_DIR", processed)
    monkeypatch.setattr(
        "carmen_agent.personalization.save_user_config",
        lambda values: saved.update(values),
    )

    personalize(mode="profile")

    profile = json.loads((processed / "style_profile.json").read_text(encoding="utf-8"))
    training = (processed / "train.jsonl").read_text(encoding="utf-8")
    assert profile["message_count"] == 55
    assert "carmen@example.com" not in training
    assert "<EMAIL>" in training
    assert saved == {"voice_mode": "profile"}


def test_personalization_status_reports_active_local_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = tmp_path / "raw.jsonl"
    raw.write_text("{}\n" * 60, encoding="utf-8")
    profile = tmp_path / "style_profile.json"
    profile.write_text("{}\n", encoding="utf-8")
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"filtering": {"kept": 54}}), encoding="utf-8")
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    (adapter / "adapters.safetensors").write_bytes(b"adapter")
    monkeypatch.setattr("carmen_agent.personalization.RAW_STYLE_PATH", raw)
    monkeypatch.setattr("carmen_agent.personalization.STYLE_PROFILE_PATH", profile)
    monkeypatch.setattr("carmen_agent.personalization.REPORT_PATH", report)
    monkeypatch.setattr("carmen_agent.personalization.DEFAULT_ADAPTER_DIR", adapter)
    monkeypatch.setattr("carmen_agent.personalization.supports_lora", lambda: True)
    monkeypatch.setattr(
        "carmen_agent.personalization.config_value",
        lambda *_args: "lora",
    )
    monkeypatch.setattr(SlackDirectConnector, "available", lambda _self: True)

    status = personalization_status()

    assert status.stage == "trained"
    assert status.message_count == 60
    assert status.usable_message_count == 54
    assert status.profile_ready is True
    assert status.adapter_active is True
    assert status.collection_transport == "direct Slack API"


def test_full_personalization_uses_packaged_python_training_and_activates_lora(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = tmp_path / "raw.jsonl"
    raw.write_text("{}\n" * 50, encoding="utf-8")
    processed = tmp_path / "processed"
    adapter = tmp_path / "adapter"
    commands: list[list[str]] = []
    saved: list[dict[str, object]] = []

    def run(command, **_kwargs):
        commands.append(command)
        adapter.mkdir(parents=True)
        (adapter / "adapters.safetensors").write_bytes(b"trained locally")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("carmen_agent.personalization.RAW_STYLE_PATH", raw)
    monkeypatch.setattr("carmen_agent.personalization.PROCESSED_DIR", processed)
    monkeypatch.setattr("carmen_agent.personalization.DEFAULT_ADAPTER_DIR", adapter)
    monkeypatch.setattr("carmen_agent.personalization.supports_lora", lambda: True)
    monkeypatch.setattr(
        "carmen_agent.personalization.prepare",
        lambda *_args: {"filtering": {"kept": 50}},
    )
    monkeypatch.setattr(
        "carmen_agent.personalization.save_user_config",
        lambda values: saved.append(values),
    )
    monkeypatch.setattr("carmen_agent.personalization.subprocess.run", run)

    personalize(mode="full")

    assert commands[0][1:4] == ["-m", "mlx_lm.lora", "--config"]
    assert commands[0][4].endswith("lora.yaml")
    assert saved == [{"voice_mode": "profile"}, {"voice_mode": "lora"}]


def test_existing_local_adapter_can_be_activated_without_retraining(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "style_profile.json"
    profile.write_text("{}\n", encoding="utf-8")
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    (adapter / "adapters.safetensors").write_bytes(b"already trained")
    saved: dict[str, object] = {}
    monkeypatch.setattr("carmen_agent.personalization.STYLE_PROFILE_PATH", profile)
    monkeypatch.setattr("carmen_agent.personalization.DEFAULT_ADAPTER_DIR", adapter)
    monkeypatch.setattr("carmen_agent.personalization.supports_lora", lambda: True)
    monkeypatch.setattr(
        "carmen_agent.personalization.save_user_config",
        lambda values: saved.update(values),
    )
    monkeypatch.setattr(
        "carmen_agent.personalization.subprocess.run",
        lambda *_args, **_kwargs: pytest.fail("activation must not retrain"),
    )

    personalize(mode="activate")

    assert saved == {"voice_mode": "lora"}
