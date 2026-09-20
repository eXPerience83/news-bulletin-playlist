from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from news_bulletin_playlist.models import PlaylistId
from news_bulletin_playlist.persistence import SQLiteStore

NOW = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)


def test_released_legacy_run_tables_remain_present_but_inert(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()

    store.record_playlist_state(
        PlaylistId("playlist"),
        started_at=NOW,
        finished_at=NOW,
        ok=True,
    )

    state = store.get_playlist_state(PlaylistId("playlist"))
    assert state is not None
    assert state.last_attempt_at == NOW
    assert state.last_success_at == NOW
    assert state.last_error is None

    with sqlite3.connect(store.path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        playlist_run_count = connection.execute("SELECT COUNT(*) FROM playlist_runs").fetchone()[0]
        source_run_count = connection.execute("SELECT COUNT(*) FROM source_runs").fetchone()[0]

    assert {
        "source_runs",
        "source_state",
        "playlist_runs",
        "canonical_editions",
        "spotify_matches",
    } <= tables
    assert playlist_run_count == 0
    assert source_run_count == 0

    retired_planner_tables = {
        "spotify_planner_catalogue_state",
        "spotify_planner_destination_state",
        "spotify_planner_unresolved_state",
        "spotify_planner_match_inputs",
        "spotify_planner_matched_recovery_state",
        "spotify_planner_persistence_rejection_state",
    }
    assert tables.isdisjoint(retired_planner_tables)

    with sqlite3.connect(store.path) as connection:
        spotify_match_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(spotify_matches)").fetchall()
        }
    assert "spotify_duration_seconds" not in spotify_match_columns
