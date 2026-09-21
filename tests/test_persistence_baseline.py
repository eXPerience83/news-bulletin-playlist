from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from news_bulletin_playlist.diagnostics import DiagnosticEventStore
from news_bulletin_playlist.models import PlaylistId
from news_bulletin_playlist.persistence import (
    DATABASE_FORMAT_VERSION,
    PersistenceError,
    SQLiteStore,
)
from news_bulletin_playlist.snapshot_pending import PendingSnapshotJournal
from news_bulletin_playlist.spotify_backoff import SpotifyRateLimitJournal
from news_bulletin_playlist.spotify_metrics import SpotifyRequestMetrics
from news_bulletin_playlist.spotify.first_persistence import SpotifyFirstJournal
from news_bulletin_playlist.spotify.public_detector import PublicDetectorJournal


EXPECTED_CLEAN_TABLES = {
    "database_format",
    "diagnostic_events",
    "playlist_state",
    "spotify_first_destination_degraded_state",
    "spotify_first_destination_state",
    "spotify_first_episode_state",
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

    assert store.format_version() == DATABASE_FORMAT_VERSION == 1
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
