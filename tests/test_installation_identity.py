"""Synthetic, strictly read-only installation detection tests for #552."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from news_bulletin_playlist.installation_identity import (
    INSTALLATION_IDENTITY_FILENAME,
    InstallationDisposition,
    InstallationIdentityError,
    InstallationInspection,
    inspect_installation_root,
)
from news_bulletin_playlist.managed_state import MANAGED_STATE_FILENAME
from news_bulletin_playlist.managed_provisioning import MANAGED_PROVISIONING_FILENAME
from news_bulletin_playlist.persistence import DEFAULT_DB_FILENAME
from news_bulletin_playlist.spotify.auth import SPOTIFY_AUTH_FILENAME

_ID = "123e4567-e89b-42d3-a456-426614174000"


def _write_marker(root: Path, *, mode: str = "opt_in") -> Path:
    marker = root / INSTALLATION_IDENTITY_FILENAME
    marker.write_text(
        json.dumps({"schema_version": 1, "installation_id": _ID, "mode": mode}),
        encoding="utf-8",
    )
    return marker


def test_nonexistent_and_empty_root_are_only_unclaimed_candidates(tmp_path: Path) -> None:
    absent = tmp_path / "absent"
    expected = InstallationInspection(InstallationDisposition.EMPTY_UNCLAIMED)
    assert inspect_installation_root(absent) == expected
    assert not absent.exists()
    assert inspect_installation_root(tmp_path) == expected
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "entry",
    [
        MANAGED_STATE_FILENAME,
        DEFAULT_DB_FILENAME,
        SPOTIFY_AUTH_FILENAME,
        MANAGED_PROVISIONING_FILENAME,
        "custom-covers",
        ".unknown-hidden",
        "unexpected-journal.tmp",
    ],
)
def test_any_unclaimed_durable_entry_keeps_legacy_compatibility(
    tmp_path: Path, entry: str
) -> None:
    path = tmp_path / entry
    if entry == "custom-covers":
        path.mkdir()
    else:
        path.write_bytes(b"legacy")
    before = tuple(tmp_path.iterdir())
    assert inspect_installation_root(tmp_path) == InstallationInspection(
        InstallationDisposition.LEGACY_COMPATIBLE
    )
    assert tuple(tmp_path.iterdir()) == before


def test_broken_symlink_is_not_mistaken_for_empty_root(tmp_path: Path) -> None:
    (tmp_path / "old-oauth").symlink_to(tmp_path / "missing")
    result = inspect_installation_root(tmp_path)
    assert result.disposition is InstallationDisposition.LEGACY_COMPATIBLE


def test_symlink_root_and_symlink_marker_fail_closed(tmp_path: Path) -> None:
    link = tmp_path / "linked-root"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(InstallationIdentityError, match="real directory"):
        inspect_installation_root(link)

    marker = tmp_path / INSTALLATION_IDENTITY_FILENAME
    marker.symlink_to(tmp_path / "missing")
    with pytest.raises(InstallationIdentityError, match="regular file"):
        inspect_installation_root(tmp_path)


def test_directory_at_marker_path_fails_closed(tmp_path: Path) -> None:
    (tmp_path / INSTALLATION_IDENTITY_FILENAME).mkdir()
    with pytest.raises(InstallationIdentityError, match="regular file"):
        inspect_installation_root(tmp_path)


def test_valid_marker_is_stable_even_after_sqlite_and_restart(tmp_path: Path) -> None:
    _write_marker(tmp_path)
    first = inspect_installation_root(tmp_path)
    assert first == InstallationInspection(InstallationDisposition.CLAIMED_OPT_IN, _ID)
    (tmp_path / DEFAULT_DB_FILENAME).write_bytes(b"synthetic sqlite")
    assert inspect_installation_root(tmp_path) == first


def test_explicit_legacy_marker_preserves_legacy_disposition(tmp_path: Path) -> None:
    _write_marker(tmp_path, mode="legacy_compatible")
    assert inspect_installation_root(tmp_path) == InstallationInspection(
        InstallationDisposition.CLAIMED_LEGACY, _ID
    )


@pytest.mark.parametrize(
    "payload",
    [
        '{"schema_version":1,"schema_version":1,"installation_id":"' + _ID
        + '","mode":"opt_in"}',
        "{",
        "[]",
        "{}",
        '{"schema_version":true,"installation_id":"' + _ID + '","mode":"opt_in"}',
        '{"schema_version":2,"installation_id":"' + _ID + '","mode":"opt_in"}',
        '{"schema_version":1,"installation_id":"abc","mode":"opt_in"}',
        '{"schema_version":1,"installation_id":"' + _ID.upper() + '","mode":"opt_in"}',
        '{"schema_version":1,"installation_id":"' + _ID + '","mode":"unknown"}',
        '{"schema_version":1,"installation_id":"' + _ID
        + '","mode":"opt_in","extra":true}',
    ],
)
def test_invalid_or_ambiguous_marker_never_selects_opt_in(
    tmp_path: Path, payload: str
) -> None:
    marker = tmp_path / INSTALLATION_IDENTITY_FILENAME
    marker.write_text(payload, encoding="utf-8")
    original = marker.read_bytes()
    with pytest.raises(InstallationIdentityError):
        inspect_installation_root(tmp_path)
    assert marker.read_bytes() == original


def test_non_utf8_marker_fails_closed_without_rewriting(tmp_path: Path) -> None:
    marker = tmp_path / INSTALLATION_IDENTITY_FILENAME
    marker.write_bytes(b"\xff")
    with pytest.raises(InstallationIdentityError, match="invalid"):
        inspect_installation_root(tmp_path)
    assert marker.read_bytes() == b"\xff"
