"""Read-only offline SQLite backup acceptance on disposable copies only.

This standalone helper never opens the original database with SQLite, never
selects v11, and cannot prove process quiescence or external snapshot atomicity.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from news_bulletin_playlist.offline_dataset_inventory import (
    DatasetFileEvidence,
    OfflineDatasetInventory,
)
from news_bulletin_playlist.v11_shadow_staging import (
    V11ShadowStageError,
    _verified_offline_inventory,
)

RUNTIME_SQLITE_FILENAME = "news-bulletin-playlist-v1.sqlite3"
_COPY_BLOCK_SIZE = 1024 * 1024


class OfflineSQLiteBackupError(ValueError):
    """Missing or unverified backup evidence blocks migration."""


@dataclass(frozen=True, slots=True)
class OfflineSQLiteBackupInspection:
    baseline: OfflineDatasetInventory
    sqlite_sha256: str
    wal_present: bool
    schema_table_count: int

    @property
    def permits_cutover(self) -> bool:
        return False


def _copy_verified(
    directory_fd: int, name: str, destination: Path, expected: DatasetFileEvidence
) -> None:
    """Copy from a verified fd, rejecting symlink swaps and changed input bytes."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size != expected.size
                or stat.S_IMODE(before.st_mode) != expected.mode
                or (before.st_uid, before.st_gid, before.st_nlink)
                != (expected.uid, expected.gid, expected.nlink)
            ):
                raise OfflineSQLiteBackupError("SQLite backup file metadata changed")
            digest = hashlib.sha256()
            out_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                while chunk := os.read(fd, _COPY_BLOCK_SIZE):
                    digest.update(chunk)
                    remaining = memoryview(chunk)
                    while remaining:
                        written = os.write(out_fd, remaining)
                        if written == 0:
                            raise OfflineSQLiteBackupError("incomplete SQLite copy")
                        remaining = remaining[written:]
                os.fsync(out_fd)
            finally:
                os.close(out_fd)
            after = os.fstat(fd)
            if (
                (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                    before.st_mode,
                    before.st_uid,
                    before.st_gid,
                )
                != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                    after.st_mode,
                    after.st_uid,
                    after.st_gid,
                )
                or digest.hexdigest() != expected.sha256
            ):
                raise OfflineSQLiteBackupError("SQLite backup changed during verified copy")
        finally:
            os.close(fd)
    except OSError as exc:
        raise OfflineSQLiteBackupError("could not safely copy offline SQLite evidence") from exc


def _check_disposable_sqlite(path: Path) -> int:
    """Check SQLite only in scratch, where WAL recovery may write sidecars."""
    try:
        with closing(
            sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True, timeout=1)
        ) as connection:
            connection.execute("PRAGMA trusted_schema=OFF")
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise OfflineSQLiteBackupError("restored SQLite integrity check failed")
            row = connection.execute(
                "SELECT COUNT(*) FROM sqlite_schema WHERE type = 'table'"
            ).fetchone()
            if row is None or not isinstance(row[0], int) or row[0] < 1:
                raise OfflineSQLiteBackupError("restored SQLite schema is empty")
            return row[0]
    except sqlite3.DatabaseError as exc:
        raise OfflineSQLiteBackupError("restored SQLite could not be read consistently") from exc


def inspect_offline_sqlite_backup(
    root: Path, backup: Path, *, operator_asserts_offline: bool = False
) -> OfflineSQLiteBackupInspection:
    """Verify known runtime SQLite on scratch after a matching full-/data backup.

    This never proves the service was actually stopped or WAL fully captured.
    Neither source nor backup is writable by this function.
    """
    if not operator_asserts_offline:
        raise OfflineSQLiteBackupError("explicit offline operator assertion required")
    try:
        baseline = _verified_offline_inventory(root, backup)
    except V11ShadowStageError as exc:
        raise OfflineSQLiteBackupError("offline backup does not match source") from exc
    by_name = {item.path: item for item in baseline.files}
    main = by_name.get(RUNTIME_SQLITE_FILENAME)
    if main is None:
        raise OfflineSQLiteBackupError("runtime SQLite database missing from backup")
    wal_name = f"{RUNTIME_SQLITE_FILENAME}-wal"
    if f"{RUNTIME_SQLITE_FILENAME}-journal" in by_name:
        raise OfflineSQLiteBackupError("unresolved SQLite rollback journal in backup")
    if f"{RUNTIME_SQLITE_FILENAME}-shm" in by_name and wal_name not in by_name:
        raise OfflineSQLiteBackupError("orphan SQLite shared-memory file in backup")
    # -shm is a derived shared-memory index; SQLite rebuilds it on scratch.
    try:
        with tempfile.TemporaryDirectory(prefix="nbp-sqlite-restore-") as folder:
            scratch = Path(folder)
            backup_fd = os.open(backup, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                _copy_verified(
                    backup_fd, RUNTIME_SQLITE_FILENAME, scratch / RUNTIME_SQLITE_FILENAME, main
                )
                if wal_name in by_name:
                    _copy_verified(backup_fd, wal_name, scratch / wal_name, by_name[wal_name])
            finally:
                os.close(backup_fd)
            table_count = _check_disposable_sqlite(scratch / RUNTIME_SQLITE_FILENAME)
    except OSError as exc:
        raise OfflineSQLiteBackupError("could not inspect temporary SQLite restoration") from exc
    try:
        if _verified_offline_inventory(root, backup) != baseline:
            raise OfflineSQLiteBackupError("source or backup changed during SQLite test")
    except V11ShadowStageError as exc:
        raise OfflineSQLiteBackupError("source or backup changed during SQLite test") from exc
    return OfflineSQLiteBackupInspection(
        baseline=baseline,
        sqlite_sha256=main.sha256,
        wal_present=wal_name in by_name,
        schema_table_count=table_count,
    )
