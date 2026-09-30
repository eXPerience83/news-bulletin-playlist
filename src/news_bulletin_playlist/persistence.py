from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from news_bulletin_playlist.models import PlaylistId
from news_bulletin_playlist.spotify_first_history_schema import (
    EPISODE_HISTORY_COLUMNS,
    EPISODE_HISTORY_INDEX,
    EPISODE_HISTORY_TABLE,
)

DEFAULT_DB_FILENAME = "news-bulletin-playlist-v1.sqlite3"
DEFAULT_DB_PATH = Path("/data") / DEFAULT_DB_FILENAME
DATABASE_FORMAT_VERSION = 3


class PersistenceError(RuntimeError):
    """Raised when durable state cannot be read or updated safely."""


@dataclass(frozen=True, slots=True)
class PlaylistState:
    playlist_id: PlaylistId
    last_attempt_at: datetime
    last_success_at: datetime | None
    last_error: str | None


@dataclass(frozen=True, slots=True)
class PlaylistAttestation:
    playlist_id: PlaylistId
    destination_id: str
    snapshot_id: str
    desired_fingerprint: str
    updated_at: datetime


_FORMAT_TABLE_SQL = """
CREATE TABLE database_format (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    format_version INTEGER NOT NULL CHECK (format_version > 0),
    created_at TEXT NOT NULL
)
"""

