from __future__ import annotations

import argparse
import os
import platform
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from carmen_agent.apps.automation import (
    DEFAULT_DAILY_HOUR,
    DEFAULT_DAILY_MINUTE,
    DEFAULT_REMINDER_MINUTES,
)
from carmen_agent.paths import PROJECT_ROOT, WORK_DIR, WORKSPACE_CA_PATH

DAILY_LABEL = "com.carmen.morning-work-brief"
REMINDER_LABEL = "com.carmen.task-reminders"
LABEL = DAILY_LABEL
DEFAULT_OUTPUT = WORK_DIR / f"{DAILY_LABEL}.plist"
DEFAULT_REMINDER_OUTPUT = WORK_DIR / f"{REMINDER_LABEL}.plist"


def _resolve_ca_bundle(explicit: Path | None) -> Path | None:
    """Pick a CA bundle: explicit override, project-local Koi CA, or current env."""
    if explicit is not None:
        return explicit if explicit.is_file() else None
    if WORKSPACE_CA_PATH.is_file():
        return WORKSPACE_CA_PATH
    for env_var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        raw = os.environ.get(env_var, "").strip()
        if raw:
            candidate = Path(raw)
            if candidate.is_file():
                return candidate
    return None


def _default_python() -> Path:
    candidates = (
        PROJECT_ROOT / ".venv" / "Scripts" / "python.exe",
        PROJECT_ROOT / ".venv" / "bin" / "python",
    )
    for project_python in candidates:
        if project_python.is_file():
            return project_python
    return Path(sys.executable)


def _default_codex() -> Path | None:
    discovered = shutil.which("codex")
    if discovered:
        return Path(discovered)
    local_codex = Path.home() / ".local" / "bin" / "codex"
    return local_codex if local_codex.is_file() else None


