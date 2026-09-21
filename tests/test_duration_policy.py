from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.desired_state import (
    DURATION_EXCEEDS_DEFAULT_MAX,
    DURATION_EXCEEDS_EXCEPTION_MAX,
    DURATION_EXCEPTION,
)
from news_bulletin_playlist.managed_state import (
    ManagedState,
    activate_template,
    compile_engine_config,
)
from news_bulletin_playlist.models import (
    AdapterId,
    CountryCode,
    DestinationReference,
    DurationPolicy,
    DurationPolicyException,
    ExternalReference,
    LanguageTag,
    ParserId,
    PlaylistDefinition,
    PlaylistId,
    SourceDefinition,
    SourceId,
    SourceSelection,
)
from news_bulletin_playlist.spotify.first_desired import build_spotify_first_desired_state
from news_bulletin_playlist.spotify.first_models import SpotifyFirstEpisode, TimestampSource

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
SER_SHOW_ID = "A" * 22
DW_SHOW_ID = "D" * 22


def _playlist() -> PlaylistDefinition:
    return PlaylistDefinition(
        id=PlaylistId("test"),
        display_name="test",
        description="test",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        enabled=True,
        source_selection=SourceSelection(explicit=(SourceId("ser"),)),
        destination=DestinationReference(AdapterId("spotify"), "playlist"),
        duration_policy=DurationPolicy(
            default_max_seconds=480,
            exceptions=(
                DurationPolicyException(
                    id="ser_morning_0800",
                    source_id=SourceId("ser"),
                    edition_local_time=time(8, 0),
                    max_seconds=1800,
                ),
            ),
        ),
    )


def _source(
    source_id: str = "ser",
    *,
    timezone: str = "Europe/Madrid",
    parser_id: str = "ser",
    show_id: str = SER_SHOW_ID,
) -> SourceDefinition:
    return SourceDefinition(
        id=SourceId(source_id),
        display_name=source_id,
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        timezone=ZoneInfo(timezone),
        enabled=True,
        parser_id=ParserId(parser_id),
        external_references=(ExternalReference("spotify", "show", show_id),),
    )


def _episode(
    hour_utc: int,
    duration: int | None,
    *,
    source_id: str = "ser",
    show_id: str = SER_SHOW_ID,
    semantic_time: bool = True,
) -> SpotifyFirstEpisode:
    episode_id = f"{hour_utc:02d}" + "B" * 20
    observed = datetime(2026, 9, 3, hour_utc, 2, tzinfo=UTC)
    edition_at = datetime(2026, 9, 3, hour_utc, tzinfo=UTC) if semantic_time else None
    return SpotifyFirstEpisode(
        source_id=SourceId(source_id),
        spotify_show_id=show_id,
        spotify_episode_id=episode_id,
        spotify_episode_uri=f"spotify:episode:{episode_id}",
        title="Bulletin",
        edition_at=edition_at,
        timestamp_source=TimestampSource.TITLE if semantic_time else TimestampSource.FIRST_SEEN,
        release_date_raw="2026-09-03",
        release_date_precision="day",
        duration_seconds=duration,
        first_seen_at=observed,
        last_seen_at=observed,
    )


def _build(
    episode: SpotifyFirstEpisode,
    *,
    playlist: PlaylistDefinition | None = None,
    source: SourceDefinition | None = None,
):
    selected_playlist = _playlist() if playlist is None else playlist
    selected_source = _source() if source is None else source
    return build_spotify_first_desired_state(
        selected_playlist,
        (episode,),
        sources={selected_source.id: selected_source},
        now=NOW,
    )


def test_default_ceiling_accepts_480_and_rejects_481_seconds() -> None:
    accepted = _build(_episode(9, 480))
    assert accepted.desired.uris == ("spotify:episode:" + "09" + "B" * 20,)

    rejected = _build(_episode(10, 481))
    assert rejected.desired.uris == ()
    decision = rejected.desired.duration_decisions[0]
    assert decision.reason == DURATION_EXCEEDS_DEFAULT_MAX
    assert decision.max_seconds == 480


def test_dw_long_episode_is_source_eligible_but_destination_duration_excludes_it() -> None:
    playlist = PlaylistDefinition(
        id=PlaylistId("dw"),
        display_name="DW",
        description="DW",
        countries=(CountryCode("DE"),),
        languages=(LanguageTag("es"),),
        enabled=True,
        source_selection=SourceSelection(explicit=(SourceId("dw_actualidad"),)),
        destination=DestinationReference(AdapterId("spotify"), "playlist"),
        duration_policy=DurationPolicy(default_max_seconds=1800),
    )
    source = _source(
        "dw_actualidad",
        timezone="Europe/Berlin",
        parser_id="release_date_title",
        show_id=DW_SHOW_ID,
    )
    episode = _episode(
        11,
        1901,
        source_id="dw_actualidad",
        show_id=DW_SHOW_ID,
        semantic_time=False,
    )

    built = _build(episode, playlist=playlist, source=source)

    assert built.desired.uris == ()
    assert built.desired.duration_decisions[0].reason == DURATION_EXCEEDS_DEFAULT_MAX


def test_ser_0800_exception_uses_source_timezone_and_is_bounded() -> None:
    # 06:00 UTC is 08:00 Europe/Madrid in September.
    accepted = _build(_episode(6, 1200))
    assert accepted.desired.uris == ("spotify:episode:" + "06" + "B" * 20,)
    assert accepted.desired.duration_decisions[0].reason == DURATION_EXCEPTION
    assert accepted.desired.duration_decisions[0].exception_id == "ser_morning_0800"

    rejected = _build(_episode(6, 1801))
    assert rejected.desired.uris == ()
    assert rejected.desired.duration_decisions[0].reason == DURATION_EXCEEDS_EXCEPTION_MAX
    assert rejected.desired.duration_decisions[0].max_seconds == 1800


def test_long_ser_non_0800_and_missing_semantic_time_do_not_match_exception() -> None:
    seven_local = _build(_episode(5, 1200))
    assert seven_local.desired.duration_decisions[0].reason == DURATION_EXCEEDS_DEFAULT_MAX

    no_semantic = _build(_episode(9, 1200, semantic_time=False))
    assert no_semantic.desired.duration_decisions[0].reason == DURATION_EXCEEDS_DEFAULT_MAX


def test_unknown_spotify_duration_is_excluded_and_counted() -> None:
    built = _build(_episode(9, None))

    assert built.desired.uris == ()
    assert built.desired.duration_decisions == ()
    assert built.duration_unavailable_count == 1


def test_builtin_catalog_and_managed_compile_use_current_global_30_minute_default() -> None:
    for template in BUILTIN_CATALOG.playlists:
        assert template.duration_policy.default_max_seconds == 1800
        assert template.duration_policy.exceptions == ()

    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    managed = activate_template(template, "destination")
    assert managed.max_duration_seconds == 1800
    state = ManagedState(playlists=(managed,))
    compiled = compile_engine_config(BUILTIN_CATALOG, state)
    assert compiled.playlists[0].duration_policy.default_max_seconds == 1800
    assert compiled.playlists[0].duration_policy.exceptions == ()


