"""Validated automation settings independent of UI and OS service managers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping

from carmen_agent.configuration import config_value

DEFAULT_DAILY_HOUR = 5
DEFAULT_DAILY_MINUTE = 0
DEFAULT_REMINDER_MINUTES = 30
DAILY_REFRESH_PRESETS = tuple((hour, 0) for hour in range(4, 11))
REMINDER_INTERVAL_PRESETS = (15, 30, 60, 120, 240, 1_440)

ConfigReader = Callable[[str, str, str], str]


class AutomationSettingsError(ValueError):
    """Raised when an automation settings payload violates the public schema."""


def _clock_label(hour: int, minute: int) -> str:
    return datetime(2000, 1, 1, hour, minute).strftime("%I:%M %p").lstrip("0")


def _reminder_label(interval: int) -> str:
    if interval == 1_440:
        return "Once a day"
    if interval >= 120:
        return f"Every {interval // 60} hours"
    if interval == 60:
        return "Every hour"
    return f"Every {interval} minutes"


def _configured_integer(
    reader: ConfigReader,
    environment_name: str,
    config_name: str,
    default: int,
) -> int:
    raw = reader(environment_name, config_name, str(default))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class AutomationSettings:
    """One normalized daily refresh and reminder schedule."""

    hour: int = DEFAULT_DAILY_HOUR
    minute: int = DEFAULT_DAILY_MINUTE
    reminder_minutes: int = DEFAULT_REMINDER_MINUTES

    def __post_init__(self) -> None:
        if not 0 <= self.hour <= 23 or not 0 <= self.minute <= 59:
            raise AutomationSettingsError("Schedule time is invalid.")
        if not 5 <= self.reminder_minutes <= 1_440:
            raise AutomationSettingsError(
                "Reminder interval must be between 5 and 1,440 minutes."
            )

    @property
    def daily_refresh(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"

    @property
    def daily_refresh_label(self) -> str:
        return _clock_label(self.hour, self.minute)

    def config_values(self) -> dict[str, int]:
        return {
            "daily_refresh_hour": self.hour,
            "daily_refresh_minute": self.minute,
            "reminder_minutes": self.reminder_minutes,
        }

    def to_api_dict(self) -> dict[str, Any]:
        daily_options = [
            {
                "value": f"{hour:02d}:{minute:02d}",
                "label": _clock_label(hour, minute),
            }
            for hour, minute in DAILY_REFRESH_PRESETS
        ]
        if (self.hour, self.minute) not in DAILY_REFRESH_PRESETS:
            daily_options.append(
                {
                    "value": self.daily_refresh,
                    "label": f"{self.daily_refresh_label} (custom)",
                }
            )
        reminder_options = [
            {"value": interval, "label": _reminder_label(interval)}
            for interval in REMINDER_INTERVAL_PRESETS
        ]
        if self.reminder_minutes not in REMINDER_INTERVAL_PRESETS:
            reminder_options.append(
                {
                    "value": self.reminder_minutes,
                    "label": f"{_reminder_label(self.reminder_minutes)} (custom)",
                }
            )
        return {
            "daily_refresh": self.daily_refresh,
            "reminder_minutes": self.reminder_minutes,
            "daily_refresh_options": daily_options,
            "reminder_options": reminder_options,
        }


def load_automation_settings(*, reader: ConfigReader | None = None) -> AutomationSettings:
    """Load bounded settings, falling back field-by-field on invalid config."""
    read = reader or config_value
    hour = _configured_integer(
        read, "CARMEN_DAILY_REFRESH_HOUR", "daily_refresh_hour", DEFAULT_DAILY_HOUR
    )
    minute = _configured_integer(
        read,
        "CARMEN_DAILY_REFRESH_MINUTE",
        "daily_refresh_minute",
        DEFAULT_DAILY_MINUTE,
    )
    reminder_minutes = _configured_integer(
        read,
        "CARMEN_REMINDER_MINUTES",
        "reminder_minutes",
        DEFAULT_REMINDER_MINUTES,
    )
    if not 0 <= hour <= 23:
        hour = DEFAULT_DAILY_HOUR
    if not 0 <= minute <= 59:
        minute = DEFAULT_DAILY_MINUTE
    if not 5 <= reminder_minutes <= 1_440:
        reminder_minutes = DEFAULT_REMINDER_MINUTES
    return AutomationSettings(hour, minute, reminder_minutes)


def parse_automation_preferences(payload: Mapping[str, Any]) -> AutomationSettings:
    """Validate the bounded UI payload against the published preset choices."""
    allowed = {"daily_refresh", "reminder_minutes"}
    unknown = set(payload) - allowed
    if unknown:
        raise AutomationSettingsError(
            "Unknown automation setting: " + ", ".join(sorted(unknown)) + "."
        )
    if set(payload) != allowed:
        raise AutomationSettingsError(
            "Choose a daily refresh time and reminder interval."
        )
    daily_refresh = payload["daily_refresh"]
    reminder_minutes = payload["reminder_minutes"]
    if not isinstance(daily_refresh, str) or type(reminder_minutes) is not int:
        raise AutomationSettingsError("Automation settings have an invalid format.")
    parts = daily_refresh.split(":")
    if len(parts) != 2 or any(len(part) != 2 or not part.isdigit() for part in parts):
        raise AutomationSettingsError("Daily refresh time is invalid.")
    hour, minute = (int(part) for part in parts)
    if (hour, minute) not in DAILY_REFRESH_PRESETS:
        raise AutomationSettingsError(
            "Choose one of the available daily refresh times."
        )
    if reminder_minutes not in REMINDER_INTERVAL_PRESETS:
        raise AutomationSettingsError(
            "Choose one of the available reminder intervals."
        )
    return AutomationSettings(hour, minute, reminder_minutes)
