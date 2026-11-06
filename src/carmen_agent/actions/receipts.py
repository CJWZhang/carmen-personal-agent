"""Durable receipts for external connector actions."""
from __future__ import annotations

import json
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


MAX_RECEIPT_STORE_BYTES = 2 * 1024 * 1024
MAX_RECEIPTS = 1_000
_RECEIPT_FIELDS = {
    "id", "task_id", "source", "action_id", "status", "requested_at",
    "completed_at", "external_id", "message", "verified",
}


@dataclass(frozen=True)
class ActionReceipt:
    id: str
    task_id: str
    source: str
    action_id: str
    status: str
    requested_at: str
    completed_at: str = ""
    external_id: str = ""
    message: str = ""
    verified: bool = False

    @classmethod
    def create(
        cls, *, task_id: str, source: str, action_id: str, status: str,
        external_id: str = "", message: str = "", verified: bool = False,
    ) -> "ActionReceipt":
        stamp = datetime.now(timezone.utc).isoformat()
        return cls(
            id="receipt-" + uuid4().hex,
            task_id=task_id,
            source=source,
            action_id=action_id,
            status=status,
            requested_at=stamp,
            completed_at=stamp if status in {"completed", "verified", "failed"} else "",
            external_id=external_id[:512],
            message=message[:1_000],
            verified=verified,
        )


def validate_receipt_store(path: Path) -> list[dict[str, object]]:
    """Read the bounded receipt ledger without overwriting malformed history."""
    if not path.is_file():
        return []
    if path.stat().st_size > MAX_RECEIPT_STORE_BYTES:
        raise ValueError("Action receipt store exceeds its size limit.")
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Action receipt store is not valid JSON.") from exc
    if not isinstance(loaded, list) or len(loaded) > MAX_RECEIPTS:
        raise ValueError("Action receipt store must be a bounded JSON array.")
    records: list[dict[str, object]] = []
    for index, entry in enumerate(loaded):
        if not isinstance(entry, dict) or set(entry) != _RECEIPT_FIELDS:
            raise ValueError(f"Action receipt {index} has an invalid shape.")
        if any(
            not isinstance(entry[field], str)
            for field in _RECEIPT_FIELDS - {"verified"}
        ) or type(entry["verified"]) is not bool:
            raise ValueError(f"Action receipt {index} has invalid field types.")
        records.append(entry)
    return records


def append_receipt(receipt: ActionReceipt, path: Path) -> None:
    records = validate_receipt_store(path)[-(MAX_RECEIPTS - 1):]
    records.append(asdict(receipt))
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=".receipts-",
        suffix=".json", delete=False,
    ) as handle:
        json.dump(records, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)
