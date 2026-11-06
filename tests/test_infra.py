from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from carmen_agent.evals.runner import run_extraction_eval
from carmen_agent.evals.world_snapshot import (
    RecordingProvider,
    SnapshotProvider,
    WorldSnapshot,
)
from carmen_agent.hosted.storage import HostedUser, Installation, SQLiteStore
from carmen_agent.infra.agent import ModelConfig, ResolvedConfig, StaticAgentDefinition
from carmen_agent.infra.context import ContextEngine
from carmen_agent.infra.durable import DurableStepRunner, DurableToolExecutor
from carmen_agent.infra.runtime import RunContext, ToolRuntime, UserActor
from carmen_agent.infra.tools import (
    Capability,
    Tool,
    ToolExecutor,
    ToolPolicy,
    ToolScope,
    build_execution_plan,
    stable_idempotency_key,
    ToolCall,
)
from carmen_agent.skills.schemas import InboxItem, TodoItem


@dataclass(frozen=True)
class ReadThing(Tool):
    name = "read_thing"
    description = "Read one test value."
    policy = ToolPolicy(
        scopes=(ToolScope.USER,),
        read_only=True,
        required_permissions=frozenset({"read"}),
        snapshot_safe=True,
    )
    value: str

    def run(self, runtime: ToolRuntime):
        return {"value": self.value}


@dataclass(frozen=True)
class WriteThing(Tool):
    name = "write_thing"
    description = "Write one test value."
    policy = ToolPolicy(
        scopes=(ToolScope.USER,),
        read_only=False,
        capabilities=frozenset({Capability.EXTERNAL_WRITE}),
        required_permissions=frozenset({"write"}),
        approval="explicit",
        snapshot_safe=True,
    )
    value: str

    def run(self, runtime: ToolRuntime):
        runtime.session_state["writes"] = runtime.session_state.get("writes", 0) + 1
        return {"written": self.value}


class TestAgent(StaticAgentDefinition):
    name = "test"
    model = ModelConfig(primary="fake")
    system_prompt = "Static system prompt."
    tools = (WriteThing, ReadThing)


def _context(*permissions: str) -> RunContext:
    return RunContext(
        run_id="run-1",
        tenant_id="T1",
        user_id="U1",
        actor=UserActor(user_id="U1"),
        surface="test",
        permissions=frozenset(permissions),
    )


def _store(path: Path) -> SQLiteStore:
    store = SQLiteStore(path)
    store.save_installation(
        Installation(
            team_id="T1", team_name="Test", enterprise_id="", bot_user_id="B1",
            bot_token_encrypted="encrypted", user_token_encrypted="",
            installing_user_id="U1", scopes="commands",
        )
    )
    store.save_user(HostedUser(team_id="T1", user_id="U1"))
    return store


def test_agent_config_filters_permissions_and_sorts_tools() -> None:
    config = TestAgent().build_config(_context("read"))
    assert [tool.name for tool in config.tools] == ["read_thing"]
    assert ReadThing.input_schema()["properties"]["value"]["type"] == "string"


def test_tool_policy_is_mandatory_at_class_definition_time() -> None:
    with pytest.raises(TypeError, match="must declare policy"):
        class MissingPolicy(Tool):
            name = "missing_policy"
            description = "Invalid test tool."

            def run(self, runtime: ToolRuntime):
                return None


def test_context_engine_keeps_static_prefix_and_sorts_dynamic_sections() -> None:
    config = ResolvedConfig(
        system_prompt="Static system prompt.",
        tools=(WriteThing, ReadThing),
        skills=(),
        model=ModelConfig(primary="fake"),
        max_iterations=1,
    )
    assembly = ContextEngine().assemble(
        config=config,
        session_context=("Tenant fact",),
        transcript=({"role": "assistant", "content": "Prior"},),
        current_user_message="Current",
    )
    assert assembly.messages[0]["content"] == "Static system prompt."
    assert assembly.resolved_tool_names == ("read_thing", "write_thing")
    assert assembly.messages[-1] == {"role": "user", "content": "Current"}


