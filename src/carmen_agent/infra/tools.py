from __future__ import annotations

import dataclasses
import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, ClassVar, Iterable, Literal, Mapping, Protocol, get_args, get_origin, get_type_hints

from carmen_agent.infra.runtime import RunContext, ToolRuntime


class ToolScope(str, Enum):
    USER = "user"
    TENANT = "tenant"
    INTERNAL = "internal"


class Capability(str, Enum):
    READ_PRIVATE_DATA = "read_private_data"
    EXTERNAL_EGRESS = "external_egress"
    EXTERNAL_WRITE = "external_write"
    LOCAL_EXECUTION = "local_execution"


@dataclass(frozen=True)
class ToolPolicy:
    scopes: tuple[ToolScope, ...]
    read_only: bool
    capabilities: frozenset[Capability] = field(default_factory=frozenset)
    required_permissions: frozenset[str] = field(default_factory=frozenset)
    workload_permissions: frozenset[str] = field(default_factory=frozenset)
    approval: Literal["none", "explicit"] = "none"
    phase: Literal["skill", "normal"] = "normal"
    snapshot_safe: bool = False


class Tool(ABC):
    """One declared capability. Subclasses must be dataclasses with a policy."""

    name: ClassVar[str]
    description: ClassVar[str]
    policy: ClassVar[ToolPolicy]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.__dict__.get("__abstractmethods__"):
            return
        for attribute in ("name", "description", "policy"):
            if not hasattr(cls, attribute):
                raise TypeError(f"{cls.__name__} must declare {attribute}.")
        if not isinstance(cls.policy, ToolPolicy):
            raise TypeError(f"{cls.__name__}.policy must be a ToolPolicy.")

    @abstractmethod
    def run(self, runtime: ToolRuntime) -> Any: ...

    @classmethod
    def available(cls, context: RunContext) -> bool:
        return True

    @classmethod
    def input_schema(cls) -> dict[str, Any]:
        if not dataclasses.is_dataclass(cls):
            return {"type": "object", "properties": {}, "additionalProperties": False}
        properties: dict[str, Any] = {}
        required: list[str] = []
        hints = get_type_hints(cls)
        for item in dataclasses.fields(cls):
            properties[item.name] = _json_type(hints.get(item.name, item.type))
            if item.default is dataclasses.MISSING and item.default_factory is dataclasses.MISSING:
                required.append(item.name)
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    @classmethod
    def as_openai_tool(cls) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": cls.name,
                "description": cls.description,
                "parameters": cls.input_schema(),
            },
        }


def _json_type(annotation: Any) -> dict[str, Any]:
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in {list, tuple, set, frozenset}:
        return {"type": "array", "items": _json_type(args[0] if args else str)}
    if origin is dict or annotation in {dict, Mapping}:
        return {"type": "object"}
    if origin is Literal:
        values = list(args)
        return {"type": "string", "enum": values}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    return {"type": "string"}


class ToolRegistry:
    def __init__(self, tools: Iterable[type[Tool]] = ()) -> None:
        self._tools: dict[str, type[Tool]] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: type[Tool]) -> None:
        existing = self._tools.get(tool.name)
        if existing is not None and existing is not tool:
            raise ValueError(f"Duplicate tool name {tool.name!r}.")
        self._tools[tool.name] = tool

    def get(self, name: str) -> type[Tool]:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"Unknown tool {name!r}.") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    tool: Tool


@dataclass(frozen=True)
class ExecutionPlan:
    skills: tuple[ToolCall, ...]
    approvals: tuple[ToolCall, ...]
    reads: tuple[ToolCall, ...]
    writes: tuple[ToolCall, ...]


def build_execution_plan(calls: Iterable[ToolCall]) -> ExecutionPlan:
    ordered = tuple(calls)
    return ExecutionPlan(
        skills=tuple(call for call in ordered if call.tool.policy.phase == "skill"),
        approvals=tuple(call for call in ordered if call.tool.policy.approval == "explicit"),
        reads=tuple(
            call for call in ordered
            if call.tool.policy.phase != "skill"
            and call.tool.policy.approval != "explicit"
            and call.tool.policy.read_only
        ),
        writes=tuple(
            call for call in ordered
            if call.tool.policy.phase != "skill"
            and call.tool.policy.approval != "explicit"
            and not call.tool.policy.read_only
        ),
    )


class ToolCallRecorder(Protocol):
    def started(self, tool: Tool, runtime: ToolRuntime, arguments: dict[str, Any]) -> None: ...
    def completed(self, tool: Tool, runtime: ToolRuntime, output: Any) -> None: ...
    def failed(self, tool: Tool, runtime: ToolRuntime, error: str) -> None: ...


class ToolExecutor:
    def __init__(self, recorder: ToolCallRecorder | None = None) -> None:
        self.recorder = recorder

    def execute(self, tool: Tool, runtime: ToolRuntime) -> Any:
        policy = tool.policy
        missing = policy.required_permissions - runtime.context.permissions
        if missing:
            raise PermissionError(
                f"Tool {tool.name!r} requires permissions: {', '.join(sorted(missing))}."
            )
        if not tool.available(runtime.context):
            raise RuntimeError(f"Tool {tool.name!r} is unavailable for this run.")
        if policy.approval == "explicit" and not runtime.approved:
            raise PermissionError(f"Tool {tool.name!r} requires explicit approval.")
        arguments = dataclasses.asdict(tool) if dataclasses.is_dataclass(tool) else {}
        if self.recorder:
            self.recorder.started(tool, runtime, arguments)
        try:
            output = tool.run(runtime)
        except Exception as exc:
            if self.recorder:
                self.recorder.failed(tool, runtime, f"{type(exc).__name__}: {exc}")
            raise
        if self.recorder:
            self.recorder.completed(tool, runtime, output)
        return output


def stable_idempotency_key(run_id: str, step_name: str, tool_name: str) -> str:
    raw = "\x1f".join((run_id, step_name, tool_name))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def serialize_tool_output(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return serialize_tool_output(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(key): serialize_tool_output(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [serialize_tool_output(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def redacted_snapshot(value: Any, *, safe: bool) -> Any:
    serialized = serialize_tool_output(value)
    if safe:
        return serialized
    encoded = json.dumps(serialized, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return {"redacted": True, "sha256": hashlib.sha256(encoded).hexdigest()}
