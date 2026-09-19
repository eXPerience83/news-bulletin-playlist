from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone

import pytest

from news_bulletin_playlist.models import SourceId
from news_bulletin_playlist.spotify.first_models import SpotifyFirstEpisode, TimestampSource

SHOW_ID = "A" * 22
EPISODE_A = "B" * 22
EPISODE_B = "C" * 22


def _episode(
    episode_id: str = EPISODE_A,
    *,
    edition_at: datetime | None = datetime(
        2026,
        8,
        25,
        18,
        30,
        tzinfo=timezone(timedelta(hours=2)),
    ),
    first_seen_at: datetime = datetime(2026, 8, 25, 16, 31, tzinfo=UTC),
    last_seen_at: datetime = datetime(2026, 8, 25, 16, 32, tzinfo=UTC),
    duration_seconds: int | None = 300,
) -> SpotifyFirstEpisode:
    return SpotifyFirstEpisode(
        source_id=SourceId("rne"),
        spotify_show_id=SHOW_ID,
        spotify_episode_id=episode_id,
        spotify_episode_uri=f"spotify:episode:{episode_id}",
        title="NOTICIAS RNE - 25.08.2026 - 18.30 H",
        edition_at=edition_at,
        timestamp_source=TimestampSource.TITLE,
        release_date_raw="2026-08-25",
        release_date_precision="day",
        duration_seconds=duration_seconds,
        first_seen_at=first_seen_at,
        last_seen_at=last_seen_at,
    )


def test_spotify_first_episode_normalizes_semantic_time_and_is_frozen() -> None:
    episode = _episode()

    assert episode.edition_at == datetime(2026, 8, 25, 16, 30, tzinfo=UTC)
    with pytest.raises(FrozenInstanceError):
        episode.title = "changed"  # type: ignore[misc]


def test_distinct_spotify_episode_ids_can_share_one_apparent_edition() -> None:
    first = _episode(EPISODE_A)
    second = _episode(EPISODE_B)

    assert first.spotify_episode_id != second.spotify_episode_id
    assert first.edition_at == second.edition_at


@pytest.mark.parametrize("field", ["edition_at", "first_seen_at", "last_seen_at"])
def test_spotify_first_episode_rejects_naive_datetimes(field: str) -> None:
    values = {
        "edition_at": datetime(2026, 8, 25, 16, 30, tzinfo=UTC),
        "first_seen_at": datetime(2026, 8, 25, 16, 31, tzinfo=UTC),
        "last_seen_at": datetime(2026, 8, 25, 16, 32, tzinfo=UTC),
    }
    values[field] = datetime(2026, 8, 25, 16, 30)

    with pytest.raises(ValueError, match="timestamp must be timezone-aware"):
        _episode(
            edition_at=values["edition_at"],
            first_seen_at=values["first_seen_at"],
            last_seen_at=values["last_seen_at"],
        )


def test_duration_must_be_non_negative() -> None:
    with pytest.raises(ValueError, match="duration_seconds"):
        _episode(duration_seconds=-1)
