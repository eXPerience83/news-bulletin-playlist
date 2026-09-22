from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from news_bulletin_playlist.models import SourceId
from news_bulletin_playlist.persistence import SQLiteStore
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

    assert counts.removed == 0
    persisted = journal.list_episodes(source_ids=(episode.source_id,))
    assert len(persisted) == 1
    assert persisted[0].spotify_episode_id == EPISODE_ID
    assert persisted[0].duration_seconds == 305