def test_context_engine_gathers_all_explicit_multi_source_evidence_with_coverage() -> None:
    task = TodoItem(
        source="slack",
        id="work-42",
        title="Ship PR 42",
        why="Launch thread and PR are linked",
        urgency="today",
        needs_reply=True,
        sources=["slack", "github"],
        source_ids=["slack:s1", "github:g1"],
    )
    items = [
        InboxItem(
            source="slack", id="s1", sender="Alice", subject="Launch",
            body="Can you ship this?", ts="2026-08-13T13:00:00+00:00",
        ),
        InboxItem(
            source="github", id="g1", sender="Carmen", subject="PR 42",
            body="Approved and passing.", ts="2026-08-13T14:00:00+00:00",
            metadata={"latest_activity_at": "2026-08-13T14:30:00+00:00"},
        ),
    ]
    bundle = ContextEngine().task_context(task=task, items=items)
    assert {(item.source, item.id) for item in bundle.evidence} == {
        ("slack", "s1"), ("github", "g1")
    }
    assert bundle.covered_sources == ("github", "slack")
    assert bundle.missing_sources == ()
    assert bundle.latest_activity_at == "2026-08-13T14:30:00+00:00"


def test_context_engine_reports_missing_evidence_and_enforces_body_budget() -> None:
    task = TodoItem(
        source="slack", id="work", title="Investigate", why="Linked sources",
        urgency="today", needs_reply=False, sources=["slack", "github"],
        source_ids=["slack:s1"],
    )
    bundle = ContextEngine().task_context(
        task=task,
        items=[InboxItem(source="slack", id="s1", sender="A", subject="Ask", body="x" * 50)],
        max_body_chars=10,
    )
    assert bundle.evidence[0].body == "x" * 10
    assert bundle.missing_sources == ("github",)
    assert bundle.truncated is True


def test_tool_executor_enforces_permission_and_explicit_approval() -> None:
    tool = WriteThing("x")
    key = stable_idempotency_key("run-1", "execute", tool.name)
    runtime = ToolRuntime(
        context=_context("write"), tool_call_id="call-1", idempotency_key=key
    )
    with pytest.raises(PermissionError, match="explicit approval"):
        ToolExecutor().execute(tool, runtime)
    runtime.approved = True
    assert ToolExecutor().execute(tool, runtime) == {"written": "x"}


def test_execution_plan_separates_reads_writes_and_approvals() -> None:
    plan = build_execution_plan(
        [ToolCall("1", ReadThing("r")), ToolCall("2", WriteThing("w"))]
    )
    assert [call.tool.name for call in plan.reads] == ["read_thing"]
    assert [call.tool.name for call in plan.approvals] == ["write_thing"]
    assert plan.writes == ()


def test_durable_steps_and_write_tools_replay_without_duplicate_effect(tmp_path: Path) -> None:
    store = _store(tmp_path / "infra.db")
    context = _context("write")
    store.start_agent_run(
        context=context, agent_name="test", agent_version=1, config={"tools": ["write_thing"]}
    )
    runner = DurableStepRunner(store, context.run_id)
    calls = {"count": 0}

    def work():
        calls["count"] += 1
        return {"answer": 42}

    key = stable_idempotency_key(context.run_id, "work", "test")
    assert runner.step("work", work, idempotency_key=key) == {"answer": 42}
    assert runner.step("work", work, idempotency_key=key) == {"answer": 42}
    assert calls["count"] == 1

    session: dict[str, int] = {}
    tool = WriteThing("hello")
    tool_key = stable_idempotency_key(context.run_id, "execute", tool.name)
    first = ToolRuntime(
        context=context,
        tool_call_id="call-write",
        idempotency_key=tool_key,
        session_state=session,
        approved=True,
    )
    executor = DurableToolExecutor(store, "execute")
    assert executor.execute(tool, first) == {"written": "hello"}
    assert executor.execute(tool, first) == {"written": "hello"}
    assert session["writes"] == 1


class EchoProvider:
    def generate(self, messages, **_):
        return "snapshot output: " + messages[-1]["content"]


def test_world_snapshot_replays_exact_calls_and_refuses_drift(tmp_path: Path) -> None:
    snapshot = WorldSnapshot()
    recorder = RecordingProvider(EchoProvider(), snapshot)  # type: ignore[arg-type]
    messages = [{"role": "user", "content": "hello"}]
    assert recorder.generate(messages, max_tokens=10, temperature=0.0).endswith("hello")
    path = tmp_path / "snapshot.json"
    snapshot.save(path)
    replay = SnapshotProvider(WorldSnapshot.load(path))
    assert replay.generate(messages, max_tokens=10, temperature=0.0).endswith("hello")
    with pytest.raises(KeyError, match="live fallback is disabled"):
        replay.generate([{"role": "user", "content": "changed"}], max_tokens=10, temperature=0.0)


def test_extraction_eval_meets_precision_first_gate() -> None:
    report = run_extraction_eval()
    assert report.cases >= 6
    assert report.precision == 1.0
    assert report.recall == 1.0
    assert report.type_accuracy == 1.0
