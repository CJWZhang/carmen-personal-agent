from __future__ import annotations

import json
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_wheel_bundles_personalization_contract_and_training_config() -> None:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        configuration = tomllib.load(handle)
    included = configuration["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]

    assert included["configs/style-corpus.schema.json"] == (
        "carmen_agent/resources/style-corpus.schema.json"
    )
    assert included["configs/lora.yaml"] == "carmen_agent/resources/lora.yaml"


def test_sendable_source_archive_is_private_and_self_verifying(tmp_path: Path) -> None:
    built = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "build-release-archive.py"),
            "--output-dir",
            str(tmp_path),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    archive_path = tmp_path / "carmen-personal-agent-0.1.0-source.zip"
    assert str(archive_path) in built.stdout
    assert archive_path.with_suffix(".zip.sha256").is_file()

    verified = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "verify-release.py"), str(archive_path)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "Verified" in verified.stdout

    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        prefix = "carmen-personal-agent-0.1.0/"
        manifest = json.loads(archive.read(prefix + "RELEASE-MANIFEST.json"))

    assert prefix + "LICENSE" in names
    assert prefix + "src/carmen_agent/ui/assets/index.html" in names
    assert prefix + "src/carmen_agent/personalization.py" in names
    assert prefix + "configs/style-corpus.schema.json" in names
    assert prefix + "configs/lora.yaml" in names
    assert prefix + "data/inbox/README.md" in names
    assert prefix + "data/inbox/slack.json" not in names
    private_prefixes = (
        "adapters/",
        "data/drafts/",
        "data/processed/",
        "data/raw/",
        "outputs/",
        "work/",
    )
    assert not any(
        name.startswith(prefix + private)
        for name in names
        for private in private_prefixes
    )
    assert manifest["project"] == "carmen-personal-agent"
