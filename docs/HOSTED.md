# Hosted Carmen Assistant

The hosted runtime keeps running when a laptop is closed. It is deliberately
separate from the cross-platform desktop runtime:

```text
Slack OAuth + signed events
            |
            v
      WSGI web service
            |
            v
         Postgres
         /              \
  durable queue worker   hourly schedulers
         |
         v
  configured model provider
         |
         v
   private Slack DM
```

The web service never imports MLX. Linux deployments use an explicitly
configured model provider, while the existing `carmen chat` and
`carmen today` commands use Ollama or another OpenAI-compatible endpoint on any
desktop OS, with optional MLX acceleration on Apple Silicon.

## What the hosted runtime does

- Installs into multiple Slack workspaces with OAuth v2.
- Stores each workspace bot token and each user's token encrypted at rest.
- Verifies every Slack event and slash command with the signing secret.
- Persists only explicitly connected users' messages in the writing-style corpus.
- Extracts direct incoming asks and explicit user commitments into an
  evidence-backed action ledger.
- Uses retrieved authored examples to draft in that user's style.
- Queues `/draft` requests and sends suggestions privately by DM.
- Produces one timezone-aware daily action DM per user and date.
- Reconciles open Slack threads and deduplicates time-sensitive reminders.
- Records versioned runs, retryable steps, and redacted policy-aware tool calls.
- Reclaims abandoned work after a lease timeout and retries ordinary failures
  up to three total attempts with stable idempotency keys.
- Never sends a drafted reply as the user.

Current limitation: collection is event-driven from installation onward; there
is no historical Slack backfill. Direct hosted Gmail, Calendar, and GitHub
collectors are also future work. The local `carmen today` workflow already
supports normalized snapshots from those sources.

## Privacy boundary

The hosted mode changes the privacy boundary:

- OAuth tokens and authored Slack messages are stored in the hosted database.
- Tokens are encrypted with `CARMEN_TOKEN_ENCRYPTION_KEY`.
- Coworker messages are transient during extraction. For a real action, only a
  bounded evidence excerpt, source IDs, and permalink are persisted.
- `/draft` may fetch recent channel context transiently. That context and the
  selected authored examples are sent to the configured inference provider.
- Completed job payloads are erased from the queue row.
- Retryable payloads are retained only until the bounded retry budget is
  exhausted. Tool arguments/results are hashed by default so OAuth tokens and
  private Slack context do not enter the audit trace.
- The local Slack training corpus and MLX adapter are excluded from Docker
  builds by `.dockerignore`.

Cloud inference is not model training, but it is still data disclosure to the
chosen provider. Review that provider's retention and training controls before
using real workspace data. Fully private inference should use MLX or Ollama
locally, or another self-hosted compatible endpoint.

## 1. Create the Slack app

Import `configs/slack-app-manifest.yaml` in Slack's app settings after replacing
`YOUR_DOMAIN` with the deployed HTTPS hostname.

The defaults request:

- Bot: `commands`, `chat:write`, `im:write`
- User: `channels:history`, `groups:history`, `im:history`, `mpim:history`

These user scopes let the worker read recent context available to the
authorizing user. Reduce the set if private-channel or DM context is not
required.

Configure these URLs:

```text
OAuth redirect: https://YOUR_DOMAIN/slack/oauth/callback
Events:         https://YOUR_DOMAIN/slack/events
Slash command:  https://YOUR_DOMAIN/slack/commands
Install:        https://YOUR_DOMAIN/slack/install
```

Slack's official references:

