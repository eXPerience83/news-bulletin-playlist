"""Contract: no beta image can publish without an isolated held v10->v11 smoke."""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/verify-v11-container-cutover.sh"
CONTAINER = ROOT / ".github/workflows/container.yml"
BETA = ROOT / ".github/workflows/publish-beta-amd64.yml"


def _step_index(path: Path, marker: str) -> int:
    workflow = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["publish" if path == BETA else "validate"]["steps"]
    matches = [
        index
        for index, step in enumerate(steps)
        if marker in str(step.get("run", ""))
    ]
    assert len(matches) == 1, f"{path}: missing or duplicate {marker}"
    return matches[0]


def test_container_and_beta_both_require_packaged_offline_migration() -> None:
    invocation = "bash scripts/verify-v11-container-cutover.sh"
    assert _step_index(CONTAINER, invocation) > _step_index(
        CONTAINER, "news-bulletin-playlist:ci ."
    )
    assert _step_index(BETA, invocation) > _step_index(
        BETA, "--tag news-bulletin-playlist:beta-candidate"
    )
    payload = yaml.load(BETA.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    steps = payload["jobs"]["publish"]["steps"]
    login = next(
        i
        for i, step in enumerate(steps)
        if str(step.get("uses", "")).startswith("docker/login-action@")
    )
    assert _step_index(BETA, invocation) < login, "cutover check must run before GHCR login"
    publish = _step_index(BETA, 'docker push "$sha_ref"')
    assert _step_index(BETA, invocation) < publish, "cutover check must precede any publication"


def test_offline_cutover_has_hold_restart_and_exact_destinations() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    assert script.startswith("#!/usr/bin/env bash")
    assert script.count("--network none") == 3, "seed + held boot + restart must deny egress"
    assert "NEWS_PLAYLIST_STARTUP_INSPECTION=1" in script
    assert "start_held first" in script
    assert "start_held restart" in script
    for token in (
        "legacy_comparison",
        "safe_to_review_without_spotify_writes",
        "historical_covers",
        "spotify_playlist_id",
        "v11_state_sha256",
        "owner_templates",
        'character * 22 for character in "ABCD"',
    ):
        assert token in script
