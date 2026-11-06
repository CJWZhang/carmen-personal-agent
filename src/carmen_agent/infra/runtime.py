from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, TypeAlias


@dataclass(frozen=True, kw_only=True)
class UserActor:
    kind: Literal["user"] = "user"
    user_id: str


@dataclass(frozen=True, kw_only=True)
class SystemActor:
    kind: Literal["system"] = "system"
    service: str


@dataclass(frozen=True, kw_only=True)
class AttributedSystemActor:
    kind: Literal["system_on_behalf_of"] = "system_on_behalf_of"
    service: str
    user_id: str


@dataclass(frozen=True, kw_only=True)
class InternalActor:
    kind: Literal["internal"] = "internal"
    component: str


Actor: TypeAlias = UserActor | SystemActor | AttributedSystemActor | InternalActor


def attributed_user_id(actor: Actor) -> str | None:
    if isinstance(actor, (UserActor, AttributedSystemActor)):
        return actor.user_id
    return None


@dataclass(frozen=True, kw_only=True)
class RunContext:
    """Serializable identity and authorization facts fixed for one run."""

    run_id: str
    tenant_id: str
    user_id: str | None
    actor: Actor
    surface: str
    thread_id: str | None = None
    permissions: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        actor_user = attributed_user_id(self.actor)
        if actor_user is not None and self.user_id != actor_user:
            raise ValueError("RunContext user_id must match its attributed actor.")


@dataclass
class ToolRuntime:
    """Live, non-serializable execution handle reconstructed at a tool boundary."""

    context: RunContext
    tool_call_id: str
    idempotency_key: str
    session_state: dict[str, Any] = field(default_factory=dict)
    services: Mapping[str, Any] = field(default_factory=dict)
    approved: bool = False

    def service(self, name: str) -> Any:
        try:
            return self.services[name]
        except KeyError as exc:
            raise RuntimeError(f"Tool runtime service {name!r} is unavailable.") from exc
