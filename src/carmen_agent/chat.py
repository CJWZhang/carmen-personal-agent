from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

from carmen_agent.paths import (
    DEFAULT_ADAPTER_DIR as DEFAULT_ADAPTER,
    DEFAULT_MODEL_NAME as DEFAULT_MODEL,
    PROJECT_ROOT,
    STYLE_PROFILE_PATH as DEFAULT_PROFILE,
)
from carmen_agent.configuration import config_value
from carmen_agent.privacy import enforce_private_runtime


def fallback_system_prompt() -> str:
    name = config_value("CARMEN_USER_NAME", "user_name", "the user")
    return (
        f"You are a local writing assistant that drafts concise, natural Slack messages in {name}'s voice. "
        "Match the current conversation's formality and energy. Do not invent personal facts or reveal private "
        "details from training data. Return only the draft message unless asked to explain."
    )


# Kept as a public compatibility constant for callers that imported it before
# user identity became configurable. Runtime profile loading uses the function
# above so a newly saved config is honored.
FALLBACK_SYSTEM_PROMPT = fallback_system_prompt()
EMOJI_ALIAS_RE = re.compile(r":([a-zA-Z0-9_+\-]+):")
THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
SPECIAL_TOKEN_RE = re.compile(r"<\|[^>]+?\|>")


