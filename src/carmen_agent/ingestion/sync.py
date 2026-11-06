from __future__ import annotations

import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from carmen_agent.collector import record_collection_started, write_snapshots
from carmen_agent.connectors.direct.base import DirectConnectorError
from carmen_agent.connectors.direct.registry import DirectConnectorRegistry
from carmen_agent.ingestion.cursors import CursorStore
from carmen_agent.paths import INBOX_DIR
from carmen_agent.observability.telemetry import TelemetryStore
from carmen_agent.skills.schemas import Source


class DirectSyncCoordinator:
    """Refresh sources independently; one failure cannot poison another source."""

    def __init__(
        self,
        *,
        registry: DirectConnectorRegistry | None = None,
        cursor_store: CursorStore | None = None,
        inbox_dir: Path = INBOX_DIR,
        telemetry: TelemetryStore | None = None,
    ) -> None:
        self.registry = registry or DirectConnectorRegistry()
        app_root = (
            inbox_dir.parent.parent
            if inbox_dir.parent.name == "data"
            else inbox_dir.parent
        )
        self.cursor_store = cursor_store or CursorStore(
            app_root / "work" / "source-cursors.json"
        )
        self.inbox_dir = inbox_dir
        self.telemetry = telemetry or TelemetryStore(
            app_root / "work" / "quality-telemetry.jsonl"
        )
        # Network reads can run concurrently, while snapshot/status commits stay
        # serialized so independent connector completion cannot lose health data.
        self._commit_lock = threading.Lock()

    def sync_source(
        self,
        source: Source,
        *,
        hours: int = 36,
        mark_started: bool = True,
    ) -> tuple[int, dict[str, str], float]:
        connector = self.registry.get(source)
        if mark_started:
            with self._commit_lock:
                record_collection_started([source], inbox_dir=self.inbox_dir)
        started = time.monotonic()
        if connector is None or not connector.available():
            elapsed = time.monotonic() - started
            coverage = {
                "source": source,
                "status": "unavailable",
                "detail": (
                    "Direct connector is not authorized on this device. "
                    "The last successful snapshot was preserved."
                ),
                "transport": "direct_api",
                "duration_ms": str(round(elapsed * 1_000, 1)),
            }
            with self._commit_lock:
                write_snapshots(
                    {"items": [], "coverage": [coverage]}, inbox_dir=self.inbox_dir
                )
            self._record(source, coverage["status"], elapsed, 0)
            return 0, coverage, elapsed
        try:
            result = connector.sync(cursor=self.cursor_store.get(source), hours=hours)
            if result.source != source:
                raise DirectConnectorError("Connector returned the wrong source.")
            elapsed = time.monotonic() - started
            coverage = {
                "source": source,
                "status": "ok" if result.items else "empty",
                "detail": result.detail[:2_000],
                "transport": "direct_api",
                "duration_ms": str(round(elapsed * 1_000, 1)),
            }
            with self._commit_lock:
                counts = write_snapshots(
                    {"items": list(result.items), "coverage": [coverage]},
                    inbox_dir=self.inbox_dir,
                )
                if result.cursor:
                    self.cursor_store.set(source, result.cursor)
            self._record(source, coverage["status"], elapsed, counts.get(source, 0))
            return counts.get(source, 0), coverage, elapsed
        except Exception as exc:
            elapsed = time.monotonic() - started
            coverage = {
                "source": source,
                "status": "error",
                "detail": f"Direct {source} refresh failed: {str(exc)[:1_500]}",
                "transport": "direct_api",
                "duration_ms": str(round(elapsed * 1_000, 1)),
            }
            with self._commit_lock:
                write_snapshots(
                    {"items": [], "coverage": [coverage]}, inbox_dir=self.inbox_dir
                )
            self._record(source, coverage["status"], elapsed, 0)
            return 0, coverage, elapsed

    def _record(self, source: Source, status: str, elapsed: float, count: int) -> None:
        self.telemetry.record(
            "connector_sync",
            source=source,
            status=status,
            duration_ms=round(elapsed * 1_000, 1),
            item_count=count,
        )

    def sync(
        self,
        sources: list[Source],
        *,
        hours: int = 36,
    ) -> tuple[dict[str, int], list[dict[str, str]], dict[str, float]]:
        if not sources:
            return {}, [], {}
        with self._commit_lock:
            record_collection_started(sources, inbox_dir=self.inbox_dir)
        results: dict[Source, tuple[int, dict[str, str], float]] = {}
        # Each provider has its own request path and health receipt. A slow Gmail
        # read no longer prevents a fast Calendar or GitHub result from landing.
        with ThreadPoolExecutor(
            max_workers=min(4, len(sources)), thread_name_prefix="carmen-sync"
        ) as executor:
            futures = {
                executor.submit(
                    self.sync_source, source, hours=hours, mark_started=False
                ): source
                for source in sources
            }
            for future in as_completed(futures):
                results[futures[future]] = future.result()
        return (
            {source: results[source][0] for source in sources},
            [results[source][1] for source in sources],
            {source: results[source][2] for source in sources},
        )


__all__ = ["DirectSyncCoordinator"]
