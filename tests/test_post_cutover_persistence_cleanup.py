from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from news_bulletin_playlist.models import PlaylistId
from news_bulletin_playlist.persistence import SQLiteStore

NOW = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)


def test_fresh_database_does_not_replay_retired_legacy_schema(tmp_path: Path) -> None:
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

    assert "playlist_state" in tables
    retired_tables = {
        "canonical_editions",
        "source_runs",
        "source_state",
        "spotify_matches",
        "playlist_runs",
        "spotify_planner_catalogue_state",
        "spotify_planner_destination_state",
        "spotify_planner_unresolved_state",
        "spotify_planner_match_inputs",
        "spotify_planner_matched_recovery_state",
        "spotify_planner_persistence_rejection_state",
    }
    assert tables.isdisjoint(retired_tables)
