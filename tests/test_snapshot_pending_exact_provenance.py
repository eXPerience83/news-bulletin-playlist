from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from news_bulletin_playlist.models import PlaylistId
from news_bulletin_playlist.persistence import SQLiteStore
from news_bulletin_playlist.snapshot_pending import (
    PendingSnapshotConfirmation,
    PendingSnapshotJournal,
)

NOW = datetime(2026, 9, 13, 7, 0, tzinfo=UTC)
PLAYLIST_ID = PlaylistId("spain_spanish_news")
FINGERPRINT = "a" * 64


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    return store


def _pending(*, readback_exact: bool) -> PendingSnapshotConfirmation:
    return PendingSnapshotConfirmation(
        playlist_id=PLAYLIST_ID,
        destination_id="destination",
        baseline_snapshot_id="snapshot-a",
        expected_snapshot_id="snapshot-b",
        desired_fingerprint=FINGERPRINT,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        readback_exact=readback_exact,
    )


def test_pending_round_trip_preserves_exact_readback_provenance(tmp_path: Path) -> None:
    journal = PendingSnapshotJournal(_store(tmp_path))
    journal.set(_pending(readback_exact=True))

    restored = journal.get(PLAYLIST_ID)

    assert restored is not None
    assert restored.readback_exact is True


def test_legacy_pending_row_migrates_fail_closed_to_non_exact(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            CREATE TABLE spotify_pending_snapshot_confirmations (
                playlist_id TEXT PRIMARY KEY,
                destination_id TEXT NOT NULL,
                baseline_snapshot_id TEXT NOT NULL,
                expected_snapshot_id TEXT NOT NULL,
                desired_fingerprint TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO spotify_pending_snapshot_confirmations (
                playlist_id, destination_id, baseline_snapshot_id, expected_snapshot_id,
                desired_fingerprint, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(PLAYLIST_ID),
                "destination",
                "snapshot-a",
                "snapshot-b",
                FINGERPRINT,
                NOW.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                (NOW + timedelta(hours=1))
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z"),
            ),
        )
        connection.commit()

    restored = PendingSnapshotJournal(SQLiteStore(store.path)).get(PLAYLIST_ID)

    assert restored is not None
    assert restored.readback_exact is False

    with sqlite3.connect(store.path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(spotify_pending_snapshot_confirmations)"
            ).fetchall()
        }
    assert "readback_exact" in columns
