"""SQLite persistence for the isolated Spotify-first Phase C shadow."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import cast

from news_bulletin_playlist.models import SourceDefinition, SourceId
from news_bulletin_playlist.persistence import PersistenceError, SQLiteStore
from news_bulletin_playlist.spotify.first_models import (
    RefreshReason, SourceShadowState, SpotifyFirstEpisode, TimestampSource, UpsertCounts, as_utc,
)
from news_bulletin_playlist.spotify.public_detector import spotify_show_id

_EPISODE_TABLE = """CREATE TABLE IF NOT EXISTS spotify_first_episode_state (
 source_id TEXT NOT NULL, spotify_show_id TEXT NOT NULL, spotify_episode_id TEXT NOT NULL,
 spotify_episode_uri TEXT NOT NULL, title TEXT NOT NULL, edition_at TEXT,
 timestamp_source TEXT NOT NULL CHECK(timestamp_source IN ('title','first_seen')),
 release_date_raw TEXT NOT NULL, release_date_precision TEXT NOT NULL,
 duration_seconds INTEGER, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
 PRIMARY KEY(source_id, spotify_episode_id))"""
_SOURCE_TABLE = """CREATE TABLE IF NOT EXISTS spotify_first_source_state (
 source_id TEXT PRIMARY KEY, spotify_show_id TEXT NOT NULL, last_refresh_at TEXT NOT NULL,
 refresh_reason TEXT NOT NULL, detector_episode_id TEXT, api_episode_id TEXT,
 request_count INTEGER NOT NULL, catalogue_complete INTEGER NOT NULL,
 coverage_proven INTEGER NOT NULL, coverage_cutoff_at TEXT NOT NULL)"""
_UPSERT_EPISODE = """INSERT INTO spotify_first_episode_state(
 source_id,spotify_show_id,spotify_episode_id,spotify_episode_uri,title,edition_at,
 timestamp_source,release_date_raw,release_date_precision,duration_seconds,first_seen_at,last_seen_at)
 VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id,spotify_episode_id) DO UPDATE SET
 spotify_show_id=excluded.spotify_show_id,spotify_episode_uri=excluded.spotify_episode_uri,
 title=excluded.title,edition_at=excluded.edition_at,timestamp_source=excluded.timestamp_source,
 release_date_raw=excluded.release_date_raw,release_date_precision=excluded.release_date_precision,
 duration_seconds=excluded.duration_seconds,
 first_seen_at=MIN(spotify_first_episode_state.first_seen_at,excluded.first_seen_at),
 last_seen_at=MAX(spotify_first_episode_state.last_seen_at,excluded.last_seen_at)
 WHERE excluded.last_seen_at >= spotify_first_episode_state.last_seen_at"""


class SpotifyFirstJournal:
    """Phase C state kept separate from legacy canonical/match tables."""

    def __init__(self, store: SQLiteStore) -> None:
        self.path = store.path

    def initialize(self) -> None:
        with self._connection("initialize Spotify-first shadow") as db:
            self._ensure(db)

    def get_source_state(self, source_id: SourceId) -> SourceShadowState | None:
        with self._connection("read Spotify-first source state") as db:
            self._ensure(db)
            row = db.execute(
                "SELECT * FROM spotify_first_source_state WHERE source_id=?", (str(source_id),)
            ).fetchone()
        if row is None:
            return None
        return SourceShadowState(
            SourceId(_text(row,"source_id")), _text(row,"spotify_show_id"),
            _parse_time(_text(row,"last_refresh_at")), RefreshReason(_text(row,"refresh_reason")),
            _optional_text(row["detector_episode_id"]), _optional_text(row["api_episode_id"]),
            _integer(row,"request_count"), bool(_integer(row,"catalogue_complete")),
            bool(_integer(row,"coverage_proven")), _parse_time(_text(row,"coverage_cutoff_at")),
        )

    def list_episodes(self) -> tuple[SpotifyFirstEpisode, ...]:
        with self._connection("list Spotify-first episodes") as db:
            self._ensure(db)
            rows = db.execute(
                "SELECT * FROM spotify_first_episode_state ORDER BY source_id,spotify_episode_id"
            ).fetchall()
        return tuple(_episode_from_row(row) for row in rows)

    def upsert_episodes(self, episodes: Iterable[SpotifyFirstEpisode]) -> UpsertCounts:
        values = tuple(episodes)
        if not values:
            return UpsertCounts(0,0,0)
        if len({item.source_id for item in values}) != 1:
            raise ValueError("one source required per Spotify-first upsert")
        if len({item.spotify_episode_id for item in values}) != len(values):
            raise ValueError("duplicate Spotify episode id in refresh")
        source_id = str(values[0].source_id)
        with self._connection("upsert Spotify-first episodes") as db:
            self._ensure(db)
            existing = {
                _text(row,"spotify_episode_id"): row
                for row in db.execute(
                    "SELECT * FROM spotify_first_episode_state WHERE source_id=?", (source_id,)
                ).fetchall()
            }
            added = updated = unchanged = 0
            for episode in values:
                row = existing.get(episode.spotify_episode_id)
                if row is None: added += 1
                elif _same_metadata(row, episode): unchanged += 1
                else: updated += 1
                db.execute(_UPSERT_EPISODE, _episode_row(episode))
        return UpsertCounts(added,updated,unchanged)

    def record_refresh(
        self, source: SourceDefinition, *, refreshed_at: datetime, reason: RefreshReason,
        detector_episode_id: str | None, api_episode_id: str | None, request_count: int,
        complete: bool, coverage_proven: bool, coverage_cutoff_at: datetime,
    ) -> None:
        with self._connection("persist Spotify-first source refresh") as db:
            self._ensure(db)
            db.execute(
                """INSERT INTO spotify_first_source_state VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source_id) DO UPDATE SET spotify_show_id=excluded.spotify_show_id,
                last_refresh_at=excluded.last_refresh_at,refresh_reason=excluded.refresh_reason,
                detector_episode_id=excluded.detector_episode_id,api_episode_id=excluded.api_episode_id,
                request_count=excluded.request_count,catalogue_complete=excluded.catalogue_complete,
                coverage_proven=excluded.coverage_proven,coverage_cutoff_at=excluded.coverage_cutoff_at
                WHERE excluded.last_refresh_at >= spotify_first_source_state.last_refresh_at""",
                (str(source.id),spotify_show_id(source),_format_time(refreshed_at),reason.value,
                 detector_episode_id,api_episode_id,request_count,int(complete),int(coverage_proven),
                 _format_time(coverage_cutoff_at)),
            )

    def prune(self, *, before: datetime) -> int:
        with self._connection("prune Spotify-first episodes") as db:
            self._ensure(db)
            row = db.execute(
                "DELETE FROM spotify_first_episode_state WHERE last_seen_at < ?", (_format_time(before),)
            )
            return max(row.rowcount,0)

    @staticmethod
    def _ensure(db: sqlite3.Connection) -> None:
        db.execute(_EPISODE_TABLE); db.execute(_SOURCE_TABLE)

    @contextmanager
    def _connection(self, operation: str) -> Iterator[sqlite3.Connection]:
        db: sqlite3.Connection | None = None
        try:
            db=sqlite3.connect(self.path,timeout=10.0); db.row_factory=sqlite3.Row
            db.execute("PRAGMA busy_timeout=5000")
            with db: yield db
        except sqlite3.Error as exc:
            raise PersistenceError(f"{operation} failed for {self.path}: {exc}") from exc
        finally:
            if db is not None: db.close()


def _same_metadata(row: sqlite3.Row, episode: SpotifyFirstEpisode) -> bool:
    return tuple(row[key] for key in (
        "spotify_show_id","spotify_episode_uri","title","edition_at","timestamp_source",
        "release_date_raw","release_date_precision","duration_seconds")) == (
        episode.spotify_show_id,episode.spotify_episode_uri,episode.title,
        None if episode.edition_at is None else _format_time(episode.edition_at),
        episode.timestamp_source.value,episode.release_date_raw,episode.release_date_precision,
        episode.duration_seconds)


def _episode_row(episode: SpotifyFirstEpisode) -> tuple[object,...]:
    return (str(episode.source_id),episode.spotify_show_id,episode.spotify_episode_id,
            episode.spotify_episode_uri,episode.title,
            None if episode.edition_at is None else _format_time(episode.edition_at),
            episode.timestamp_source.value,episode.release_date_raw,episode.release_date_precision,
            episode.duration_seconds,_format_time(episode.first_seen_at),_format_time(episode.last_seen_at))


def _episode_from_row(row: sqlite3.Row) -> SpotifyFirstEpisode:
    edition=_optional_text(row["edition_at"]); duration=row["duration_seconds"]
    if duration is not None and not isinstance(duration,int):
        raise PersistenceError("invalid Spotify-first duration")
    return SpotifyFirstEpisode(
        SourceId(_text(row,"source_id")),_text(row,"spotify_show_id"),_text(row,"spotify_episode_id"),
        _text(row,"spotify_episode_uri"),_text(row,"title"),None if edition is None else _parse_time(edition),
        TimestampSource(_text(row,"timestamp_source")),_text(row,"release_date_raw"),
        _text(row,"release_date_precision"),cast(int | None,duration),
        _parse_time(_text(row,"first_seen_at")),_parse_time(_text(row,"last_seen_at")))


def _format_time(value: datetime) -> str: return as_utc(value).isoformat(timespec="microseconds")

def _parse_time(value: str) -> datetime:
    try: parsed=datetime.fromisoformat(value)
    except ValueError as exc: raise PersistenceError("invalid Spotify-first timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None: raise PersistenceError("naive Spotify-first timestamp")
    return parsed.astimezone(UTC)

def _text(row: sqlite3.Row,key: str) -> str:
    value=row[key]
    if not isinstance(value,str): raise PersistenceError(f"Spotify-first {key} is not text")
    return value

def _optional_text(value: object) -> str | None:
    if value is None: return None
    if not isinstance(value,str): raise PersistenceError("Spotify-first optional text is invalid")
    return value

def _integer(row: sqlite3.Row,key: str) -> int:
    value=row[key]
    if isinstance(value,bool) or not isinstance(value,int): raise PersistenceError(f"Spotify-first {key} is not integer")
    return value
