"""Canonical filesystem locations for the project.

Every module reaches for the same handful of directories and default files;
centralising them here avoids the drift that comes from re-deriving PROJECT_ROOT
in every module. If a caller needs a different path they still override via CLI
flags — these are just the defaults everything shares.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _default_app_home() -> Path:
    explicit = os.environ.get("CARMEN_HOME", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    source_root = Path(__file__).resolve().parents[2]
    if (source_root / "pyproject.toml").is_file():
        return source_root
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "CarmenAgent"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "CarmenAgent"
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "carmen-agent"


# In a source checkout this remains the repository root for compatibility. A
# wheel installation writes state to the platform user-data directory instead
# of attempting to mutate site-packages. CARMEN_HOME always overrides it.
PROJECT_ROOT: Path = _default_app_home()

# Top-level directories.
DATA_DIR: Path = PROJECT_ROOT / "data"
INBOX_DIR: Path = DATA_DIR / "inbox"
DRAFTS_DIR: Path = DATA_DIR / "drafts"
PROCESSED_DIR: Path = DATA_DIR / "processed"
RAW_DIR: Path = DATA_DIR / "raw"
ADAPTERS_DIR: Path = PROJECT_ROOT / "adapters"
CONFIGS_DIR: Path = PROJECT_ROOT / "configs"
WORK_DIR: Path = PROJECT_ROOT / "work"
EVENTS_DIR: Path = DATA_DIR / "events"

# Files the pipeline writes.
BRIEF_PATH: Path = PROJECT_ROOT / "TODO.md"
TODOS_PATH: Path = INBOX_DIR / "todos.json"
STATE_PATH: Path = WORK_DIR / "daily-chief-of-staff-state.json"
HABITS_PATH: Path = WORK_DIR / "habits.json"
REMINDER_STATE_PATH: Path = WORK_DIR / "reminder-deliveries.json"
ACTION_RECEIPTS_PATH: Path = WORK_DIR / "action-receipts.json"
CALENDAR_PREFERENCES_PATH: Path = WORK_DIR / "calendar-preferences.json"
WORKSPACE_CA_PATH: Path = WORK_DIR / "koi-root-ca.pem"
SOURCE_EVENT_LOG_PATH: Path = EVENTS_DIR / "source-events.jsonl"
SOURCE_CURSOR_PATH: Path = WORK_DIR / "source-cursors.json"
TELEMETRY_PATH: Path = WORK_DIR / "quality-telemetry.jsonl"

# Files the pipeline reads.
STYLE_PROFILE_PATH: Path = PROCESSED_DIR / "style_profile.json"
STYLE_EXEMPLARS_PATH: Path = RAW_DIR / "style_exemplars.json"
_SOURCE_ROOT: Path = Path(__file__).resolve().parents[2]
_SOURCE_SCHEMA: Path = _SOURCE_ROOT / "configs" / "collector-output.schema.json"
_PACKAGED_SCHEMA: Path = (
    Path(__file__).resolve().parent / "resources" / "collector-output.schema.json"
)
COLLECTOR_SCHEMA_PATH: Path = (
    _SOURCE_SCHEMA if _SOURCE_SCHEMA.is_file() else _PACKAGED_SCHEMA
)

# Model + adapter defaults.
DEFAULT_MODEL_NAME: str = "mlx-community/Qwen3-4B-Instruct-2507-4bit"
DEFAULT_ADAPTER_DIR: Path = ADAPTERS_DIR / "carmen-qwen3-4b"


__all__ = [
    "PROJECT_ROOT",
    "DATA_DIR",
    "INBOX_DIR",
    "DRAFTS_DIR",
    "PROCESSED_DIR",
    "RAW_DIR",
    "ADAPTERS_DIR",
    "CONFIGS_DIR",
    "WORK_DIR",
    "EVENTS_DIR",
    "BRIEF_PATH",
    "TODOS_PATH",
    "STATE_PATH",
    "HABITS_PATH",
    "REMINDER_STATE_PATH",
    "ACTION_RECEIPTS_PATH",
    "CALENDAR_PREFERENCES_PATH",
    "WORKSPACE_CA_PATH",
    "SOURCE_EVENT_LOG_PATH",
    "SOURCE_CURSOR_PATH",
    "TELEMETRY_PATH",
    "STYLE_PROFILE_PATH",
    "STYLE_EXEMPLARS_PATH",
    "COLLECTOR_SCHEMA_PATH",
    "DEFAULT_MODEL_NAME",
    "DEFAULT_ADAPTER_DIR",
]
