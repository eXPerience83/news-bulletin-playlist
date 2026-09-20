from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from news_bulletin_playlist.models import PlaylistId

DEFAULT_DB_FILENAME = "news-bulletin-playlist.sqlite3"
DEFAULT_DB_PATH = Path("/data") / DEFAULT_DB_FILENAME
LATEST_SCHEMA_VERSION = 8


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


_MIGRATION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
)
"""

_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
            """
            CREATE TABLE canonical_editions (
                source_id TEXT NOT NULL,
                source_native_id TEXT NOT NULL,
                title TEXT NOT NULL,
                published_at TEXT NOT NULL,
                edition_at TEXT,
                duration_seconds INTEGER CHECK (
                    duration_seconds IS NULL OR duration_seconds >= 0
                ),
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                PRIMARY KEY (source_id, source_native_id)
            )
            """,
            """
            CREATE INDEX canonical_editions_published_at_idx
            ON canonical_editions (published_at)
            """,
            """
            CREATE TABLE source_runs (
                source_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                ok INTEGER NOT NULL CHECK (ok IN (0, 1)),
                edition_count INTEGER NOT NULL CHECK (edition_count >= 0),
                error TEXT,
                PRIMARY KEY (source_id, started_at)
            )
            """,
            """
            CREATE INDEX source_runs_finished_at_idx
            ON source_runs (finished_at)
            """,
            """
            CREATE TABLE source_state (
                source_id TEXT PRIMARY KEY,
                last_attempt_at TEXT NOT NULL,
                last_success_at TEXT,
                last_error TEXT
            )
            """,
            """
            CREATE TABLE spotify_matches (
                source_id TEXT NOT NULL,
                source_native_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('matched', 'pending', 'ambiguous')),
                spotify_episode_uri TEXT,
                diagnostics TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (source_id, source_native_id),
                FOREIGN KEY (source_id, source_native_id)
                    REFERENCES canonical_editions (source_id, source_native_id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE playlist_runs (
                playlist_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                ok INTEGER NOT NULL CHECK (ok IN (0, 1)),
                desired_count INTEGER NOT NULL CHECK (desired_count >= 0),
                applied_count INTEGER NOT NULL CHECK (applied_count >= 0),
                error TEXT,
                PRIMARY KEY (playlist_id, started_at)
            )
            """,
            """
            CREATE INDEX playlist_runs_finished_at_idx
            ON playlist_runs (finished_at)
            """,
            """
            CREATE TABLE playlist_state (
                playlist_id TEXT PRIMARY KEY,
                last_attempt_at TEXT NOT NULL,
                last_success_at TEXT,
                last_error TEXT
            )
            """,
        ),
    ),
    (
        2,
        (
            """
            CREATE TABLE spotify_playlist_attestations (
                playlist_id TEXT PRIMARY KEY,
                destination_id TEXT NOT NULL,
                snapshot_id TEXT NOT NULL,
                desired_fingerprint TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
        ),
    ),
    (
        3,
        (
            """
            ALTER TABLE spotify_matches
            ADD COLUMN spotify_duration_seconds INTEGER
                CHECK (spotify_duration_seconds IS NULL OR spotify_duration_seconds >= 0)
            """,
        ),
    ),
    (
        4,
        (
            """
            CREATE TABLE spotify_request_metrics (
                bucket_at TEXT NOT NULL,
                surface TEXT NOT NULL CHECK (surface IN ('web_api', 'oauth')),
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
        ),
    ),
    (
        5,
        (
            """
            CREATE TABLE spotify_planner_catalogue_state (
                context_key TEXT PRIMARY KEY,
                contract_fingerprint TEXT NOT NULL,
                last_verified_at TEXT,
                next_audit_at TEXT,
                CHECK (
                    (last_verified_at IS NULL AND next_audit_at IS NULL)
                    OR (last_verified_at IS NOT NULL AND next_audit_at IS NOT NULL)
                )
            )
            """,
            """
            CREATE TABLE spotify_planner_destination_state (
                playlist_id TEXT PRIMARY KEY,
                destination_context_fingerprint TEXT NOT NULL,
                config_fingerprint TEXT NOT NULL,
                auth_context_fingerprint TEXT NOT NULL,
                observed_desired_fingerprint TEXT NOT NULL,
                confirmed_desired_fingerprint TEXT,
                dirty INTEGER NOT NULL CHECK (dirty IN (0, 1)),
                unknown INTEGER NOT NULL CHECK (unknown IN (0, 1)),
                last_verified_at TEXT,
                next_audit_at TEXT,
                CHECK (
                    (last_verified_at IS NULL AND next_audit_at IS NULL)
                    OR (last_verified_at IS NOT NULL AND next_audit_at IS NOT NULL)
                )
            )
            """,
            """
            CREATE TABLE spotify_planner_unresolved_state (
                source_id TEXT NOT NULL,
                source_native_id TEXT NOT NULL,
                input_fingerprint TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('pending', 'ambiguous')),
                reason TEXT NOT NULL CHECK (
                    reason IN ('no_viable_candidate', 'multiple_candidates')
                ),
                first_unresolved_at TEXT NOT NULL,
                last_attempt_at TEXT NOT NULL,
                next_retry_at TEXT NOT NULL,
                PRIMARY KEY (source_id, source_native_id),
                FOREIGN KEY (source_id, source_native_id)
                    REFERENCES canonical_editions (source_id, source_native_id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE spotify_planner_match_inputs (
                source_id TEXT NOT NULL,
                source_native_id TEXT NOT NULL,
                input_fingerprint TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                PRIMARY KEY (source_id, source_native_id),
                FOREIGN KEY (source_id, source_native_id)
                    REFERENCES canonical_editions (source_id, source_native_id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX spotify_planner_unresolved_retry_idx
            ON spotify_planner_unresolved_state (next_retry_at)
            """,
        ),
    ),
    (
        6,
        (
            """
            CREATE TABLE spotify_planner_matched_recovery_state (
                source_id TEXT NOT NULL,
                source_native_id TEXT NOT NULL,
                input_fingerprint TEXT NOT NULL,
                reason TEXT NOT NULL CHECK (reason = 'inconclusive_catalogue'),
                first_attempt_at TEXT NOT NULL,
                last_attempt_at TEXT NOT NULL,
                next_retry_at TEXT NOT NULL,
                PRIMARY KEY (source_id, source_native_id),
                FOREIGN KEY (source_id, source_native_id)
                    REFERENCES canonical_editions (source_id, source_native_id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX spotify_planner_matched_recovery_retry_idx
            ON spotify_planner_matched_recovery_state (next_retry_at)
            """,
        ),
    ),
    (
        7,
        (
            """
            CREATE TABLE spotify_planner_persistence_rejection_state (
                source_id TEXT NOT NULL,
                source_native_id TEXT NOT NULL,
                protected_status TEXT NOT NULL
                    CHECK (protected_status IN ('matched', 'pending', 'ambiguous')),
                protected_spotify_episode_uri TEXT,
                protected_spotify_duration_seconds INTEGER
                    CHECK (
                        protected_spotify_duration_seconds IS NULL
                        OR protected_spotify_duration_seconds >= 0
                    ),
                protected_match_updated_at TEXT NOT NULL,
                first_rejected_at TEXT NOT NULL,
                last_rejected_at TEXT NOT NULL,
                PRIMARY KEY (source_id, source_native_id),
                FOREIGN KEY (source_id, source_native_id)
                    REFERENCES canonical_editions (source_id, source_native_id)
                    ON DELETE CASCADE
            )
            """,
        ),
    ),
    (
        8,
        (
            """
            ALTER TABLE spotify_request_metrics
            RENAME TO spotify_request_metrics_v7
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
            INSERT INTO spotify_request_metrics (
                bucket_at, surface, operation, outcome, http_status, quota_reason, request_count
            )
            SELECT bucket_at, surface, operation, outcome, http_status, quota_reason, request_count
            FROM spotify_request_metrics_v7
            """,
            """
            DROP TABLE spotify_request_metrics_v7
            """,
            """
            CREATE INDEX spotify_request_metrics_bucket_idx
            ON spotify_request_metrics (bucket_at DESC)
            """,
        ),
    ),
)


class SQLiteStore:
    """Small explicit SQLite store for durable multi-playlist operational state."""

    def __init__(self, path: Path | str = DEFAULT_DB_PATH) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        """Create the database and apply pending migrations transactionally."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PersistenceError(f"initialize database failed for {self.path}: {exc}") from exc

        with self._connection("initialize database") as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(_MIGRATION_TABLE_SQL)
            current = self._schema_version(connection)
            if current > LATEST_SCHEMA_VERSION:
                raise PersistenceError(
                    f"database schema {current} is newer than supported "
                    f"version {LATEST_SCHEMA_VERSION}"
                )

            for version, statements in _MIGRATIONS:
                if version <= current:
                    continue
                for statement in statements:
                    connection.execute(statement)
                if version == 7:
                    self._seed_persistence_rejection_barriers(connection)
                if version == 8 and 0 < current < 8:
                    self._mark_request_metrics_coverage_gap(
                        connection,
                        observed_at=datetime.now(UTC),
                    )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, _format_timestamp(datetime.now(UTC))),
                )

    def schema_version(self) -> int:
        """Return the latest applied migration version."""
        with self._connection("read schema version") as connection:
            return self._schema_version(connection)

    @staticmethod
    def _mark_request_metrics_coverage_gap(
        connection: sqlite3.Connection,
        *,
        observed_at: datetime,
    ) -> None:
        """Mark pre-v8 request history incomplete because public attempts were not counted."""
        connection.execute(
            """
            INSERT INTO spotify_request_metrics_coverage (singleton, incomplete_at)
            VALUES (1, ?)
            ON CONFLICT(singleton) DO UPDATE SET incomplete_at = excluded.incomplete_at
            WHERE excluded.incomplete_at > spotify_request_metrics_coverage.incomplete_at
            """,
            (_format_timestamp(observed_at),),
        )

    @staticmethod
    def _seed_persistence_rejection_barriers(connection: sqlite3.Connection) -> None:
        """Migrate only implicit barriers that can be reconstructed safely.

        Some historical/synthetic databases contain only the tables needed by the migration
        under test. Creating the v7 table remains valid there, but attempting a DML insert into
        a child table whose canonical-edition parent is absent fails with foreign_keys enabled.
        Treat those partial databases as having no reconstructable barrier rather than making
        unrelated legacy upgrades fail.
        """
        required = {
            "canonical_editions",
            "spotify_matches",
            "spotify_planner_matched_recovery_state",
            "spotify_planner_unresolved_state",
            "spotify_planner_persistence_rejection_state",
        }
        placeholders = ", ".join("?" for _ in required)
        rows = connection.execute(
            f"SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ({placeholders})",
            tuple(sorted(required)),
        ).fetchall()
        if {str(row[0]) for row in rows} != required:
            return

        connection.execute(
            """
            INSERT OR IGNORE INTO spotify_planner_persistence_rejection_state (
                source_id, source_native_id, protected_status,
                protected_spotify_episode_uri, protected_spotify_duration_seconds,
                protected_match_updated_at, first_rejected_at, last_rejected_at
            )
            SELECT m.source_id, m.source_native_id, m.status, m.spotify_episode_uri,
                   m.spotify_duration_seconds, m.updated_at,
                   r.first_attempt_at, r.last_attempt_at
            FROM spotify_matches AS m
            JOIN spotify_planner_matched_recovery_state AS r
              ON r.source_id = m.source_id
             AND r.source_native_id = m.source_native_id
            WHERE m.status = 'matched' AND m.updated_at > r.first_attempt_at
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO spotify_planner_persistence_rejection_state (
                source_id, source_native_id, protected_status,
                protected_spotify_episode_uri, protected_spotify_duration_seconds,
                protected_match_updated_at, first_rejected_at, last_rejected_at
            )
            SELECT m.source_id, m.source_native_id, m.status, m.spotify_episode_uri,
                   m.spotify_duration_seconds, m.updated_at,
                   u.first_unresolved_at, u.last_attempt_at
            FROM spotify_matches AS m
            JOIN spotify_planner_unresolved_state AS u
              ON u.source_id = m.source_id
             AND u.source_native_id = m.source_native_id
            WHERE m.status != 'matched' AND m.updated_at > u.last_attempt_at
            """
        )

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

    def _schema_version(self, connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
        ).fetchone()
        if row is None:
            return 0
        value = row["version"]
        if not isinstance(value, int):
            raise PersistenceError("schema migration version is not an integer")
        return value

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


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise PersistenceError("stored optional integer value is invalid")
    return value


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PersistenceError("stored optional text value is invalid")
    return value
