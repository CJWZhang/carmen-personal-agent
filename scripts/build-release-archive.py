#!/usr/bin/env python3
"""Build a deterministic, privacy-checked source archive."""
from __future__ import annotations

import argparse
import json
import sys
import tomllib
import zipfile
from pathlib import Path

from release_common import ReleaseCheckError, inspect_content, public_source_files, sha256


ROOT = Path(__file__).resolve().parents[1]


def project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def build(*, output_dir: Path, check_only: bool = False) -> Path | None:
    files = public_source_files(ROOT)
    entries: list[tuple[str, bytes]] = []
    for source in files:
        relative = source.relative_to(ROOT).as_posix()
        data = source.read_bytes()
        inspect_content(relative, data)
        entries.append((relative, data))

    version = project_version()
    archive_root = f"carmen-personal-agent-{version}"
    manifest = {
        "format": 1,
        "project": "carmen-personal-agent",
        "version": version,
        "files": {name: sha256(data) for name, data in entries},
    }
    if check_only:
        print(f"Release check passed for {len(entries)} public files.")
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / f"{archive_root}-source.zip"
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative, data in entries:
            info = zipfile.ZipInfo(f"{archive_root}/{relative}", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if relative.endswith(".sh") else 0o644) << 16
            archive.writestr(info, data)
        manifest_data = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
        info = zipfile.ZipInfo(
            f"{archive_root}/RELEASE-MANIFEST.json", date_time=(1980, 1, 1, 0, 0, 0)
        )
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        archive.writestr(info, manifest_data)

    digest_path = destination.with_suffix(destination.suffix + ".sha256")
    digest_path.write_text(f"{sha256(destination.read_bytes())}  {destination.name}\n", encoding="utf-8")
    print(destination)
    print(digest_path)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    try:
        build(output_dir=args.output_dir, check_only=args.check_only)
    except ReleaseCheckError as exc:
        print(f"Release check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
