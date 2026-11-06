# Sync architecture

## Product invariant

Carmen may use events for speed, but it may claim freshness or resolution only
after reading and validating the corresponding source state. Webhook delivery is
an invalidation signal, not proof that the dashboard is complete.

## Current local architecture

The local app has two read transports:

- first-party read-only provider API adapters for Slack, Gmail, Google Calendar,
  and GitHub when their credentials are configured;
- the bounded Codex connector bridge as a compatibility/recovery path for each
  source that has not been directly authorized.

Direct provider reads execute concurrently. Each has its own cursor, health
receipt, duration metric, append-only observations, and serialized local commit.
A completed source becomes visible immediately; failure retains the previous
snapshot and does not change another source's health. The legacy bridge remains
separately bounded because multiple `codex exec` processes can contend for the
same local runtime state.

Every successful read becomes a sparse `SourceObservation` in
`data/events/source-events.jsonl`. Payload hashes detect tampering. A
deterministic projector creates the compatible `data/inbox/<source>.json` read
model, and deterministic source rules—not the LLM—apply completion state. The
event log, cursor state, snapshots, and source health are local/private files.

## Next event-driven increment

```text
Provider event
    ↓
Public HTTPS ingress → verify signature/channel → idempotent receipt
    ↓
Queue one source/account cursor job
    ↓
Incremental provider read → sparse observation → validate → append
    ↓
Deterministically project snapshot + task transition; persist cursor
    ↓
Notify the local dashboard (SSE/WebSocket or short poll)
```

### Google Calendar

- Use OAuth and `events.watch` for each calendar.
- A push notification indicates that a watched resource changed; it does not
  contain the changed event. Enqueue an incremental `events.list` read using the
  stored `syncToken`.
- Re-read the exact event before resolving a task. RSVP accepted/declined,
  cancellation, reschedule, or passed action window can close or reprioritize it
  under the deterministic Calendar rules.
- Watch channels expire and must be renewed before expiration. Store channel ID,
  resource ID, expiration, and current sync token.

### Gmail

- Use Gmail `watch`, which publishes mailbox changes through Google Cloud
  Pub/Sub.
- Store the newest `historyId`; after a notification, call `history.list` and
  fetch affected threads before reconciliation.
- Renew the watch and run scheduled reconciliation because history ranges and
  notification delivery are bounded.

### Slack

- The hosted service already verifies Slack request signatures and deduplicates
  Events API deliveries.
- Expand the affected thread and reconcile the tracked ask. A message event by
  itself does not prove that an earlier ask was answered.

### GitHub

- Use a GitHub App webhook, verify the signature, and deduplicate by delivery ID.
- Re-read the PR or issue after relevant events so merge state, CI, review
  requests, and unresolved threads are reconciled from current source truth.

## Recovery loop

Events can be delayed, duplicated, reordered, or missed. Keep all three paths:

- event-triggered incremental sync for speed;
- user-triggered **Sync now** for immediate recovery;
- scheduled reconciliation (at least hourly for active sources, plus the daily
  5:00 AM brief) to repair missed events and renew subscriptions.

## Deployment boundary

A browser-only localhost app cannot receive provider webhooks because Google,
Slack, and GitHub require a reachable HTTPS endpoint. Real-time sync therefore
needs a small hosted ingress/worker or a user-managed public relay. The local app
can retain tasks, drafts, model inference, and training on device; the hosted
component should store only encrypted OAuth credentials, subscription/cursor
state, idempotency receipts, and the minimum normalized event data required for
reconciliation.

## Remaining implementation work

1. Replace environment-only direct authorization with UI OAuth and encrypted
   token storage for Slack, Google, and GitHub.
2. Add provider pagination/backoff and source-specific incremental APIs on top
   of the existing per-source cursors.
3. Add subscription/channel records with expiry, signed webhook/Pub/Sub ingress,
   and idempotent delivery receipts.
4. Add subscription renewal and periodic full reconciliation jobs.
5. Add a secure local update channel and display `last event`, `last sync`, and
   `last full reconciliation` separately.

Provider references:

- [Google Calendar push notifications](https://developers.google.com/workspace/calendar/api/guides/push)
- [Gmail push notifications](https://developers.google.com/gmail/api/guides/push)
- [GitHub webhook events and payloads](https://docs.github.com/en/webhooks/webhook-events-and-payloads)
- [Slack Events API](https://docs.slack.dev/apis/events-api/)
