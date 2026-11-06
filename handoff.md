# Carmen Personal Agent — Project Handoff

Last updated: 2026-08-14  
Project version: `0.1.0` (alpha)  
Current verification: `204 passed`; frozen precision/recall/type accuracy `1.0`  
Primary product docs: `README.md`, `docs/ARCHITECTURE.md`, `docs/SYNC_ARCHITECTURE.md`

This is the authoritative continuation document. It records the product intent,
decisions, implemented architecture, privacy boundaries, known gaps, and exact
steps for another developer or computer to continue the project safely.

## 1. What this product is

Carmen is a local-first personal work assistant. It turns activity from Slack,
Gmail, Google Calendar, and GitHub into an evidence-backed daily plan and helps
the user move each open loop to a verified resolution.

The local product is single-owner per OS user / installation. Identity, source
authorization, tasks, style corpus, profile, and adapter belong to that owner;
there is no shared fine-tuned model across open-source installations. The hosted
foundation is separately tenant-scoped and must not reuse local owner state.

The intended experience is:

1. Refresh work sources automatically at 5:00 AM local time and whenever the
   user presses **Sync**.
2. Find asks, commitments, messages needing replies, meeting preparation,
   decisions, follow-ups, and GitHub work.
3. Correlate related records and preserve enough source context to explain why
   each task exists.
4. Rank the best next actions using urgency, ownership, source freshness, and
   learned user preferences.
5. Draft a response or help plan the work using only relevant, bounded context.
6. Let the user edit, start, wait on, dismiss, complete, or act on the task.
7. Re-read the source before claiming that externally completed work is resolved.

The primary product object is an **open loop**, not a chat message. An open loop
has stable source IDs, evidence, freshness, lifecycle state, ranking reasons,
and resolution proof.

## 2. Why the product exists

The user has work distributed across communication, calendar, and engineering
systems. Existing task trackers such as Linear are strong once someone creates
an issue, but they do not reliably discover every implicit ask or commitment in
Slack, email, meetings, and pull requests. Carmen fills that gap.

The differentiator is not merely “AI-generated TODOs.” It is:

- discovery across work systems;
- evidence-backed context;
- personalized prioritization;
- writing assistance in the user's voice;
- source-aware resolution and reconciliation;
- local/private operation where practical.

### Positioning and competitive decision

The chosen wedge is **the private personal attention layer for product and
engineering leaders**, not a generic assistant, calendar optimizer, email
client, meeting notetaker, or shared project tracker.

Competitors validate each adjacent job:

- Lindy is the closest broad executive-assistant competitor and is ahead on
  connector onboarding and proactive workflows.
- Motion and Reclaim are stronger at automatic calendar planning once tasks are
  known.
- Sunsama and Akiflow are stronger at guided planning and polished task-inbox
  interactions.
- Linear is the team system of record for structured product work and agents.
- Superhuman, Gemini, and Microsoft Copilot are stronger inside their native
  email/productivity ecosystems.
- Granola is stronger at zero-friction meeting capture and meeting memory.

Carmen's intended distinction is the complete open-loop lifecycle:

```text
Discover → Verify → Prioritize → Understand → Act → Confirm resolution
```

The product begins before a formal task exists, correlates evidence across work
systems, drafts in the owner's voice, and remains responsible for the loop until
source truth proves it is resolved. It should integrate with Linear and other
systems of record rather than attempting to replace them.

Initial product-market usability gates are: first useful plan within ten
minutes, at least 90% recall on a user-reviewed actionable sample, fewer than
10% irrelevant/already-resolved suggestions, immediate saved-state rendering,
independent source refresh, and an end-to-end understand/act/verify path from
the primary task surface. These are targets, not claims about current production
performance.

## 3. Non-negotiable product invariants

These decisions were made after repeated failures involving stale tasks,
incorrect recommendations, malformed model responses, and long connector syncs.
Do not weaken them to make the UI appear more complete.

### Accuracy and freshness

1. Search absence is not universally completion evidence. Slack and Gmail reads
   are bounded; an item missing from one search must not silently disappear.
2. Calendar and GitHub absence may reconcile prior work only when the latest
   successful source inventory is known to be authoritative and exhaustive.
