# Changelog

All notable changes will be documented here. This project uses Semantic
Versioning and keeps unreleased work at the top.

## [Unreleased]

- Documented the product wedge, competitor landscape, first target user,
  differentiation, and measurable usability gates for packaging and handoff.
- Added guided **Your voice** onboarding in the local UI: exact-user-authored
  Gmail Sent and Slack import, strict corpus validation, local sanitation, readiness/progress,
  cross-platform profile activation, and optional packaged Apple Silicon LoRA.
- Added email as a distinct voice-training register while excluding quoted
  history, forwarded blocks, signatures, other authors, and automated mail.
- Activated trained LoRA adapters only for grounded message drafts while keeping
  task planning and general reasoning on the clean instruction model.
- Added first-party read-only Slack, Gmail, Google Calendar, and GitHub API
  adapters with per-source cursors, parallel reads, independent health, latency
  receipts, and compatibility fallback for sources not yet directly authorized.
- Added a private append-only source event log plus deterministic source and task
  projectors; snapshots remain disposable read models and model output never
  decides terminal task state.
- Added sparse source-specific observation contracts with payload integrity
  hashes instead of persisting universal placeholder-heavy connector metadata.
- Added query routing, exact/lexical/temporal retrieval, and bounded context
  packets so local chat receives relevant evidence under an explicit token budget.
- Added inspectable personalized ranking and aggregate-only sync/chat telemetry,
  with frozen precision/recall gates now running continuously in CI.
- Simplified the dashboard around three decision signals, one ranked next
  action, compact context, and a **More** menu for secondary task controls.
- Fixed the task-detail **More** menu sizing and anchoring so it no longer
  stretches outside the panel or obscures its own labels.
- Consolidated stale connector messaging into one workspace sync state while
  retaining cached tasks, per-source receipts, and last-success timestamps.
- Changed UI refreshes to progressive, separately bounded source reads with
  immediate per-source commits, removing the combined session's all-or-nothing
  timeout without reintroducing concurrent local-runtime contention.
- Increased the per-source UI refresh ceiling to two minutes after validating
  the installed Gmail bridge, while retaining independent progressive commits.
- Prevented connector collection from inheriting expensive interactive
  `high`/`xhigh` reasoning settings to reduce refresh latency.
- Disabled Ollama/Qwen thinking on bounded loopback structured-output turns so
  the final response cannot be starved by hidden reasoning tokens.
- Made task snapshot publication atomic so background refresh cannot expose a
  truncated JSON file to concurrent dashboard reads.
- Distinguished connector timeouts from missing connections in Settings and
  replaced misleading installation guidance with preserved-snapshot recovery copy.
- Added customizable daily-refresh and reminder automation presets in the local
  UI, with persisted cross-platform scheduler repair.
- Introduced a domain-oriented `apps/` layer, extracted automation policy from
  the UI transport, added mirrored domain tests, and documented dependency and
  side-effect boundaries.

## [0.1.0] - 2026-08-12

- Added the local personal-assistant pipeline, task brief, reminders, drafts, and
  terminal chat.
- Added a loopback-only local web UI and installable PWA assets.
- Added MLX, Ollama, and OpenAI-compatible model backends.
- Added source collection, hosted Slack plumbing, cross-platform scheduling, and
  privacy-safe regression evaluations.
- Added portable source/wheel builds and sanitized release archives.