def _launch_environment(
    *,
    python: Path,
    codex_executable: Path | None,
    ca_bundle: Path | None,
) -> dict[str, str]:
    env: dict[str, str] = {
        "PYTHONPATH": str(PROJECT_ROOT / "src"),
        "HOME": str(Path.home()),
    }
    path_entries = [str(python.parent)]
    if codex_executable is not None:
        path_entries.append(str(codex_executable.parent))
    path_entries.extend(
        [
            str(Path.home() / ".local" / "bin"),
            str(Path.home() / ".cargo" / "bin"),
            str(Path.home() / ".bun" / "bin"),
            "/opt/homebrew/bin",
            "/opt/homebrew/sbin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        ]
    )
    env["PATH"] = os.pathsep.join(dict.fromkeys(path_entries))
    if ca_bundle is not None:
        ca_str = str(ca_bundle)
        env["SSL_CERT_FILE"] = ca_str
        env["REQUESTS_CA_BUNDLE"] = ca_str
    return env


def build_launch_agent(
    *,
    hour: int = DEFAULT_DAILY_HOUR,
    minute: int = DEFAULT_DAILY_MINUTE,
    python_executable: Path | None = None,
    collector_command: str = "",
    ca_bundle: Path | None = None,
    codex_executable: Path | None = None,
) -> dict[str, Any]:
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("hour must be 0–23 and minute must be 0–59")
    # Do not resolve the .venv Python symlink. Invoking the resolved base
    # interpreter bypasses the virtual environment and cannot import mlx_lm.
    python = python_executable or _default_python()
    arguments = [str(python), "-m", "carmen_agent.skills.cli"]
    if collector_command:
        arguments.extend(["--collector-command", collector_command])
    resolved_ca = _resolve_ca_bundle(ca_bundle)
    resolved_codex = codex_executable or _default_codex()
    env = _launch_environment(
        python=python,
        codex_executable=resolved_codex,
        ca_bundle=resolved_ca,
    )
    return {
        "Label": DAILY_LABEL,
        "ProgramArguments": arguments,
        "WorkingDirectory": str(PROJECT_ROOT),
        "EnvironmentVariables": env,
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": str(PROJECT_ROOT / "work" / "daily-agent.log"),
        "StandardErrorPath": str(PROJECT_ROOT / "work" / "daily-agent.error.log"),
    }


def build_reminder_launch_agent(
    *,
    interval_minutes: int = DEFAULT_REMINDER_MINUTES,
    python_executable: Path | None = None,
    ca_bundle: Path | None = None,
    codex_executable: Path | None = None,
) -> dict[str, Any]:
    if interval_minutes < 5 or interval_minutes > 1_440:
        raise ValueError("reminder interval must be between 5 and 1440 minutes")
    python = python_executable or _default_python()
    env = _launch_environment(
        python=python,
        codex_executable=codex_executable or _default_codex(),
        ca_bundle=_resolve_ca_bundle(ca_bundle),
    )
    return {
        "Label": REMINDER_LABEL,
        "ProgramArguments": [
            str(python),
            "-m",
            "carmen_agent.reminders",
            "--notify",
        ],
        "WorkingDirectory": str(PROJECT_ROOT),
        "EnvironmentVariables": env,
        "StartInterval": interval_minutes * 60,
        "StandardOutPath": str(PROJECT_ROOT / "work" / "reminder-agent.log"),
        "StandardErrorPath": str(PROJECT_ROOT / "work" / "reminder-agent.error.log"),
    }


def write_launch_agent(config: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(config, sort_keys=True))
    return path


def install_launch_agent(config: dict[str, Any]) -> Path:
    label = str(config["Label"])
    target = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    write_launch_agent(config, target)
    domain = f"gui/{os.getuid()}"
    subprocess.run(
        ["launchctl", "bootout", domain, str(target)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(["launchctl", "bootstrap", domain, str(target)], check=True)
    return target


def _service_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_systemd_service(
    *,
    hour: int = DEFAULT_DAILY_HOUR,
    minute: int = DEFAULT_DAILY_MINUTE,
    reminder_minutes: int = DEFAULT_REMINDER_MINUTES,
    python_executable: Path | None = None,
    no_reminders: bool = False,
) -> str:
    python = python_executable or _default_python()
    arguments = [
            str(python), "-m", "carmen_agent.scheduler", "--hour", str(hour),
            "--minute", str(minute), "--reminder-minutes", str(reminder_minutes),
    ]
    if no_reminders:
        arguments.append("--no-reminders")
    command = " ".join(_service_quote(value) for value in arguments)
    return "\n".join(
        (
            "[Unit]",
            "Description=Carmen Assistant portable scheduler",
            "After=default.target",
            "",
            "[Service]",
            "Type=simple",
            f"WorkingDirectory={_service_quote(str(PROJECT_ROOT))}",
            "EnvironmentFile=-%h/.config/carmen-agent/env",
            f"ExecStart={command}",
            "Restart=on-failure",
            "RestartSec=15",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        )
    )


def install_systemd_service(
    content: str,
    *,
    runner: Any = subprocess.run,
    target: Path | None = None,
) -> Path:
    destination = target or Path.home() / ".config" / "systemd" / "user" / "carmen-agent.service"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    runner(["systemctl", "--user", "daemon-reload"], check=True)
    runner(["systemctl", "--user", "enable", "--now", destination.name], check=True)
    return destination


def build_windows_launcher(
    *,
    hour: int = DEFAULT_DAILY_HOUR,
    minute: int = DEFAULT_DAILY_MINUTE,
    reminder_minutes: int = DEFAULT_REMINDER_MINUTES,
    python_executable: Path | None = None,
    no_reminders: bool = False,
) -> str:
    python = python_executable or _default_python()
    reminder_flag = " --no-reminders" if no_reminders else ""
    return (
        "@echo off\r\n"
        f'cd /d "{PROJECT_ROOT}"\r\n'
        f'"{python}" -m carmen_agent.scheduler --hour {hour} --minute {minute} '
        f"--reminder-minutes {reminder_minutes}{reminder_flag}\r\n"
    )


def install_windows_task(
    launcher: str,
    *,
    runner: Any = subprocess.run,
    target: Path | None = None,
) -> Path:
    destination = target or WORK_DIR / "carmen-scheduler.cmd"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(launcher, encoding="utf-8", newline="")
    runner(
        [
            "schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN",
            "Carmen Assistant Scheduler", "/TR", str(destination),
        ],
        check=True,
    )
    runner(["schtasks", "/Run", "/TN", "Carmen Assistant Scheduler"], check=True)
    return destination


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or install the Carmen schedule for macOS, Linux, or Windows."
    )
    parser.add_argument("--hour", type=int, default=DEFAULT_DAILY_HOUR)
    parser.add_argument("--minute", type=int, default=DEFAULT_DAILY_MINUTE)
    parser.add_argument(
        "--backend",
        choices=("auto", "launchd", "systemd", "windows"),
        default="auto",
        help="Service manager to configure; auto selects from the current OS.",
    )
    parser.add_argument(
        "--collector-command",
        default="",
        help="Command that refreshes source JSON immediately before the morning run.",
    )
    parser.add_argument(
        "--ca-bundle",
        type=Path,
        default=None,
        help=(
            "PEM bundle for the Koi TLS proxy. Defaults to work/koi-root-ca.pem "
            "if present, else the current SSL_CERT_FILE/REQUESTS_CA_BUNDLE."
        ),
    )
    parser.add_argument(
        "--python-executable",
        type=Path,
        default=None,
        help="Python executable for launchd. Defaults to the project's .venv Python.",
    )
    parser.add_argument(
        "--codex-executable",
        type=Path,
        default=None,
        help="Codex executable whose directory should be placed on launchd's PATH.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--reminder-output",
        type=Path,
        default=DEFAULT_REMINDER_OUTPUT,
    )
    parser.add_argument(
        "--reminder-minutes",
        type=int,
        default=DEFAULT_REMINDER_MINUTES,
        help="How often the portable scheduler checks for due reminders.",
    )
    parser.add_argument(
        "--no-reminders",
        action="store_true",
        help="Generate or install only the morning brief schedule.",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Install and start the schedule using the selected OS service manager.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    detected_backend = {
        "Darwin": "launchd",
        "Linux": "systemd",
        "Windows": "windows",
    }.get(platform.system())
    backend = detected_backend if args.backend == "auto" else args.backend
    if backend is None:
        raise SystemExit(
            "No automatic service installer exists for this OS. Run `carmen scheduler` "
            "under your preferred process manager."
        )
    if backend == "systemd":
        content = build_systemd_service(
            hour=args.hour,
            minute=args.minute,
            reminder_minutes=args.reminder_minutes,
            python_executable=args.python_executable,
            no_reminders=args.no_reminders,
        )
        preview = args.output.with_suffix(".service")
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_text(content, encoding="utf-8")
        print(f"Wrote systemd service preview to {preview}")
        if args.install:
            try:
                installed = install_systemd_service(content)
            except (OSError, subprocess.CalledProcessError) as exc:
                raise SystemExit(f"Could not install systemd user service: {exc}") from exc
            print(f"Installed and started {installed}")
        return
    if backend == "windows":
        launcher = build_windows_launcher(
            hour=args.hour,
            minute=args.minute,
            reminder_minutes=args.reminder_minutes,
            python_executable=args.python_executable,
            no_reminders=args.no_reminders,
        )
        preview = args.output.with_suffix(".cmd")
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_text(launcher, encoding="utf-8", newline="")
        print(f"Wrote Windows launcher preview to {preview}")
        if args.install:
            try:
                installed = install_windows_task(launcher)
            except (OSError, subprocess.CalledProcessError) as exc:
                raise SystemExit(f"Could not install Windows scheduled task: {exc}") from exc
            print(f"Installed and started Windows task from {installed}")
        return
    try:
        config = build_launch_agent(
            hour=args.hour,
            minute=args.minute,
            python_executable=args.python_executable,
            collector_command=args.collector_command,
            ca_bundle=args.ca_bundle,
            codex_executable=args.codex_executable,
        )
        reminder_config = None
        if not args.no_reminders:
            reminder_config = build_reminder_launch_agent(
                interval_minutes=args.reminder_minutes,
                python_executable=args.python_executable,
                ca_bundle=args.ca_bundle,
                codex_executable=args.codex_executable,
            )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    preview = write_launch_agent(config, args.output)
    print(f"Wrote schedule preview to {preview}")
    if reminder_config is not None:
        reminder_preview = write_launch_agent(reminder_config, args.reminder_output)
        print(f"Wrote reminder schedule preview to {reminder_preview}")
    if args.install:
        try:
            installed = install_launch_agent(config)
            installed_reminder = (
                install_launch_agent(reminder_config)
                if reminder_config is not None
                else None
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise SystemExit(f"Could not install schedule: {exc}") from exc
        print(f"Installed {installed}; the agent will run daily at {args.hour:02d}:{args.minute:02d}.")
        if installed_reminder is not None:
            print(
                f"Installed {installed_reminder}; reminders are checked every "
                f"{args.reminder_minutes} minute(s)."
            )


if __name__ == "__main__":
    main()
