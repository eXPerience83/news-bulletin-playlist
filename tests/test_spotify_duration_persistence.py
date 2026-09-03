from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from news_bulletin_playlist.models import CanonicalEdition, SourceId
from news_bulletin_playlist.persistence import LATEST_SCHEMA_VERSION, MatchStatus, SQLiteStore

NOW = datetime(2026, 9, 3, 9, 0, tzinfo=UTC)


def _edition() -> CanonicalEdition:
    return CanonicalEdition(
        source_id=SourceId("ser"),
        source_native_id="duration-state",
        title="Las noticias de la SER, 11:00 (03/09/2026)",
        published_at=NOW,
        edition_at=NOW,
        duration_seconds=300,
    )


def test_spotify_duration_round_trips_with_match_state(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    edition = _edition()
    store.upsert_editions((edition,), observed_at=NOW)
    store.set_match_state(
        edition.source_id,
        edition.source_native_id,
        status=MatchStatus.MATCHED,
        spotify_episode_uri="spotify:episode:duration",
        spotify_duration_seconds=305,
        updated_at=NOW,
    )

    state = store.get_match_state(edition.source_id, edition.source_native_id)
    assert state is not None
    assert state.spotify_duration_seconds == 305


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

    assert store.schema_version() == LATEST_SCHEMA_VERSION == 3
    connection = sqlite3.connect(path)
    row = connection.execute(
        "SELECT spotify_duration_seconds FROM spotify_matches WHERE source_native_id='old'"
    ).fetchone()
    connection.close()
    assert row == (None,)
