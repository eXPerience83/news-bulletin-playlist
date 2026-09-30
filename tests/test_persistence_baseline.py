from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from news_bulletin_playlist.diagnostics import DiagnosticEventStore
from news_bulletin_playlist.models import PlaylistId, SourceId
from news_bulletin_playlist.persistence import (
    DATABASE_FORMAT_VERSION,
    PersistenceError,
    SQLiteStore,
)
from news_bulletin_playlist.snapshot_pending import PendingSnapshotJournal
from news_bulletin_playlist.spotify.first_models import SpotifyFirstEpisode, TimestampSource
from news_bulletin_playlist.spotify.first_persistence import SpotifyFirstJournal
from news_bulletin_playlist.spotify.public_detector import PublicDetectorJournal
from news_bulletin_playlist.spotify_backoff import SpotifyRateLimitJournal
from news_bulletin_playlist.spotify_metrics import SpotifyRequestMetrics

EXPECTED_CLEAN_TABLES = {
    "database_format",
    "diagnostic_events",
    "playlist_state",
    "spotify_first_destination_degraded_state",
    "spotify_first_destination_state",
    "spotify_first_episode_state",
    "spotify_first_episode_history",
    "spotify_first_source_state",
    "spotify_pending_snapshot_confirmations",
    "spotify_playlist_attestations",
    "spotify_public_detector_state",
    "spotify_rate_limit_backoff",
    "spotify_request_metrics",
    "spotify_request_metrics_coverage",
}


def _tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            ).fetchall()
        }


def test_clean_baseline_initializes_only_current_spotify_first_schema(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()

    DiagnosticEventStore(store.path).initialize()
    SpotifyRequestMetrics(store.path).initialize()
    SpotifyFirstJournal(store).initialize()
    PublicDetectorJournal(store).initialize()
    PendingSnapshotJournal(store).get(PlaylistId("playlist"))
    SpotifyRateLimitJournal(store).get()

    assert store.format_version() == DATABASE_FORMAT_VERSION == 3
    assert _tables(store.path) == EXPECTED_CLEAN_TABLES


def test_pre_baseline_private_database_is_rejected_explicitly(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations(version, applied_at)
            VALUES (8, '2026-09-21T00:00:00Z');
            CREATE TABLE playlist_state (
                playlist_id TEXT PRIMARY KEY,
                last_attempt_at TEXT NOT NULL,
                last_success_at TEXT,
                last_error TEXT
            );
            """
        )

    with pytest.raises(PersistenceError, match="predates the supported 0.1.0 clean format"):
        SQLiteStore(path).initialize()


def test_newer_database_format_is_rejected_explicitly(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE database_format SET format_version = ? WHERE singleton = 1",
            (DATABASE_FORMAT_VERSION + 1,),
        )

    with pytest.raises(PersistenceError, match="newer than supported"):
        store.initialize()


def test_clean_format_missing_required_table_fails_closed(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TABLE spotify_playlist_attestations")

    with pytest.raises(PersistenceError, match="missing required tables"):
        store.initialize()


def test_new_baseline_marks_request_history_incomplete(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()

    with sqlite3.connect(store.path) as connection:
        row = connection.execute(
            "SELECT incomplete_at FROM spotify_request_metrics_coverage WHERE singleton = 1"
        ).fetchone()

    assert row is not None
    snapshot = SpotifyRequestMetrics(store.path).last_24_hours()
    assert snapshot.coverage_complete is False


def _format_one_store(path: Path, *, with_episodes: bool) -> SQLiteStore:
    """Construct a real format-1 SQLite database without the format-2 table."""
    store = SQLiteStore(path)
    store.initialize()
    if with_episodes:
        journal = SpotifyFirstJournal(store)
        journal.initialize()
        now = datetime(2026, 9, 24, tzinfo=UTC)
        episode_id = "B" * 22
        journal.replace_source_episodes(
            SourceId("ser"),
            (
                SpotifyFirstEpisode(
                    source_id=SourceId("ser"),
                    spotify_show_id="A" * 22,
                    spotify_episode_id=episode_id,
                    spotify_episode_uri=f"spotify:episode:{episode_id}",
                    title="Accepted",
                    edition_at=None,
                    timestamp_source=TimestampSource.FIRST_SEEN,
                    release_date_raw="2026-09-24",
                    release_date_precision="day",
                    duration_seconds=305,
                    first_seen_at=now,
                    last_seen_at=now,
                ),
            ),
            coverage_proven=True,
        )
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE spotify_first_episode_history")
        connection.execute("UPDATE database_format SET format_version = 1")
    return store


@pytest.mark.parametrize("with_episodes", [False, True])
def test_format_one_migrates_atomically_and_backfills_existing_accepted_rows(
    tmp_path: Path, *, with_episodes: bool
) -> None:
    store = _format_one_store(tmp_path / "state.sqlite3", with_episodes=with_episodes)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "INSERT INTO playlist_state(playlist_id, last_attempt_at) VALUES (?, ?)",
            ("existing", "2026-09-24T00:00:00.000000Z"),
        )
    before = _tables(store.path)
    store.initialize()
    assert _tables(store.path) == before | {"spotify_first_episode_history"}
    journal = SpotifyFirstJournal(store)
    journal.initialize()
    history = journal.list_episode_history(
        observed_since=datetime(2026, 9, 1, tzinfo=UTC),
        observed_until=datetime(2026, 10, 1, tzinfo=UTC),
    )
    assert store.format_version() == 3
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT playlist_id FROM playlist_state").fetchone() == (
            "existing",
        )
    assert len(history) == int(with_episodes)
    if with_episodes:
        assert history[0].duration_seconds == 305
        assert (
            history[0].first_seen_at == history[0].last_seen_at == datetime(2026, 9, 24, tzinfo=UTC)
        )
        assert journal.list_episodes() == history
    store.initialize()
    assert (
        journal.list_episode_history(
            observed_since=datetime(2026, 9, 1, tzinfo=UTC),
            observed_until=datetime(2026, 10, 1, tzinfo=UTC),
        )
        == history
    )


def test_failed_format_one_migration_keeps_old_version_and_rows(tmp_path: Path) -> None:
    store = _format_one_store(tmp_path / "state.sqlite3", with_episodes=True)
    with sqlite3.connect(store.path) as connection:
        # A conflicting table makes the real CREATE TABLE migration fail.
        connection.execute("CREATE TABLE spotify_first_episode_history (unexpected TEXT)")
    with pytest.raises(PersistenceError, match="initialize database failed"):
        store.initialize()
    assert store.format_version() == 1
    with sqlite3.connect(store.path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM spotify_first_episode_state").fetchone()
        assert count == (1,)
        assert (
            connection.execute("PRAGMA table_info(spotify_first_episode_history)").fetchall()[0][1]
            == "unexpected"
        )


def test_backfill_failure_rolls_back_new_table_and_version(tmp_path: Path) -> None:
    store = _format_one_store(tmp_path / "state.sqlite3", with_episodes=True)
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute("UPDATE spotify_first_episode_state SET duration_seconds = -1")
    with pytest.raises(PersistenceError, match="initialize database failed"):
        store.initialize()
    assert store.format_version() == 1
    assert "spotify_first_episode_history" not in _tables(store.path)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT duration_seconds FROM spotify_first_episode_state"
        ).fetchone() == (-1,)