_BASELINE_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE playlist_state (
        playlist_id TEXT PRIMARY KEY,
        last_attempt_at TEXT NOT NULL,
        last_success_at TEXT,
        last_error TEXT
    )
    """,
    """
    CREATE TABLE spotify_playlist_attestations (
        playlist_id TEXT PRIMARY KEY,
        destination_id TEXT NOT NULL,
        snapshot_id TEXT NOT NULL,
        desired_fingerprint TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE spotify_request_metrics (
        bucket_at TEXT NOT NULL,
        surface TEXT NOT NULL CHECK (surface IN ('public', 'web_api', 'oauth')),
        operation TEXT NOT NULL,
        outcome TEXT NOT NULL CHECK (
            outcome IN ('success', 'http_error', 'transport_error', 'response_error')
        ),
        http_status INTEGER NOT NULL DEFAULT -1,
        quota_reason TEXT NOT NULL DEFAULT '',
        request_count INTEGER NOT NULL CHECK (request_count > 0),
        PRIMARY KEY (bucket_at, surface, operation, outcome, http_status, quota_reason)
    )
    """,
    """
    CREATE INDEX spotify_request_metrics_bucket_idx
    ON spotify_request_metrics (bucket_at DESC)
    """,
    """
    CREATE TABLE spotify_request_metrics_coverage (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        incomplete_at TEXT NOT NULL
    )
    """,
)

_REQUIRED_BASELINE_TABLES = frozenset(
    {
        "database_format",
        "playlist_state",
        "spotify_playlist_attestations",
        "spotify_request_metrics",
        "spotify_request_metrics_coverage",
    }
)


class SQLiteStore:
    """Small explicit SQLite store for durable multi-playlist operational state."""

    def __init__(self, path: Path | str = DEFAULT_DB_PATH) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        """Create or validate the clean Spotify-first database format."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PersistenceError(f"initialize database failed for {self.path}: {exc}") from exc

        with self._connection("initialize database") as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            tables = self._application_tables(connection)
            if not tables:
                connection.execute("BEGIN IMMEDIATE")
                self._create_baseline(connection, created_at=datetime.now(UTC))
                return

            if "database_format" not in tables:
                raise PersistenceError(
                    "database predates the supported 0.1.0 clean format; "
                    "back up and replace the old SQLite database before starting this build"
                )

            current = self._format_version(connection)
            if current > DATABASE_FORMAT_VERSION:
                raise PersistenceError(
                    f"database format {current} is newer than supported "
                    f"version {DATABASE_FORMAT_VERSION}"
                )
            missing = sorted(_REQUIRED_BASELINE_TABLES - tables)
            if missing:
                raise PersistenceError(
                    "database format is missing required tables: " + ", ".join(missing)
                )
            if current == 1:
                self._migrate_format_1_to_2(connection, tables)
                current = 2
                tables = self._application_tables(connection)
            if current == 2:
                if not connection.in_transaction:
                    connection.execute("BEGIN IMMEDIATE")
                self._migrate_format_2_to_3(connection, tables)
            elif current != DATABASE_FORMAT_VERSION:
                raise PersistenceError(f"database format {current} has no supported migration")
            elif "spotify_first_episode_history" not in tables:
                raise PersistenceError(
                    "database format is missing required tables: spotify_first_episode_history"
                )

    def format_version(self) -> int:
        """Return the clean Spotify-first database format version."""
        with self._connection("read database format") as connection:
            return self._format_version(connection)

    @staticmethod
    def _application_tables(connection: sqlite3.Connection) -> frozenset[str]:
        rows = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            """
        ).fetchall()
        return frozenset(str(row["name"]) for row in rows)

    @staticmethod
    def _format_version(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT format_version FROM database_format WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise PersistenceError("database format row is missing")
        value = row["format_version"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise PersistenceError("database format version is invalid")
        return value

    @staticmethod
    def _create_baseline(
        connection: sqlite3.Connection,
        *,
        created_at: datetime,
    ) -> None:
        created = _format_timestamp(created_at)
        connection.execute(_FORMAT_TABLE_SQL)
        for statement in _BASELINE_STATEMENTS:
            connection.execute(statement)
        connection.execute(EPISODE_HISTORY_TABLE)
        connection.execute(EPISODE_HISTORY_INDEX)
        connection.execute(
            """
            INSERT INTO database_format(singleton, format_version, created_at)
            VALUES (1, ?, ?)
            """,
            (DATABASE_FORMAT_VERSION, created),
        )
        # A new/reset database has no request history for the preceding 24 hours.
        connection.execute(
            """
            INSERT INTO spotify_request_metrics_coverage(singleton, incomplete_at)
            VALUES (1, ?)
            """,
            (created,),
        )

    @staticmethod
    def _migrate_format_1_to_2(connection: sqlite3.Connection, tables: frozenset[str]) -> None:
        # DDL, backfill and version marker commit or roll back together.
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(EPISODE_HISTORY_TABLE)
        connection.execute(EPISODE_HISTORY_INDEX)
        if "spotify_first_episode_state" in tables:
            columns = ", ".join(EPISODE_HISTORY_COLUMNS)
            connection.execute(
                f"INSERT INTO spotify_first_episode_history ({columns}) "
                f"SELECT {columns} FROM spotify_first_episode_state"
            )
        connection.execute("UPDATE database_format SET format_version = 2 WHERE singleton = 1")

    @staticmethod
    def _migrate_format_2_to_3(connection: sqlite3.Connection, tables: frozenset[str]) -> None:
        """Rebuild only episode tables to widen provenance, retaining rows and indexes."""
        if "spotify_first_episode_history" not in tables:
            raise PersistenceError("database format is missing required episode history")
        old_check = "timestamp_source IN ('title', 'first_seen')"
        new_check = "timestamp_source IN ('title', 'spotify_public_release', 'first_seen')"
        for table in ("spotify_first_episode_state", "spotify_first_episode_history"):
            if table not in tables:
                continue  # The current catalogue table is created lazily by its journal.
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()
            sql = row["sql"]
            columns = tuple(
                item["name"] for item in connection.execute(f"PRAGMA table_info({table})")
            )
            if columns != EPISODE_HISTORY_COLUMNS or (
                old_check not in sql and new_check not in sql
            ):
                raise PersistenceError("incompatible episode schema for provenance migration")
            if new_check in sql:
                continue  # History created by the preceding format-1 migration.
            indexes = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' "
                "AND tbl_name = ? AND sql IS NOT NULL",
                (table,),
            ).fetchall()
            replacement = table + "_provenance_migration"
            connection.execute(sql.replace(table, replacement, 1).replace(old_check, new_check))
            names = ", ".join(EPISODE_HISTORY_COLUMNS)
            connection.execute(f"INSERT INTO {replacement} ({names}) SELECT {names} FROM {table}")
            connection.execute(f"DROP TABLE {table}")
            connection.execute(f"ALTER TABLE {replacement} RENAME TO {table}")
            for index in indexes:
                connection.execute(index["sql"])
        connection.execute("UPDATE database_format SET format_version = 3 WHERE singleton = 1")

    def record_playlist_state(
        self,
        playlist_id: PlaylistId,
        *,
        started_at: datetime,
        finished_at: datetime,
        ok: bool,
        error: str | None = None,
    ) -> None:
        """Update only current playlist state; released run-history tables remain inert."""
        _, finished = _run_timestamps(started_at, finished_at)
        success_at = finished if ok else None
        last_error = None if ok else error

        with self._connection("record playlist state") as connection:
            connection.execute(
                """
                INSERT INTO playlist_state (
                    playlist_id, last_attempt_at, last_success_at, last_error
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(playlist_id) DO UPDATE SET
                    last_attempt_at = MAX(
                        playlist_state.last_attempt_at,
                        excluded.last_attempt_at
                    ),
                    last_success_at = CASE
                        WHEN playlist_state.last_success_at IS NULL
                            THEN excluded.last_success_at
                        WHEN excluded.last_success_at IS NULL
                            THEN playlist_state.last_success_at
                        ELSE MAX(
                            playlist_state.last_success_at,
                            excluded.last_success_at
                        )
                    END,
                    last_error = CASE
                        WHEN excluded.last_attempt_at >= playlist_state.last_attempt_at
                            THEN excluded.last_error
                        ELSE playlist_state.last_error
                    END
                """,
                (str(playlist_id), finished, success_at, last_error),
            )

    def get_playlist_state(self, playlist_id: PlaylistId) -> PlaylistState | None:
        """Load the latest reconciliation state for one configured playlist."""
        with self._connection("read playlist state") as connection:
            row = connection.execute(
                """
                SELECT playlist_id, last_attempt_at, last_success_at, last_error
                FROM playlist_state
                WHERE playlist_id = ?
                """,
                (str(playlist_id),),
            ).fetchone()
        if row is None:
            return None
        return PlaylistState(
            playlist_id=PlaylistId(_row_str(row, "playlist_id")),
            last_attempt_at=_parse_timestamp(_row_str(row, "last_attempt_at")),
            last_success_at=_parse_optional_timestamp(row["last_success_at"]),
            last_error=_optional_str(row["last_error"]),
        )

    def set_playlist_attestation(
        self,
        playlist_id: PlaylistId,
        *,
        destination_id: str,
        snapshot_id: str,
        desired_fingerprint: str,
        updated_at: datetime,
    ) -> None:
        """Persist evidence tying one desired playlist state to a Spotify snapshot."""
        destination = destination_id.strip()
        snapshot = snapshot_id.strip()
        fingerprint = desired_fingerprint.strip()
        if not destination:
            raise ValueError("playlist attestation destination_id must not be empty")
        if not snapshot:
            raise ValueError("playlist attestation snapshot_id must not be empty")
        if len(fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in fingerprint
        ):
            raise ValueError("playlist attestation fingerprint must be lowercase SHA-256")

        with self._connection("set Spotify playlist attestation") as connection:
            connection.execute(
                """
                INSERT INTO spotify_playlist_attestations (
                    playlist_id,
                    destination_id,
                    snapshot_id,
                    desired_fingerprint,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(playlist_id) DO UPDATE SET
                    destination_id = excluded.destination_id,
                    snapshot_id = excluded.snapshot_id,
                    desired_fingerprint = excluded.desired_fingerprint,
                    updated_at = excluded.updated_at
                WHERE excluded.updated_at >= spotify_playlist_attestations.updated_at
                """,
                (
                    str(playlist_id),
                    destination,
                    snapshot,
                    fingerprint,
                    _format_timestamp(updated_at),
                ),
            )

    def get_playlist_attestation(
        self,
        playlist_id: PlaylistId,
    ) -> PlaylistAttestation | None:
        """Load the latest durable Spotify playlist attestation."""
        with self._connection("read Spotify playlist attestation") as connection:
            row = connection.execute(
                """
                SELECT playlist_id, destination_id, snapshot_id,
                       desired_fingerprint, updated_at
                FROM spotify_playlist_attestations
                WHERE playlist_id = ?
                """,
                (str(playlist_id),),
            ).fetchone()
        if row is None:
            return None
        return PlaylistAttestation(
            playlist_id=PlaylistId(_row_str(row, "playlist_id")),
            destination_id=_row_str(row, "destination_id"),
            snapshot_id=_row_str(row, "snapshot_id"),
            desired_fingerprint=_row_str(row, "desired_fingerprint"),
            updated_at=_parse_timestamp(_row_str(row, "updated_at")),
        )

    @contextmanager
    def _connection(self, operation: str) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path, timeout=5.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            with connection:
                yield connection
        except sqlite3.Error as exc:
            raise PersistenceError(f"{operation} failed for {self.path}: {exc}") from exc
        finally:
            if connection is not None:
                connection.close()


def _run_timestamps(started_at: datetime, finished_at: datetime) -> tuple[str, str]:
    started = _format_timestamp(started_at)
    finished = _format_timestamp(finished_at)
    if finished_at < started_at:
        raise ValueError("finished_at must not be before started_at")
    return started, finished


def _format_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("database timestamps must be timezone-aware")
    normalized = value.astimezone(UTC)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_optional_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PersistenceError("stored timestamp is not text")
    return _parse_timestamp(value)


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PersistenceError(f"invalid stored timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PersistenceError(f"stored timestamp is timezone-naive: {value!r}")
    return parsed.astimezone(UTC)


def _row_str(row: sqlite3.Row, column: str) -> str:
    value = row[column]
    if not isinstance(value, str):
        raise PersistenceError(f"stored {column} is not text")
    return value


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PersistenceError("stored optional text value is invalid")
    return value
