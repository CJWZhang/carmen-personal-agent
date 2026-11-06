from __future__ import annotations

import argparse
import getpass
import importlib.util
import os
import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from carmen_agent.configuration import CONFIG_PATH, config_value, save_user_config
from carmen_agent.paths import (
    ADAPTERS_DIR,
    DATA_DIR,
    DRAFTS_DIR,
    INBOX_DIR,
    PROCESSED_DIR,
    PROJECT_ROOT,
    RAW_DIR,
    WORK_DIR,
)
from carmen_agent.skills.model import default_local_provider


@dataclass(frozen=True)
class Diagnostic:
    level: str
    name: str
    detail: str


def diagnostics(
    *, system: str | None = None, machine: str | None = None
) -> list[Diagnostic]:
    detected_system = system or platform.system()
    detected_machine = machine or platform.machine()
    selected = default_local_provider()
    from carmen_agent.connectors.direct.registry import DirectConnectorRegistry

    direct_sources = DirectConnectorRegistry().available_sources()
    results = [
        Diagnostic("ok", "Python", platform.python_version()),
        Diagnostic("ok", "Platform", f"{detected_system} {detected_machine}"),
        Diagnostic("ok", "Data home", str(PROJECT_ROOT)),
        Diagnostic("ok", "Model provider", selected),
    ]
    if selected == "mlx":
        available = importlib.util.find_spec("mlx_lm") is not None
        results.append(
            Diagnostic(
                "ok" if available else "error",
                "MLX runtime",
                "installed" if available else "missing; run `uv sync --extra mlx` on Apple Silicon",
            )
        )
    elif selected == "ollama":
        executable = shutil.which("ollama")
        endpoint = config_value(
            "CARMEN_MODEL_BASE_URL", "model_base_url", "http://127.0.0.1:11434/v1"
        )
        results.append(
            Diagnostic(
                "ok" if executable else "warning",
                "Ollama",
                str(executable) if executable else f"CLI not found; expected endpoint {endpoint}",
            )
        )
    notification = {
        "Darwin": "/usr/bin/osascript",
        "Linux": shutil.which("notify-send") or "console fallback",
        "Windows": shutil.which("powershell") or shutil.which("pwsh") or "console fallback",
    }.get(detected_system, "console fallback")
    scheduler = {
        "Darwin": shutil.which("launchctl"),
        "Linux": shutil.which("systemctl"),
        "Windows": shutil.which("schtasks"),
    }.get(detected_system)
    results.extend(
        (
            Diagnostic("ok", "Notifications", str(notification)),
            Diagnostic(
                "ok" if scheduler else "warning",
                "Scheduler install",
                str(scheduler) if scheduler else "use `carmen scheduler` under a process manager",
            ),
            Diagnostic(
                "ok" if shutil.which("codex") else "warning",
                "Connected-source collector",
                shutil.which("codex") or "Codex CLI not found; direct mode and --no-collect remain available",
            ),
            Diagnostic(
                "ok" if len(direct_sources) == 4 else "warning",
                "Direct source APIs",
                (
                    "Slack, Gmail, Calendar, and GitHub authorized"
                    if len(direct_sources) == 4
                    else "authorized: " + (", ".join(sorted(direct_sources)) or "none")
                ),
            ),
        )
    )
    return results


def initialize(
    *, provider: str, model: str, base_url: str, user_name: str = ""
) -> Path:
    for directory in (
        DATA_DIR, INBOX_DIR, DRAFTS_DIR, PROCESSED_DIR, RAW_DIR, ADAPTERS_DIR, WORK_DIR
    ):
        directory.mkdir(parents=True, exist_ok=True)
    return save_user_config(
        {
            "model_provider": provider,
            "model_name": model,
            "model_base_url": base_url,
            "user_name": user_name.strip() or getpass.getuser(),
        }
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Initialize or diagnose Carmen Assistant.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    init = subparsers.add_parser("init")
    init.add_argument("--provider", choices=("mlx", "ollama", "openai-compatible"), default="ollama")
    init.add_argument("--model", default="qwen3:4b")
    init.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    init.add_argument(
        "--name",
        default=getpass.getuser(),
        help="Name used to identify you in source collection and writing-style prompts.",
    )
    subparsers.add_parser("doctor")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "init":
        path = initialize(
            provider=args.provider,
            model=args.model,
            base_url=args.base_url,
            user_name=args.name,
        )
        print(f"Initialized Carmen data at {PROJECT_ROOT}")
        print(f"Wrote non-secret configuration to {path}")
        print("Keep API keys in environment variables, never config.json.")
        return
    failed = False
    for item in diagnostics():
        marker = {"ok": "OK", "warning": "WARN", "error": "ERROR"}[item.level]
        print(f"[{marker}] {item.name}: {item.detail}")
        failed = failed or item.level == "error"
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