- [Installing with OAuth](https://docs.slack.dev/authentication/installing-with-oauth/)
- [Verifying requests](https://docs.slack.dev/authentication/verifying-requests-from-slack/)
- [Events API](https://docs.slack.dev/apis/events-api/)

## 2. Configure secrets

Copy `.env.example` and populate it only in a local secret manager or Railway
service variables:

```bash
cp .env.example .env
```

Generate the token-encryption key:

```bash
uv run --extra hosted python -c \
  "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

The same key must be supplied to the web service and worker. Back it up
securely: losing it makes stored Slack tokens unreadable. Rotating it currently
requires reinstallation or a dedicated re-encryption migration.

## 3. Choose a model backend

The initial hosted backend supports any Chat Completions-compatible HTTPS API:

```dotenv
CARMEN_MODEL_PROVIDER=openai-compatible
CARMEN_MODEL_BASE_URL=https://api.openai.com/v1
CARMEN_MODEL_API_KEY=...
CARMEN_MODEL_NAME=...
```

The API key is used only for inference. Per-user LoRA training is not part of
the hosted path; style comes from retrieving representative messages authored
by that user.

The provider interface is in `src/carmen_agent/providers/base.py`. Additional
Ollama, llama.cpp, or vLLM endpoints can use the same OpenAI-compatible backend
when they expose the same API shape.

## 4. Run locally

Install hosted dependencies:

```bash
uv sync --extra hosted --extra dev
```

For a local smoke test, SQLite can replace Postgres:

```dotenv
DATABASE_URL=sqlite:///work/hosted-dev.db
```

Then run the two long-lived processes:

```bash
uv run carmen-hosted serve --port 8080
uv run carmen-hosted worker
```

Queue due daily digests manually:

```bash
uv run carmen-hosted enqueue-daily
uv run carmen-hosted enqueue-reconcile
```

Expose the local web service through an HTTPS tunnel only for Slack app
development. Never expose a development instance without signing verification
and token encryption configured.

## 5. Deploy to Railway

Create one Railway project with Postgres and four services from this
repository:

| Service | Config path | Purpose |
| --- | --- | --- |
| Web | `/railway.toml` | OAuth, signed events, `/draft`, health check |
| Worker | `/deploy/railway-worker.toml` | Model jobs and private DMs |
| Scheduler | `/deploy/railway-daily.toml` | Hourly timezone-aware digest enqueue |
| Reconciler | `/deploy/railway-reconcile.toml` | Hourly idempotent source re-check |

Attach the same Postgres `DATABASE_URL` and secret variables to all three.
Only the web and worker need all Slack/model secrets, but using a shared Railway
variable group keeps their encryption key consistent.

The web config runs `carmen-hosted migrate` before deployment and exposes
`/healthz`. The scheduler runs hourly; application logic ensures each user gets
at most one digest on their local date at their configured hour.

The durable runtime tables are `agent_runs`, `run_steps`, and `tool_calls`.
They store resolved surface/model/tool configuration, step status, attempts,
stable idempotency keys, declared tool policy, and redacted traces. The action
ledger remains separate because actions outlive any individual execution run.

Railway reads `railway.toml` as configuration-as-code and automatically uses a
root `Dockerfile`. For the worker and scheduler, set the service's config-file
path to the corresponding file under `deploy/`.

Official Railway references:

- [Dockerfile deployments](https://docs.railway.com/builds/dockerfiles)
- [Configuration as code](https://docs.railway.com/config-as-code/reference)
- [Build and deploy services](https://docs.railway.com/build-deploy)

## 6. Verify before real workspace use

```bash
uv run pytest
uv run carmen eval
docker build -t carmen-assistant .
docker run --rm --env-file .env -p 8080:8080 carmen-assistant
```

Check:

```bash
curl https://YOUR_DOMAIN/healthz
```

Expected:

```json
{"status": "ok"}
```

Then visit `https://YOUR_DOMAIN/slack/install`, authorize a test workspace, send
new messages as the connected user, and invoke:

```text
/draft say I can review this tomorrow
```

The result arrives as a private DM. It is never posted into the source channel.

## Open-source status

The project is licensed under Apache License 2.0. Before publishing a deployment
fork, run the release checks in `docs/RELEASING.md` and verify that generated
archives and Git history do not contain private training data, adapters, OAuth
credentials, or proprietary source snapshots.
