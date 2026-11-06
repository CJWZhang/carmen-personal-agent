from __future__ import annotations

import sys
from collections.abc import Callable


HELP = """Carmen Assistant

Usage:
  carmen init [options]                  Initialize cross-platform user data/config
  carmen doctor                          Check this computer's runtime capabilities
  carmen ui [options]                    Open the private local dashboard
  carmen chat [options]                 Chat with the configured local model in your voice
  carmen today [options]                Refresh context and build today's brief
  carmen collect [options]              Refresh connected source snapshots
  carmen tasks                          List open tasks
  carmen add <task> [options]           Add a personal task
  carmen show <task>                    Show task context
  carmen draft <task>                   Show a suggested response
  carmen remind <task> --at <time>      Set an exact reminder
  carmen snooze <task> [--minutes N]    Snooze a reminder
  carmen done <task>                    Mark a task complete
  carmen do <task> --with-command PATH  Execute through an action adapter
  carmen reminders [--notify]           Show or deliver due reminders
  carmen schedule [--install]           Configure the daily run and reminders
  carmen scheduler [--once]             Run the portable scheduler process
  carmen eval [options]                  Run frozen quality evaluations

Run `carmen <command> --help` for command-specific options.
"""


def _delegate(main: Callable[[], None], args: list[str]) -> None:
    sys.argv = [sys.argv[0], *args]
    main()


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in {"help", "--help", "-h"}:
        print(HELP)
        return
    command, forwarded = args[0], args[1:]

    if command == "chat":
        from carmen_agent.chat import main as handler

        _delegate(handler, forwarded)
        return
    if command == "ui":
        from carmen_agent.ui.app import main as handler

        _delegate(handler, forwarded)
        return
    if command in {"init", "doctor"}:
        from carmen_agent.setup import main as handler

        _delegate(handler, [command, *forwarded])
        return
    if command in {"today", "daily"}:
        from carmen_agent.skills.cli import main as handler

        _delegate(handler, forwarded)
        return
    if command == "collect":
        from carmen_agent.collector import main as handler

        _delegate(handler, forwarded)
        return
    if command == "reminders":
        from carmen_agent.reminders import main as handler

        _delegate(handler, forwarded)
        return
    if command == "schedule":
        from carmen_agent.skills.schedule import main as handler

        _delegate(handler, forwarded)
        return
    if command == "scheduler":
        from carmen_agent.scheduler import main as handler

        _delegate(handler, forwarded)
        return
    if command == "eval":
        from carmen_agent.evals.runner import main as handler

        _delegate(handler, forwarded)
        return

    task_commands = {
        "tasks": "list",
        "add": "add",
        "show": "show",
        "draft": "draft",
        "remind": "remind",
        "snooze": "snooze",
        "done": "complete",
        "do": "execute",
    }
    task_command = task_commands.get(command)
    if task_command is not None:
        from carmen_agent.commands import main as handler

        _delegate(handler, [task_command, *forwarded])
        return

    raise SystemExit(f"Unknown command {command!r}.\n\n{HELP}")


if __name__ == "__main__":
    main()
