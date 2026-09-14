from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from news_bulletin_playlist.models import PlaylistId
from news_bulletin_playlist.soft_retention_engine import _SoftRetentionStore


class _RunnerStub:
    def __init__(self, playlist_id: PlaylistId) -> None:
        self._soft_retention_deferred_applied_counts = {playlist_id: 2}
        self._soft_retention_recorded_applied_counts: dict[PlaylistId, int] = {}

    def _soft_retention_applied_count(self, *_args: Any, **_kwargs: Any) -> int:
        raise AssertionError("recording must not recompute soft-retention evidence")


def test_record_playlist_run_uses_already_proven_physical_count(tmp_path: Path) -> None:
    playlist_id = PlaylistId("playlist")
    runner = _RunnerStub(playlist_id)
    store = _SoftRetentionStore(tmp_path / "state.sqlite3", runner)  # type: ignore[arg-type]
    store.initialize()
    observed_at = datetime(2026, 9, 14, 8, 0, tzinfo=UTC)

    store.record_playlist_run(
        playlist_id,
        started_at=observed_at,
        finished_at=observed_at,
        ok=True,
        desired_count=1,
        applied_count=1,
    )

    with sqlite3.connect(store.path) as connection:
        counts = connection.execute(
            """
            SELECT desired_count, applied_count
            FROM playlist_runs
            WHERE playlist_id = ?
            """,
            (str(playlist_id),),
        ).fetchone()

    assert counts == (1, 2)
    assert runner._soft_retention_recorded_applied_counts[playlist_id] == 2
