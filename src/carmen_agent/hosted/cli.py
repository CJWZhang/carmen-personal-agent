from __future__ import annotations

import argparse
import os
from pathlib import Path
from wsgiref.simple_server import make_server

from carmen_agent.hosted.app import build_application
from carmen_agent.hosted.config import HostedSettings
from carmen_agent.hosted.crypto import FernetTokenCipher
from carmen_agent.hosted.slack_api import SlackAPI
from carmen_agent.hosted.storage import build_store
from carmen_agent.hosted.worker import (
    HostedWorker,
    enqueue_due_daily_digests,
    enqueue_reconciliation,
)
from carmen_agent.providers import build_provider


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the hosted Carmen Assistant services.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="Run a development WSGI server.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    worker = subparsers.add_parser("worker", help="Process queued model jobs.")
    worker.add_argument("--once", action="store_true")
    subparsers.add_parser(
        "enqueue-daily",
        help="Queue timezone-aware daily digests; safe to run hourly.",
    )
    subparsers.add_parser(
        "enqueue-reconcile",
        help="Queue idempotent source-resolution checks; safe to run hourly.",
    )
    subparsers.add_parser("migrate", help="Apply hosted Postgres migrations.")
    return parser


def migrate(database_url: str) -> None:
    if not database_url.startswith(("postgresql://", "postgres://")):
        raise SystemExit("Hosted migrations require a Postgres DATABASE_URL.")
    try:
        import psycopg
    except ImportError as exc:
        raise SystemExit("Install the `hosted` dependency extra first.") from exc
    working_migrations = Path.cwd() / "migrations"
    packaged_migrations = Path(__file__).resolve().parents[1] / "resources" / "migrations"
    source_migrations = Path(__file__).resolve().parents[3] / "migrations"
    default_dir = next(
        (
            candidate
            for candidate in (working_migrations, packaged_migrations, source_migrations)
            if candidate.is_dir()
        ),
        working_migrations,
    )
    migrations_dir = Path(os.environ.get("CARMEN_MIGRATIONS_DIR", default_dir))
    paths = sorted(migrations_dir.glob("*.sql"))
    if not paths:
        raise SystemExit(f"No migrations found in {migrations_dir}.")
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        for path in paths:
            applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE name=%s",
                (path.name,),
            ).fetchone()
            if applied:
                continue
            connection.execute(path.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO schema_migrations(name) VALUES (%s)",
                (path.name,),
            )
            print(f"Applied {path.name}")


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "migrate":
        # Database migrations do not contact Slack, serve HTTP, decrypt tokens,
        # or call a model provider. Requiring the complete hosted configuration
        # here prevents a legitimate one-purpose migration shell from running.
        database_url = os.environ.get("DATABASE_URL", "").strip()
        if not database_url:
            raise ConfigurationError(
                "Missing hosted configuration for migration: DATABASE_URL"
            )
        migrate(database_url)
        return
    settings = HostedSettings.from_env()
    if args.command == "serve":
        app = build_application()
        with make_server(args.host, args.port, app) as server:
            print(f"Serving Carmen Assistant on http://{args.host}:{args.port}")
            server.serve_forever()
        return
    store = build_store(settings.database_url)
    if args.command == "enqueue-daily":
        count = enqueue_due_daily_digests(store)
        print(f"Queued {count} daily digest(s).")
        return
    if args.command == "enqueue-reconcile":
        count = enqueue_reconciliation(store)
        print(f"Queued {count} reconciliation job(s).")
        return
    worker = HostedWorker(
        store=store,
        cipher=FernetTokenCipher(settings.token_encryption_key),
        slack=SlackAPI(),
        provider=build_provider(),
    )
    if args.once:
        print("Processed one job." if worker.run_once() else "No queued jobs.")
    else:
        worker.run_forever()


if __name__ == "__main__":
    main()
