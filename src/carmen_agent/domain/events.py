"""Sparse, immutable observations from connected work sources."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from carmen_agent.skills.schemas import InboxItem, Source, inbox_item_from_dict

EventType = Literal["observed", "resolved", "deleted", "checkpoint"]

_COMMON_ATTRIBUTES = {
    "kind", "reason", "deadline_at", "expires_at", "latest_activity_at",
    "next_action", "resolution_evidence", "conversation_id",
}
_SOURCE_ATTRIBUTES: dict[str, set[str]] = {
    "slack": {
        "user_replied_after_ask",
    },
    "gmail": {
        "user_replied_after_ask",
    },
    "calendar": {
        "response_status", "organizer", "series_id", "cancelled",
        "needs_decision", "optional", "historical_instances",
        "historical_accepted", "historical_declined", "historical_tentative",
        "historical_no_response", "historical_attended", "historical_missed",
    },
    "github": {
        "repository", "repo", "number", "state", "review_state", "ci_state",
        "mergeable_state", "review_requested", "assigned", "is_draft",
        "has_unresolved_threads", "ci_passing", "mergeable",
    },
}
_CONTENT_FIELDS = {
    "sender", "subject", "body", "channel", "thread_id", "kind", "status",
    "participants", "related_keys",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sparse(values: dict[str, Any]) -> dict[str, Any]:
    """Drop schema placeholders while retaining meaningful false/zero values.

    False and zero can be real observations. They are retained only when the
    source supplied the key; empty strings and empty collections never add
    information to an event record.
    """
    return {
        str(key): value
        for key, value in values.items()
        if value not in ("", None, [], {})
    }


def _bounded_json(value: object, *, maximum: int = 128 * 1024) -> bytes:
    encoded = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > maximum:
        raise ValueError(f"Source observation exceeds {maximum} bytes.")
    return encoded


@dataclass(frozen=True)
class SourceObservation:
    """One append-only source fact with a compact typed payload."""

    event_id: str
    batch_id: str
    source: Source
    native_id: str
    entity_id: str
    event_type: EventType
    observed_at: str
    occurred_at: str = ""
    source_updated_at: str = ""
    permalink: str = ""
    cursor: str = ""
    content: dict[str, Any] = field(default_factory=dict)
    attributes: dict[str, Any] = field(default_factory=dict)
    payload_hash: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: object) -> SourceObservation:
        if not isinstance(raw, dict):
            raise ValueError("Source observation must be an object.")
        allowed = set(cls.__dataclass_fields__)
        if set(raw) != allowed:
            raise ValueError("Source observation has an invalid shape.")
        source = raw.get("source")
        if source not in _SOURCE_ATTRIBUTES:
            raise ValueError("Source observation has an invalid source.")
        event_type = raw.get("event_type")
        if event_type not in {"observed", "resolved", "deleted", "checkpoint"}:
            raise ValueError("Source observation has an invalid event type.")
        for name in (
            "event_id", "batch_id", "native_id", "entity_id", "observed_at",
            "occurred_at", "source_updated_at", "permalink", "cursor", "payload_hash",
        ):
            if not isinstance(raw.get(name), str):
                raise ValueError(f"Source observation {name} must be text.")
        if not raw.get("event_id") or not raw.get("native_id") or not raw.get("entity_id"):
            raise ValueError("Source observation identifiers cannot be empty.")
        content = raw.get("content")
        attributes = raw.get("attributes")
        if not isinstance(content, dict) or not isinstance(attributes, dict):
            raise ValueError("Source observation payloads must be objects.")
        unknown_content = set(content) - _CONTENT_FIELDS
        if unknown_content:
            raise ValueError(
                "Source observation has unsupported content fields: "
                + ", ".join(sorted(unknown_content))
            )
        if any(
            not isinstance(content.get(name), str)
            for name in set(content) & {
                "sender", "subject", "body", "channel", "thread_id", "kind", "status"
            }
        ):
            raise ValueError("Source observation text content must contain strings.")
        if any(
            not isinstance(content.get(name), list)
            for name in set(content) & {"participants", "related_keys"}
        ):
            raise ValueError("Source observation list content must contain arrays.")
        permitted = _COMMON_ATTRIBUTES | _SOURCE_ATTRIBUTES[str(source)]
        unknown = set(attributes) - permitted
        if unknown:
            raise ValueError(
                "Source observation has unsupported attributes: "
                + ", ".join(sorted(unknown))
            )
        _bounded_json(raw)
        expected_hash = hashlib.sha256(
            _bounded_json({"content": content, "attributes": attributes})
        ).hexdigest()
        if raw.get("payload_hash") != expected_hash:
            raise ValueError("Source observation payload hash does not match its content.")
        observation = cls(**raw)  # type: ignore[arg-type]
        # Reuse the public source-item contract for string, URL, enum, list, and
        # source-specific metadata value bounds after validating the sparse shape.
        inbox_item_from_dict(asdict(observation.to_inbox_item()), path="observation")
        return observation

    @classmethod
    def from_inbox_item(
        cls,
        item: InboxItem,
        *,
        batch_id: str,
        observed_at: str | None = None,
        cursor: str = "",
    ) -> SourceObservation:
        item = inbox_item_from_dict(asdict(item), path="source item")
        stamp = observed_at or utc_now()
        content = _sparse({
            "sender": item.sender,
            "subject": item.subject,
            "body": item.body,
            "channel": item.channel,
            "thread_id": item.thread_id,
            "kind": item.kind,
            "status": item.status,
            "participants": item.participants,
            "related_keys": item.related_keys,
        })
        permitted = _COMMON_ATTRIBUTES | _SOURCE_ATTRIBUTES[item.source]
        attributes = _sparse({
            key: value for key, value in item.metadata.items() if key in permitted
        })
        payload_hash = hashlib.sha256(
            _bounded_json({"content": content, "attributes": attributes})
        ).hexdigest()
        event_type: EventType = "resolved" if item.status == "done" else "observed"
        revision = str(item.metadata.get("latest_activity_at") or item.ts or payload_hash)
        identity = "\x1f".join(
            (batch_id, item.source, item.id, revision, event_type, payload_hash)
        )
        event_id = "evt-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        entity_key = next(
            (key.strip() for key in item.related_keys if key.strip()),
            f"{item.source}:{item.id}",
        )
        return cls(
            event_id=event_id,
            batch_id=batch_id,
            source=item.source,
            native_id=item.id,
            entity_id=entity_key,
            event_type=event_type,
            observed_at=stamp,
            occurred_at=item.ts,
            source_updated_at=str(item.metadata.get("latest_activity_at") or item.ts),
            permalink=item.url,
            cursor=cursor,
            content=content,
            attributes=attributes,
            payload_hash=payload_hash,
        )

    @classmethod
    def checkpoint(
        cls,
        source: Source,
        *,
        batch_id: str,
        observed_at: str | None = None,
    ) -> SourceObservation:
        stamp = observed_at or utc_now()
        content: dict[str, Any] = {"status": "done"}
        attributes: dict[str, Any] = {}
        payload_hash = hashlib.sha256(
            _bounded_json({"content": content, "attributes": attributes})
        ).hexdigest()
        return cls(
            event_id="evt-" + hashlib.sha256(
                f"{source}\x1f{batch_id}\x1fcheckpoint".encode("utf-8")
            ).hexdigest()[:24],
            batch_id=batch_id,
            source=source,
            native_id=f"checkpoint:{batch_id}",
            entity_id=f"checkpoint:{source}",
            event_type="checkpoint",
            observed_at=stamp,
            content=content,
            attributes=attributes,
            payload_hash=payload_hash,
        )

    def to_inbox_item(self) -> InboxItem:
        content = self.content
        metadata = dict(self.attributes)
        if self.source_updated_at and "latest_activity_at" not in metadata:
            metadata["latest_activity_at"] = self.source_updated_at
        if self.event_type == "resolved" and not metadata.get("resolution_evidence"):
            metadata["resolution_evidence"] = "The connected source reports this item complete."
        return InboxItem(
            source=self.source,
            id=self.native_id,
            sender=str(content.get("sender") or ""),
            subject=str(content.get("subject") or ""),
            body=str(content.get("body") or ""),
            url=self.permalink,
            ts=self.occurred_at,
            channel=str(content.get("channel") or ""),
            thread_id=str(content.get("thread_id") or ""),
            kind=str(content.get("kind") or metadata.get("kind") or ""),
            status=(
                "done"
                if self.event_type in {"resolved", "deleted"}
                else str(content.get("status") or "open")
            ),  # type: ignore[arg-type]
            participants=[str(value) for value in content.get("participants", [])],
            related_keys=[str(value) for value in content.get("related_keys", [])],
            metadata=metadata,
        )


__all__ = ["EventType", "SourceObservation", "utc_now"]
