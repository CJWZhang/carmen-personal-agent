from __future__ import annotations

import io
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from carmen_agent.platforms import parse_command
from carmen_agent.providers.base import OpenAICompatibleProvider
from carmen_agent.reminders import ReminderEvent, send_desktop_notification
from carmen_agent.scheduler import SchedulerConfig, run_tick
from carmen_agent.setup import diagnostics, initialize
from carmen_agent.skills.model import default_local_provider
from carmen_agent.skills.cli import build_parser as build_daily_parser
from carmen_agent.skills.schedule import build_systemd_service, build_windows_launcher


def test_command_parser_supports_json_and_windows_quoted_paths() -> None:
    assert parse_command('["python", "worker.py", "--flag"]') == [
        "python", "worker.py", "--flag"
    ]
    assert parse_command('"C:\\Program Files\\Agent\\run.exe" --once', windows=True) == [
        "C:\\Program Files\\Agent\\run.exe", "--once"
    ]


def test_non_apple_default_provider_is_ollama(monkeypatch) -> None:
    monkeypatch.delenv("CARMEN_MODEL_PROVIDER", raising=False)
    monkeypatch.setattr("carmen_agent.skills.model.config_value", lambda *args: "")
    monkeypatch.setattr("carmen_agent.skills.model.sys.platform", "linux")
    assert default_local_provider() == "ollama"


def test_daily_command_uses_configured_model_unless_explicitly_overridden() -> None:
    assert build_daily_parser().parse_args(["--no-collect"]).model is None


def test_apple_silicon_without_mlx_also_defaults_to_ollama(monkeypatch) -> None:
    monkeypatch.delenv("CARMEN_MODEL_PROVIDER", raising=False)
    monkeypatch.setattr("carmen_agent.skills.model.config_value", lambda *args: "")
    monkeypatch.setattr("carmen_agent.skills.model.sys.platform", "darwin")
    monkeypatch.setattr("carmen_agent.skills.model.platform.machine", lambda: "arm64")
    monkeypatch.setattr("carmen_agent.skills.model.importlib.util.find_spec", lambda _: None)
    assert default_local_provider() == "ollama"


def test_local_openai_compatible_endpoint_does_not_require_api_key(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":"hello"}}]}'

    def fake_open(request, timeout):
        captured["authorization"] = request.headers.get("Authorization")
        captured["payload"] = json.loads(request.data)
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    provider = OpenAICompatibleProvider(
        api_key="", model="qwen3:4b", base_url="http://127.0.0.1:11434/v1"
    )
    assert provider.generate(
        [{"role": "user", "content": "hi"}], top_p=0.75
    ) == "hello"
    assert captured["authorization"] is None
    assert captured["payload"]["top_p"] == 0.75  # type: ignore[index]
    assert captured["payload"]["reasoning_effort"] == "none"  # type: ignore[index]


def test_portable_scheduler_runs_daily_once_and_reminders_on_interval(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(command, **_):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    state = tmp_path / "scheduler.json"
    now = datetime(2026, 8, 12, 8, 5, tzinfo=timezone.utc)
    first = run_tick(
        config=SchedulerConfig(hour=8, minute=0, reminder_minutes=30),
        state_path=state,
        now=now,
        runner=runner,
        python_executable="python",
    )
    assert first.daily_ran and first.reminders_ran
    second = run_tick(
        config=SchedulerConfig(hour=8, minute=0, reminder_minutes=30),
        state_path=state,
        now=now + timedelta(minutes=10),
        runner=runner,
        python_executable="python",
    )
    assert not second.daily_ran and not second.reminders_ran
    third = run_tick(
        config=SchedulerConfig(hour=8, minute=0, reminder_minutes=30),
        state_path=state,
        now=now + timedelta(minutes=31),
        runner=runner,
        python_executable="python",
    )
    assert not third.daily_ran and third.reminders_ran
    assert len(calls) == 3


def test_portable_scheduler_can_run_without_reminders(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(command, **_):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    tick = run_tick(
        config=SchedulerConfig(reminders_enabled=False),
        state_path=tmp_path / "scheduler.json",
        now=datetime(2026, 8, 12, 8, 5, tzinfo=timezone.utc),
        runner=runner,
        python_executable="python",
    )
    assert tick.daily_ran and not tick.reminders_ran
    assert calls == [["python", "-m", "carmen_agent.skills.cli"]]


def test_portable_scheduler_defaults_to_five_am() -> None:
    assert SchedulerConfig().hour == 5
    assert SchedulerConfig().minute == 0


def test_linux_and_windows_service_previews_use_portable_scheduler(tmp_path: Path) -> None:
    python = tmp_path / "Python With Spaces" / "python.exe"
    systemd = build_systemd_service(python_executable=python)
    windows = build_windows_launcher(python_executable=python)
    assert "carmen_agent.scheduler" in systemd
    assert '"--hour" "5"' in systemd
    assert f'"{python}"' in systemd
    assert "carmen_agent.scheduler" in windows
    assert "--hour 5" in windows
    assert f'"{python}"' in windows


def test_portable_service_previews_can_disable_reminders(tmp_path: Path) -> None:
    python = tmp_path / "python"
    assert "--no-reminders" in build_systemd_service(
        python_executable=python, no_reminders=True
    )
    assert "--no-reminders" in build_windows_launcher(
        python_executable=python, no_reminders=True
    )


def test_notification_dispatch_can_use_console_on_any_os(capsys, monkeypatch) -> None:
    monkeypatch.setenv("CARMEN_NOTIFICATION_BACKEND", "console")
    send_desktop_notification(
        ReminderEvent("k", "t", "Prepare launch notes", "Due today"),
        system="Unknown",
    )
    assert "Prepare launch notes" in capsys.readouterr().err


def test_initialize_creates_portable_home_and_non_secret_config(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("carmen_agent.configuration.CONFIG_PATH", tmp_path / "config.json")
    # Default argument values are bound at import time, so pass the path through
    # the shared save function used by setup.
    monkeypatch.setattr(
        "carmen_agent.setup.save_user_config",
        lambda values: __import__("carmen_agent.configuration", fromlist=["save_user_config"]).save_user_config(
            values, tmp_path / "config.json"
        ),
    )
    path = initialize(
        provider="ollama", model="qwen3:4b", base_url="http://127.0.0.1:11434/v1"
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["model_provider"] == "ollama"
    assert raw["user_name"]
    assert "api_key" not in raw


def test_diagnostics_supports_linux_and_windows_simulation(monkeypatch) -> None:
    monkeypatch.setattr("carmen_agent.setup.default_local_provider", lambda: "ollama")
    for system in ("Linux", "Windows"):
        result = diagnostics(system=system, machine="x86_64")
        assert any(item.name == "Platform" and system in item.detail for item in result)
        assert not any(item.level == "error" for item in result)
