from __future__ import annotations

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
