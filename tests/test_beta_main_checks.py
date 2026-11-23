"""A beta build must reject stale, absent or failing exact-main checks."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/verify-beta-main-checks.sh"
BETA = ROOT / ".github/workflows/publish-beta-amd64.yml"
CONTAINER = ROOT / ".github/workflows/container.yml"
SHA = "a" * 40

FAKE_GH = '''#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
assert args[:3] == ["api", "--method", "GET"], args
assert "head_sha=" + os.environ["EXPECTED_SHA"] in args, args
assert "branch=main" in args and "per_page=100" in args, args
workflow = "CI" if "/ci.yml/" in args[3] else "Container"
event = "push" if workflow == "CI" else "workflow_dispatch"
assert "event=" + event in args, args
scenario = os.environ["SCENARIO"]
if scenario == "api_failure":
    sys.exit(1)

def record(**changes):
    row = dict(head_sha=os.environ["EXPECTED_SHA"], head_branch="main",
               event=event, status="completed", conclusion="success")
    row.update(changes)
    return row

rows = [record()]
if scenario == "missing_container" and workflow == "Container":
    rows = []
if scenario == "failed_ci" and workflow == "CI":
    rows = [record(conclusion="failure")]
if scenario == "pending_container" and workflow == "Container":
    rows = [record(status="in_progress", conclusion=None)]
if scenario == "stale_sha" and workflow == "Container":
    rows = [record(head_sha="b" * 40)]
if scenario == "wrong_branch" and workflow == "Container":
    rows = [record(head_branch="feature")]
if scenario == "wrong_event" and workflow == "Container":
    rows = [record(event="pull_request")]
if scenario == "newest_failed" and workflow == "Container":
    rows = [record(conclusion="failure"), record()]
if scenario == "malformed" and workflow == "Container":
    print(json.dumps({"workflow_runs": {}}))
else:
    print(json.dumps({"workflow_runs": rows}))
'''


@pytest.mark.parametrize(
    ("scenario", "should_pass"),
    [
        ("success", True),
        ("missing_container", False),
        ("failed_ci", False),
        ("pending_container", False),
        ("stale_sha", False),
        ("wrong_branch", False),
        ("wrong_event", False),
        ("newest_failed", False),
        ("malformed", False),
        ("api_failure", False),
    ],
)
def test_beta_exact_main_checks_fail_closed(
    tmp_path: Path, scenario: str, should_pass: bool
) -> None:
    fake = tmp_path / "gh"
    fake.write_text(FAKE_GH, encoding="utf-8")
    fake.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{tmp_path}:{env['PATH']}",
            "GITHUB_REPOSITORY": "eXPerience83/news-bulletin-playlist",
            "GH_TOKEN": "test-placeholder",
            "EXPECTED_SHA": SHA,
            "SCENARIO": scenario,
        }
    )
    result = subprocess.run(
        ["bash", str(SCRIPT), SHA],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
        env=env,
    )
    assert (result.returncode == 0) is should_pass, (scenario, result.stderr)


def test_beta_check_rejects_non_sha_before_api(tmp_path: Path) -> None:
    fake = tmp_path / "gh"
    fake.write_text("#!/bin/sh\nexit 70\n", encoding="utf-8")
    fake.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{tmp_path}:{env['PATH']}",
            "GITHUB_REPOSITORY": "eXPerience83/news-bulletin-playlist",
            "GH_TOKEN": "test-placeholder",
        }
    )
    result = subprocess.run(
        ["bash", str(SCRIPT), "main"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        env=env,
    )
    assert result.returncode == 2


def test_beta_workflow_orders_exact_main_gate_before_ghcr() -> None:
    workflow = yaml.load(BETA.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["publish"]["steps"]
    names = [step.get("name", str(step.get("uses", ""))) for step in steps]
    check = names.index("Require passing CI and Container on exact merged main")
    assert names.index("Resolve publication metadata") < check
    assert check < names.index("Build exact beta candidate")
    assert check < names.index("Log in to GHCR")
    assert check < names.index("Publish or reuse immutable beta SHA tag")
    assert workflow["jobs"]["publish"]["permissions"]["actions"] == "read"
    guard = steps[check]
    assert "scripts/verify-beta-main-checks.sh" in guard["run"]
    assert "steps.meta.outputs.source_sha" in guard["env"]["SOURCE_SHA"]
    assert "github.token" in guard["env"]["GH_TOKEN"]

    container = yaml.load(CONTAINER.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert "workflow_dispatch" in container["on"]
    assert "pull_request" in container["on"]
    assert container["permissions"] == {"contents": "read"}
