# Carmen architecture

This repository uses a small domain-oriented Python layout. It borrows the
useful parts of larger app architectures—clear ownership, transport/domain
separation, mirrored tests, and explicit side-effect boundaries—without adding
framework layers that a local personal agent does not need.

## Dependency direction

```text
UI / CLI / hosted transports
            │
            ▼
      apps/<domain>
            │
     ┌──────┼──────────────┐
     ▼      ▼              ▼
 actions  ingestion      context
            │              │
            ▼              ▼
      connectors       retrieval
            │
            ▼
    domain events → storage → projections
     └──────────────┬─────────────┘
            ▼
          infra
```

- Transports parse requests, authorize operations, and render results. They do
  not own product policy.
- `apps/<domain>/` owns schemas, validation, defaults, and use-case policy for
  one product capability.
- `actions/` owns typed external mutations and receipts.
- `connectors/` owns direct provider reads, normalization, and deterministic
  source semantics. Direct adapters return typed source items and never import UI code.
- `domain/` owns sparse immutable source observations. `storage/` durably appends
  them; `projections/` derives disposable snapshots and source-verifiable task state.
- `ingestion/` owns independent source orchestration, cursors, commits, and
  health. A provider failure must not roll back another provider's successful read.
- `context/` routes the query, retrieves exact/lexical/temporal evidence, and
  enforces the prompt token budget. It never uses the style model as factual memory.
- `ranking/` combines obligation, urgency, freshness, and learned preferences in
  an inspectable score with reasons.
- `observability/` accepts only aggregate scalars and tracks latency/fallbacks;
  message text and stable record identifiers are forbidden.
- `skills/` owns bounded model-assisted operations such as triage, drafting,
  synthesis, and TODO rendering. Model output is never authoritative state.
- `infra/` owns cross-cutting runtime, context, durability, tool policy, and run
  identity. It must not depend on a product transport.
- `hosted/` and `ui/` are deployment and presentation adapters.

Imports should point toward domain policy, never back toward a transport. A
top-level module may remain as a stable CLI or compatibility import while its
implementation moves behind a domain package.

## Current source layout

```text
src/carmen_agent/
  apps/
    automation/       Daily-refresh/reminder schema, presets, config parsing
  actions/            Typed connector actions, ranking, resolution, receipts
  connectors/         Slack, Gmail, Calendar, and GitHub source adapters
    direct/            First-party provider API readers
  context/             Query routing, hybrid retrieval, token-budgeted packets
  domain/              Sparse immutable source observations
  evals/              Frozen quality and world-snapshot checks
  hosted/             Hosted Slack transport and persistence adapter
  infra/              Context engine, tools, policies, durable runtime
  ingestion/          Per-source sync coordination and cursors
  observability/      Private aggregate latency/quality telemetry
  projections/        Deterministic source/task read models
  providers/          Model-provider boundary
  ranking/            Inspectable personalized task ordering
  skills/             Triage, synthesis, drafting, habits, TODO rendering
  storage/            Append-only bounded local event store
  ui/                 Loopback HTTP transport and static dashboard
  assistant.py        Stable unified CLI entry point
  collector.py        Bounded connected-source orchestration
  commands.py         Stable task-command entry point
  reminders.py        Stable reminder entry point
  scheduler.py        Portable scheduler process
```

Tests for a domain mirror its source path. For example,
`apps/automation/settings.py` is covered by
`tests/apps/automation/test_settings.py`; transport integration stays in
`tests/test_ui.py` and portability stays in `tests/test_portability.py`.

## Domain package shape

Use only the folders a domain actually needs:

```text
apps/<domain>/
  __init__.py          Narrow public API
  settings.py          Typed configuration and validation, when applicable
  schemas.py           Persisted/request shapes, when applicable
  service.py           Use-case orchestration, when it becomes substantial
  repository.py        Side-effect-free persistence interface, when needed
```

Do not create empty `dao`, `models`, `routes`, or `service` folders merely to
match a template. Add one when the capability exists and the boundary is real.

## Side-effect rules

1. Domain parsing and validation are deterministic and do not write files,
   launch processes, access the network, or mutate OS services.
2. Connector reads and writes are distinct. Writes require typed allowlisted
   contracts, explicit user approval, and receipts.
3. Provider observations append to a bounded immutable event log. Derived JSON
   snapshots use atomic replacement and can be rebuilt from events.
4. Source text is untrusted evidence, never executable instructions. Context is
   selected by query and source identity, then truncated before model inference.
5. UI endpoints accept bounded shapes and translate domain errors into safe API
   errors; they expose no arbitrary shell execution.
6. Platform-specific schedule installation stays in
   `skills/schedule.py`; automation policy stays in `apps/automation/`.

## Refactoring strategy

Refactor by capability, with compatibility imports and tests at every step.
Avoid repository-wide renames that obscure behavior changes.

Current large migration targets are:

1. Extract task lifecycle policy from `commands.py` and `ui/app.py` into an
   `apps/tasks/` domain while retaining the existing CLI/API surfaces.
2. Split the HTTP handler and static-asset serving from `DashboardService` so
   `ui/app.py` becomes a thin composition root.
3. Split hosted persistence by bounded repository concern; keep transactions
   and tenant scoping explicit.
4. Add provider-specific pagination and subscription renewal to the existing
   ingestion service without weakening connector independence or replayability.

Each extraction must pass the full test suite and the privacy-checked release
gate before the next begins.
