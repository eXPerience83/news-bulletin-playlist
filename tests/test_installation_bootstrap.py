"""Isolated contract tests for the future, not-yet-enabled root bootstrap."""

from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from news_bulletin_playlist.installation_bootstrap import claim_pristine_installation_root
from news_bulletin_playlist.installation_identity import (
    INSTALLATION_IDENTITY_FILENAME,
    InstallationDisposition,
    InstallationIdentityError,
    inspect_installation_root,
)
from news_bulletin_playlist.managed_provisioning import MANAGED_PROVISIONING_FILENAME
from news_bulletin_playlist.managed_state import MANAGED_STATE_FILENAME
from news_bulletin_playlist.persistence import DEFAULT_DB_FILENAME
from news_bulletin_playlist.spotify.auth import SPOTIFY_AUTH_FILENAME


def test_empty_root_claim_is_durable_unique_and_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "fresh"
    root.mkdir()
    first = claim_pristine_installation_root(root)
    assert first.disposition is InstallationDisposition.CLAIMED_OPT_IN
    assert first.installation_id is not None
    marker = root / INSTALLATION_IDENTITY_FILENAME
    assert {p.name for p in root.iterdir()} == {INSTALLATION_IDENTITY_FILENAME}
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert json.loads(marker.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "installation_id": first.installation_id,
        "mode": "opt_in",
    }
    original = marker.read_bytes()
    assert inspect_installation_root(root) == first
    assert claim_pristine_installation_root(root) == first
    assert marker.read_bytes() == original


def test_missing_and_symlink_roots_fail_without_creation(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(InstallationIdentityError, match="existing real directory"):
        claim_pristine_installation_root(missing)
    assert not missing.exists()
    linked = tmp_path / "linked"
    linked.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(InstallationIdentityError, match="existing real directory"):
        claim_pristine_installation_root(linked)


@pytest.mark.parametrize(
    "entry",
    [
        MANAGED_STATE_FILENAME,
        DEFAULT_DB_FILENAME,
        SPOTIFY_AUTH_FILENAME,
        MANAGED_PROVISIONING_FILENAME,
        "custom-covers",
        ".hidden",
        ".installation-identity-stranded.tmp",
    ],
)
def test_preexisting_data_and_missing_managed_state_remain_legacy(
    tmp_path: Path, entry: str
) -> None:
    path = tmp_path / entry
    if entry == "custom-covers":
        path.mkdir()
    else:
        path.write_bytes(b"private preserved legacy evidence")
    original = {p.name: p.read_bytes() if p.is_file() else None for p in tmp_path.iterdir()}
    inspection = claim_pristine_installation_root(tmp_path)
    assert inspection.disposition is InstallationDisposition.LEGACY_COMPATIBLE
    assert {p.name: p.read_bytes() if p.is_file() else None for p in tmp_path.iterdir()} == original
    assert not (tmp_path / INSTALLATION_IDENTITY_FILENAME).exists()


def test_invalid_and_existing_markers_are_never_rewritten(tmp_path: Path) -> None:
    marker = tmp_path / INSTALLATION_IDENTITY_FILENAME
    marker.write_text("invalid", encoding="utf-8")
    original = marker.read_bytes()
    with pytest.raises(InstallationIdentityError):
        claim_pristine_installation_root(tmp_path)
    assert marker.read_bytes() == original


def test_existing_legacy_marker_cannot_be_converted_to_opt_in(tmp_path: Path) -> None:
    marker = tmp_path / INSTALLATION_IDENTITY_FILENAME
    marker.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "installation_id": "123e4567-e89b-42d3-a456-426614174000",
                "mode": "legacy_compatible",
            }
        ),
        encoding="utf-8",
    )
    before = marker.read_bytes()
    result = claim_pristine_installation_root(tmp_path)
    assert result.disposition is InstallationDisposition.CLAIMED_LEGACY
    assert marker.read_bytes() == before


def test_two_contending_bootstrappers_observe_one_identity(tmp_path: Path) -> None:
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = tuple(pool.map(claim_pristine_installation_root, (tmp_path, tmp_path)))
    assert first == second
    assert first.disposition is InstallationDisposition.CLAIMED_OPT_IN
    assert {p.name for p in tmp_path.iterdir()} == {INSTALLATION_IDENTITY_FILENAME}


def test_failed_exclusive_publication_cannot_leave_partial_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject_link(*args: object, **kwargs: object) -> None:
        raise OSError("synthetic link failure")

    monkeypatch.setattr(os, "link", reject_link)
    with pytest.raises(InstallationIdentityError, match="bootstrap failed"):
        claim_pristine_installation_root(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_racing_foreign_file_blocks_opt_in_and_is_never_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = os.fsync
    inserted = False

    def inject_durable_evidence(fd: int) -> None:
        nonlocal inserted
        if not inserted and stat.S_ISREG(os.fstat(fd).st_mode):
            inserted = True
            (tmp_path / "unexpected.sqlite3").write_bytes(b"legacy")
        original(fd)

    monkeypatch.setattr(os, "fsync", inject_durable_evidence)
    with pytest.raises(InstallationIdentityError, match="data appeared"):
        claim_pristine_installation_root(tmp_path)
    assert (tmp_path / "unexpected.sqlite3").read_bytes() == b"legacy"
    assert not (tmp_path / INSTALLATION_IDENTITY_FILENAME).exists()
    assert claim_pristine_installation_root(tmp_path).disposition is (
        InstallationDisposition.LEGACY_COMPATIBLE
    )


def test_uncertain_after_link_failure_does_not_blindly_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = os.fsync
    once = False

    def fail_first_directory_fsync(fd: int) -> None:
        nonlocal once
        if not once and stat.S_ISDIR(os.fstat(fd).st_mode):
            once = True
            raise OSError("simulated fsync uncertainty")
        original(fd)

    monkeypatch.setattr(os, "fsync", fail_first_directory_fsync)
    with pytest.raises(InstallationIdentityError, match="bootstrap failed"):
        claim_pristine_installation_root(tmp_path)
    # Marker can already be visible even when durable publication was uncertain.
    # It is valid and cannot be overwritten or assigned a new identity on retry.
    marker = tmp_path / INSTALLATION_IDENTITY_FILENAME
    assert marker.is_file()
    first = inspect_installation_root(tmp_path)
    assert first.disposition is InstallationDisposition.CLAIMED_OPT_IN
    assert claim_pristine_installation_root(tmp_path) == first
