from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

from carmen_agent.chat import DEFAULT_ADAPTER, DEFAULT_PROFILE, PROJECT_ROOT
from carmen_agent.collector import (
    DEFAULT_COLLECT_SOURCES,
    CollectorError,
    collect_sources,
    parse_sources,
    record_collection_failure,
)
from carmen_agent.source_rules import partition_source_items
from carmen_agent.source_status import (
    authoritative_sources,
    configured_max_age_hours,
    fresh_sources,
    inspect_source_snapshots,
)
from carmen_agent.skills.draft import draft_all
from carmen_agent.skills.habits import format_suppression_hints
from carmen_agent.skills.calendar_preferences import personalize_calendar_tasks
from carmen_agent.skills.model import load_session
from carmen_agent.skills.schemas import dump_todos, load_inbox, parse_stamp
from carmen_agent.skills.synthesis import reconcile_state, synthesize_todos
from carmen_agent.skills.todo import write_morning_brief
from carmen_agent.platforms import parse_command
from carmen_agent.projections.tasks import TaskProjector
from carmen_agent.skills.triage import triage_all


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Build a cross-source morning work brief from Gmail, Slack, Calendar, "
            "and GitHub snapshots using the local personal agent."
        )
    )
    p.add_argument("--inbox", type=Path, default=PROJECT_ROOT / "data/inbox")
    p.add_argument("--drafts", type=Path, default=PROJECT_ROOT / "data/drafts")
    p.add_argument(
        "--brief",
        "--todo",
        dest="brief",
        type=Path,
        default=PROJECT_ROOT / "TODO.md",
        help="Morning brief output path. --todo remains as a backwards-compatible alias.",
    )
    p.add_argument("--todos-json", type=Path, default=PROJECT_ROOT / "data/inbox/todos.json")
    p.add_argument(
        "--state",
        type=Path,
        default=PROJECT_ROOT / "work/daily-chief-of-staff-state.json",
        help="Metadata-only carryover/progress state.",
    )
    p.add_argument(
        "--model",
        default=None,
        help="Override the model saved by `carmen init` for this run.",
    )
    p.add_argument("--adapter", type=Path, default=DEFAULT_ADAPTER)
    p.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    p.add_argument("--base-only", action="store_true")
    p.add_argument("--no-drafts", action="store_true", help="Skip reply drafting; just triage.")
    p.add_argument(
        "--carryover-max-days",
        type=int,
        default=3,
        help=(
            "Drop carryover items that haven't been re-seen in the source in this many days. "
            "Prevents merged PRs / resolved threads from accreting in the brief."
        ),
    )
    p.add_argument(
        "--collector-command",
        default=os.environ.get("CARMEN_COLLECT_COMMAND", ""),
        help=(
            "Optional command that refreshes data/inbox/*.json before triage. "
            "Executed directly without a shell."
        ),
    )
    p.add_argument(
        "--collect-sources",
        default=os.environ.get("CARMEN_COLLECT_SOURCES", DEFAULT_COLLECT_SOURCES),
        help="Sources for the built-in Codex connector bridge. Default: all four.",
    )
    p.add_argument(
        "--collection-hours",
        type=int,
        default=36,
        help="Lookback window for the built-in connector bridge.",
    )
    p.add_argument(
        "--no-collect",
        action="store_true",
        help="Do not refresh connected sources; process existing local snapshots only.",
    )
    p.add_argument(
        "--source-max-age-hours",
        type=int,
        default=configured_max_age_hours(),
        help="Ignore source snapshots older than this many hours. Default: 48.",
    )
    return p


