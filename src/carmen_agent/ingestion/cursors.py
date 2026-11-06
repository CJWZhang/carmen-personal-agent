from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from carmen_agent.paths import SOURCE_CURSOR_PATH
from carmen_agent.skills.schemas import Source

MAX_CURSOR_FILE_BYTES = 256 * 1024


class CursorStore:
    def __init__(self, path: Path = SOURCE_CURSOR_PATH) -> None:
        self.path = path

    def _load(self) -> dict[str, object]:
        if not self.path.is_file() or self.path.stat().st_size > MAX_CURSOR_FILE_BYTES:
            return {"version": 1, "sources": {}}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "sources": {}}
        if not isinstance(raw, dict) or raw.get("version") != 1:
            return {"version": 1, "sources": {}}
        if not isinstance(raw.get("sources"), dict):
            raw["sources"] = {}
        return raw

    def get(self, source: Source) -> str:
        record = self._load()["sources"].get(source, {})  # type: ignore[index,union-attr]
        return str(record.get("cursor") or "") if isinstance(record, dict) else ""

    def set(self, source: Source, cursor: str) -> None:
        raw = self._load()
        sources = raw["sources"]
        assert isinstance(sources, dict)
        sources[source] = {
            "cursor": cursor[:4_096],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.path.parent,
            prefix=".source-cursors-", suffix=".json", delete=False,
        ) as handle:
            json.dump(raw, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            temporary = Path(handle.name)
        temporary.replace(self.path)


__all__ = ["CursorStore"]
