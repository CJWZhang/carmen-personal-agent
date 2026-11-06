"""Deterministic source and task projections."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Iterable

from carmen_agent.domain.events import SourceObservation
from carmen_agent.skills.schemas import InboxItem, Source, TodoItem
from carmen_agent.source_rules import partition_source_items


class TaskProjector:
    """Project current source truth without asking a model to mutate state."""

    def source_items(
        self,
        events: Iterable[SourceObservation],
        *,
        source: Source | None = None,
    ) -> list[InboxItem]:
        selected_events = [
            event for event in events if source is None or event.source == source
        ]
        latest_checkpoint: dict[str, SourceObservation] = {}
        for event in selected_events:
            if event.event_type == "checkpoint":
                # JSONL append order is the commit order. Do not let two batches
                # sharing the same clock tick choose a random hash ordering.
                latest_checkpoint[event.source] = event
        latest: dict[tuple[str, str], SourceObservation] = {}
        for event in selected_events:
            if event.event_type == "checkpoint":
                continue
            checkpoint = latest_checkpoint.get(event.source)
            if checkpoint is not None and event.batch_id != checkpoint.batch_id:
                continue
            key = (event.source, event.entity_id)
            previous = latest.get(key)
            ordering = (
                event.source_updated_at or event.observed_at,
                event.observed_at,
                event.event_id,
            )
            previous_ordering = (
                previous.source_updated_at or previous.observed_at,
                previous.observed_at,
                previous.event_id,
            ) if previous else ("", "", "")
            if previous is None or ordering > previous_ordering:
                latest[key] = event
        return sorted(
            (event.to_inbox_item() for event in latest.values()),
            key=lambda item: (item.source, item.id),
        )

    def apply_source_truth(
        self,
        tasks: Iterable[TodoItem],
        items: list[InboxItem],
        *,
        now: datetime | None = None,
    ) -> list[TodoItem]:
        """Apply only source-verifiable terminal states to synthesized tasks."""
        _, completions = partition_source_items(items, now=now)
        completed_by_source_id = {
            source_id: task
            for task in completions
            for source_id in task.source_ids
        }
        projected: list[TodoItem] = []
        for task in tasks:
            evidence = next(
                (
                    completed_by_source_id[source_id]
                    for source_id in (task.source_ids or [f"{task.source}:{task.id}"])
                    if source_id in completed_by_source_id
                ),
                None,
            )
            if evidence is None:
                projected.append(task)
                continue
            projected.append(
                replace(
                    task,
                    status="done",
                    lifecycle_stage="done",
                    resolution_mode="source_verified",
                    completion_evidence=evidence.completion_evidence,
                    last_verified_at=evidence.last_verified_at,
                )
            )
        return projected


__all__ = ["TaskProjector"]
