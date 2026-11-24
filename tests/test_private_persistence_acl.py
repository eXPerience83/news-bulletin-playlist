"""Regression for ACL inheritance overriding requested creation modes."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from _legacy_dataset import legacy_root

from news_bulletin_playlist.inspection_fence import (
    INSPECTION_FILENAME,
    RELEASE_CONFIRMATION,
    InspectionGateError,
    inspect_gate,
    release_gate,
    startup_gate,
)
from news_bulletin_playlist.private_persistence import open_private_directory
from news_bulletin_playlist.upgrade_inspection_report import upgrade_inspection_report
from news_bulletin_playlist.v11_operational_storage import V11OperationalStore


@pytest.fixture
def inherited_acl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model NFSv4 passthrough: requested private creation returns 0770."""
    original_open = os.open
    original_mkdir = Path.mkdir

    def widened_open(path, flags, mode=0o777, *, dir_fd=None):
        fd = original_open(path, flags, mode, dir_fd=dir_fd)
        if flags & os.O_CREAT and flags & os.O_EXCL and mode == 0o600:
            os.fchmod(fd, 0o770)
        return fd

    def widened_mkdir(path, mode=0o777, parents=False, exist_ok=False):
        existed = path.exists()
        original_mkdir(path, mode, parents, exist_ok)
        if mode == 0o700 and not existed:
            path.chmod(0o770)

    monkeypatch.setattr(os, "open", widened_open)
    monkeypatch.setattr(Path, "mkdir", widened_mkdir)


def test_migration_and_release_under_inherited_acl(tmp_path: Path, inherited_acl: None) -> None:
    root, _ = legacy_root(tmp_path, 10)
    assert startup_gate(root, requested=True)
    store = V11OperationalStore(root)
    store.initialize_from_legacy()
    report = upgrade_inspection_report(root)
    assert report["safe_to_review_without_spotify_writes"] is True
    for path in (
        root / INSPECTION_FILENAME,
        root / ".managed-state-v11.authority",
        root / "managed-state-v11.json",
        root / "custom-covers",
        root / "migrated-covers",
        root / "migrated-covers" / "index.json",
        *(root / "custom-covers").glob("*.jpg"),
    ):
        assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
    assert startup_gate(root, requested=False)
    snapshot = inspect_gate(root)
    released = release_gate(
        root, expected_sha256=snapshot.state_sha256, confirm=RELEASE_CONFIRMATION
    )
    assert released.state == "released"
    assert not startup_gate(root, requested=False)
    assert (root / INSPECTION_FILENAME).stat().st_mode & 0o077 == 0


def test_failed_release_privacy_preserves_held_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = legacy_root(tmp_path, 10)
    startup_gate(root, requested=True)
    V11OperationalStore(root).initialize_from_legacy()
    snapshot = inspect_gate(root)
    marker = root / INSPECTION_FILENAME
    before = marker.read_bytes()
    original_open = os.open
    original_chmod = os.fchmod

    def widened_open(path, flags, mode=0o777, *, dir_fd=None):
        fd = original_open(path, flags, mode, dir_fd=dir_fd)
        if str(path).startswith(".spotify-inspection-") and flags & os.O_EXCL:
            original_chmod(fd, 0o770)
        return fd

    def ineffective_chmod(fd: int, mode: int) -> None:
        pass

    monkeypatch.setattr(os, "open", widened_open)
    monkeypatch.setattr(os, "fchmod", ineffective_chmod)
    with pytest.raises(InspectionGateError):
        release_gate(root, expected_sha256=snapshot.state_sha256, confirm=RELEASE_CONFIRMATION)
    assert marker.read_bytes() == before
    assert inspect_gate(root).held
    assert not list(root.glob(".spotify-inspection-*.tmp"))


def test_existing_insecure_directory_is_not_silently_repaired(tmp_path: Path) -> None:
    path = tmp_path / "custom-covers"
    path.mkdir()
    path.chmod(0o770)
    with pytest.raises(PermissionError):
        open_private_directory(path, create=True)
    assert stat.S_IMODE(path.stat().st_mode) == 0o770
