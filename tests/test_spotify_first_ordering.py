from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from news_bulletin_playlist.models import (
    AdapterId,
    CountryCode,
    DestinationReference,
    ExternalReference,
    LanguageTag,
    ParserId,
    PlaylistDefinition,
    PlaylistId,
    SourceDefinition,
    SourceId,
    SourceOrigin,
    SourceSelection,
)
from news_bulletin_playlist.spotify.first_desired import build_spotify_first_desired_state
from news_bulletin_playlist.spotify.first_models import (
    SpotifyFirstEpisode,
    TimestampSource,
    episode_evidence_period,
    episode_order_key,
)

RNE_SHOW = "A" * 22
SER_SHOW = "B" * 22
GENERIC_SHOW = "C" * 22


def _source(source_id: str, show_id: str, timezone: str) -> SourceDefinition:
    return SourceDefinition(
        id=SourceId(source_id),
        display_name=source_id,
        countries=(CountryCode("ES"),),
        origin=SourceOrigin.country(CountryCode("ES")),
        languages=(LanguageTag("es"),),
        timezone=ZoneInfo(timezone),
        enabled=True,
        parser_id=ParserId("release_date_title"),
        external_references=(ExternalReference("spotify", "show", show_id),),
    )


def _playlist(*source_ids: str) -> PlaylistDefinition:
    return PlaylistDefinition(
        id=PlaylistId("ordering"),
        display_name="ordering",
        description="ordering",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        enabled=True,
        source_selection=SourceSelection(explicit=tuple(SourceId(value) for value in source_ids)),
        destination=DestinationReference(AdapterId("spotify"), "playlist"),
    )


def _episode(
    source_id: str,
    show_id: str,
    episode_id: str,
    *,
    release_date: str,
    precision: str = "day",
    first_seen_at: datetime,
    edition_at: datetime | None = None,
    timestamp_source: TimestampSource = TimestampSource.FIRST_SEEN,
) -> SpotifyFirstEpisode:
    return SpotifyFirstEpisode(
        source_id=SourceId(source_id),
        spotify_show_id=show_id,
        spotify_episode_id=episode_id,
        spotify_episode_uri=f"spotify:episode:{episode_id}",
        title="Bulletin",
        edition_at=edition_at,
        timestamp_source=timestamp_source,
        release_date_raw=release_date,
        release_date_precision=precision,
        duration_seconds=300,
        first_seen_at=first_seen_at,
        last_seen_at=first_seen_at,
    )


def test_madrid_midnight_fallback_does_not_jump_above_later_exact_bulletin() -> None:
    """Regression for the production #458 mixed-calendar ordering failure."""
    rne = _source("rne", RNE_SHOW, "Europe/Madrid")
    ser = _source("ser", SER_SHOW, "Europe/Madrid")
    midnight_fallback = _episode(
        "rne",
        RNE_SHOW,
        "R" * 22,
        release_date="2026-10-02",
        first_seen_at=datetime(2026, 10, 1, 23, 7, 57, tzinfo=UTC),
    )
    later_exact = _episode(
        "ser",
        SER_SHOW,
        "S" * 22,
        release_date="2026-10-02",
        first_seen_at=datetime(2026, 10, 2, 16, 5, tzinfo=UTC),
        edition_at=datetime(2026, 10, 2, 16, 0, tzinfo=UTC),
        timestamp_source=TimestampSource.TITLE,
    )

    build = build_spotify_first_desired_state(
        _playlist("rne", "ser"),
        (midnight_fallback, later_exact),
        sources={rne.id: rne, ser.id: ser},
        now=datetime(2026, 10, 2, 17, 0, tzinfo=UTC),
    )

    assert build.desired.uris == (
        later_exact.spotify_episode_uri,
        midnight_fallback.spotify_episode_uri,
    )
    assert episode_order_key(midnight_fallback, rne.timezone)[0] == midnight_fallback.first_seen_at
    assert midnight_fallback.edition_at is None
    assert midnight_fallback.timestamp_source is TimestampSource.FIRST_SEEN


def test_bootstrap_fallback_is_capped_by_old_release_period_end() -> None:
    zone = ZoneInfo("Europe/Madrid")
    first_seen = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
    episode = _episode(
        "generic",
        GENERIC_SHOW,
        "D" * 22,
        release_date="2026-09-30",
        first_seen_at=first_seen,
    )

    _, release_end = episode_evidence_period(episode, zone)
    order_instant = episode_order_key(episode, zone)[0]

    assert release_end == datetime(2026, 9, 30, 21, 59, 59, 999999, tzinfo=UTC)
    assert order_instant == release_end
    assert order_instant < first_seen


@pytest.mark.parametrize(
    ("release_date", "precision", "expected_end"),
    [
        ("2026-09-30", "day", datetime(2026, 9, 30, 21, 59, 59, 999999, tzinfo=UTC)),
        ("2026-09", "month", datetime(2026, 9, 30, 21, 59, 59, 999999, tzinfo=UTC)),
        ("2026", "year", datetime(2026, 12, 31, 22, 59, 59, 999999, tzinfo=UTC)),
    ],
)
def test_coarse_precision_caps_late_first_seen_at_physical_period_end(
    release_date: str, precision: str, expected_end: datetime
) -> None:
    episode = _episode(
        "generic",
        GENERIC_SHOW,
        "E" * 22,
        release_date=release_date,
        precision=precision,
        first_seen_at=datetime(2027, 1, 2, 12, 0, tzinfo=UTC),
    )

    assert episode_order_key(episode, ZoneInfo("Europe/Madrid"))[0] == expected_end


