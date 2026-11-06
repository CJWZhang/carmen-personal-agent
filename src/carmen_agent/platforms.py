from __future__ import annotations

import json
import os
import shlex


def parse_command(command: str, *, windows: bool | None = None) -> list[str]:
    """Parse a user-configured command portably.

    A JSON string array is the unambiguous format on every OS. Plain command
    strings remain convenient and use the current platform's quoting rules.
    """
    raw = command.strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON command array: {exc}") from exc
        if not isinstance(value, list) or not value or not all(
            isinstance(item, str) and item for item in value
        ):
            raise ValueError("Command JSON must be a non-empty array of strings.")
        return value
    use_windows = os.name == "nt" if windows is None else windows
    parts = shlex.split(raw, posix=not use_windows)
    if use_windows:
        parts = [
            item[1:-1] if len(item) >= 2 and item[0] == item[-1] == '"' else item
            for item in parts
        ]
    return parts
