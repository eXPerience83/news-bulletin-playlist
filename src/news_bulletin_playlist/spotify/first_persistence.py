"""SQLite persistence for the authoritative Spotify-first runtime."""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from news_bulletin_playlist.models import PlaylistId, SourceDefinition, SourceId
from news_bulletin_playlist.persistence import PersistenceError, SQLiteStore
from news_bulletin_playlist.spotify.first_models import (
    RefreshReason,
    SpotifyFirstEpisode,
    SpotifyFirstSourceState,
    TimestampSource,
    UpsertCounts,
    as_utc,
)
from news_bulletin_playlist.spotify.public_detector import spotify_show_id

_EPISODE_TABLE = """
CREATE TABLE IF NOT EXISTS spotify_first_episode_state (
    source_id TEXT NOT NULL,
    spotify_show_id TEXT NOT NULL,
    spotify_episode_id TEXT NOT NULL,
    spotify_episode_uri TEXT NOT NULL,
    title TEXT NOT NULL,
    edition_at TEXT,
    timestamp_source TEXT NOT NULL
        CHECK (timestamp_source IN ('title', 'first_seen')),
    release_date_raw TEXT NOT NULL,
    release_date_precision TEXT NOT NULL
        CHECK (release_date_precision IN ('day', 'month', 'year')),
    duration_seconds INTEGER
        CHECK (duration_seconds IS NULL OR duration_seconds >= 0),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (source_id, spotify_episode_id)
)
"""

_SOURCE_TABLE = """
CREATE TABLE IF NOT EXISTS spotify_first_source_state (
    source_id TEXT PRIMARY KEY,
    spotify_show_id TEXT NOT NULL,
    last_refresh_at TEXT NOT NULL,
    refresh_reason TEXT NOT NULL
        CHECK (refresh_reason IN ('bootstrap', 'change', 'inactivity_audit', 'stale_escalation')),
    detector_episode_id TEXT,
    api_episode_id TEXT,
    request_count INTEGER NOT NULL CHECK (request_count >= 0),
    catalogue_complete INTEGER NOT NULL CHECK (catalogue_complete IN (0, 1)),
    coverage_proven INTEGER NOT NULL CHECK (coverage_proven IN (0, 1)),
    coverage_cutoff_at TEXT NOT NULL
)
"""

_DESTINATION_TABLE = """
CREATE TABLE IF NOT EXISTS spotify_first_destination_state (
    playlist_id TEXT PRIMARY KEY,
    destination_id TEXT NOT NULL,
    desired_fingerprint TEXT NOT NULL,
    verified_at TEXT NOT NULL
)
"""

_DEGRADED_DESTINATION_TABLE = """
CREATE TABLE IF NOT EXISTS spotify_first_destination_degraded_state (
    playlist_id TEXT PRIMARY KEY,
    destination_id TEXT NOT NULL,
    desired_fingerprint TEXT NOT NULL,
    verified_at TEXT NOT NULL
)
"""