@pytest.mark.parametrize(
    ("zone_name", "first_seen"),
    [
        ("Europe/Madrid", datetime(2026, 10, 1, 22, 30, tzinfo=UTC)),
        ("America/New_York", datetime(2026, 10, 2, 4, 30, tzinfo=UTC)),
    ],
)
def test_local_release_day_crossing_utc_midnight_keeps_real_first_seen_instant(
    zone_name: str, first_seen: datetime
) -> None:
    episode = _episode(
        "generic",
        GENERIC_SHOW,
        "F" * 22,
        release_date="2026-10-02",
        first_seen_at=first_seen,
    )

    assert episode_order_key(episode, ZoneInfo(zone_name))[0] == first_seen


@pytest.mark.parametrize(
    ("release_date", "expected_hours"),
    [("2026-03-08", 23), ("2026-11-01", 25)],
)
def test_release_interval_preserves_dst_day_width(release_date: str, expected_hours: int) -> None:
    episode = _episode(
        "generic",
        GENERIC_SHOW,
        "G" * 22,
        release_date=release_date,
        first_seen_at=datetime(2026, 11, 2, 12, 0, tzinfo=UTC),
    )

    start, end = episode_evidence_period(episode, ZoneInfo("America/New_York"))

    assert end - start + timedelta(microseconds=1) == timedelta(hours=expected_hours)


@pytest.mark.parametrize("reverse_input", [False, True])
def test_cross_source_fallback_order_uses_each_source_timezone(reverse_input: bool) -> None:
    # If both periods are treated as UTC, identity ties must produce the wrong order.
    madrid = _source("z_madrid", RNE_SHOW, "Europe/Madrid")
    new_york = _source("a_new_york", SER_SHOW, "America/New_York")
    first_seen = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    madrid_episode = _episode(
        "z_madrid",
        RNE_SHOW,
        "Z" * 22,
        release_date="2026-10-02",
        first_seen_at=first_seen,
    )
    new_york_episode = _episode(
        "a_new_york",
        SER_SHOW,
        "A" * 22,
        release_date="2026-10-02",
        first_seen_at=first_seen,
    )

    episodes = (madrid_episode, new_york_episode)
    if reverse_input:
        episodes = tuple(reversed(episodes))

    build = build_spotify_first_desired_state(
        _playlist("z_madrid", "a_new_york"),
        episodes,
        sources={madrid.id: madrid, new_york.id: new_york},
        now=datetime(2026, 10, 3, 13, 0, tzinfo=UTC),
    )

    assert episode_order_key(madrid_episode, madrid.timezone)[0] == datetime(
        2026, 10, 2, 21, 59, 59, 999999, tzinfo=UTC
    )
    assert episode_order_key(new_york_episode, new_york.timezone)[0] == datetime(
        2026, 10, 3, 3, 59, 59, 999999, tzinfo=UTC
    )
    assert build.desired.uris == (
        new_york_episode.spotify_episode_uri,
        madrid_episode.spotify_episode_uri,
    )


def test_mixed_exact_public_and_first_seen_use_one_physical_timeline() -> None:
    zone = ZoneInfo("UTC")
    title = _episode(
        "generic",
        GENERIC_SHOW,
        "H" * 22,
        release_date="2026-10-02",
        first_seen_at=datetime(2026, 10, 2, 10, 5, tzinfo=UTC),
        edition_at=datetime(2026, 10, 2, 10, 0, tzinfo=UTC),
        timestamp_source=TimestampSource.TITLE,
    )
    fallback = _episode(
        "generic",
        GENERIC_SHOW,
        "I" * 22,
        release_date="2026-10-02",
        first_seen_at=datetime(2026, 10, 2, 10, 30, tzinfo=UTC),
    )
    public = _episode(
        "generic",
        GENERIC_SHOW,
        "J" * 22,
        release_date="2026-10-02",
        first_seen_at=datetime(2026, 10, 2, 11, 5, tzinfo=UTC),
        edition_at=datetime(2026, 10, 2, 11, 0, tzinfo=UTC),
        timestamp_source=TimestampSource.SPOTIFY_PUBLIC_RELEASE,
    )

    ordered = sorted(
        (title, fallback, public),
        key=lambda episode: episode_order_key(episode, zone),
        reverse=True,
    )

    assert ordered == [public, fallback, title]
    assert title.timestamp_source is TimestampSource.TITLE
    assert public.timestamp_source is TimestampSource.SPOTIFY_PUBLIC_RELEASE
    assert fallback.timestamp_source is TimestampSource.FIRST_SEEN
    assert fallback.edition_at is None


def test_equal_coarse_fallback_bounds_have_stable_identity_tie_break() -> None:
    zone = ZoneInfo("Europe/Madrid")
    observed = datetime(2026, 10, 2, 10, 30, tzinfo=UTC)
    first = _episode(
        "generic",
        GENERIC_SHOW,
        "K" * 22,
        release_date="2026-10-02",
        first_seen_at=observed,
    )
    second = _episode(
        "generic",
        GENERIC_SHOW,
        "L" * 22,
        release_date="2026-10-02",
        first_seen_at=observed,
    )

    ordered = sorted(
        (first, second),
        key=lambda episode: episode_order_key(episode, zone),
        reverse=True,
    )

    assert [episode.spotify_episode_id for episode in ordered] == ["L" * 22, "K" * 22]
    assert all(episode.edition_at is None for episode in ordered)
    assert all(episode.timestamp_source is TimestampSource.FIRST_SEEN for episode in ordered)
