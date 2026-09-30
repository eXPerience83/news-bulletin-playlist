from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CI_PATH = ROOT / ".github/workflows/ci.yml"

EXPECTED_PR_PATHS = {
    "src/**",
    "tests/**",
    "scripts/**/*.py",
    "pyproject.toml",
    "assets/covers/spotify/spain_spanish_news.jpg",
    ".github/workflows/ci.yml",
    ".github/workflows/publish-pr-candidate-amd64.yml",
    ".github/workflows/publish-pr-candidate-worker-amd64.yml",
}


def _workflow() -> dict[object, object]:
    payload = yaml.load(CI_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(payload, dict), "CI workflow must parse to a mapping"
    return payload


def test_pull_request_ci_paths_cover_tested_contract_inputs() -> None:
    workflow = _workflow()
    trigger = workflow.get("on")
    assert isinstance(trigger, dict), "workflow on trigger must be a mapping"

    push = trigger.get("push")
    assert isinstance(push, dict), "push trigger must be a mapping"
    assert push == {"branches": ["main"]}

    pull_request = trigger.get("pull_request")
    assert isinstance(pull_request, dict), "pull_request trigger must be a mapping"
    assert set(pull_request) == {"paths"}

    paths = pull_request.get("paths")
    assert isinstance(paths, list), "pull_request paths must be a list"
    assert all(isinstance(path, str) for path in paths)
    assert len(paths) == len(set(paths)), "pull_request paths must not contain duplicates"
    assert set(paths) == EXPECTED_PR_PATHS
