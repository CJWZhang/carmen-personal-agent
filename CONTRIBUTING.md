# Contributing

Thanks for helping improve Carmen Personal Agent. This project is local-first,
and protecting user data is a release requirement rather than an optional
convention.

## Set up a development checkout

Install Python 3.11 or 3.12 and [uv](https://docs.astral.sh/uv/), then run:

```bash
uv sync --extra dev
uv run carmen init
uv run pytest -q
uv run carmen eval
```

MLX is optional and only supported on Apple Silicon Macs:

```bash
uv sync --extra dev --extra mlx
```

The default cross-platform local model path uses Ollama. The test suite does not
require Ollama, MLX, Slack, Gmail, GitHub, Google Calendar, or hosted credentials.

## Make a change

1. Create a focused branch.
2. Add or update tests for behavior changes.
3. Run `uv run pytest -q` and `uv run carmen eval`.
4. Run `python scripts/build-release-archive.py --check-only` to catch private or
   machine-specific files before committing.
5. Open a pull request explaining the user outcome, implementation, and tests.

Do not commit real messages, emails, calendar events, access tokens, databases,
model adapters, generated drafts, or evaluation snapshots. Use synthetic data in
tests and `evals/cases/`.

## Design boundaries

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the complete dependency
direction, domain package shape, and staged refactoring rules.

- Collection, reasoning, actions, and presentation should stay separable.
- Product policy belongs in `src/carmen_agent/apps/<domain>/`; UI, CLI, and
  hosted handlers should remain transports and composition roots.
- Any external write must remain explicit and reviewable.
- New source connectors should request the least privilege they need.
- Local state belongs under the configured `CARMEN_HOME`, not inside an installed
  Python package.
- Features should work without MLX unless they are explicitly MLX-only.

By submitting a contribution, you agree that it is licensed under the Apache
License 2.0.
