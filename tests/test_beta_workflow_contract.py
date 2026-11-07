"""Immutable beta publication never advances mutable image channels."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
BETA_PATH = ROOT / ".github/workflows/publish-beta-amd64.yml"


def test_beta_publication_is_manual_main_only_with_explicit_sha() -> None:
    data = yaml.load(BETA_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(data, dict)
    assert set(data["on"]) == {"workflow_dispatch"}
    explicit = data["on"]["workflow_dispatch"]["inputs"]["source_sha"]
    assert explicit["required"] == "true"
    assert explicit["type"] == "string"
    assert data["permissions"] == {"contents": "read"}

    job = data["jobs"]["publish"]
    assert job["permissions"] == {"contents": "read", "packages": "write"}
    assert data["concurrency"] == {"group": "publish-beta-amd64", "cancel-in-progress": "false"}

    scripts = {step["name"]: step["run"] for step in job["steps"] if "run" in step}
    owner_guard = scripts["Require repository owner to authorize beta publication"]
    owner_step = next(
        step
        for step in job["steps"]
        if step.get("name") == "Require repository owner to authorize beta publication"
    )
    assert "github.actor" in owner_step["env"]["DISPATCH_ACTOR"]
    assert "github.repository_owner" in owner_step["env"]["REPOSITORY_OWNER"]
    assert '"$DISPATCH_ACTOR" != "$REPOSITORY_OWNER"' in owner_guard
    main_guard = scripts["Require main branch"]
    metadata = scripts["Resolve publication metadata"]
    prebuild = scripts["Refuse a queued stale main revision before building"]
    prepublish = scripts["Require source revision to remain current main"]

    assert '"refs/heads/main"' in main_guard
    assert "INPUT_SOURCE_SHA" in metadata
    assert '"$INPUT_SOURCE_SHA" != "$GITHUB_SHA"' in metadata
    assert 'version = "0.3.0b1"' in metadata
    assert '__version__ = "0.3.0b1"' in metadata
    assert "branches/main" in prebuild
    assert "branches/main" in prepublish


def test_beta_build_and_published_digest_are_exact_and_no_alias_moves() -> None:
    data = yaml.load(BETA_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(data, dict)
    steps = data["jobs"]["publish"]["steps"]
    scripts = {step["name"]: step["run"] for step in steps if "run" in step}

    build = scripts["Build exact beta candidate"]
    smoke = scripts["Smoke test exact beta candidate"]
    immutable = scripts["Publish or reuse immutable beta SHA tag"]
    summary = scripts["Validate immutable beta reference and publish digest summary"]
    all_scripts = "\n".join(scripts.values())

    assert '--build-arg "SOURCE_REVISION=${GITHUB_SHA}"' in build
    assert '--label "org.opencontainers.image.version=0.3.0b1"' in build
    for token in ("--read-only", "--cap-drop ALL", "--security-opt no-new-privileges:true"):
        assert token in smoke
    assert 'beta_tag="beta-0.3.0b1-${SOURCE_SHA}"' in immutable
    assert 'resolved_ref="${image}@${digest}"' in immutable
    assert "local_id" in immutable and "resolved_id" in immutable
    assert 'resolved="$(docker buildx imagetools inspect' in summary
    assert "IMAGE_DIGEST" in summary
    assert "docker buildx imagetools create" not in all_scripts
    for forbidden in (
        ':edge"',
        ':edge-amd64"',
        ':stable"',
        ':stable-amd64"',
        ':latest"',
        ':dev"',
        ':dev-amd64"',
    ):
        assert forbidden not in all_scripts
