"""Public distribution metadata must agree across the project and container."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_mit_license_and_project_metadata() -> None:
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    metadata = project["project"]

    assert license_text.startswith("MIT License\n")
    assert "Copyright (c) 2026 Roberto Ribes" in license_text
    assert metadata["license"] == "MIT"
    assert metadata["license-files"] == ["LICENSE"]
    assert metadata["authors"] == [{"name": "Roberto Ribes"}]


def test_container_includes_distribution_license() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert 'org.opencontainers.image.licenses="MIT"' in dockerfile
    assert 'org.opencontainers.image.authors="Roberto Ribes"' in dockerfile
    assert "COPY pyproject.toml README.md LICENSE ./" in dockerfile
    assert "COPY LICENSE /usr/share/licenses/news-bulletin-playlist/LICENSE" in dockerfile
