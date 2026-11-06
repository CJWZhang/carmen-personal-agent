"""Bounded append-only storage for source observations."""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

from carmen_agent.domain.events import SourceObservation
from carmen_agent.paths import SOURCE_EVENT_LOG_PATH

MAX_EVENT_LOG_BYTES = 128 * 1024 * 1024
MAX_EVENT_LINE_BYTES = 256 * 1024


class EventStoreError(ValueError):
    pass


class JsonlEventStore:
    """Small local event store with idempotent event IDs and durable appends."""

    _lock = threading.Lock()

    def __init__(self, path: Path = SOURCE_EVENT_LOG_PATH) -> None:
        self.path = path

    @contextmanager
    def _process_lock(self):
        """Portable, bounded lock that keeps event batches contiguous."""
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 10.0
        descriptor: int | None = None
        while descriptor is None:
            try:
                descriptor = os.open(
                    lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                os.write(descriptor, str(os.getpid()).encode("ascii"))
            except FileExistsError:
                try:
                    stale = time.time() - lock_path.stat().st_mtime > 30.0
                except OSError:
                    stale = False
                if stale:
                    try:
                        lock_path.unlink()
                    except OSError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise EventStoreError("Timed out waiting for the source event lock.")
                time.sleep(0.02)
        try:
            yield
        finally:
            os.close(descriptor)
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass

    def read(self) -> list[SourceObservation]:
        if not self.path.is_file():
            return []
        if self.path.stat().st_size > MAX_EVENT_LOG_BYTES:
            raise EventStoreError("Source event log exceeds its size limit.")
        events: list[SourceObservation] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if len(line.encode("utf-8")) > MAX_EVENT_LINE_BYTES:
                    raise EventStoreError(f"Source event line {line_number} is too large.")
                if not line.strip():
                    continue
                try:
                    events.append(SourceObservation.from_dict(json.loads(line)))
                except (json.JSONDecodeError, ValueError) as exc:
                    raise EventStoreError(
                        f"Source event line {line_number} is invalid: {exc}"
                    ) from exc
        return events

    def append(self, events: Iterable[SourceObservation]) -> int:
        candidates = list(events)
        if not candidates:
            return 0
        encoded: list[tuple[SourceObservation, bytes]] = []
        for event in candidates:
            validated = SourceObservation.from_dict(event.as_dict())
            line = (
                json.dumps(
                    validated.as_dict(), ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            if len(line) > MAX_EVENT_LINE_BYTES:
                raise EventStoreError("Source event exceeds its line-size limit.")
            encoded.append((validated, line))

        with self._lock:
            with self._process_lock():
                existing = {event.event_id for event in self.read()}
                additions: list[bytes] = []
                seen = set(existing)
                for event, line in encoded:
                    if event.event_id in seen:
                        continue
                    additions.append(line)
                    seen.add(event.event_id)
                if not additions:
                    return 0
                current_size = self.path.stat().st_size if self.path.is_file() else 0
                if current_size + sum(map(len, additions)) > MAX_EVENT_LOG_BYTES:
                    raise EventStoreError("Appending would exceed the source event log limit.")
                descriptor = os.open(
                    self.path,
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                    0o600,
                )
                try:
                    for line in additions:
                        os.write(descriptor, line)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                return len(additions)

    def latest_by_entity(self) -> dict[tuple[str, str], SourceObservation]:
        latest: dict[tuple[str, str], SourceObservation] = {}
        for event in self.read():
            key = (event.source, event.entity_id)
            previous = latest.get(key)
            if previous is None or (
                event.source_updated_at or event.observed_at,
                event.observed_at,
                event.event_id,
            ) > (
                previous.source_updated_at or previous.observed_at,
                previous.observed_at,
                previous.event_id,
            ):
                latest[key] = event
        return latest


__all__ = ["EventStoreError", "JsonlEventStore"]
