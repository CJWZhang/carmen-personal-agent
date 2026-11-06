## What changed

Describe the user outcome and the implementation.

## Verification

- [ ] `uv run pytest -q`
- [ ] `uv run carmen eval` when extraction, ranking, or drafting changed
- [ ] `python scripts/build-release-archive.py --check-only`
- [ ] Documentation and changelog updated when needed

## Privacy and safety

- [ ] This change contains no real messages, emails, events, credentials,
      databases, model adapters, drafts, or machine-specific paths
- [ ] New external writes require explicit approval and are idempotent
- [ ] New connector permissions are least-privilege and documented