def main() -> None:
    args = build_parser().parse_args()
    refresh_failure = ""
    if args.collector_command:
        try:
            command = parse_command(args.collector_command)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        if not command:
            raise SystemExit("--collector-command did not contain an executable.")
        print("Refreshing Slack, Gmail, Calendar, and GitHub snapshots…", flush=True)
        try:
            subprocess.run(command, cwd=PROJECT_ROOT, check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise SystemExit(f"Collector command failed: {exc}") from exc
    elif not args.no_collect:
        print("Refreshing connected work sources independently…", flush=True)
        sources = parse_sources(args.collect_sources)
        try:
            counts, coverage = collect_sources(
                sources=sources,
                hours=args.collection_hours,
                inbox_dir=args.inbox,
                mode=os.environ.get("CARMEN_CONNECTOR_MODE", "auto"),
            )
        except CollectorError as exc:
            record_collection_failure(sources, str(exc), inbox_dir=args.inbox)
            existing = any(
                (args.inbox / f"{source}.json").is_file()
                for source in ("slack", "gmail", "calendar", "github")
            )
            if not existing:
                raise SystemExit(
                    f"Connected-source refresh failed and no local snapshots exist: {exc}"
                ) from exc
            print(f"Source refresh failed; using existing snapshots: {exc}", flush=True)
            refresh_failure = str(exc)
        else:
            summary = ", ".join(
                f"{record['source']}={record['status']}:{counts.get(record['source'], 0)}"
                for record in coverage
            )
            print(f"Source refresh complete ({summary}).", flush=True)
            # Surface the collector-reported reason for any non-ok/empty source so
            # transient errors don't stay silent — the summary alone hides why.
            for record in coverage:
                if record["status"] in {"error", "unavailable"}:
                    detail = record.get("detail") or "no detail provided"
                    print(
                        f"  {record['source']} {record['status']}: {detail}",
                        flush=True,
                    )
            failed_sources = [
                record for record in coverage
                if record["status"] in {"error", "unavailable"}
            ]
            if failed_sources:
                refresh_failure = "; ".join(
                    f"{record['source']}: {record.get('detail') or record['status']}"
                    for record in failed_sources
                )
    if not args.inbox.is_dir():
        raise SystemExit(
            f"No inbox directory at {args.inbox}. Populate it through the Codex connectors first "
            "(see data/inbox/README.md)."
        )
    health = inspect_source_snapshots(
        args.inbox, max_age_hours=args.source_max_age_hours
    )
    # A cached file may still be younger than the age limit after a failed
    # connector attempt. During a real refresh, process only sources whose
    # current attempt succeeded; otherwise cached records would incorrectly
    # advance task last_seen and look newly verified. Explicit --no-collect is
    # the recovery/import path and may intentionally process fresh cached files.
    accepted_sources = (
        fresh_sources(health)
        if args.no_collect
        else authoritative_sources(health)
    )
    for record in health:
        if not record.fresh:
            age = (
                f" ({record.age_hours:.1f}h old)"
                if record.age_hours is not None
                else ""
            )
            print(
                f"WARNING: ignoring {record.source} snapshot: "
                f"{record.status}{age}. Run `uv run carmen today` to refresh.",
                flush=True,
            )
        elif record.status in {"error", "unavailable"}:
            print(
                f"WARNING: {record.source} refresh is degraded: "
                f"{record.detail or record.status}.",
                flush=True,
            )
    items = load_inbox(args.inbox, sources=accepted_sources)
    actionable_items, deterministic_done = partition_source_items(items)
    current = []
    if actionable_items:
        session = load_session(
            model_name=args.model,
            adapter=args.adapter,
            profile_path=args.profile,
            base_only=args.base_only,
        )
        print(f"Triaging {len(actionable_items)} current item(s)…", flush=True)
        hints = format_suppression_hints()
        if hints:
            print(
                f"Injecting {hints.count(chr(10) + '-')} learned dismissal pattern(s) "
                "into triage.",
                flush=True,
            )
        raw_todos = triage_all(session, actionable_items, suppression_hints=hints)
        personalize_calendar_tasks(raw_todos, actionable_items)

        if not args.no_drafts:
            items_by_id = {
                f"{item.source}:{item.id}": item for item in actionable_items
            }
            reply_count = sum(1 for todo in raw_todos if todo.needs_reply)
            if reply_count:
                print(f"Drafting {reply_count} reply/replies…", flush=True)
                draft_all(session, items_by_id, raw_todos, args.drafts)
        current = synthesize_todos(items, raw_todos + deterministic_done)
    elif deterministic_done:
        print(
            f"Reconciled {len(deterministic_done)} completed/past source item(s) "
            "without loading the model.",
            flush=True,
        )
        current = synthesize_todos(items, deterministic_done)
    else:
        print(f"No current source items in {args.inbox}; reconciling prior work.")

    # Terminal states come from deterministic source rules, never from an LLM
    # guess. This makes the same observations project to the same task state.
    current = TaskProjector().apply_source_truth(current, items)

    todos, progress = reconcile_state(
        current,
        args.state,
        carryover_max_days=args.carryover_max_days,
        authoritative_sources=authoritative_sources(health) & {"github", "calendar"},
        source_last_success={
            record.source: refreshed
            for record in health
            if (refreshed := parse_stamp(record.last_success_at)) is not None
        },
    )
    coverage = accepted_sources
    write_morning_brief(todos, progress, args.brief, coverage=coverage)
    dump_todos(todos, args.todos_json)
    print(
        f"Wrote {args.brief} ({len(todos)} open item(s), "
        f"{len(progress.completed)} completed since the prior run)."
    )
    if refresh_failure:
        raise SystemExit(
            "Refresh was incomplete. Last good snapshots were preserved. "
            + refresh_failure
        )


if __name__ == "__main__":
    main()