def load_profile(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"system_prompt": fallback_system_prompt(), "allowed_slack_emoji": []}
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"system_prompt": fallback_system_prompt(), "allowed_slack_emoji": []}
    if not isinstance(profile, dict):
        return {"system_prompt": fallback_system_prompt(), "allowed_slack_emoji": []}
    prompt = profile.get("system_prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        profile["system_prompt"] = fallback_system_prompt()
    return profile


def load_system_prompt(path: Path) -> str:
    return str(load_profile(path)["system_prompt"])


def sanitize_slack_emoji(text: str, allowed: set[str]) -> str:
    if not allowed:
        return text

    def replace(match: re.Match[str]) -> str:
        return match.group(0) if match.group(1) in allowed else ""

    cleaned = EMOJI_ALIAS_RE.sub(replace, text)
    return re.sub(r" {2,}", " ", cleaned).strip()


def clean_model_output(text: str) -> str:
    """Remove model-control text that must never appear in a user-facing draft."""
    cleaned = THINK_BLOCK_RE.sub("", text)
    lowered = cleaned.lower()
    for marker in ("<think>", "<tool_call", "<tool_response", "<function_call"):
        index = lowered.find(marker)
        if index >= 0:
            cleaned = cleaned[:index]
            lowered = cleaned.lower()
    cleaned = SPECIAL_TOKEN_RE.sub("", cleaned)
    return cleaned.strip()


def _load_runtime() -> tuple[Any, Any, Any]:
    enforce_private_runtime()
    try:
        from mlx_lm import load, stream_generate
        from mlx_lm.sample_utils import make_sampler
    except ImportError as exc:
        raise SystemExit("MLX-LM is not installed. Run `uv sync --extra mlx` on Apple Silicon.") from exc
    return load, stream_generate, make_sampler


def generate_reply(
    model: Any,
    tokenizer: Any,
    stream_generate: Any,
    make_sampler: Any,
    messages: list[dict[str, str]],
    *,
    max_tokens: int,
    temperature: float,
    top_p: float,
    allowed_emoji: set[str],
) -> str:
    prompt = tokenizer.apply_chat_template(messages, add_generation_prompt=True)
    sampler = make_sampler(temp=temperature, top_p=top_p)
    pieces: list[str] = []
    for response in stream_generate(
        model=model,
        tokenizer=tokenizer,
        prompt=prompt,
        max_tokens=max_tokens,
        sampler=sampler,
    ):
        pieces.append(response.text)
    reply = sanitize_slack_emoji(clean_model_output("".join(pieces)), allowed_emoji)
    print(reply, flush=True)
    return reply


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Chat with the local Carmen-style agent.")
    parser.add_argument(
        "--provider",
        choices=("auto", "mlx", "ollama", "openai-compatible"),
        default="auto",
        help=(
            "Inference backend. Auto uses installed MLX on Apple Silicon and Ollama otherwise; "
            "CARMEN_MODEL_PROVIDER overrides auto."
        ),
    )
    parser.add_argument("--model", default=None, help="Override the configured backend model.")
    parser.add_argument("--adapter", type=Path, default=DEFAULT_ADAPTER)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--message", help="Generate one reply and exit instead of starting the REPL.")
    parser.add_argument("--max-tokens", type=int, default=160)
    parser.add_argument("--temperature", type=float, default=0.8)
    parser.add_argument("--top-p", type=float, default=0.9)
    parser.add_argument("--base-only", action="store_true", help="Use the style profile without the LoRA adapter.")
    parser.add_argument(
        "--action-command",
        default=os.environ.get("CARMEN_ACTION_COMMAND", ""),
        help="Action-adapter executable used only after an explicit /do command.",
    )
    return parser


def shell_command_hint(text: str) -> str | None:
    command = text.strip().split(maxsplit=1)[0] if text.strip() else ""
    if command != "carmen" and not command.startswith("carmen-"):
        return None
    return (
        f"`{text.strip()}` is a terminal command, not a chat message.\n"
        "Enter `/quit`, then run it at your normal shell prompt with:\n"
        f'uv run --project "{PROJECT_ROOT}" {text.strip()}'
    )


def main() -> None:
    args = build_parser().parse_args()
    from carmen_agent.commands import (
        CommandError,
        add_task,
        execute_task,
        format_task,
        format_task_list,
        load_tasks,
        mark_complete,
        read_drafts,
        resolve_task,
        set_task_reminder,
        snooze_task,
    )

    def run_task_command(text: str) -> bool:
        command, _, selector = text.partition(" ")
        if command == "/task":
            command = "/tasks"
        if command == "/daily":
            print(
                "`/daily` runs outside chat so it can load and release the work-agent model safely.\n"
                "Enter `/quit`, then run:\n"
                f'uv run --project "{PROJECT_ROOT}" carmen-daily'
            )
            return True
        if command == "/help":
            print(
                "Commands: /tasks, /add <task>, /show <id>, /draft <id>, "
                "/remind <id> <YYYY-MM-DD HH:MM>, /snooze <id> [minutes], "
                "/do <id>, /done <id>, /daily, /reset, /quit"
            )
            return True
        if command not in {
            "/tasks",
            "/add",
            "/show",
            "/draft",
            "/remind",
            "/snooze",
            "/done",
            "/do",
        }:
            return False
        try:
            tasks = load_tasks()
            if command == "/tasks":
                print(format_task_list(tasks))
                return True
            if command == "/add":
                if not selector.strip():
                    print("usage: /add <task>")
                    return True
                print(add_task(tasks, selector))
                return True
            if command == "/remind":
                task_selector, separator, reminder_at = selector.strip().partition(" ")
                if not separator or not reminder_at.strip():
                    print("usage: /remind <task number or id> <YYYY-MM-DD HH:MM>")
                    return True
                task = resolve_task(tasks, task_selector)
                print(set_task_reminder(tasks, task, reminder_at))
                return True
            if command == "/snooze":
                task_selector, _, raw_minutes = selector.strip().partition(" ")
                if not task_selector:
                    print("usage: /snooze <task number or id> [minutes]")
                    return True
                try:
                    minutes = int(raw_minutes) if raw_minutes else 60
                except ValueError:
                    print("snooze minutes must be a whole number")
                    return True
                task = resolve_task(tasks, task_selector)
                print(snooze_task(tasks, task, minutes=minutes))
                return True
            if not selector.strip():
                print(f"usage: {command} <task number or id>")
                return True
            task = resolve_task(tasks, selector)
            if command == "/show":
                print(format_task(task))
            elif command == "/draft":
                print(read_drafts(task))
            elif command == "/done":
                print(mark_complete(tasks, task))
            elif command == "/do":
                print(
                    execute_task(
                        tasks,
                        task,
                        executor_command=args.action_command,
                    )
                )
        except CommandError as exc:
            print(f"task command error: {exc}")
        return True

    if args.message and args.message.startswith("/"):
        if run_task_command(args.message):
            return
    if args.message:
        hint = shell_command_hint(args.message)
        if hint:
            print(hint)
            return
    from carmen_agent.skills.model import load_session

    session = load_session(
        model_name=args.model,
        adapter=args.adapter,
        profile_path=args.profile,
        base_only=args.base_only,
        provider_name=None if args.provider == "auto" else args.provider,
    )
    history: list[dict[str, str]] = [
        {"role": "system", "content": session.system_prompt}
    ]

    def run_turn(text: str) -> None:
        history.append({"role": "user", "content": text})
        reply = session.generate(
            history,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            top_p=args.top_p,
        )
        print(reply, flush=True)
        history.append({"role": "assistant", "content": reply})

    if args.message:
        run_turn(args.message)
        return
    print(
        "Carmen-style agent. Commands: /tasks, /add <task>, /show <id>, "
        "/draft <id>, /remind <id> <time>, /snooze <id>, /do <id>, "
        "/done <id>, /daily, /help, /reset, /quit. "
        "`carmen` and `carmen-*` commands run at the normal terminal prompt."
    )
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text in {"/quit", "/exit"}:
            break
        if text == "/reset":
            history[:] = history[:1]
            print("context cleared")
            continue
        hint = shell_command_hint(text)
        if hint:
            print(hint)
            continue
        if run_task_command(text):
            continue
        run_turn(text)


if __name__ == "__main__":
    main()
