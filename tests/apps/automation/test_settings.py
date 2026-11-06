from __future__ import annotations

import pytest

from carmen_agent.apps.automation import (
    AutomationSettings,
    AutomationSettingsError,
    load_automation_settings,
    parse_automation_preferences,
)
from carmen_agent.skills.schedule import DEFAULT_DAILY_HOUR


def test_defaults_are_shared_with_the_scheduler_compatibility_module() -> None:
    settings = load_automation_settings(reader=lambda _env, _name, default: default)

    assert settings == AutomationSettings(hour=5, minute=0, reminder_minutes=30)
    assert DEFAULT_DAILY_HOUR == settings.hour


def test_invalid_persisted_fields_fall_back_independently() -> None:
    configured = {
        "daily_refresh_hour": "99",
        "daily_refresh_minute": "not-a-number",
        "reminder_minutes": "60",
    }

    settings = load_automation_settings(
        reader=lambda _env, name, default: configured.get(name, default)
    )

    assert settings == AutomationSettings(hour=5, minute=0, reminder_minutes=60)


def test_api_shape_contains_bounded_options_and_current_values() -> None:
    payload = AutomationSettings(hour=7, minute=0, reminder_minutes=120).to_api_dict()

    assert payload["daily_refresh"] == "07:00"
    assert payload["reminder_minutes"] == 120
    assert {item["value"] for item in payload["daily_refresh_options"]} == {
        "04:00",
        "05:00",
        "06:00",
        "07:00",
        "08:00",
        "09:00",
        "10:00",
    }
    assert {item["value"] for item in payload["reminder_options"]} == {
        15,
        30,
        60,
        120,
        240,
        1_440,
    }


def test_preference_parser_rejects_unpublished_or_unbounded_values() -> None:
    assert parse_automation_preferences(
        {"daily_refresh": "08:00", "reminder_minutes": 60}
    ) == AutomationSettings(hour=8, minute=0, reminder_minutes=60)

    with pytest.raises(AutomationSettingsError, match="Unknown automation setting"):
        parse_automation_preferences(
            {"daily_refresh": "08:00", "reminder_minutes": 60, "shell": "oops"}
        )
    with pytest.raises(AutomationSettingsError, match="available daily refresh"):
        parse_automation_preferences(
            {"daily_refresh": "08:30", "reminder_minutes": 60}
        )
    with pytest.raises(AutomationSettingsError, match="available reminder"):
        parse_automation_preferences(
            {"daily_refresh": "08:00", "reminder_minutes": 45}
        )
