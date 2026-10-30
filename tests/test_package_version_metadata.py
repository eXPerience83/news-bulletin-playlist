"""Packaging version must agree with the runtime version shown by the app."""

from __future__ import annotations

import tomllib
from pathlib import Path

from news_bulletin_playlist import __version__


def test_package_and_runtime_versions_match() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    assert document["project"]["version"] == __version__
