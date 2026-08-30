from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from news_bulletin_playlist.models import CanonicalEdition, PlaylistId, SourceId

DEFAULT_DB_FILENAME = "news-bulletin-playlist.sqlite3"
DEFAULT_DB_PATH = Path("/data") / DEFAULT_DB_FILENAME
DEFAULT_RETENTION_DAYS = 30
LATEST_SCHEMA_VERSION = 1


class PersistenceError(RuntimeError):
    """Raised when durable state cannot be read or updated safely."""


class MatchStatus(StrEnum):
    """Persistent Spotify catalogue matching state for one canonical edition."""

    MATCHED = "matched"
    PENDING = "pending"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class SourceState:
    source_id: SourceId
    last_attempt_at: datetime
    last_success_at: datetime | None
    last_error: str | None


@dataclass(frozen=True, slots=True)
class PlaylistState:
    playlist_id: PlaylistId
    last_attempt_at: datetime
    last_success_at: datetime | None
    last_error: str | None


@dataclass(frozen=True, slots=True)
class EditionMatch:
    source_id: SourceId
    source_native_id: str
    status: MatchStatus
    spotify_episode_uri: str | None
    diagnostics: str | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class RetentionResult:
    source_runs_deleted: int
    playlist_runs_deleted: int


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
            raise PersistenceError(
                f"initialize database failed for {self.path}: {exc}"
            ) from exc

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
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, _format_timestamp(datetime.now(UTC))),
                )

    def schema_version(self) -> int:
        """Return the latest applied migration version."""
        with self._connection("read schema version") as connection:
            return self._schema_version(connection)

    def upsert_editions(
        self,
        editions: Iterable[CanonicalEdition],
        *,
        observed_at: datetime,
    ) -> None:
        """Persist canonical editions once globally, keyed by source-native identity."""
        observed = _format_timestamp(observed_at)
        rows = [
            (
                str(edition.source_id),
                edition.source_native_id,
                edition.title,
                _format_timestamp(edition.published_at),
                _format_optional_timestamp(edition.edition_at),
                edition.duration_seconds,
                observed,
                observed,
            )
            for edition in editions
        ]
        if not rows:
            return

        sql = """
        INSERT INTO canonical_editions (
            source_id,
            source_native_id,
            title,
            published_at,
            edition_at,
            duration_seconds,
            first_seen_at,
            last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id, source_native_id) DO UPDATE SET
            title = CASE
                WHEN excluded.last_seen_at >= canonical_editions.last_seen_at
                THEN excluded.title ELSE canonical_editions.title
            END,
            published_at = CASE
                WHEN excluded.last_seen_at >= canonical_editions.last_seen_at
                THEN excluded.published_at ELSE canonical_editions.published_at
            END,
            edition_at = CASE
                WHEN excluded.last_seen_at >= canonical_editions.last_seen_at
                THEN excluded.edition_at ELSE canonical_editions.edition_at
            END,
            duration_seconds = CASE
                WHEN excluded.last_seen_at >= canonical_editions.last_seen_at
                THEN excluded.duration_seconds ELSE canonical_editions.duration_seconds
            END,
            last_seen_at = MAX(canonical_editions.last_seen_at, excluded.last_seen_at)
        """
        with self._connection("upsert canonical editions") as connection:
            connection.executemany(sql, rows)

    def get_edition(
        self,
        source_id: SourceId,
        source_native_id: str,
    ) -> CanonicalEdition | None:
        """Load one canonical edition by stable source identity."""
        with self._connection("read canonical edition") as connection:
            row = connection.execute(
                """
                SELECT source_id, source_native_id, title, published_at, edition_at,
                       duration_seconds
                FROM canonical_editions
                WHERE source_id = ? AND source_native_id = ?
                """,
                (str(source_id), source_native_id),
            ).fetchone()
        return None if row is None else _edition_from_row(row)

    def list_editions(
        self,
        *,
        source_id: SourceId | None = None,
    ) -> tuple[CanonicalEdition, ...]:
        """List canonical editions without introducing playlist-specific copies."""
        with self._connection("list canonical editions") as connection:
            if source_id is None:
                rows = connection.execute(
                    """
                    SELECT source_id, source_native_id, title, published_at, edition_at,
                           duration_seconds
                    FROM canonical_editions
                    ORDER BY published_at DESC, source_id, source_native_id
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT source_id, source_native_id, title, published_at, edition_at,
                           duration_seconds
                    FROM canonical_editions
                    WHERE source_id = ?
                    ORDER BY published_at DESC, source_native_id
                    """,
                    (str(source_id),),
                ).fetchall()
        return tuple(_edition_from_row(row) for row in rows)

    def record_source_run(
        self,
        source_id: SourceId,
        *,
        started_at: datetime,
        finished_at: datetime,
        ok: bool,
        edition_count: int,
        error: str | None = None,
    ) -> None:
        """Upsert one source-run outcome and its last-known-good state atomically."""
        if edition_count < 0:
            raise ValueError("edition_count must be non-negative")
        started, finished = _run_timestamps(started_at, finished_at)
        success_at = finished if ok else None
        last_error = None if ok else error

        with self._connection("record source run") as connection:
            connection.execute(
                """
                INSERT INTO source_runs (
                    source_id, started_at, finished_at, ok, edition_count, error
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, started_at) DO UPDATE SET
                    finished_at = excluded.finished_at,
                    ok = excluded.ok,
                    edition_count = excluded.edition_count,
                    error = excluded.error
                WHERE excluded.finished_at >= source_runs.finished_at
                """,
                (str(source_id), started, finished, int(ok), edition_count, last_error),
            )
            connection.execute(
                """
                INSERT INTO source_state (
                    source_id, last_attempt_at, last_success_at, last_error
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    last_attempt_at = MAX(
                        source_state.last_attempt_at,
                        excluded.last_attempt_at
                    ),
                    last_success_at = CASE
                        WHEN source_state.last_success_at IS NULL
                            THEN excluded.last_success_at
                        WHEN excluded.last_success_at IS NULL
                            THEN source_state.last_success_at
                        ELSE MAX(
                            source_state.last_success_at,
                            excluded.last_success_at
                        )
                    END,
                    last_error = CASE
                        WHEN excluded.last_attempt_at >= source_state.last_attempt_at
                            THEN excluded.last_error
                        ELSE source_state.last_error
                    END
                """,
                (str(source_id), finished, success_at, last_error),
            )

    def get_source_state(self, source_id: SourceId) -> SourceState | None:
        """Load the latest source attempt and last-known-good timestamp."""
        with self._connection("read source state") as connection:
            row = connection.execute(
                """
                SELECT source_id, last_attempt_at, last_success_at, last_error
                FROM source_state
                WHERE source_id = ?
                """,
                (str(source_id),),
            ).fetchone()
        if row is None:
            return None
        return SourceState(
            source_id=SourceId(_row_str(row, "source_id")),
            last_attempt_at=_parse_timestamp(_row_str(row, "last_attempt_at")),
            last_success_at=_parse_optional_timestamp(row["last_success_at"]),
            last_error=_optional_str(row["last_error"]),
        )

    def set_match_state(
        self,
        source_id: SourceId,
        source_native_id: str,
        *,
        status: MatchStatus,
        updated_at: datetime,
        spotify_episode_uri: str | None = None,
        diagnostics: str | None = None,
    ) -> None:
        """Persist deterministic source-edition to Spotify matching state."""
        if status is MatchStatus.MATCHED:
            if spotify_episode_uri is None or not spotify_episode_uri.strip():
                raise ValueError("matched state requires spotify_episode_uri")
        elif spotify_episode_uri is not None:
            raise ValueError("non-matched state must not store spotify_episode_uri")

        with self._connection("set Spotify match state") as connection:
            connection.execute(
                """
                INSERT INTO spotify_matches (
                    source_id,
                    source_native_id,
                    status,
                    spotify_episode_uri,
                    diagnostics,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, source_native_id) DO UPDATE SET
                    status = excluded.status,
                    spotify_episode_uri = excluded.spotify_episode_uri,
                    diagnostics = excluded.diagnostics,
                    updated_at = excluded.updated_at
                WHERE excluded.updated_at >= spotify_matches.updated_at
                """,
                (
                    str(source_id),
                    source_native_id,
                    status.value,
                    spotify_episode_uri,
                    diagnostics,
                    _format_timestamp(updated_at),
                ),
            )

    def get_match_state(
        self,
        source_id: SourceId,
        source_native_id: str,
    ) -> EditionMatch | None:
        """Load persistent Spotify matching state for one canonical edition."""
        with self._connection("read Spotify match state") as connection:
            row = connection.execute(
                """
                SELECT source_id, source_native_id, status, spotify_episode_uri,
                       diagnostics, updated_at
                FROM spotify_matches
                WHERE source_id = ? AND source_native_id = ?
                """,
                (str(source_id), source_native_id),
            ).fetchone()
        if row is None:
            return None
        return EditionMatch(
            source_id=SourceId(_row_str(row, "source_id")),
            source_native_id=_row_str(row, "source_native_id"),
            status=MatchStatus(_row_str(row, "status")),
            spotify_episode_uri=_optional_str(row["spotify_episode_uri"]),
            diagnostics=_optional_str(row["diagnostics"]),
            updated_at=_parse_timestamp(_row_str(row, "updated_at")),
        )

    def get_spotify_episode_uri(
        self,
        source_id: SourceId,
        source_native_id: str,
    ) -> str | None:
        """Return the durable Spotify episode URI only for a matched edition."""
        match = self.get_match_state(source_id, source_native_id)
        if match is None or match.status is not MatchStatus.MATCHED:
            return None
        return match.spotify_episode_uri

    def record_playlist_run(
        self,
        playlist_id: PlaylistId,
        *,
        started_at: datetime,
        finished_at: datetime,
        ok: bool,
        desired_count: int,
        applied_count: int,
        error: str | None = None,
    ) -> None:
        """Upsert one playlist reconciliation outcome and latest state atomically."""
        if desired_count < 0 or applied_count < 0:
            raise ValueError("playlist counts must be non-negative")
        started, finished = _run_timestamps(started_at, finished_at)
        success_at = finished if ok else None
        last_error = None if ok else error

        with self._connection("record playlist run") as connection:
            connection.execute(
                """
                INSERT INTO playlist_runs (
                    playlist_id,
                    started_at,
                    finished_at,
                    ok,
                    desired_count,
                    applied_count,
                    error
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(playlist_id, started_at) DO UPDATE SET
                    finished_at = excluded.finished_at,
                    ok = excluded.ok,
                    desired_count = excluded.desired_count,
                    applied_count = excluded.applied_count,
                    error = excluded.error
                WHERE excluded.finished_at >= playlist_runs.finished_at
                """,
                (
                    str(playlist_id),
                    started,
                    finished,
                    int(ok),
                    desired_count,
                    applied_count,
                    last_error,
                ),
            )
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

    def prune_operational_history(
        self,
        *,
        now: datetime,
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> RetentionResult:
        """Delete eligible old run history while retaining correctness-critical state."""
        if retention_days <= 0:
            raise ValueError("retention_days must be positive")
        cutoff = _format_timestamp(now - timedelta(days=retention_days))

        with self._connection("prune operational history") as connection:
            source_cursor = connection.execute(
                "DELETE FROM source_runs WHERE finished_at < ?",
                (cutoff,),
            )
            playlist_cursor = connection.execute(
                "DELETE FROM playlist_runs WHERE finished_at < ?",
                (cutoff,),
            )
        return RetentionResult(
            source_runs_deleted=max(source_cursor.rowcount, 0),
            playlist_runs_deleted=max(playlist_cursor.rowcount, 0),
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


def _edition_from_row(row: sqlite3.Row) -> CanonicalEdition:
    duration = row["duration_seconds"]
    if duration is not None and not isinstance(duration, int):
        raise PersistenceError("duration_seconds is not an integer")
    return CanonicalEdition(
        source_id=SourceId(_row_str(row, "source_id")),
        source_native_id=_row_str(row, "source_native_id"),
        title=_row_str(row, "title"),
        published_at=_parse_timestamp(_row_str(row, "published_at")),
        edition_at=_parse_optional_timestamp(row["edition_at"]),
        duration_seconds=duration,
    )


def _run_timestamps(started_at: datetime, finished_at: datetime) -> tuple[str, str]:
    started = _format_timestamp(started_at)
    finished = _format_timestamp(finished_at)
    if finished_at < started_at:
        raise ValueError("finished_at must not be before started_at")
    return started, finished


def _format_optional_timestamp(value: datetime | None) -> str | None:
    return None if value is None else _format_timestamp(value)


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