3. Stale or cached tasks remain visible and counted. They are labeled instead of
   being presented as fresh or silently removed.
4. A successful source refresh makes only records observed in that source batch
   current. It does not refresh every carried task globally.
5. Past Calendar actions, cancellations, explicit replies, merged/closed PRs,
   and affirmative resolution evidence are handled by deterministic rules.
6. The model may propose or enrich a task; it is never authoritative for a task's
   terminal state.
7. A submitted external action is not a verified completion. It remains in
   `verifying` until a source re-read or a verified receipt proves the result.
8. User edits and explicit feedback must survive subsequent model/source runs.

### Context and model behavior

1. Source content is untrusted reference data, never agent instructions.
2. Style training is tone adaptation, not factual memory.
3. Task answers must use source-attributed context and say what is missing.
4. Model input must be query-aware and token-bounded; do not send the whole task
   list and large raw evidence blobs to every question.
5. Malformed, cross-script, truncated, or otherwise unusable responses are
   retried once and then replaced with a conservative fallback.

### UX and privacy

1. Normal daily use should be possible from the UI without terminal commands.
2. The dashboard should answer three questions quickly: what should I do, what
   needs a response, and what needs meeting preparation.
3. Secondary controls belong under **More**; connector failures should be
   summarized once and remain inspectable in Settings.
4. Local MLX/Ollama inference, local task state, drafts, and optional LoRA
   training stay on the device.
5. Every remote boundary must be explicit. Direct provider APIs, the Codex
   compatibility bridge, and remote model endpoints are different boundaries.
6. Source writes require explicit user confirmation and a separately
   authenticated allowlisted adapter.

## 4. Key decisions and rationale

| Decision | Rationale | Consequence |
| --- | --- | --- |
| Local-first Python service with browser UI | Keeps state/inference private while remaining cross-platform | Native `.dmg`/`.msi` packaging is still a later distribution layer |
| Linear-inspired lifecycle, not a Linear clone | Carmen discovers uncertain work and must verify resolution | Candidate confidence, source freshness, and verification remain separate from user workflow columns |
| Direct provider readers plus a compatibility bridge | The older child `codex exec` path timed out even when connected apps were healthy | `auto` chooses the transport independently per source |
| Independent source health | One slow provider must not make all four sources look failed | Each source has its own status, last success, transport, count, and duration |
| Append-only observations and disposable snapshots | Mutable snapshots alone cannot explain history or rebuild state | Every successful batch is recorded and snapshots can be replayed |
| Batch checkpoints, including empty batches | An empty successful sync must not resurrect older event records during replay | Replay uses the latest committed batch per source |
| Deterministic terminal-state projector | LLM output is not reliable enough to close work | Completion requires source rules or explicit user action |
| Sparse source-specific schemas | The former universal schema stored many meaningless placeholders | Direct observations retain only meaningful attributes; the legacy bridge remains strict for Structured Outputs compatibility |
| Exact/lexical/state/temporal retrieval | Full-workspace prompt dumps were slow and produced irrelevant answers | Chat receives a small query-specific packet; embeddings are not yet required |
| Inspectable personalized ranking | Personalization should affect priority without becoming an opaque fact claim | Scores include reasons and preferences can be corrected by the user |
| Aggregate-only telemetry | Accuracy and latency need measurement without retaining more private text | Nested payloads, message content, and stable record IDs are rejected |
| Base Qwen for reasoning; LoRA for voice only | The narrow Slack LoRA degraded general instruction following | Dashboard reasoning uses the clean base model plus style profile |
| One-click, staged voice onboarding | Raw corpus preparation and shell training were not viable end-user setup | UI imports exact-user-authored Gmail Sent and/or Slack writing, sanitizes it, activates a profile, and offers LoRA only where supported |

## 5. Current user-facing lifecycle

The intended visible workflow is:

```text
Inbox / Triage → Todo → In Progress → Waiting → Done
                         ↘
                        Canceled
```

Internal persisted lifecycle values are:

- `candidate`
- `open`
- `in_progress`
- `waiting`
- `verifying`
- `done`
- `dismissed`
- `superseded`

