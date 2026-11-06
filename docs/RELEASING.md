# Releasing Carmen Personal Agent

This checklist creates both normal Python artifacts and a privacy-checked source
archive that someone can unzip and run without Git.

## Before the first public push

1. Create a new empty public repository and enable private vulnerability
   reporting in its security settings.
2. Review every tracked file and the complete Git history. `.gitignore` prevents
   future additions; it does not erase data from old commits.
3. Confirm that all examples and tests use synthetic data.
4. Add the repository URL to the clone command in `README.md` after it exists.
5. Set the default branch to require the cross-platform test and package jobs.

Because this workspace may have been used with real messages, the safest first
public history is a fresh repository initialized from the sanitized source
archive, not a push of an older private Git history.

## Build and verify locally

```bash
uv sync --extra dev
uv run pytest -q
uv run carmen eval
python scripts/build-release-archive.py --check-only
python scripts/build-release-archive.py
python scripts/verify-release.py dist/carmen-personal-agent-0.1.0-source.zip
uv run --with build python -m build
```

The source-archive builder uses an explicit allowlist. It excludes source
snapshots, drafts, processed training data, adapters, databases, environment
files, work state, local TODOs, logs, and editor files. It also rejects common
credential formats, private keys, absolute workstation paths, and symlinks.
`RELEASE-MANIFEST.json` records a SHA-256 digest for every included file, and a
digest for the archive is written next to it.

Unzip the archive into a temporary directory and run the installation steps in
its README before publishing it.

## Publish a GitHub release

Update the version in `pyproject.toml`, refresh `uv.lock`, and update
`CHANGELOG.md`. Commit the release, then create and push the matching tag:

```bash
git tag -s v0.1.0 -m "Carmen Personal Agent 0.1.0"
git push origin v0.1.0
```

The release workflow requires the tag to match the package version, reruns the
tests and evaluations, builds the wheel, source distribution, sanitized source
zip, and checksums, and attaches them to a GitHub release. If signed tags are not
configured, use an annotated tag after documenting that decision.

Publishing to PyPI is intentionally not automatic. Configure PyPI trusted
publishing and add a separate reviewed workflow if package-registry distribution
becomes a project requirement.
