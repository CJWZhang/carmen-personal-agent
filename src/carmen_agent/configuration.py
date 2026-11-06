from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from carmen_agent.paths import PROJECT_ROOT

CONFIG_PATH = Path(
    os.environ.get("CARMEN_CONFIG", PROJECT_ROOT / "config.json")
).expanduser()


def load_user_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def config_value(environment_name: str, config_name: str, default: str = "") -> str:
    environment = os.environ.get(environment_name)
    if environment is not None and environment.strip():
        return environment.strip()
    value = load_user_config().get(config_name, default)
    return str(value).strip()


def save_user_config(values: dict[str, Any], path: Path = CONFIG_PATH) -> Path:
    current = load_user_config(path)
    current.update(values)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path
