from __future__ import annotations

import sys

import pytest

from carmen_agent import assistant


def test_assistant_help(capsys, monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["carmen", "--help"])
    assistant.main()
    output = capsys.readouterr().out
    assert "carmen today" in output
    assert "carmen remind" in output
    assert "carmen ui" in output


def test_assistant_rejects_unknown_command(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["carmen", "dance"])
    with pytest.raises(SystemExit, match="Unknown command"):
        assistant.main()
