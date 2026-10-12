from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CI_PATH = ROOT / ".github/workflows/ci.yml"

def _workflow() -> dict[object, object]:
    payload = yaml.load(CI_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(payload, dict), "CI workflow must parse to a mapping"
    return payload


def test_required_ci_check_runs_for_every_pull_request() -> None:
    workflow = _workflow()
    trigger = workflow.get("on")
    assert isinstance(trigger, dict), "workflow on trigger must be a mapping"
    assert trigger.get("push") == {"branches": ["main"]}
    # Required checks must not be skipped for docs-only or admin-only PRs.
    assert trigger.get("pull_request") == {}

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
