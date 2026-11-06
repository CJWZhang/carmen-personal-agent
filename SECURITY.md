# Security Policy

## Supported versions

Security fixes are applied to the latest release. The project is currently in
alpha, so users should review connector permissions and keep external actions in
approval mode.

## Report a vulnerability

Do not open a public issue containing credentials, private message content, or a
reproducible exploit. Use the repository host's private vulnerability-reporting
feature. If that feature has not been enabled, contact the repository owner
privately and share only the minimum reproduction data required.

Include the affected version, impact, reproduction steps, and any proposed
mitigation. Never attach a real Slack export, mailbox snapshot, token, database,
or model adapter.

## Data and credential model

- Local source snapshots, task state, drafts, fine-tuning data, adapters, and
  evaluation snapshots are excluded from source and release archives.
- `.env` files and common private-key formats are excluded from Git and Docker
  build contexts.
- Secrets must be supplied through environment variables or a deployment secret
  manager; `carmen init` only writes non-secret configuration.
- The local UI binds to loopback by default and protects state-changing requests
  with a session token. Do not expose it directly to a public network.
- Hosted deployments must use HTTPS, encrypted OAuth tokens, Slack signature
  verification, and least-privilege scopes.

Before publishing a fork, run the release checker documented in the README and
review the complete Git history. Ignoring a file now does not remove it from old
commits.
