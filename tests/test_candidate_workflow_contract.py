from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = ROOT / ".github/workflows/publish-pr-candidate-amd64.yml"
WORKER_PATH = ROOT / ".github/workflows/publish-pr-candidate-worker-amd64.yml"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _between(text: str, start: str, end: str | None) -> str:
    _, separator, remainder = text.partition(start)
    assert separator, f"missing workflow anchor: {start!r}"
    if end is None:
        return remainder
    block, separator, _ = remainder.partition(end)
    assert separator, f"missing workflow anchor: {end!r}"
    return block


def _active(block: str) -> str:
    return "\n".join(line for line in block.splitlines() if not line.lstrip().startswith("#"))


def _assert_contains(block: str, expected: tuple[str, ...]) -> None:
    for token in expected:
        assert token in block, f"missing candidate-workflow contract token: {token!r}"


def _assert_absent(block: str, forbidden: tuple[str, ...]) -> None:
    for token in forbidden:
        assert token not in block, f"forbidden candidate-workflow token: {token!r}"


def test_candidate_request_gate_trigger_and_privileges_are_locked() -> None:
    gate = _read(GATE_PATH)
    trigger = _active(_between(gate, "on:\n", "\npermissions:"))

    _assert_contains(trigger, ("issue_comment:\n    types: [created]",))
    _assert_absent(
        trigger,
        (
            "push:",
            "pull_request:",
            "pull_request_target:",
            "workflow_run:",
            "workflow_call:",
            "workflow_dispatch:",
            "schedule:",
        ),
    )

    dispatch = _active(_between(gate, "  dispatch:\n", None))
    _assert_contains(
        dispatch,
        (
            "github.event.issue.pull_request &&",
            "startsWith(github.event.comment.body, '/publish-candidate ') &&",
            "github.event.comment.user.login == github.repository_owner",
            "cache-mode: none",
            "actions: write",
            "pull-requests: read",
            'if [[ "$requested_sha" != "$head_sha" ]]',
            "--arg ref main",
            "publish-pr-candidate-worker-amd64.yml/dispatches",
        ),
    )
    _assert_absent(
        dispatch,
        ("actions/checkout@", "packages: write", "actions/cache/", "upload-artifact@"),
    )


def test_candidate_worker_trigger_and_build_boundary_are_locked() -> None:
    worker = _read(WORKER_PATH)
    trigger = _active(_between(worker, "on:\n", "\npermissions:"))

    _assert_contains(
        trigger,
        ("workflow_dispatch:", "pr_number:", "head_sha:", "authorization_comment_id:"),
    )
    _assert_absent(
        trigger,
        (
            "push:",
            "pull_request:",
            "pull_request_target:",
            "issue_comment:",
            "workflow_run:",
            "workflow_call:",
            "schedule:",
        ),
    )

    build = _active(_between(worker, "  build:\n", "\n  publish:\n"))
    _assert_contains(
        build,
        (
            "cache-mode: none",
            "contents: read",
            "issues: read",
            "pull-requests: read",
            'if [[ "$GITHUB_REF" != "refs/heads/main" ]]',
            'if [[ "$head_sha" != "$INPUT_HEAD_SHA" ]]',
            "persist-credentials: false",
            "candidate-image-run-${GITHUB_RUN_ID}-attempt-${GITHUB_RUN_ATTEMPT}",
            'archive_dir="${RUNNER_TEMP}/candidate-transfer"',
            'test ! -L "$archive"',
            "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9",
            "archive: false",
            "if-no-files-found: error",
            "overwrite: false",
            "retention-days: 1",
            "artifact_id: ${{ steps.transfer.outputs.artifact-id }}",
        ),
    )
    _assert_absent(build, ("packages: write", "actions/cache/", "actions: write", "restore-keys:"))


def test_candidate_publication_and_cleanup_boundaries_are_locked() -> None:
    worker = _read(WORKER_PATH)
    publish = _active(_between(worker, "  publish:\n", "\n  cleanup:\n"))

    _assert_contains(
        publish,
        (
            "cache-mode: none",
            "packages: write",
            "actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333",
            "artifact-ids: ${{ needs.build.outputs.artifact_id }}",
            "skip-decompress: true",
            "digest-mismatch: error",
            "Validate exact same-attempt transfer artifact",
            '[[ "$ARTIFACT_ID" =~ ^[1-9][0-9]*$ ]]',
            '(.workflow_run.id | tostring) == $run',
            '.name == $name',
            '.expired == false',
            '$BUILD_HEAD" == "$AUTHORIZED_HEAD"',
            'if [[ "$actual_sha256" != "$EXPECTED_SHA256" ]]',
            "Revalidate authorization before registry publication",
            "current_head_sha=",
            ".head.sha",
            'current_head_sha" != "$AUTHORIZED_SHA"',
            "Publish or reuse immutable candidate tag",
            'expected_version="candidate-pr-${PR_NUMBER}"',
            'resolved_revision="$(docker image inspect "$resolved_ref"',
            'resolved_version="$(docker image inspect "$resolved_ref"',
            'if [[ "$resolved_revision" != "$HEAD_SHA"',
            'if [[ "$existing" == false ]]',
            "Revalidate authorization immediately before dev promotion",
            "Promote exact candidate digest to dev aliases",
            'if [[ "$candidate_digest" != "$IMAGE_DIGEST" ]]',
        ),
    )
    _assert_absent(
        publish,
        (
            "actions/checkout@",
            "restore-keys:",
            "actions/cache/",
            '--tag "${image}:edge',
            '--tag "${image}:stable',
            '--tag "${image}:latest',
        ),
    )

    cleanup = _active(_between(worker, "  cleanup:\n", None))
    _assert_contains(
        cleanup,
        (
            "needs: [build, publish]",
            "cache-mode: none",
            "actions: write",
            "actions/artifacts/${ARTIFACT_ID}",
            '(.workflow_run.id | tostring) == $run',
            '.name == $name',
            'gh api --method DELETE "repos/${GITHUB_REPOSITORY}/actions/artifacts/${ARTIFACT_ID}"',
        ),
    )
