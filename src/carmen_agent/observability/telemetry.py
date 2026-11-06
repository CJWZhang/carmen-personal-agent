from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from carmen_agent.paths import TELEMETRY_PATH

MAX_TELEMETRY_BYTES = 10 * 1024 * 1024
_ALLOWED = {
    "kind", "recorded_at", "source", "status", "intent", "duration_ms",
    "input_tokens", "output_chars", "item_count", "fallback_used", "truncated",
    "precision", "recall", "type_accuracy", "cases",
}


class TelemetryStore:
    """Append only aggregate metrics; source content and identifiers are forbidden."""

    _lock = threading.Lock()

    def __init__(self, path: Path = TELEMETRY_PATH) -> None:
        self.path = path

    def record(self, kind: str, **metrics: object) -> None:
        payload: dict[str, Any] = {
            "kind": kind[:64],
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            **metrics,
        }
        unknown = set(payload) - _ALLOWED
        if unknown:
            raise ValueError(
                "Telemetry contains forbidden fields: " + ", ".join(sorted(unknown))
            )
        # Never allow nested payloads where source text or identifiers could hide.
        if any(isinstance(value, (dict, list, tuple, set)) for value in payload.values()):
            raise ValueError("Telemetry values must be aggregate scalars.")
        encoded = (
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.is_file() and self.path.stat().st_size + len(encoded) > MAX_TELEMETRY_BYTES:
                rotated = self.path.with_suffix(self.path.suffix + ".previous")
                self.path.replace(rotated)
            descriptor = os.open(
                self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
            )
            try:
                os.write(descriptor, encoded)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def summary(self, *, last: int = 500) -> dict[str, object]:
        if not self.path.is_file():
            return {"records": 0, "sync": {}, "chat": {}}
        rows: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines()[-last:]:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        sync = [row for row in rows if row.get("kind") == "connector_sync"]
        chat = [row for row in rows if row.get("kind") == "chat"]

        def average(values: list[float]) -> float:
            return round(sum(values) / len(values), 1) if values else 0.0

        return {
            "records": len(rows),
            "sync": {
                "samples": len(sync),
                "average_duration_ms": average([
                    float(row.get("duration_ms", 0)) for row in sync
                ]),
                "success_rate": round(
                    sum(row.get("status") in {"ok", "empty"} for row in sync)
                    / len(sync), 4,
                ) if sync else 0.0,
            },
            "chat": {
                "samples": len(chat),
                "average_duration_ms": average([
                    float(row.get("duration_ms", 0)) for row in chat
                ]),
                "average_input_tokens": average([
                    float(row.get("input_tokens", 0)) for row in chat
                ]),
                "fallback_rate": round(
                    sum(bool(row.get("fallback_used")) for row in chat) / len(chat), 4
                ) if chat else 0.0,
            },
        }


__all__ = ["TelemetryStore"]
