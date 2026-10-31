"""A rollback baseline must include every synthetic /data file, not just managed JSON."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from test_legacy_catalog_snapshot import _root

from news_bulletin_playlist.legacy_catalog_snapshot import write_legacy_snapshot_v2
from news_bulletin_playlist.offline_dataset_inventory import (
    DatasetInventoryError,
    inventory_offline_dataset,
)
from news_bulletin_playlist.v11_candidate_storage import publish_v11_candidate


def _synthetic_dataset(tmp_path: Path) -> Path:
    root, preview = _root(tmp_path)
    write_legacy_snapshot_v2(root, preview)
    publish_v11_candidate(root)
    (root / "history.sqlite3-wal").write_bytes(b"synthetic WAL records")
    (root / "history.sqlite3-shm").write_bytes(b"synthetic shared memory")
    nested = root / "custom-covers" / "uploads"
    nested.mkdir(parents=True)
    (nested / "own-cover.jpg").write_bytes(b"synthetic jpeg asset")
    (root / "pending-provisioning.json").write_bytes(b"synthetic journal")
    (root / "oauth-private.json").write_bytes(b"synthetic OAuth")
    return root


def test_full_data_inventory_matches_independent_offline_restore(tmp_path: Path) -> None:
    root = _synthetic_dataset(tmp_path)
    before = inventory_offline_dataset(root)
    names = {item.path for item in before.files}
    assert {
        "managed-state.json",
        "managed-state-v11.candidate.json",
        "frozen-legacy-catalog.v2.json",
        "history.sqlite3",
        "history.sqlite3-wal",
        "history.sqlite3-shm",
        "oauth-private.json",
        "pending-provisioning.json",
        "custom-covers/uploads/own-cover.jpg",
    }.issubset(names)
    assert before.total_bytes == sum(item.size for item in before.files)
    assert before.authorizes_migration is False

    backup = tmp_path / "offline-backup"
    shutil.copytree(root, backup)
    assert inventory_offline_dataset(backup) == before

    # Simulate interrupted/wrong migration on a disposable dataset only.
    (root / "managed-state.json").write_bytes(b"broken v11 cutover")
    (root / "history.sqlite3-wal").unlink()
    (root / "custom-covers" / "uploads" / "own-cover.jpg").write_bytes(b"corrupted cover")
    assert inventory_offline_dataset(root) != before

    restored = tmp_path / "restored-copy"
    shutil.copytree(backup, restored)
    assert inventory_offline_dataset(restored) == before
    assert inventory_offline_dataset(backup) == before


def test_symlink_and_special_files_fail_closed(tmp_path: Path) -> None:
    root = _synthetic_dataset(tmp_path)
    linked = root / "linked"
    linked.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(DatasetInventoryError, match="unsafe"):
        inventory_offline_dataset(root)
    linked.unlink()

    data_link = root / "stealth-secret.json"
    data_link.symlink_to(tmp_path / "outside")
    with pytest.raises(DatasetInventoryError, match="unsafe"):
        inventory_offline_dataset(root)
    data_link.unlink()

    os.mkfifo(root / "unexpected.fifo")
    with pytest.raises(DatasetInventoryError, match="unsafe"):
        inventory_offline_dataset(root)


def test_missing_and_symlink_root_rejected(tmp_path: Path) -> None:
    with pytest.raises(DatasetInventoryError, match="root"):
        inventory_offline_dataset(tmp_path / "missing")
    linked = tmp_path / "linked"
    linked.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(DatasetInventoryError, match="real directory"):
        inventory_offline_dataset(linked)


def test_mutated_file_during_hashing_fails_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _synthetic_dataset(tmp_path)
    source = root / "managed-state.json"
    original_read = os.read
    changed = False
    source_inode = source.stat().st_ino

    def change_after_read(fd: int, nbytes: int) -> bytes:
        nonlocal changed
        result = original_read(fd, nbytes)
        if not changed and os.fstat(fd).st_ino == source_inode:
            changed = True
            with source.open("ab") as stream:
                stream.write(b"changed during inventory")
        return result

    monkeypatch.setattr(os, "read", change_after_read)
    with pytest.raises(DatasetInventoryError, match="changed"):
        inventory_offline_dataset(root)


def test_dataset_inventory_does_not_create_files(tmp_path: Path) -> None:
    root = _synthetic_dataset(tmp_path)
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert inventory_offline_dataset(root) == inventory_offline_dataset(root)
    assert {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    } == before


def test_backup_with_equal_bytes_but_different_file_or_directory_mode(
    tmp_path: Path,
) -> None:
    root = _synthetic_dataset(tmp_path)
    original = inventory_offline_dataset(root)
    backup = tmp_path / "metadata-backup"
    shutil.copytree(root, backup)
    assert inventory_offline_dataset(backup) == original

    file = backup / "custom-covers" / "uploads" / "own-cover.jpg"
    file.chmod(file.stat().st_mode ^ 0o100)
    assert inventory_offline_dataset(backup) != original
    shutil.rmtree(backup)
    shutil.copytree(root, backup)
    directory = backup / "custom-covers"
    directory.chmod(directory.stat().st_mode ^ 0o001)
    assert inventory_offline_dataset(backup) != original


def test_backup_xattr_fidelity_includes_files_and_directories(
    tmp_path: Path,
) -> None:
    root = _synthetic_dataset(tmp_path)
    cover = root / "custom-covers" / "uploads" / "own-cover.jpg"
    directory = root / "custom-covers"
    name = "user.nbp-migration"
    try:
        os.setxattr(cover, name, b"secret-test-value")
        os.setxattr(directory, name, b"directory-value")
    except OSError as exc:
        pytest.skip(f"user xattrs not supported in test filesystem: {exc}")
    original = inventory_offline_dataset(root)
    assert "secret-test-value" not in repr(original)
    backup = tmp_path / "metadata-backup"
    shutil.copytree(root, backup)
    assert inventory_offline_dataset(backup) == original
    os.setxattr(backup / "custom-covers", name, b"changed")
    assert inventory_offline_dataset(backup) != original
    os.setxattr(backup / "custom-covers", name, b"directory-value")
    assert inventory_offline_dataset(backup) == original
    os.setxattr(backup / "custom-covers" / "uploads" / "own-cover.jpg", name, b"changed")
    assert inventory_offline_dataset(backup) != original
