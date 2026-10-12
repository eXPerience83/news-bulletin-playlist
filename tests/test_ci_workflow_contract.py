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
    ".github/workflows/publish-edge-amd64.yml",
    ".github/workflows/publish-stable-amd64.yml",
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


def test_untrusted_pr_ci_never_persists_checkout_credentials() -> None:
    workflow = _workflow()
    assert workflow.get("permissions") == {"contents": "read"}

    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict)
    test_job = jobs.get("test")
    assert isinstance(test_job, dict)
    steps = test_job.get("steps")
    assert isinstance(steps, list)

    checkouts = [
        step
        for step in steps
        if isinstance(step, dict)
        and isinstance(step.get("uses"), str)
        and str(step["uses"]).startswith("actions/checkout@")
    ]
    assert len(checkouts) == 1
    checkout_config = checkouts[0].get("with")
    assert isinstance(checkout_config, dict)
    assert checkout_config.get("persist-credentials") == "false"


def test_ci_uses_only_python_315_without_a_matrix() -> None:
    workflow = _workflow()
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict)
    job = jobs.get("test")
    assert isinstance(job, dict)
    assert "strategy" not in job, "single-interpreter CI must not create a matrix"
    steps = job.get("steps")
    assert isinstance(steps, list)
    setup_steps = [
        step
        for step in steps
        if isinstance(step, dict)
        and isinstance(step.get("uses"), str)
        and str(step["uses"]).startswith("actions/setup-python@")
    ]
    assert len(setup_steps) == 1
    config = setup_steps[0].get("with")
    assert isinstance(config, dict)
    assert config.get("python-version") == "3.15"