Resolution mode is separate: blank, `local`, `source_verified`, `dismissed`,
`expired`, or `superseded`. `verifying` is intentionally not a user-facing
completion state; it means an external action was attempted but source truth has
not yet confirmed the outcome.

## 6. Implemented architecture

```text
Slack / Gmail / Calendar / GitHub
                │
       ┌────────┴─────────┐
       ▼                  ▼
 direct provider APIs   Codex compatibility bridge
       └────────┬─────────┘
                ▼
     independent source coordinator
        │ cursor │ health │ latency
                ▼
 sparse validated SourceObservation
                ▼
 append-only JSONL event store + batch checkpoint
                ▼
 deterministic source snapshot projector
                ▼
 normalized source snapshots
        ┌───────┴─────────┐
        ▼                 ▼
 deterministic rules   local model triage/drafting
        └───────┬─────────┘
                ▼
 stable task synthesis + personalized ranking
                ▼
 local UI / TODO.md / reminders / contextual chat
                │
                ▼ explicit confirmation
 typed action adapter → receipt → source re-verification
```

### Source collection

`CARMEN_CONNECTOR_MODE` controls collection:

- `auto` — recommended. Use a direct adapter for every source with direct
  credentials; fall back to the Codex bridge for only the remaining sources.
- `direct` — use only provider APIs. Missing authorization fails immediately and
  preserves the last snapshot.
- `codex` — explicitly use the compatibility bridge.

Direct adapters exist for:

- Slack via `CARMEN_SLACK_USER_TOKEN`;
- Gmail and Calendar via Google access token or OAuth client/refresh-token
  environment values;
- GitHub via `CARMEN_GITHUB_TOKEN` and `CARMEN_GITHUB_USERNAME`.

Direct readers execute concurrently. Network calls happen in parallel; local
event/snapshot/status commits are serialized so one worker cannot overwrite
another source's health. The event store also uses a short cross-process lock so
an observation batch and its checkpoint stay contiguous.

The direct readers are pull-based and bounded. UI-managed OAuth, comprehensive
pagination/backoff, Google watch renewal, and provider webhooks are not finished.
Environment credentials are an implementation bridge, not the desired end-user
connection experience.

### Event history and deterministic projection

Every successful source batch appends:

- zero or more immutable, payload-hashed `SourceObservation` records;
- one source/batch checkpoint, including when the result is empty.

The current snapshot is derived from the latest checkpointed batch. This avoids
two replay bugs:

1. an empty new batch resurrecting records from an old batch;
2. an unchanged record being deduplicated away and therefore disappearing from
   a newer batch.

Snapshots under `data/inbox/` remain the backwards-compatible read model. They
can be rebuilt with:

```bash
uv run carmen collect --rebuild-from-events
```

### Task creation and resolution

The local model performs bounded triage and drafting. Stable synthesis combines
model output with deterministic source records and preserves user changes.
`TaskProjector.apply_source_truth` applies only source-verifiable terminal
states before lifecycle reconciliation.

Slack/Gmail nonappearance is not resolution. Calendar expiry/cancellation and
GitHub merged/closed state can resolve tasks when the source record is
authoritative. Explicit user actions can also dismiss or locally complete work.

### Context and retrieval

Chat now routes questions into `draft`, `focus`, `blocked`, `plan`, `status`,
`task`, or `workspace` intent. Retrieval combines:

- exact selected task/source identity;
- lexical query overlap with small source-domain expansions;
- workflow state such as waiting or reply-needed;
- task/source freshness;
- personalized priority adjustment;
- temporal recency and due dates.

The result is rendered into a context packet with an explicit contract,
relevant tasks, source evidence, and freshness limitation. The default packet
budget is approximately 3,000 tokens using a conservative tokenizer-independent
estimate. This is deterministic hybrid retrieval; semantic embeddings/vector
search are not implemented yet.

### Personalized ranking

Ranking combines confidence, urgency, reply requirement, lifecycle, due time,
source freshness, and `priority_adjustment`. The UI receives both a score and
human-readable reasons.

Calendar personalization supports explicit **usually skip** / **usually attend**
feedback and bounded recurring-series history. Accepted RSVP is not treated as
attendance. Attendance-based personalization is used only when the source
provides explicit attendance records.

