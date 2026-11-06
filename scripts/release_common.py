"""Shared allowlist and privacy checks for public release artifacts."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path, PurePosixPath


PUBLIC_FILES = {
    ".dockerignore",
    ".env.example",
    ".gitignore",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "Dockerfile",
    "handoff.md",
    "LICENSE",
    "NOTICE",
    "README.md",
    "SECURITY.md",
    "pyproject.toml",
    "railway.toml",
    "run-chat.sh",
    "run-train.sh",
    "uv.lock",
}
PUBLIC_DIRS = {
    ".github",
    "configs",
    "deploy",
    "docs",
    "evals/cases",
    "migrations",
    "scripts",
    "src",
    "tests",
}
PUBLIC_NESTED_FILES = {"data/inbox/README.md"}
REQUIRED_FILES = {
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "pyproject.toml",
    "src/carmen_agent/__init__.py",
    "src/carmen_agent/ui/assets/index.html",
    "configs/collector-output.schema.json",
    "configs/source-observation.schema.json",
    "configs/style-corpus.schema.json",
    "configs/lora.yaml",
    "data/inbox/README.md",
}
IGNORED_PARTS = {
    ".DS_Store",
    ".git",
    ".idea",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
}
IGNORED_SUFFIXES = {".egg-info", ".log", ".pyc", ".pyo", ".swp"}

# These expressions intentionally favor a false positive over shipping a secret
# or an absolute workstation path. Tests should use unmistakably synthetic
# placeholders that do not resemble live provider tokens.
FORBIDDEN_CONTENT = {
    "macOS user path": re.compile(rb"/Users/[A-Za-z0-9._-]+/"),
    "Windows user path": re.compile(rb"[A-Za-z]:\\Users\\[^\\\r\n]+\\"),
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "Slack token": re.compile(rb"xox[baprs]-[A-Za-z0-9-]{10,}"),
    "GitHub token": re.compile(rb"gh(?:p|o|u|s|r)_[A-Za-z0-9]{20,}"),
    "OpenAI-style key": re.compile(rb"sk-[A-Za-z0-9_-]{20,}"),
}


class ReleaseCheckError(RuntimeError):
    """Raised when a public release would contain unsafe content."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_ignored(relative: PurePosixPath) -> bool:
    for part in relative.parts:
        if part in IGNORED_PARTS or any(part.endswith(suffix) for suffix in IGNORED_SUFFIXES):
            return True
    return False


def public_source_files(root: Path) -> list[Path]:
    candidates: set[Path] = set()
    for name in PUBLIC_FILES | PUBLIC_NESTED_FILES:
        candidate = root / name
        if candidate.is_file():
            candidates.add(candidate)
    for name in PUBLIC_DIRS:
        directory = root / name
        if directory.is_dir():
            candidates.update(item for item in directory.rglob("*") if item.is_file())

    selected: list[Path] = []
    for candidate in sorted(candidates):
        relative = PurePosixPath(candidate.relative_to(root).as_posix())
        if candidate.is_symlink():
            raise ReleaseCheckError(f"Symlinks are not allowed in release input: {relative}")
        if not is_ignored(relative):
            selected.append(candidate)
    missing = sorted(name for name in REQUIRED_FILES if not (root / name).is_file())
    if missing:
        raise ReleaseCheckError("Missing required public files: " + ", ".join(missing))
    return selected


def inspect_content(relative: str, data: bytes) -> None:
    if b"\x00" in data:
        return
    for label, pattern in FORBIDDEN_CONTENT.items():
        if pattern.search(data):
            raise ReleaseCheckError(f"Possible {label} in {relative}")


def validate_member_name(name: str, expected_root: str) -> str | None:
    member = PurePosixPath(name)
    if member.is_absolute() or ".." in member.parts:
        raise ReleaseCheckError(f"Unsafe archive path: {name}")
    if not member.parts or member.parts[0] != expected_root:
        raise ReleaseCheckError(f"Archive member is outside {expected_root}/: {name}")
    if len(member.parts) == 1:
        return None
    relative = PurePosixPath(*member.parts[1:])
    if is_ignored(relative):
        raise ReleaseCheckError(f"Ignored path found in archive: {relative}")
    return relative.as_posix()
