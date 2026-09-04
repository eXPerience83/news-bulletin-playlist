"""Durable journal for Spotify snapshot transitions that are not yet confirmed.

The journal is intentionally distinct from confirmed playlist attestations. It lives in the same
SQLite database but self-initializes an auxiliary table so it can be introduced without changing
the canonical data-schema contract. Losing this table is safe: the engine falls back to strict
reconciliation; keeping it across restarts prevents duplicate writes while Spotify propagates a
new snapshot id after a partially observable readback.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType

from news_bulletin_playlist.models import PlaylistId
from news_bulletin_playlist.persistence import PersistenceError, SQLiteStore

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS spotify_pending_snapshot_confirmations (
    playlist_id TEXT PRIMARY KEY,
    destination_id TEXT NOT NULL,
    baseline_snapshot_id TEXT NOT NULL,
    expected_snapshot_id TEXT NOT NULL,
    desired_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
)
"""


@dataclass(frozen=True, slots=True)
class PendingSnapshotConfirmation:
    playlist_id: PlaylistId
    destination_id: str
    baseline_snapshot_id: str
    expected_snapshot_id: str
    desired_fingerprint: str
    created_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if not self.destination_id.strip():
            raise ValueError("pending snapshot destination_id must not be empty")
        if not self.baseline_snapshot_id.strip():
            raise ValueError("pending snapshot baseline_snapshot_id must not be empty")
        if not self.expected_snapshot_id.strip():
            raise ValueError("pending snapshot expected_snapshot_id must not be empty")
        if len(self.desired_fingerprint) != 64:
            raise ValueError("pending snapshot desired_fingerprint must be a sha256 hex digest")
        try:
            int(self.desired_fingerprint, 16)
        except ValueError as exc:
            raise ValueError(
                "pending snapshot desired_fingerprint must be a sha256 hex digest"
            ) from exc
        created = _as_utc(self.created_at)
        expires = _as_utc(self.expires_at)
        if expires <= created:
            raise ValueError("pending snapshot expires_at must be after created_at")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "expires_at", expires)


class _ConnectionContext:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        operation: str,
        path: str,
    ) -> None:
        self.connection = connection
        self.operation = operation
        self.path = path

    def __enter__(self) -> sqlite3.Connection:
        return self.connection

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_value, traceback
        try:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
        except sqlite3.Error as db_exc:
            raise PersistenceError(f"{self.operation} failed for {self.path}: {db_exc}") from db_exc
        finally:
            self.connection.close()


class PendingSnapshotJournal:
    """Small idempotent SQLite journal scoped to one existing runtime store."""

    def __init__(self, store: SQLiteStore) -> None:
        self.path = store.path

    def get(self, playlist_id: PlaylistId) -> PendingSnapshotConfirmation | None:
        with self._connection("read pending Spotify snapshot") as connection:
            self._ensure_table(connection)
            row = connection.execute(
                """
                SELECT playlist_id, destination_id, baseline_snapshot_id, expected_snapshot_id,
                       desired_fingerprint, created_at, expires_at
                FROM spotify_pending_snapshot_confirmations
                WHERE playlist_id = ?
                """,
                (str(playlist_id),),
            ).fetchone()
        if row is None:
            return None
        return PendingSnapshotConfirmation(
            playlist_id=PlaylistId(str(row["playlist_id"])),
            destination_id=str(row["destination_id"]),
            baseline_snapshot_id=str(row["baseline_snapshot_id"]),
            expected_snapshot_id=str(row["expected_snapshot_id"]),
            desired_fingerprint=str(row["desired_fingerprint"]),
            created_at=_parse_timestamp(str(row["created_at"])),
            expires_at=_parse_timestamp(str(row["expires_at"])),
        )

    def set(self, confirmation: PendingSnapshotConfirmation) -> None:
        with self._connection("persist pending Spotify snapshot") as connection:
            self._ensure_table(connection)
            connection.execute(
                """
                INSERT INTO spotify_pending_snapshot_confirmations (
                    playlist_id, destination_id, baseline_snapshot_id, expected_snapshot_id,
                    desired_fingerprint, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(playlist_id) DO UPDATE SET
                    destination_id = excluded.destination_id,
                    baseline_snapshot_id = excluded.baseline_snapshot_id,
                    expected_snapshot_id = excluded.expected_snapshot_id,
                    desired_fingerprint = excluded.desired_fingerprint,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at
                """,
                (
                    str(confirmation.playlist_id),
                    confirmation.destination_id,
                    confirmation.baseline_snapshot_id,
                    confirmation.expected_snapshot_id,
                    confirmation.desired_fingerprint,
                    _format_timestamp(confirmation.created_at),
                    _format_timestamp(confirmation.expires_at),
                ),
            )

    def clear(self, playlist_id: PlaylistId) -> None:
        with self._connection("clear pending Spotify snapshot") as connection:
            self._ensure_table(connection)
            connection.execute(
                "DELETE FROM spotify_pending_snapshot_confirmations WHERE playlist_id = ?",
                (str(playlist_id),),
            )

    @staticmethod
    def _ensure_table(connection: sqlite3.Connection) -> None:
        connection.execute(_TABLE_SQL)

    def _connection(self, operation: str) -> _ConnectionContext:
        try:
            connection = sqlite3.connect(self.path, timeout=10.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
        except sqlite3.Error as exc:
            raise PersistenceError(f"{operation} failed for {self.path}: {exc}") from exc
        return _ConnectionContext(
            connection,
            operation=operation,
            path=str(self.path),
        )


def _format_timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PersistenceError("pending Spotify snapshot contained an invalid timestamp") from exc
    return _as_utc(parsed)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("pending snapshot timestamps must be timezone-aware")
    return value.astimezone(UTC)
