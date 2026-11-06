from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol, TypeVar

from carmen_agent.infra.runtime import RunContext, ToolRuntime
from carmen_agent.infra.tools import Tool, ToolCallRecorder, redacted_snapshot
from carmen_agent.infra.tools import ToolExecutor

T = TypeVar("T")


class DurableRunStore(Protocol):
    def start_agent_run(
        self, *, context: RunContext, agent_name: str, agent_version: int,
        config: dict[str, Any]
    ) -> None: ...
    def finish_agent_run(self, run_id: str, *, error: str = "", retrying: bool = False) -> None: ...
    def get_completed_run_step(self, run_id: str, step_name: str) -> Any | None: ...
    def start_run_step(self, *, run_id: str, step_name: str, idempotency_key: str) -> None: ...
    def finish_run_step(
        self, *, run_id: str, step_name: str, output: Any = None, error: str = ""
    ) -> None: ...
    def start_tool_call(
        self, *, call_id: str, run_id: str, step_name: str, tool_name: str,
        idempotency_key: str, policy: dict[str, Any], arguments: Any
    ) -> None: ...
    def finish_tool_call(self, call_id: str, *, output: Any = None, error: str = "") -> None: ...
    def get_completed_tool_call(self, idempotency_key: str) -> Any | None: ...


@dataclass
class DurableStepRunner:
    store: DurableRunStore
    run_id: str

    def step(
        self,
        name: str,
        function: Callable[[], T],
        *,
        idempotency_key: str,
        cache: bool = True,
    ) -> T:
        if cache:
            completed = self.store.get_completed_run_step(self.run_id, name)
            if completed is not None:
                return completed  # type: ignore[return-value]
        self.store.start_run_step(
            run_id=self.run_id,
            step_name=name,
            idempotency_key=idempotency_key,
        )
        try:
            output = function()
        except Exception as exc:
            self.store.finish_run_step(
                run_id=self.run_id,
                step_name=name,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        self.store.finish_run_step(run_id=self.run_id, step_name=name, output=output)
        return output


@dataclass
class StoreToolRecorder(ToolCallRecorder):
    store: DurableRunStore
    step_name: str

    def started(self, tool: Tool, runtime: ToolRuntime, arguments: dict[str, Any]) -> None:
        policy = tool.policy
        self.store.start_tool_call(
            call_id=runtime.tool_call_id,
            run_id=runtime.context.run_id,
            step_name=self.step_name,
            tool_name=tool.name,
            idempotency_key=runtime.idempotency_key,
            policy={
                "scopes": [scope.value for scope in policy.scopes],
                "read_only": policy.read_only,
                "capabilities": sorted(item.value for item in policy.capabilities),
                "required_permissions": sorted(policy.required_permissions),
                "approval": policy.approval,
            },
            arguments=redacted_snapshot(arguments, safe=policy.snapshot_safe),
        )

    def completed(self, tool: Tool, runtime: ToolRuntime, output: Any) -> None:
        self.store.finish_tool_call(
            runtime.tool_call_id,
            output=redacted_snapshot(output, safe=tool.policy.snapshot_safe),
        )

    def failed(self, tool: Tool, runtime: ToolRuntime, error: str) -> None:
        self.store.finish_tool_call(runtime.tool_call_id, error=error)


@dataclass
class DurableToolExecutor:
    store: DurableRunStore
    step_name: str

    def execute(self, tool: Tool, runtime: ToolRuntime) -> Any:
        if not tool.policy.read_only:
            completed = self.store.get_completed_tool_call(runtime.idempotency_key)
            if completed is not None:
                return completed
        return ToolExecutor(StoreToolRecorder(self.store, self.step_name)).execute(
            tool, runtime
        )