_EPISODE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_spotify_first_episode_source_seen
ON spotify_first_episode_state (source_id, last_seen_at DESC)
"""

_UPSERT_EPISODE = """
INSERT INTO spotify_first_episode_state (
    source_id,
    spotify_show_id,
    spotify_episode_id,
    spotify_episode_uri,
    title,
    edition_at,
    timestamp_source,
    release_date_raw,
    release_date_precision,
    duration_seconds,
    first_seen_at,
    last_seen_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(source_id, spotify_episode_id) DO UPDATE SET
    spotify_show_id = excluded.spotify_show_id,
    spotify_episode_uri = excluded.spotify_episode_uri,
    title = excluded.title,
    edition_at = excluded.edition_at,
    timestamp_source = excluded.timestamp_source,
    release_date_raw = excluded.release_date_raw,
    release_date_precision = excluded.release_date_precision,
    duration_seconds = excluded.duration_seconds,
    first_seen_at = MIN(spotify_first_episode_state.first_seen_at, excluded.first_seen_at),
    last_seen_at = MAX(spotify_first_episode_state.last_seen_at, excluded.last_seen_at)
WHERE excluded.last_seen_at >= spotify_first_episode_state.last_seen_at
"""


@dataclass(frozen=True, slots=True)
class DestinationVerificationState:
    playlist_id: PlaylistId
    destination_id: str
    desired_fingerprint: str
    verified_at: datetime


class SpotifyFirstJournal:
    """Durable Spotify-first state, independent from released legacy matching tables."""

    def __init__(self, store: SQLiteStore) -> None:
        self.path = store.path

    def initialize(self) -> None:
        with self._connection("initialize Spotify-first state") as connection:
            self._ensure_tables(connection)

    def get_source_state(self, source_id: SourceId) -> SpotifyFirstSourceState | None:
        with self._connection("read Spotify-first source state") as connection:
            self._ensure_tables(connection)
            row = connection.execute(
                """
                SELECT source_id, spotify_show_id, last_refresh_at, refresh_reason,
                       detector_episode_id, api_episode_id, request_count,
                       catalogue_complete, coverage_proven, coverage_cutoff_at
                FROM spotify_first_source_state
                WHERE source_id = ?
                """,
                (str(source_id),),
            ).fetchone()
        return None if row is None else _source_state_from_row(row)

    def list_episodes(
        self,
        *,
        source_ids: Collection[SourceId] | None = None,
    ) -> tuple[SpotifyFirstEpisode, ...]:
        with self._connection("list Spotify-first episodes") as connection:
            self._ensure_tables(connection)
            if source_ids is None:
                rows = connection.execute(
                    """
                    SELECT source_id, spotify_show_id, spotify_episode_id,
                           spotify_episode_uri, title, edition_at, timestamp_source,
                           release_date_raw, release_date_precision, duration_seconds,
                           first_seen_at, last_seen_at
                    FROM spotify_first_episode_state
                    ORDER BY source_id, spotify_episode_id
                    """
                ).fetchall()
            else:
                values = tuple(sorted({str(source_id) for source_id in source_ids}))
                if not values:
                    return ()
                placeholders = ",".join("?" for _ in values)
                rows = connection.execute(
                    f"""
                    SELECT source_id, spotify_show_id, spotify_episode_id,
                           spotify_episode_uri, title, edition_at, timestamp_source,
                           release_date_raw, release_date_precision, duration_seconds,
                           first_seen_at, last_seen_at
                    FROM spotify_first_episode_state
                    WHERE source_id IN ({placeholders})
                    ORDER BY source_id, spotify_episode_id
                    """,
                    values,
                ).fetchall()
        return tuple(_episode_from_row(row) for row in rows)

    def replace_source_episodes(
        self,
        source_id: SourceId,
        episodes: Iterable[SpotifyFirstEpisode],
        *,
        coverage_proven: bool,
    ) -> UpsertCounts:
        values = tuple(episodes)
        if any(episode.source_id != source_id for episode in values):
            raise ValueError("Spotify-first replacement contains another source")
        if len({episode.spotify_episode_id for episode in values}) != len(values):
            raise ValueError("Spotify-first replacement contains duplicate episode ids")

        added = 0
        updated = 0
        unchanged = 0
        removed = 0
        with self._connection("replace Spotify-first source catalogue") as connection:
            self._ensure_tables(connection)
            rows = connection.execute(
                """
                SELECT source_id, spotify_show_id, spotify_episode_id,
                       spotify_episode_uri, title, edition_at, timestamp_source,
                       release_date_raw, release_date_precision, duration_seconds,
                       first_seen_at, last_seen_at
                FROM spotify_first_episode_state
                WHERE source_id = ?
                """,
                (str(source_id),),
            ).fetchall()
            existing = {_row_text(row, "spotify_episode_id"): row for row in rows}

            for episode in values:
                current = existing.get(episode.spotify_episode_id)
                if current is None:
                    added += 1
                elif _same_episode_metadata(current, episode):
                    unchanged += 1
                else:
                    updated += 1
                connection.execute(_UPSERT_EPISODE, _episode_values(episode))

            if coverage_proven:
                keep_ids = tuple(episode.spotify_episode_id for episode in values)
                if keep_ids:
                    placeholders = ",".join("?" for _ in keep_ids)
                    cursor = connection.execute(
                        f"""
                        DELETE FROM spotify_first_episode_state
                        WHERE source_id = ?
                          AND spotify_episode_id NOT IN ({placeholders})
                        """,
                        (str(source_id), *keep_ids),
                    )
                else:
                    cursor = connection.execute(
                        "DELETE FROM spotify_first_episode_state WHERE source_id = ?",
                        (str(source_id),),
                    )
                removed = max(cursor.rowcount, 0)

        return UpsertCounts(
            added=added,
            updated=updated,
            unchanged=unchanged,
            removed=removed,
        )

    def record_refresh(
        self,
        source: SourceDefinition,
        *,
        refreshed_at: datetime,
        reason: RefreshReason,
        detector_episode_id: str | None,
        api_episode_id: str | None,
        request_count: int,
        complete: bool,
        coverage_proven: bool,
        coverage_cutoff_at: datetime,
    ) -> None:
        if request_count < 0:
            raise ValueError("Spotify-first request_count must be non-negative")
        with self._connection("persist Spotify-first source refresh") as connection:
            self._ensure_tables(connection)
            connection.execute(
                """
                INSERT INTO spotify_first_source_state (
                    source_id,
                    spotify_show_id,
                    last_refresh_at,
                    refresh_reason,
                    detector_episode_id,
                    api_episode_id,
                    request_count,
                    catalogue_complete,
                    coverage_proven,
                    coverage_cutoff_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    spotify_show_id = excluded.spotify_show_id,
                    last_refresh_at = excluded.last_refresh_at,
                    refresh_reason = excluded.refresh_reason,
                    detector_episode_id = excluded.detector_episode_id,
                    api_episode_id = excluded.api_episode_id,
                    request_count = excluded.request_count,
                    catalogue_complete = excluded.catalogue_complete,
                    coverage_proven = excluded.coverage_proven,
                    coverage_cutoff_at = excluded.coverage_cutoff_at
                WHERE excluded.last_refresh_at >= spotify_first_source_state.last_refresh_at
                """,
                (
                    str(source.id),
                    spotify_show_id(source),
                    _format_timestamp(refreshed_at),
                    reason.value,
                    detector_episode_id,
                    api_episode_id,
                    request_count,
                    int(complete),
                    int(coverage_proven),
                    _format_timestamp(coverage_cutoff_at),
                ),
            )

    def get_destination_verification(
        self,
        playlist_id: PlaylistId,
    ) -> DestinationVerificationState | None:
        with self._connection("read Spotify-first destination verification") as connection:
            self._ensure_tables(connection)
            row = connection.execute(
                """
                SELECT playlist_id, destination_id, desired_fingerprint, verified_at
                FROM spotify_first_destination_state
                WHERE playlist_id = ?
                """,
                (str(playlist_id),),
            ).fetchone()
        if row is None:
            return None
        return DestinationVerificationState(
            playlist_id=PlaylistId(_row_text(row, "playlist_id")),
            destination_id=_row_text(row, "destination_id"),
            desired_fingerprint=_row_text(row, "desired_fingerprint"),
            verified_at=_parse_timestamp(_row_text(row, "verified_at")),
        )

    def record_destination_verification(
        self,
        playlist_id: PlaylistId,
        *,
        destination_id: str,
        desired_fingerprint: str,
        verified_at: datetime,
    ) -> None:
        if not destination_id.strip():
            raise ValueError("destination_id must not be blank")
        if len(desired_fingerprint) != 64:
            raise ValueError("desired fingerprint must be a SHA-256 hex digest")
        try:
            int(desired_fingerprint, 16)
        except ValueError as exc:
            raise ValueError("desired fingerprint must be a SHA-256 hex digest") from exc

        with self._connection("persist Spotify-first destination verification") as connection:
            self._ensure_tables(connection)
            connection.execute(
                """
                INSERT INTO spotify_first_destination_state (
                    playlist_id, destination_id, desired_fingerprint, verified_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(playlist_id) DO UPDATE SET
                    destination_id = excluded.destination_id,
                    desired_fingerprint = excluded.desired_fingerprint,
                    verified_at = excluded.verified_at
                WHERE excluded.verified_at >= spotify_first_destination_state.verified_at
                """,
                (
                    str(playlist_id),
                    destination_id.strip(),
                    desired_fingerprint,
                    _format_timestamp(verified_at),
                ),
            )

    def clear_destination_verification(self, playlist_id: PlaylistId) -> None:
        with self._connection("clear Spotify-first destination verification") as connection:
            self._ensure_tables(connection)
            connection.execute(
                "DELETE FROM spotify_first_destination_state WHERE playlist_id = ?",
                (str(playlist_id),),
            )

    def get_degraded_destination_verification(
        self,
        playlist_id: PlaylistId,
    ) -> DestinationVerificationState | None:
        with self._connection("read degraded Spotify-first destination verification") as connection:
            self._ensure_tables(connection)
            row = connection.execute(
                """
                SELECT playlist_id, destination_id, desired_fingerprint, verified_at
                FROM spotify_first_destination_degraded_state
                WHERE playlist_id = ?
                """,
                (str(playlist_id),),
            ).fetchone()
        if row is None:
            return None
        return DestinationVerificationState(
            playlist_id=PlaylistId(_row_text(row, "playlist_id")),
            destination_id=_row_text(row, "destination_id"),
            desired_fingerprint=_row_text(row, "desired_fingerprint"),
            verified_at=_parse_timestamp(_row_text(row, "verified_at")),
        )

    def record_degraded_destination_verification(
        self,
        playlist_id: PlaylistId,
        *,
        destination_id: str,
        desired_fingerprint: str,
        verified_at: datetime,
    ) -> None:
        if not destination_id.strip():
            raise ValueError("destination_id must not be blank")
        if len(desired_fingerprint) != 64:
            raise ValueError("desired fingerprint must be a SHA-256 hex digest")
        try:
            int(desired_fingerprint, 16)
        except ValueError as exc:
            raise ValueError("desired fingerprint must be a SHA-256 hex digest") from exc

        with self._connection(
            "persist degraded Spotify-first destination verification"
        ) as connection:
            self._ensure_tables(connection)
            connection.execute(
                """
                INSERT INTO spotify_first_destination_degraded_state (
                    playlist_id, destination_id, desired_fingerprint, verified_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(playlist_id) DO UPDATE SET
                    destination_id = excluded.destination_id,
                    desired_fingerprint = excluded.desired_fingerprint,
                    verified_at = excluded.verified_at
                WHERE excluded.verified_at >= spotify_first_destination_degraded_state.verified_at
                """,
                (
                    str(playlist_id),
                    destination_id.strip(),
                    desired_fingerprint,
                    _format_timestamp(verified_at),
                ),
            )

    def clear_degraded_destination_verification(self, playlist_id: PlaylistId) -> None:
        with self._connection(
            "clear degraded Spotify-first destination verification"
        ) as connection:
            self._ensure_tables(connection)
            connection.execute(
                "DELETE FROM spotify_first_destination_degraded_state WHERE playlist_id = ?",
                (str(playlist_id),),
            )

    def prune(self, *, before: datetime) -> int:
        with self._connection("prune Spotify-first episode history") as connection:
            self._ensure_tables(connection)
            cursor = connection.execute(
                "DELETE FROM spotify_first_episode_state WHERE last_seen_at < ?",
                (_format_timestamp(before),),
            )
            return max(cursor.rowcount, 0)

    @staticmethod
    def _ensure_tables(connection: sqlite3.Connection) -> None:
        connection.execute(_EPISODE_TABLE)
        connection.execute(_SOURCE_TABLE)
        connection.execute(_DESTINATION_TABLE)
        connection.execute(_DEGRADED_DESTINATION_TABLE)
        connection.execute(_EPISODE_INDEX)

    @contextmanager
    def _connection(self, operation: str) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path, timeout=10.0)
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


def _source_state_from_row(row: sqlite3.Row) -> SpotifyFirstSourceState:
    return SpotifyFirstSourceState(
        source_id=SourceId(_row_text(row, "source_id")),
        spotify_show_id=_row_text(row, "spotify_show_id"),
        last_refresh_at=_parse_timestamp(_row_text(row, "last_refresh_at")),
        reason=RefreshReason(_row_text(row, "refresh_reason")),
        detector_episode_id=_optional_text(row["detector_episode_id"]),
        api_episode_id=_optional_text(row["api_episode_id"]),
        request_count=_row_int(row, "request_count"),
        catalogue_complete=bool(_row_int(row, "catalogue_complete")),
        coverage_proven=bool(_row_int(row, "coverage_proven")),
        coverage_cutoff_at=_parse_timestamp(_row_text(row, "coverage_cutoff_at")),
    )


def _same_episode_metadata(row: sqlite3.Row, episode: SpotifyFirstEpisode) -> bool:
    return (
        _row_text(row, "spotify_show_id"),
        _row_text(row, "spotify_episode_uri"),
        _row_text(row, "title"),
        _optional_text(row["edition_at"]),
        _row_text(row, "timestamp_source"),
        _row_text(row, "release_date_raw"),
        _row_text(row, "release_date_precision"),
        _optional_int(row["duration_seconds"]),
    ) == (
        episode.spotify_show_id,
        episode.spotify_episode_uri,
        episode.title,
        None if episode.edition_at is None else _format_timestamp(episode.edition_at),
        episode.timestamp_source.value,
        episode.release_date_raw,
        episode.release_date_precision,
        episode.duration_seconds,
    )


def _episode_values(episode: SpotifyFirstEpisode) -> tuple[object, ...]:
    return (
        str(episode.source_id),
        episode.spotify_show_id,
        episode.spotify_episode_id,
        episode.spotify_episode_uri,
        episode.title,
        None if episode.edition_at is None else _format_timestamp(episode.edition_at),
        episode.timestamp_source.value,
        episode.release_date_raw,
        episode.release_date_precision,
        episode.duration_seconds,
        _format_timestamp(episode.first_seen_at),
        _format_timestamp(episode.last_seen_at),
    )


def _episode_from_row(row: sqlite3.Row) -> SpotifyFirstEpisode:
    edition_raw = _optional_text(row["edition_at"])
    return SpotifyFirstEpisode(
        source_id=SourceId(_row_text(row, "source_id")),
        spotify_show_id=_row_text(row, "spotify_show_id"),
        spotify_episode_id=_row_text(row, "spotify_episode_id"),
        spotify_episode_uri=_row_text(row, "spotify_episode_uri"),
        title=_row_text(row, "title"),
        edition_at=None if edition_raw is None else _parse_timestamp(edition_raw),
        timestamp_source=TimestampSource(_row_text(row, "timestamp_source")),
        release_date_raw=_row_text(row, "release_date_raw"),
        release_date_precision=_row_text(row, "release_date_precision"),
        duration_seconds=_optional_int(row["duration_seconds"]),
        first_seen_at=_parse_timestamp(_row_text(row, "first_seen_at")),
        last_seen_at=_parse_timestamp(_row_text(row, "last_seen_at")),
    )


def _format_timestamp(value: datetime) -> str:
    return as_utc(value).isoformat(timespec="microseconds")


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PersistenceError("Spotify-first timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PersistenceError("Spotify-first timestamp is not timezone-aware")
    return parsed.astimezone(UTC)


def _row_text(row: sqlite3.Row, key: str) -> str:
    value = row[key]
    if not isinstance(value, str):
        raise PersistenceError(f"Spotify-first {key} is not text")
    return value


def _row_int(row: sqlite3.Row, key: str) -> int:
    value = row[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise PersistenceError(f"Spotify-first {key} is not an integer")
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PersistenceError("Spotify-first optional text is invalid")
    return value


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise PersistenceError("Spotify-first optional integer is invalid")
    return value