### Continuous quality and latency evaluation

Runtime telemetry records aggregate scalars only:

- per-source duration, result status, and item count;
- chat duration, routed intent, estimated input tokens, output length, fallback,
  and truncation;
- frozen evaluation precision, recall, type accuracy, case count, and status.

Telemetry rejects nested objects and unexpected fields so source content and
record identifiers cannot be hidden inside it. The dashboard API exposes a
summary. Frozen extraction accuracy runs in CI on every push and pull request.

Production accuracy is not fully proven: the committed evaluation set is small
and privacy-safe. A manually reviewed private dataset is still required to
measure real recall, false reminders, ranking quality, and draft acceptance.

## 7. Important code map

| Area | Location | Responsibility |
| --- | --- | --- |
| Unified CLI | `src/carmen_agent/assistant.py` | `carmen` command routing |
| Source orchestration | `src/carmen_agent/collector.py` | mode selection, bridge fallback, validation, snapshot/replay CLI |
| Direct connectors | `src/carmen_agent/connectors/direct/` | Slack, Google, and GitHub read APIs |
| Independent sync | `src/carmen_agent/ingestion/sync.py` | concurrency, isolated health, cursor and commit coordination |
| Cursor storage | `src/carmen_agent/ingestion/cursors.py` | per-source incremental cursor state |
| Sparse events | `src/carmen_agent/domain/events.py` | typed observation, hash, checkpoint, source-specific attributes |
| Event storage | `src/carmen_agent/storage/event_store.py` | bounded idempotent append-only JSONL and cross-process locking |
| Projection | `src/carmen_agent/projections/tasks.py` | latest-batch snapshots and source-verifiable task transitions |
| Source rules | `src/carmen_agent/source_rules.py`, `connectors/*.py` | deterministic completion semantics |
| Schemas | `src/carmen_agent/skills/schemas.py`, `configs/*.schema.json` | bounded runtime and persisted contracts |
| Triage/drafting | `src/carmen_agent/skills/triage.py`, `draft.py` | local model-assisted task extraction and replies |
| Synthesis | `src/carmen_agent/skills/synthesis.py` | correlation, carryover, user-edit preservation |
| Context router/retrieval | `src/carmen_agent/context/` | intent, hybrid retrieval, packet budgeting |
| Evidence joins | `src/carmen_agent/infra/context.py` | exact ID/key/link provenance bundles |
| Ranking | `src/carmen_agent/ranking/personalized.py` | inspectable personalized task score |
| Telemetry | `src/carmen_agent/observability/telemetry.py` | aggregate-only operational metrics |
| Local UI service | `src/carmen_agent/ui/app.py` | loopback API, refresh, task and chat boundary |
| UI assets | `src/carmen_agent/ui/assets/` | dashboard, task detail, settings, assistant drawer |
| Task commands | `src/carmen_agent/commands.py` | local mutations and execution boundary |
| Connector actions | `src/carmen_agent/actions/contracts.py` | allowlisted external mutation contracts |
| Action receipts | `src/carmen_agent/actions/receipts.py` | bounded external-action history |
| Automation | `src/carmen_agent/apps/automation/`, `scheduler.py` | saved schedules, catch-up, reminder checks |
| Model providers | `src/carmen_agent/providers/`, `skills/model.py` | MLX, Ollama, OpenAI-compatible inference |
| Voice preparation | `src/carmen_agent/prepare.py` | authored-only sanitation and style profile |
| Voice onboarding | `src/carmen_agent/personalization.py` | authored Gmail Sent/Slack import, readiness, profile/LoRA activation |
| Voice import schema | `configs/style-corpus.schema.json` | strict bounded compatibility-bridge output |
| Evaluations | `src/carmen_agent/evals/`, `evals/cases/` | frozen and world-snapshot evaluation |
| Hosted foundation | `src/carmen_agent/hosted/` | Slack OAuth/events/worker/Postgres path |

Architecture direction and future refactoring rules are in
`docs/ARCHITECTURE.md`. Sync/webhook details are in `docs/SYNC_ARCHITECTURE.md`.

## 8. UI behavior and current task flow

The local app is a loopback-only browser UI. It currently supports:

