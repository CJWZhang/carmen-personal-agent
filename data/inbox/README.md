# Work-source snapshot directory

The connected-source collector writes normalized work items here. `carmen-daily`
reads them, correlates related work across sources, and produces `TODO.md` plus
local reply drafts in `data/drafts/`.

Files (all optional — write whichever you have data for):

- `gmail.json`
- `slack.json`
- `calendar.json`
- `github.json`

Each is a JSON array of objects matching `carmen_agent.skills.schemas.InboxItem`:

```json
[
  {
    "source": "gmail",
    "id": "msg_18f...",
    "sender": "alice@example.com",
    "subject": "Q3 review deck",
    "body": "Can you look at slide 12 before Thursday?",
    "url": "https://mail.google.com/mail/u/0/#inbox/18f...",
    "ts": "2026-07-22T14:03:00Z",
    "channel": "",
    "thread_id": "thread_18f...",
    "kind": "email",
    "status": "open",
    "participants": ["Alice"],
    "related_keys": ["q3-review"],
    "metadata": {}
  }
]
```

Field notes:

- `source`: one of `gmail`, `slack`, `calendar`, `github`.
- `id`: stable per-source id. Used as the dedup key and to name draft files.
- `channel`: Slack channel name/id, or Calendar organizer. Blank for Gmail.
- `thread_id`: optional; helps keep replies threaded.
- `kind`: optional source-native type such as `email`, `message`, `event`,
  `pull_request`, or `issue`.
- `status`: `open`, `waiting`, or `done`. Only use `done` when the source
  contains explicit completion evidence.
- `participants`: people directly connected to the item.
- `related_keys`: stable project/task identifiers shared by items from different
  sources. These are the safest way to merge a Slack thread, meeting, email, and
  PR into one task.
- `metadata`: optional source metadata. For GitHub, `repository` and `number`
  improve PR/issue correlation.

Successful snapshot writes also append a sparse immutable observation to
`data/events/source-events.jsonl`. The source-specific event contract is
documented by `configs/source-observation.schema.json`; it omits inapplicable
metadata instead of filling a universal schema with placeholders. Snapshots are
derived read models and may be rebuilt from that local event history.

This directory and the event history are gitignored. Direct connector reads do
not send source content to the reasoning model; the separate compatibility
bridge has its own documented service boundary.
