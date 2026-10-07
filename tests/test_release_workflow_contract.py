from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
EDGE_PATH = ROOT / ".github/workflows/publish-edge-amd64.yml"
STABLE_PATH = ROOT / ".github/workflows/publish-stable-amd64.yml"


def _workflow(path: Path) -> dict[object, object]:
    payload = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(payload, dict), f"{path.name} must parse to a mapping"
    return payload


def _mapping(value: object, label: str) -> dict[object, object]:
    assert isinstance(value, dict), f"{label} must be a mapping"
    return value


def _publish_job(workflow: dict[object, object]) -> dict[object, object]:
    jobs = _mapping(workflow.get("jobs"), "jobs")
    return _mapping(jobs.get("publish"), "publish job")


def _steps(job: dict[object, object]) -> list[dict[object, object]]:
    raw_steps = job.get("steps")
    assert isinstance(raw_steps, list), "publish steps must be a list"
    assert all(isinstance(step, dict) for step in raw_steps)
    return raw_steps


def _checkout(job: dict[object, object]) -> dict[object, object]:
    matches = [
        step
        for step in _steps(job)
        if isinstance(step.get("uses"), str) and str(step["uses"]).startswith("actions/checkout@")
    ]
    assert len(matches) == 1, "publish job must contain exactly one checkout step"
    return matches[0]


def _script_containing(job: dict[object, object], token: str) -> str:
    runs = [step.get("run") for step in _steps(job)]
    matches = [run for run in runs if isinstance(run, str) and token in run]
    assert len(matches) == 1, f"expected exactly one publish script containing {token!r}"
    return matches[0]


def _assert_contains(script: str, *tokens: str) -> None:
    for token in tokens:
        assert token in script, f"missing release-workflow contract token: {token!r}"


def test_edge_publication_contract_is_locked() -> None:
    workflow = _workflow(EDGE_PATH)
    trigger = _mapping(workflow.get("on"), "edge trigger")
    assert set(trigger) == {"workflow_dispatch"}
    assert workflow.get("permissions") == {"contents": "read"}

    publish = _publish_job(workflow)
    assert publish.get("permissions") == {"contents": "read", "packages": "write"}
    checkout_with = _mapping(_checkout(publish).get("with"), "edge checkout with")
    assert checkout_with.get("persist-credentials") == "false"

    main_guard = _script_containing(publish, "refs/heads/main")
    metadata = _script_containing(publish, "source_sha=$GITHUB_SHA")
    queued_guard = _script_containing(publish, "refusing stale queued edge build")
    build = _script_containing(publish, "SOURCE_REVISION=${GITHUB_SHA}")
    smoke = _script_containing(publish, "--security-opt no-new-privileges:true")
    publish_immutable = _script_containing(publish, 'sha_tag="sha-${SOURCE_SHA}"')
    pre_publish_guard = _script_containing(publish, "refusing stale edge publication")
    pre_promote_guard = _script_containing(publish, "main advanced during publication")
    promote = _script_containing(publish, '--tag "${image}:edge-amd64"')

    _assert_contains(main_guard, "GITHUB_REF", "refs/heads/main")
    _assert_contains(metadata, "GITHUB_SHA", "source_sha=")
    _assert_contains(queued_guard, "branches/main", "SOURCE_SHA")
    _assert_contains(build, '--label "org.opencontainers.image.revision=${GITHUB_SHA}"')
    _assert_contains(
        smoke,
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges:true",
    )
    _assert_contains(pre_publish_guard, "branches/main", "SOURCE_SHA")
    _assert_contains(
        publish_immutable,
        'sha_tag="sha-${SOURCE_SHA}"',
        'resolved_ref="${image}@${digest}"',
        "resolved_id",
        "local_id",
    )
    _assert_contains(pre_promote_guard, "branches/main", "SOURCE_SHA")
    _assert_contains(
        promote,
        'source_ref="${image}@${IMAGE_DIGEST}"',
        '--tag "${image}:edge"',
        '--tag "${image}:edge-amd64"',
        "sha_digest",
    )
    assert '--tag "${image}:stable"' not in promote
    assert '--tag "${image}:latest"' not in promote


def test_stable_publication_contract_is_locked() -> None:
    workflow = _workflow(STABLE_PATH)
    trigger = _mapping(workflow.get("on"), "stable trigger")
    assert set(trigger) == {"push", "workflow_dispatch"}
    assert trigger.get("push") == {"tags": ["v*"]}
    dispatch = _mapping(trigger.get("workflow_dispatch"), "stable workflow_dispatch")
    inputs = _mapping(dispatch.get("inputs"), "stable inputs")
    version = _mapping(inputs.get("version"), "version input")
    assert version.get("required") == "true"
    assert workflow.get("permissions") == {"contents": "read"}

    publish = _publish_job(workflow)
    assert publish.get("permissions") == {"contents": "read", "packages": "write"}
    checkout_with = _mapping(_checkout(publish).get("with"), "stable checkout with")
    assert checkout_with.get("persist-credentials") == "false"
    assert checkout_with.get("fetch-depth") == "0"
    assert "inputs.version" in str(checkout_with.get("ref"))

    validate_tag = _script_containing(publish, "stable publication requires exact SemVer")
    metadata = _script_containing(publish, "refs/tags/${VERSION}^{commit}")
    main_history = _script_containing(publish, "git merge-base --is-ancestor")
    newest_tag = _script_containing(publish, "refusing stable regression")
    build = _script_containing(publish, "SOURCE_REVISION=${SOURCE_SHA}")
    smoke = _script_containing(publish, "127.0.0.1:18791:8080")
    revalidate = _script_containing(publish, "release tag moved after validation")
    publish_version = _script_containing(publish, 'version_ref="${image}:${VERSION}"')
    pre_promote = _script_containing(publish, "release authorization changed during publication")
    promote = _script_containing(publish, '--tag "${image}:stable-amd64"')

    _assert_contains(validate_tag, "^v(0|[1-9][0-9]*)", "VERSION")
    _assert_contains(metadata, 'source_sha="$(git rev-parse HEAD)"', "tag_sha=")
    _assert_contains(main_history, "refs/remotes/origin/main", "SOURCE_SHA")
    _assert_contains(newest_tag, "git tag --list 'v*'", "refs/remotes/origin/main")
    _assert_contains(
        build,
        '--build-arg "SOURCE_REVISION=${SOURCE_SHA}"',
        '--label "org.opencontainers.image.revision=${SOURCE_SHA}"',
        '--label "org.opencontainers.image.version=${VERSION}"',
    )
    _assert_contains(
        smoke,
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges:true",
    )
    _assert_contains(revalidate, "commits/${VERSION}", "branches/main", "SOURCE_SHA")
    _assert_contains(
        publish_version,
        'version_ref="${image}:${VERSION}"',
        'resolved_ref="${image}@${digest}"',
        "resolved_id",
        "local_id",
    )
    _assert_contains(pre_promote, "commits/${VERSION}", "branches/main", "SOURCE_SHA")
    _assert_contains(
        promote,
        'source_ref="${image}@${IMAGE_DIGEST}"',
        '--tag "${image}:stable"',
        '--tag "${image}:stable-amd64"',
        '--tag "${image}:latest"',
        "version_digest",
    )
    assert '--tag "${image}:edge"' not in promote