- a calm ranked daily plan;
- task buckets for focus, replies, meetings, waiting, review, and completed;
- source freshness and connection details;
- editing title, reason, urgency, due date, and reminders;
- start, wait, reopen, dismiss, already handled, and complete actions;
- usually-skip/usually-attend meeting feedback;
- source links and bounded evidence context;
- contextual Ask Carmen drawer with safe Markdown rendering;
- explicit connector action review when an action adapter is configured;
- automation presets and schedule repair;
- guided **Your voice** onboarding, local fine-tuning, and evaluation controls.

First-time users see a compact personalization banner. **Settings → Your voice**
shows three explicit stages: authored writing, sanitized voice profile, and
optional local fine-tuning. The main button imports only the authenticated
user's Gmail Sent bodies and/or Slack messages, builds the cross-platform profile, and exposes LoRA only
when the device supports Apple Silicon MLX. A later update re-imports recent
writing without deleting the last successful profile if collection fails.
An existing matching adapter can be activated without retraining; activation is
a local configuration change and resets the cached draft session.

Pressing **Sync** launches collection in a background thread. When collection
finishes, the UI deterministically imports actionable normalized snapshots so a
malformed model response cannot make freshly collected tasks disappear. A
nonzero collector result marks refresh degraded but still imports successful
sources and preserves the others' last good snapshots.

The UI is intentionally not a dense kanban board yet. The current product
decision is a short ranked plan with secondary actions under **More**. A fuller
Inbox/Todo/In Progress/Waiting/Done view remains a future option.

## 9. Model and voice behavior

### Reasoning model

- Default MLX model: `mlx-community/Qwen3-4B-Instruct-2507-4bit`.
- Recommended cross-platform local runtime: Ollama with `qwen3:4b`.
- OpenAI-compatible endpoints are supported; a remote endpoint receives the
  supplied prompt and changes the privacy boundary.
- Loopback Ollama requests disable Qwen thinking for bounded structured-output
  turns so the model spends its output budget on the validated final response.

The dashboard intentionally runs the clean base model for reasoning. It uses the
style profile for tone. When `voice_mode` is `lora` and a valid adapter exists,
the UI reloads the session with LoRA only for a grounded message-drafting turn;
the next general planning turn reloads the clean base model. Training completion
resets the cached session so the newly active voice is used immediately.

### LoRA voice fine-tuning

`run-train.sh` performs local MLX LoRA fine-tuning. Base weights remain frozen;
small low-rank adapter weights are learned. The configured full run is 600
iterations. `./run-train.sh --iters 20` is the smoke test.

The normal product path no longer depends on that shell script. The packaged UI
runs `carmen_agent.personalization`, which imports writing, calls the sanitizer,
uses the packaged `configs/lora.yaml`, activates `voice_mode`, and resets the
draft model session. The shell script remains a source-checkout developer helper.

The preparation pipeline keeps only the configured user's authored sent-email
bodies or messages, removes quoted email history, signatures, sensitive identifiers,
and unsuitable examples, deduplicates content, and preserves only observed
emoji/custom Slack aliases. Email and chat remain distinct training registers. Training and prepared
data remain local unless the user explicitly enables network access for a base
model download.

Recommended retraining cadence is monthly or after several hundred reviewed,
representative new messages—not every day.

## 10. Privacy and data boundaries

| Operation | Where data goes |
| --- | --- |
| Direct Slack/Gmail/Calendar/GitHub read | From the device to that provider's API; not to the reasoning model during collection |
| Codex compatibility bridge | Through the authenticated Codex connector/service boundary |
| Direct voice import | Authenticated device → Slack API; only exact-user-authored messages are retained |
| Bridge voice import | Through authenticated Gmail/Slack Codex connector boundaries; only exact-user-authored Sent bodies/messages pass a strict read-only bounded schema |
| Local MLX or Ollama inference | Device-local endpoint |
| Remote OpenAI-compatible inference | Configured remote model endpoint receives the assembled prompt/context |
| LoRA preparation/training | Local files and local MLX runtime, except an explicitly permitted model download |
| Typed external source action | User-confirmed authenticated adapter, then provider API |
| Runtime telemetry | Local aggregate-only JSONL; no source text or stable record IDs |

