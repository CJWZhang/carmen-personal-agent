#!/usr/bin/env python3
"""Verify the structure, hashes, and privacy boundary of a source archive."""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path, PurePosixPath

from release_common import (
    REQUIRED_FILES,
    ReleaseCheckError,
    inspect_content,
    sha256,
    validate_member_name,
)


ROOT = Path(__file__).resolve().parents[1]


def verify(archive_path: Path) -> None:
    if not archive_path.is_file():
        raise ReleaseCheckError(f"Archive not found: {archive_path}")
    if not archive_path.name.endswith("-source.zip"):
        raise ReleaseCheckError("Expected a *-source.zip archive")
    expected_root = archive_path.name.removesuffix("-source.zip")
    seen: dict[str, bytes] = {}
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            relative = validate_member_name(info.filename, expected_root)
            if relative is None or info.is_dir():
                continue
            if relative in seen:
                raise ReleaseCheckError(f"Duplicate archive member: {relative}")
            data = archive.read(info)
            inspect_content(relative, data)
            seen[relative] = data

    missing = sorted(REQUIRED_FILES - seen.keys())
    if missing:
        raise ReleaseCheckError("Archive is missing: " + ", ".join(missing))
    manifest_name = "RELEASE-MANIFEST.json"
    if manifest_name not in seen:
        raise ReleaseCheckError("Archive has no RELEASE-MANIFEST.json")
    manifest = json.loads(seen.pop(manifest_name))
    expected_hashes = manifest.get("files")
    actual_hashes = {name: sha256(data) for name, data in seen.items()}
    if expected_hashes != actual_hashes:
        raise ReleaseCheckError("Manifest hashes do not match archive contents")

    forbidden_roots = {"adapters", "outputs", "work", "fused_model"}
    for name in seen:
        parts = PurePosixPath(name).parts
        if parts and (parts[0] in forbidden_roots or name.startswith("data/raw/") or name.startswith("data/processed/") or name.startswith("data/drafts/")):
            raise ReleaseCheckError(f"Private/generated path found: {name}")
        if name.startswith("data/inbox/") and name != "data/inbox/README.md":
            raise ReleaseCheckError(f"Source snapshot found: {name}")
    print(f"Verified {archive_path} ({len(seen)} public files).")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    try:
        verify(args.archive)
    except (ReleaseCheckError, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        print(f"Release verification failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
