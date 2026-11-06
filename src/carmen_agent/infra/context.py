from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from carmen_agent.infra.agent import ResolvedConfig
from carmen_agent.infra.agent import ModelConfig
from carmen_agent.skills.schemas import InboxItem, TodoItem, parse_stamp

Message = dict[str, str]


@dataclass(frozen=True)
class ContextAssembly:
    messages: tuple[Message, ...]
    resolved_tool_names: tuple[str, ...]
    loaded_skill_versions: tuple[str, ...]


@dataclass(frozen=True)
class EvidenceRecord:
    """One bounded, source-attributed record supplied to a reasoning model."""

    source: str
    id: str
    sender: str
    subject: str
    body: str
    url: str
    timestamp: str
    channel: str
    thread_id: str
    kind: str
    status: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class TaskContextBundle:
    """Auditable multi-source context for one synthesized task."""

    task_id: str
    evidence: tuple[EvidenceRecord, ...]
    expected_sources: tuple[str, ...]
    covered_sources: tuple[str, ...]
    missing_sources: tuple[str, ...]
    latest_activity_at: str
    conflicts: tuple[str, ...]
    truncated: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "evidence": [asdict(record) for record in self.evidence],
            "coverage": {
                "expected_sources": list(self.expected_sources),
                "covered_sources": list(self.covered_sources),
                "missing_sources": list(self.missing_sources),
            },
            "latest_activity_at": self.latest_activity_at,
            "conflicts": list(self.conflicts),
            "truncated": self.truncated,
        }


class ContextEngine:
    """Assemble a stable static prefix followed by increasingly dynamic context."""

    def task_context(
        self,
        *,
        task: TodoItem,
        items: Iterable[InboxItem],
        max_items: int = 8,
        max_body_chars: int = 12_000,
    ) -> TaskContextBundle:
        """Gather every explicitly-linked record for a task within strict bounds.

        Source ids are the strongest join. Stable related keys and exact links are
        secondary joins so a Slack thread and a GitHub PR can contribute to the same
        context without fuzzy title matching.
        """
        source_ids = set(task.source_ids or [f"{task.source}:{task.id}"])
        task_keys = {key.strip().lower() for key in task.related_keys if key.strip()}
        task_links = {link.strip() for link in task.links if link.strip()}
        matches: list[tuple[bool, InboxItem]] = []
        for item in items:
            native_id = f"{item.source}:{item.id}"
            item_keys = {key.strip().lower() for key in item.related_keys if key.strip()}
            item_keys |= {f"explicit:{key}" for key in item_keys}
            exact = native_id in source_ids or (
                item.source == task.source and item.id == task.id
            )
            related = bool(task_keys & item_keys)
            linked = bool(item.url and item.url in task_links)
            if exact or related or linked:
                matches.append((exact, item))

        def ordering(match: tuple[bool, InboxItem]) -> tuple[int, float, str]:
            exact, item = match
            activity = str(item.metadata.get("latest_activity_at") or item.ts)
            parsed = parse_stamp(activity)
            return (
                0 if exact else 1,
                -(parsed.timestamp() if parsed is not None else 0.0),
                f"{item.source}:{item.id}",
            )

        matches.sort(key=ordering)
        selected: list[EvidenceRecord] = []
        used_chars = 0
        truncated = len(matches) > max_items
        for _, item in matches[:max_items]:
            remaining = max(0, max_body_chars - used_chars)
            body = item.body[:remaining]
            if len(body) < len(item.body):
                truncated = True
            used_chars += len(body)
            selected.append(
                EvidenceRecord(
                    source=item.source,
                    id=item.id,
                    sender=item.sender,
                    subject=item.subject,
                    body=body,
                    url=item.url,
                    timestamp=item.ts,
                    channel=item.channel,
                    thread_id=item.thread_id,
                    kind=item.kind,
                    status=item.status,
                    metadata=dict(item.metadata),
                )
            )
            if used_chars >= max_body_chars:
                truncated = truncated or len(matches) > len(selected)
                break

        expected = tuple(sorted(set(task.sources or [task.source])))
        covered = tuple(sorted({record.source for record in selected}))
        missing = tuple(source for source in expected if source not in covered)
        activities = [
            str(record.metadata.get("latest_activity_at") or record.timestamp)
            for record in selected
            if record.metadata.get("latest_activity_at") or record.timestamp
        ]
        parsed_activities = [
            (parse_stamp(value), value) for value in activities
        ]
        latest = max(
            parsed_activities,
            key=lambda pair: pair[0].timestamp() if pair[0] is not None else 0.0,
            default=(None, ""),
        )[1]
        statuses = {record.status for record in selected}
        conflicts: list[str] = []
        if "done" in statuses and statuses - {"done"}:
            conflicts.append("Sources disagree whether this work is complete.")
        return TaskContextBundle(
            task_id=task.id,
            evidence=tuple(selected),
            expected_sources=expected,
            covered_sources=covered,
            missing_sources=missing,
            latest_activity_at=latest,
            conflicts=tuple(conflicts),
            truncated=truncated,
        )

    def assemble(
        self,
        *,
        config: ResolvedConfig,
        static_context: Iterable[str] = (),
        session_context: Iterable[str] = (),
        recalled_context: Iterable[str] = (),
        loaded_skills: Iterable[tuple[str, str, str]] = (),
        transcript: Iterable[Message] = (),
        current_user_message: str = "",
    ) -> ContextAssembly:
        tool_names = tuple(sorted({tool.name for tool in config.tools}))
        messages: list[Message] = [
            {"role": "system", "content": config.system_prompt.strip()}
        ]
        for text in static_context:
            if text.strip():
                messages.append({"role": "system", "content": text.strip()})
        if tool_names:
            messages.append(
                {
                    "role": "system",
                    "content": "Available tool names: " + json.dumps(tool_names),
                }
            )
        for text in session_context:
            if text.strip():
                messages.append({"role": "system", "content": text.strip()})
        for text in recalled_context:
            if text.strip():
                messages.append({"role": "system", "content": text.strip()})
        loaded_versions: list[str] = []
        for name, version, body in loaded_skills:
            if body.strip():
                messages.append({"role": "system", "content": body.strip()})
                loaded_versions.append(f"{name}@{version}")
        messages.extend(
            {"role": str(item["role"]), "content": str(item["content"])}
            for item in transcript
            if item.get("content", "").strip()
        )
        if current_user_message.strip():
            messages.append({"role": "user", "content": current_user_message.strip()})
        return ContextAssembly(
            messages=tuple(messages),
            resolved_tool_names=tool_names,
            loaded_skill_versions=tuple(loaded_versions),
        )

    def assemble_prompt(
        self,
        *,
        system_prompt: str,
        current_user_message: str,
        transcript: Iterable[Message] = (),
        session_context: Iterable[str] = (),
    ) -> list[Message]:
        """Small-surface adapter that still preserves the shared ordering contract."""
        return list(
            self.assemble(
                config=ResolvedConfig(
                    system_prompt=system_prompt,
                    tools=(),
                    skills=(),
                    model=ModelConfig(primary="runtime"),
                    max_iterations=1,
                ),
                session_context=session_context,
                transcript=transcript,
                current_user_message=current_user_message,
            ).messages
        )
