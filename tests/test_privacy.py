from __future__ import annotations

import os

from carmen_agent.privacy import enforce_private_runtime


def test_private_runtime_disables_network_and_telemetry(monkeypatch) -> None:
    names = (
        "CARMEN_ALLOW_NETWORK",
        "DO_NOT_TRACK",
        "HF_HUB_DISABLE_TELEMETRY",
        "HF_HUB_DISABLE_IMPLICIT_TOKEN",
        "HF_HUB_OFFLINE",
        "TRANSFORMERS_OFFLINE",
        "UV_OFFLINE",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)
    enforce_private_runtime()
    assert os.environ["DO_NOT_TRACK"] == "1"
    assert os.environ["HF_HUB_DISABLE_TELEMETRY"] == "1"
    assert os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] == "1"
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["TRANSFORMERS_OFFLINE"] == "1"
    assert os.environ["UV_OFFLINE"] == "1"


def test_explicit_network_bootstrap_keeps_hub_online(monkeypatch) -> None:
    monkeypatch.setenv("CARMEN_ALLOW_NETWORK", "1")
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    enforce_private_runtime()
    assert "HF_HUB_OFFLINE" not in os.environ
    assert "TRANSFORMERS_OFFLINE" not in os.environ
    assert os.environ["HF_HUB_DISABLE_TELEMETRY"] == "1"
