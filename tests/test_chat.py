import json
from pathlib import Path

from carmen_agent.chat import (
    FALLBACK_SYSTEM_PROMPT,
    clean_model_output,
    load_system_prompt,
    sanitize_slack_emoji,
    shell_command_hint,
)


def test_load_system_prompt(tmp_path: Path) -> None:
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"system_prompt": "write like carmen"}), encoding="utf-8")
    assert load_system_prompt(profile) == "write like carmen"


def test_missing_profile_uses_fallback(tmp_path: Path) -> None:
    assert load_system_prompt(tmp_path / "missing.json") == FALLBACK_SYSTEM_PROMPT


def test_unknown_slack_emoji_alias_is_removed() -> None:
    text = "sounds good :blob_pray: :made_up_emoji:"
    assert sanitize_slack_emoji(text, {"blob_pray"}) == "sounds good :blob_pray:"


def test_empty_emoji_allowlist_does_not_change_text() -> None:
    assert sanitize_slack_emoji("hello :wave:", set()) == "hello :wave:"


def test_clean_model_output_removes_reasoning_and_tool_artifacts() -> None:
    assert clean_model_output("<think>private reasoning</think>\nsounds good!") == "sounds good!"
    assert clean_model_output("usable text\n<tool_call>\n{}") == "usable text"
    assert clean_model_output("<think>\nunfinished reasoning") == ""


def test_shell_command_is_not_treated_as_chat() -> None:
    hint = shell_command_hint("carmen-daily")
    assert hint is not None
    assert "terminal command" in hint
    assert "uv run --project" in hint


def test_unified_carmen_command_is_not_treated_as_chat() -> None:
    hint = shell_command_hint("carmen today")
    assert hint is not None
    assert "terminal command" in hint


def test_regular_message_has_no_shell_hint() -> None:
    assert shell_command_hint("can you help with this?") is None
