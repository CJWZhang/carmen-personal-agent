from __future__ import annotations

from dataclasses import dataclass, field

from carmen_agent.actions.schemas import SourceName


@dataclass(frozen=True)
class SourceMessage:
    id: str
    actor_id: str
    text: str
    occurred_at: str = ""


@dataclass(frozen=True)
class SourceEvent:
    source: SourceName
    tenant_id: str
    target_user_id: str
    source_id: str
    actor_id: str
    text: str
    channel_id: str = ""
    thread_id: str = ""
    permalink: str = ""
    occurred_at: str = ""
    direct: bool = False
    context: tuple[SourceMessage, ...] = field(default_factory=tuple)