Secrets must stay in environment variables or an operating-system secret store.
Carmen does not silently load `.env`; it must be explicitly sourced. Never put
provider tokens, model API keys, or encryption keys in `config.json`, training
data, task files, source archives, or prompts.

## 11. Persisted state and transfer policy

### Safe public/source package

The privacy-checked source archive contains only allowlisted source material:

- `src/`, `tests/`, `configs/`, `docs/`, `scripts/`;
- `.github/`, `deploy/`, `migrations/`, `evals/cases/`;
- public project metadata, licenses, shell helpers, and documentation;
- `data/inbox/README.md`, but no live snapshots.

### Private state: transfer only in a separate encrypted bundle

- `config.json` — non-secret preferences and identity/model choices;
- `data/inbox/*.json` — live normalized source snapshots and tasks;
- `data/events/source-events.jsonl` — replayable source history;
- `data/drafts/` — generated replies;
- `data/raw/` and `data/processed/` — writing corpus, style profile, MLX data;
- `adapters/carmen-qwen3-4b/` — learned LoRA weights;
- `work/daily-chief-of-staff-state.json` — carried lifecycle state;
- `work/habits.json` — learned dismissal patterns;
- `work/calendar-preferences.json` — meeting preferences;
- `work/reminder-deliveries.json` — reminder delivery state;
- `work/action-receipts.json` — external action history;
- `work/source-cursors.json` — direct provider cursor positions;
- `work/quality-telemetry.jsonl` — optional aggregate operational history;
- `TODO.md` — optional generated brief.

It is safe to omit telemetry. It is safe to omit the event log when starting
fresh, but replay history will be lost. Cursor state should be copied only with
the matching snapshots/event history; otherwise reauthorize and begin with a
full provider read.

### Do not transfer

- `.venv/`, Python caches, build outputs, logs, or `work/uv-cache/`;
- `.env`, shell history, provider tokens, session cookies, or credential stores;
- launchd/systemd/Windows scheduled-task exports;
- machine certificates, private keys, or `work/koi-root-ca.pem`;
- absolute action-adapter paths from the old device;
- provider/model login caches unless a deliberate licensed offline transfer is
  required.

Connectors must be reauthenticated and schedules reinstalled on the destination.

## 12. Packaging status and developer transfer

The verified shareable artifacts are:

- `dist/carmen-personal-agent-0.1.0-source.zip`
- `dist/carmen-personal-agent-0.1.0-source.zip.sha256`

The current source archive contains 160 public files and passed
`scripts/verify-release.py`. The builder rejects common secret formats, absolute
workstation paths, private keys, unsafe symlinks, and non-allowlisted private
state.

The Python wheel was not rebuilt in the final sandbox pass: the existing virtual
environment did not include the `build` module, and the sandbox-provided `uv`
binary failed in its macOS system-configuration layer. CI is configured to build
the wheel and source distribution in a normal runner. A developer should rerun
the normal package build before publishing a release:

```bash
uv sync --extra dev
uv run python -m pip install build
uv run python -m build
```

The workspace did not expose usable version-control history during this handoff.
Do not assume an unseen Git history is safe to publish. Start the public Git
repository from the verified source archive or perform a deliberate history and
secret audit first.

## 13. Destination installation runbook

### Cross-platform contributor setup

1. Transfer the source zip and checksum.
2. Verify the checksum and unzip the archive.
3. Install `uv` and Python 3.11 or 3.12.
4. From the extracted root:

   ```bash
   uv sync --extra dev
   uv run pytest -q
   uv run carmen eval
   uv run carmen init --name "Your Name" --provider ollama
   uv run carmen doctor
   uv run carmen ui
   ```

5. Install Ollama separately and pull `qwen3:4b` if using local cross-platform
   inference.
6. Configure direct provider credentials or authenticate the Codex compatibility
   connectors, depending on the intended collection mode.
7. Open **Settings → Connections**, run **Sync**, and verify each source's
   transport, status, last success, duration, and count.
8. Open **Settings → Your voice** and select **Personalize my agent**. Confirm
   the imported and usable message counts before optionally selecting local LoRA.
