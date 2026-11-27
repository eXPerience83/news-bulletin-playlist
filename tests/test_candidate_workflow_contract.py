from __future__ import annotations

import gzip
import hashlib
import json
import os
import subprocess
import tempfile
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
            "(.workflow_run.id | tostring) == $run",
            ".name == $name",
            ".expired == false",
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
            "(.workflow_run.id | tostring) == $run",
            ".name == $name",
            'gh api --method DELETE "repos/${GITHUB_REPOSITORY}/actions/artifacts/${ARTIFACT_ID}"',
        ),
    )

def _candidate_step_scripts(worker: str, name: str) -> list[str]:
    scripts = []
    for step in worker.split(f"      - name: {name}\n")[1:]:
        _, separator, body = step.partition("        run: |\n")
        assert separator, name
        lines = []
        for line in body.splitlines():
            if line and not line.startswith("          "):
                break
            lines.append(line[10:])
        scripts.append("\n".join(lines) + "\n")
    return scripts


def _exercise_candidate_artifact_guards(worker: str, prefix: str) -> None:
    guards = _candidate_step_scripts(worker, "Validate exact same-attempt transfer artifact")
    cleanup = _candidate_step_scripts(
        worker, "Delete exact same-attempt transfer artifact after build handoff"
    )
    hash_steps = []
    for name in (
        "Verify transfer artifact hash and load image",
        "Hash and load the candidate images",
        "Match the verified archive and load the images",
    ):
        hash_steps.extend(_candidate_step_scripts(worker, name))
    expected_consumers = 2 if prefix == "candidate-images" else 1
    assert len(guards) == len(hash_steps) == expected_consumers
    assert len(cleanup) == 1

    # Exercise the actual shell and jq, but never contact GitHub or Docker.
    stub = """
gh() {
    if [[ "$#" == 2 && "$1" == api ]]; then
        [[ "$2" == "repos/$GITHUB_REPOSITORY/actions/artifacts/$ARTIFACT_ID" ]] || return 91
        [[ ! -e "$DELETED" ]] || return 44
        printf '%s\\n' "$METADATA"
    elif [[ "$#" == 4 && "$1 $2 $3" == "api --method DELETE" ]]; then
        [[ "$4" == "repos/$GITHUB_REPOSITORY/actions/artifacts/$ARTIFACT_ID" ]] || return 92
        touch "$DELETED"
        printf 'delete\\n' >> "$TRACE"
    else
        return 93
    fi
}
docker() {
    [[ "$#" == 1 && "$1" == load ]] || return 94
    cat >/dev/null
    printf 'load\\n' >> "$TRACE"
}
"""
    name = f"{prefix}-run-101-attempt-2.tar.gz"
    metadata = {
        "id": 17,
        "name": name,
        "workflow_run": {"id": 101},
        "expired": False,
    }
    head = "a" * 40
    cases = (
        ("valid", {}, {}),
        ("invalid-id", {}, {"ARTIFACT_ID": "17,18"}),
        ("empty-id", {}, {"ARTIFACT_ID": ""}),
        ("different-id", {"id": 18}, {}),
        ("other-run", {"workflow_run": {"id": 102}}, {}),
        ("previous-attempt", {"name": f"{prefix}-run-101-attempt-1.tar.gz"}, {}),
        ("expired", {"expired": True}, {}),
        ("missing-run", {"workflow_run": None}, {}),
        ("wrong-head", {}, {"BUILD_HEAD": "b" * 40}),
        ("wrong-pr", {}, {"BUILD_PR": "9"}),
        ("wrong-comment", {}, {"BUILD_COMMENT": "9"}),
        ("wrong-short", {}, {"BUILD_SHORT": "b" * 12}),
    )

    def run(script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-c", stub + script],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    for label, metadata_override, env_override in cases:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace = root / "trace"
            deleted = root / "deleted"
            env = {
                "PATH": os.environ["PATH"],
                "GITHUB_REPOSITORY": "fixture/repository",
                "GITHUB_RUN_ID": "101",
                "GITHUB_RUN_ATTEMPT": "2",
                "RUNNER_TEMP": temporary,
                "ARTIFACT_ID": "17",
                "AUTHORIZED_PR": "3",
                "AUTHORIZED_HEAD": head,
                "AUTHORIZED_COMMENT": "5",
                "BUILD_PR": "3",
                "BUILD_HEAD": head,
                "BUILD_COMMENT": "5",
                "BUILD_SHORT": head[:12],
                "METADATA": json.dumps({**metadata, **metadata_override}),
                "TRACE": str(trace),
                "DELETED": str(deleted),
                **env_override,
            }
            for guard in guards:
                trace.unlink(missing_ok=True)
                transfer = root / "candidate-transfer"
                if transfer.exists():
                    transfer.rmdir()
                result = run(guard + 'printf "download\\n" >> "$TRACE"\n', env)
                assert (result.returncode == 0) == (label == "valid"), (
                    label,
                    result.stderr,
                )
                assert trace.exists() == (label == "valid"), label

            # Cleanup intentionally authenticates the artifact, not build outputs.
            # It must never delete another run's or attempt's artifact.
            trace.unlink(missing_ok=True)
            result = run(cleanup[0], env)
            cleanup_allowed = label == "valid" or label.startswith("wrong-")
            assert (result.returncode == 0) == cleanup_allowed, (label, result.stderr)
            assert deleted.exists() == cleanup_allowed, label
            if cleanup_allowed:
                assert trace.read_text() == "delete\n"
                assert run(cleanup[0], env).returncode != 0
                assert trace.read_text() == "delete\n"

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        transfer = root / "candidate-transfer"
        transfer.mkdir()
        archive = transfer / name
        payload = gzip.compress(b"synthetic candidate fixture")
        archive.write_bytes(payload)
        trace = root / "trace"
        env = {
            "PATH": os.environ["PATH"],
            "RUNNER_TEMP": temporary,
            "GITHUB_RUN_ID": "101",
            "GITHUB_RUN_ATTEMPT": "2",
            "GITHUB_OUTPUT": str(root / "outputs"),
            "TRACE": str(trace),
            "EXPECTED_SHA256": hashlib.sha256(payload).hexdigest(),
        }
        for script in hash_steps:
            trace.unlink(missing_ok=True)
            assert run(script, env).returncode == 0
            assert trace.read_text() == "load\n"
            trace.unlink()
            assert run(script, {**env, "EXPECTED_SHA256": "0" * 64}).returncode != 0
            assert not trace.exists()
            archive.unlink()
            assert run(script, env).returncode != 0
            assert not trace.exists()
            archive.write_bytes(payload)


def test_candidate_artifact_guards_fail_closed() -> None:
    _exercise_candidate_artifact_guards(_read(WORKER_PATH), "candidate-image")
