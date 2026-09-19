from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from news_bulletin_playlist.models import SourceId
from news_bulletin_playlist.persistence import LATEST_SCHEMA_VERSION, SQLiteStore
from news_bulletin_playlist.spotify.first_models import SpotifyFirstEpisode, TimestampSource
from news_bulletin_playlist.spotify.first_persistence import SpotifyFirstJournal

NOW = datetime(2026, 9, 3, 9, 0, tzinfo=UTC)
SHOW_ID = "A" * 22
EPISODE_ID = "B" * 22


def _episode() -> SpotifyFirstEpisode:
    return SpotifyFirstEpisode(
        source_id=SourceId("ser"),
        spotify_show_id=SHOW_ID,
        spotify_episode_id=EPISODE_ID,
        spotify_episode_uri=f"spotify:episode:{EPISODE_ID}",
        title="Las noticias de la SER, 11:00 (03/09/2026)",
        edition_at=NOW,
        timestamp_source=TimestampSource.TITLE,
        release_date_raw="2026-09-03",
        release_date_precision="day",
        duration_seconds=305,
        first_seen_at=NOW,
        last_seen_at=NOW,
    )


def test_spotify_duration_round_trips_with_spotify_first_state(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    journal = SpotifyFirstJournal(store)
    journal.initialize()
    episode = _episode()

    counts = journal.replace_source_episodes(
        episode.source_id,
        (episode,),
        coverage_proven=True,
    )

    assert counts.added == 1
    persisted = journal.list_episodes(source_ids=(episode.source_id,))
    assert len(persisted) == 1
    assert persisted[0].spotify_episode_id == EPISODE_ID
    assert persisted[0].duration_seconds == 305


def test_schema_v2_database_migrates_duration_column_without_inventing_value(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        INSERT INTO schema_migrations(version, applied_at) VALUES (1, '2026-09-01T00:00:00Z');
        INSERT INTO schema_migrations(version, applied_at) VALUES (2, '2026-09-01T00:00:01Z');
        CREATE TABLE spotify_matches (
            source_id TEXT NOT NULL,
            source_native_id TEXT NOT NULL,
            status TEXT NOT NULL,
            spotify_episode_uri TEXT,
            diagnostics TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (source_id, source_native_id)
        );
        INSERT INTO spotify_matches VALUES (
            'ser', 'old', 'matched', 'spotify:episode:old', 'legacy', '2026-09-01T00:00:00Z'
        );
        """
    )
    connection.commit()
    connection.close()

    store = SQLiteStore(path)
    store.initialize()

    assert store.schema_version() == LATEST_SCHEMA_VERSION
    connection = sqlite3.connect(path)
    row = connection.execute(
        "SELECT spotify_duration_seconds FROM spotify_matches WHERE source_native_id='old'"
    ).fetchone()
    connection.close()
    assert row == (None,)