9. Save/repair automation. Defaults are 5:00 AM local refresh and a reminder
   check every 30 minutes.

### Apple Silicon MLX setup

```bash
uv sync --extra dev --extra mlx
uv run carmen init --name "Carmen" --provider mlx \
  --model mlx-community/Qwen3-4B-Instruct-2507-4bit
uv run carmen doctor
uv run carmen ui
```

Restore the LoRA adapter only with the matching base-model family and MLX
configuration. MLX adapter inference is not directly portable to Intel Mac,
Linux, Windows, or Ollama.

### Restoring private continuity state

1. Install and initialize first so the destination data root exists.
2. Stop the UI and scheduler.
3. Decrypt and copy only the intended private state.
4. Update non-secret provider/model paths in `config.json`.
5. Recreate secrets in the destination environment/secret manager.
6. Reauthenticate every connector.
7. Reinstall the schedule.
8. Run `carmen doctor`, start the UI, and sync.
9. Confirm stale records remain labeled until observed on the new device.

Default installed state locations:

| OS | Data location |
| --- | --- |
| macOS | `~/Library/Application Support/CarmenAgent` |
| Linux | `$XDG_DATA_HOME/carmen-agent` or `~/.local/share/carmen-agent` |
| Windows | `%LOCALAPPDATA%\CarmenAgent` |

A source checkout stores state in the checkout for compatibility. `CARMEN_HOME`
overrides the default and is recommended for an explicit isolated data root.

## 14. Essential commands

```bash
uv run carmen doctor                          # device/config/connector health
uv run carmen ui                              # local dashboard
uv run carmen today                           # collect, triage, draft, reconcile
uv run carmen collect                         # refresh snapshots only
uv run carmen collect --mode direct           # direct provider APIs only
uv run carmen collect --mode codex            # compatibility bridge only
uv run carmen collect --sources slack         # one source only
uv run carmen collect --rebuild-from-events   # rebuild snapshot read models
uv run carmen tasks                           # list open work
uv run carmen eval                            # frozen quality gate
uv run pytest -q                              # implementation suite
uv run carmen schedule --install              # install/repair automation
uv run carmen-prepare                         # sanitize voice corpus
./run-train.sh --iters 20                     # MLX smoke training
./run-train.sh                                # 600-iteration LoRA training
python scripts/build-release-archive.py --check-only
python scripts/build-release-archive.py
python scripts/verify-release.py dist/carmen-personal-agent-0.1.0-source.zip
```

Inside terminal chat, shell commands do not execute. Chat commands begin with
`/`; typing `carmen-daily` as plain chat sends those words to Qwen.

## 15. Verification completed at handoff

Verified on macOS Apple Silicon on 2026-08-13:

- Python source and tests compile;
- 204 tests pass;
- frozen extraction evaluation reports precision `1.0`, recall `1.0`, and type
  accuracy `1.0` over six committed privacy-safe cases;
- JavaScript parses with `node --check`;
- JSON schemas parse;
- the release privacy check passes for 160 public files;
- the source archive and its manifest/checksum verify;
- event tests cover idempotent append, payload tamper detection, latest-batch
  projection, empty-batch replay, and unchanged records across batches;
- task snapshot tests cover atomic replacement while dashboard refresh runs in
  the background;
- connector tests cover one direct source succeeding while another fails;
- context tests cover draft routing, relevance filtering, and token bounds;
- ranking and aggregate-only telemetry have focused regression tests;
- voice onboarding tests cover exact authenticated authorship, strict corpus
  shapes, Gmail Sent and Slack writing registers, sanitation, profile activation,
  packaged training invocation, and draft-only LoRA session selection.

Earlier portability verification also covered all installed `carmen*` help
entry points, isolated initialization and task flows, scheduler previews for
launchd/systemd/Windows Task Scheduler, and a one-shot scheduler run.

Not verified with live provider credentials in the final architecture pass:

- real Slack/Gmail/Calendar/GitHub direct API reads;
- provider pagination/rate-limit behavior in a high-volume workspace;
- source writes through real authenticated action adapters;
- a newly bound localhost UI listener inside the restricted development sandbox;
- a real MLX fine-tuning run inside the headless sandbox, which has no Metal device;
- signed native desktop installers.

