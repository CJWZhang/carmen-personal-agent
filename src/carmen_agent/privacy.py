from __future__ import annotations

import os


def enforce_private_runtime() -> None:
    """Disable model-hub network access and telemetry unless explicitly allowed."""
    os.environ.setdefault("DO_NOT_TRACK", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
    if os.environ.get("CARMEN_ALLOW_NETWORK", "0") != "1":
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        os.environ.setdefault("UV_OFFLINE", "1")
