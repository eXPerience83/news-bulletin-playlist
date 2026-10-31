"""Real SQLite/WAL restore acceptance, isolated from production."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
from test_v11_shadow_staging import _two_roots

from news_bulletin_playlist.offline_dataset_inventory import inventory_offline_dataset
from news_bulletin_playlist.offline_sqlite_backup import (
    RUNTIME_SQLITE_FILENAME,
    OfflineSQLiteBackupError,
    inspect_offline_sqlite_backup,
)


def _real_wal_pair(tmp_path: Path) -> tuple[Path, Path, sqlite3.Connection]:
    root, backup = _two_roots(tmp_path)
    writer = sqlite3.connect(root / RUNTIME_SQLITE_FILENAME)
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE wal_only (id INTEGER PRIMARY KEY, data TEXT NOT NULL)")
    writer.execute("INSERT INTO wal_only(data) VALUES ('committed row')")
    writer.commit()
    assert (root / (RUNTIME_SQLITE_FILENAME + "-wal")).stat().st_size > 0
    for ending in ("", "-wal", "-shm"):
        file = root / (RUNTIME_SQLITE_FILENAME + ending)
        if file.exists():
            shutil.copy2(file, backup / file.name)
    assert inventory_offline_dataset(root) == inventory_offline_dataset(backup)
    return root, backup, writer


def test_real_uncheckpointed_wal_replays_in_scratch_without_original_writes(
    tmp_path: Path,
) -> None:
    root, backup, writer = _real_wal_pair(tmp_path)
    try:
        before_root = inventory_offline_dataset(root)
        before_backup = inventory_offline_dataset(backup)
        inspected = inspect_offline_sqlite_backup(root, backup, operator_asserts_offline=True)
        assert inspected.wal_present
        assert inspected.schema_table_count >= 1
        assert not inspected.permits_cutover
        assert inventory_offline_dataset(root) == before_root
        assert inventory_offline_dataset(backup) == before_backup
    finally:
        writer.close()


def test_missing_ack_and_mismatched_backup_fail_closed(tmp_path: Path) -> None:
    root, backup, writer = _real_wal_pair(tmp_path)
    try:
        with pytest.raises(OfflineSQLiteBackupError, match="assertion"):
            inspect_offline_sqlite_backup(root, backup)
        (backup / "custom-covers" / "image.jpg").write_bytes(b"tampered")
        with pytest.raises(OfflineSQLiteBackupError, match="does not match"):
            inspect_offline_sqlite_backup(root, backup, operator_asserts_offline=True)
    finally:
        writer.close()


def test_missing_sqlite_file_fails_on_matching_dataset(tmp_path: Path) -> None:
    root, backup = _two_roots(tmp_path)
    with pytest.raises(OfflineSQLiteBackupError, match="database missing"):
        inspect_offline_sqlite_backup(root, backup, operator_asserts_offline=True)


def test_matching_corrupt_sqlite_fails_without_original_modification(tmp_path: Path) -> None:
    root, backup = _two_roots(tmp_path)
    for directory in (root, backup):
        (directory / RUNTIME_SQLITE_FILENAME).write_bytes(b"not a sqlite database")
    before = inventory_offline_dataset(backup)
    with pytest.raises(OfflineSQLiteBackupError, match="could not be read"):
        inspect_offline_sqlite_backup(root, backup, operator_asserts_offline=True)
    assert inventory_offline_dataset(backup) == before


def test_independent_offline_restore_remains_readable(tmp_path: Path) -> None:
    root, backup, writer = _real_wal_pair(tmp_path)
    try:
        restored = tmp_path / "restored-independent-root"
        shutil.copytree(backup, restored)
        assert inventory_offline_dataset(restored) == inventory_offline_dataset(root)
        accepted = inspect_offline_sqlite_backup(restored, backup, operator_asserts_offline=True)
        assert accepted.schema_table_count >= 1
        assert not accepted.permits_cutover
    finally:
        writer.close()


def test_rollback_journal_rejected(tmp_path: Path) -> None:
    root, backup, writer = _real_wal_pair(tmp_path)
    try:
        for directory in (root, backup):
            (directory / (RUNTIME_SQLITE_FILENAME + "-journal")).write_bytes(b"pending")
        with pytest.raises(OfflineSQLiteBackupError, match="rollback journal"):
            inspect_offline_sqlite_backup(root, backup, operator_asserts_offline=True)
    finally:
        writer.close()


def test_checkpointed_sqlite_without_wal_is_valid(tmp_path: Path) -> None:
    root, backup = _two_roots(tmp_path)
    database = root / RUNTIME_SQLITE_FILENAME
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE checkpointed (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO checkpointed VALUES (1)")
    shutil.copy2(database, backup / RUNTIME_SQLITE_FILENAME)
    accepted = inspect_offline_sqlite_backup(root, backup, operator_asserts_offline=True)
    assert not accepted.wal_present
    assert accepted.schema_table_count >= 1
    assert not accepted.permits_cutover


def test_orphan_shared_memory_rejected(tmp_path: Path) -> None:
    root, backup = _two_roots(tmp_path)
    for directory in (root, backup):
        (directory / RUNTIME_SQLITE_FILENAME).write_bytes(b"not actually SQLite")
        (directory / (RUNTIME_SQLITE_FILENAME + "-shm")).write_bytes(b"orphan")
    with pytest.raises(OfflineSQLiteBackupError, match="orphan"):
        inspect_offline_sqlite_backup(root, backup, operator_asserts_offline=True)