The prior connected-app health check reached all four connected apps quickly,
while child `codex exec` collection timed out. That evidence motivated direct
readers, but it is not equivalent to a live credential test of the new direct
adapter implementations.

## 16. Known limitations and prioritized next work

### P0 — connection and sync correctness

1. Build UI-managed OAuth and encrypted token storage for Slack, Google, and
   GitHub; remove environment setup from normal end-user onboarding.
2. Add provider-specific pagination, retries/backoff, quota reporting, and
   incremental cursor tests. The current direct readers are deliberately bounded
   and may not cover every high-volume workspace record.
3. Add Calendar/Gmail watch renewal and GitHub/Slack webhook invalidation through
   a small reachable HTTPS service. Treat notifications as invalidation only;
   always re-read current source truth.
4. Add scheduled full reconciliation in addition to incremental/event reads.
5. Add per-source observation timestamps to multi-source tasks.
6. Build a manually reviewed private recall dataset and measure missed tasks,
   false tasks, false reminders, and resolution latency.

### P0 — completing work in source

1. Implement real authenticated Slack reply, Gmail reply, Calendar RSVP, and
   GitHub comment/review/merge adapters.
2. Use provider-understood idempotency keys and source-side re-reads.
3. Keep sending disabled until the user approves the exact typed payload.

### P1 — context and ranking quality

1. Add source-specific thread expansion budgets and visible coverage diagnostics.
2. Add conflict-resolution UI when source records disagree.
3. Add a cited task timeline pointing to native records.
4. Evaluate whether semantic embeddings improve retrieval over the current
   deterministic exact/lexical/temporal hybrid without weakening privacy.
5. Add feedback-derived ranking, resolution, and draft-quality evaluation cases.

### P1 — lifecycle UI

1. Decide whether to add an explicit Inbox/Todo/In Progress/Waiting/Done view or
   retain the current ranked-plan home as the primary surface.
2. Add bulk triage, keyboard status changes, undo, activity history, and an
   explicit “who owns the next move” field.
3. Keep source freshness and verification as metadata, not cosmetic task columns.

### P2 — distribution and architecture cleanup

1. Package signed `.dmg`, `.msi`, and Linux desktop artifacts, likely using a
   thin Tauri shell over the existing Python service.
2. Run native release smoke tests on macOS, Linux, and Windows.
3. Extract task lifecycle policy from `commands.py` and `ui/app.py` into an
   `apps/tasks/` domain while retaining stable CLI/API contracts.
4. Split HTTP routing/static serving from `DashboardService`.
5. Split hosted persistence by bounded repository concern while preserving
   transactions, encryption, and tenant scoping.

## 17. Safe first steps for the next developer

1. Read this file, `README.md`, `docs/ARCHITECTURE.md`, and
   `docs/SYNC_ARCHITECTURE.md` before changing sync or task lifecycle behavior.
2. Run the complete verification commands before modifying code.
3. Use synthetic fixtures unless explicitly authorized to access private state.
4. Do not publish the current working directory without the release allowlist
   and secret/history review.
5. For connector work, test one success plus one simultaneous failure; preserve
   last-good snapshots and independent health.
6. For event work, test empty batches, unchanged re-observations, replay order,
   duplicate IDs, and process interruption.
7. For model/context work, assert token bounds and grounding behavior. Do not
   make the model responsible for source truth or task completion.
8. For schema migrations, keep old snapshots readable or provide an explicit
   replay/migration path.
9. Update this handoff whenever product decisions, verification counts, private
   state locations, connector boundaries, or release artifacts change.

## 18. Definition of success

The product succeeds when the user can open Carmen and trust that:

1. important asks and commitments are not silently missing;
2. every recommendation explains its evidence and ranking;
3. stale information is obvious without erasing open work;
4. Ask Carmen uses relevant context and produces coherent grounded output;
5. drafts sound like the user without inventing facts or emoji aliases;
6. completing work is smooth, deliberate, and verifiable in its source;
7. automatic and manual syncs are fast, independent, and recoverable;
8. routine use requires no terminal;
9. private work data and voice training remain under the user's control.
