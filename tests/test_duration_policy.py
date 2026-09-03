from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.config import ConfigError, parse_config
from news_bulletin_playlist.desired_state import (
    DURATION_EXCEEDS_DEFAULT_MAX,
    DURATION_EXCEEDS_EXCEPTION_MAX,
    DURATION_EXCEPTION,
    DesiredStateError,
    build_playlist_desired_state,
)
from news_bulletin_playlist.managed_state import (
    ManagedState,
    activate_template,
    compile_engine_config,
)
from news_bulletin_playlist.models import (
    AdapterId,
    CanonicalEdition,
    CountryCode,
    DestinationReference,
    DurationPolicy,
    DurationPolicyException,
    LanguageTag,
    PlaylistDefinition,
    PlaylistId,
    SourceId,
    SourceSelection,
)
from news_bulletin_playlist.persistence import EditionMatch, MatchStatus

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
MADRID = ZoneInfo("Europe/Madrid")


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
            exceptions=(
                DurationPolicyException(
                    id="ser_morning_0800",
                    source_id=SourceId("ser"),
                    edition_local_time=time(8, 0),
                    max_seconds=1800,
                ),
            )
        ),
    )


def _edition(hour_utc: int, *, edition_at: datetime | None = None) -> CanonicalEdition:
    semantic = edition_at if edition_at is not None else datetime(2026, 9, 3, hour_utc, tzinfo=UTC)
    return CanonicalEdition(
        source_id=SourceId("ser"),
        source_native_id=f"ser-{hour_utc}",
        title="SER bulletin",
        published_at=datetime(2026, 9, 3, hour_utc, 2, tzinfo=UTC),
        edition_at=semantic,
    )


def _match(edition: CanonicalEdition, duration: int | None) -> EditionMatch:
    return EditionMatch(
        source_id=edition.source_id,
        source_native_id=edition.source_native_id,
        status=MatchStatus.MATCHED,
        spotify_episode_uri=f"spotify:episode:{edition.source_native_id}",
        diagnostics="matched",
        updated_at=NOW,
        spotify_duration_seconds=duration,
    )


def _build(edition: CanonicalEdition, duration: int | None):
    return build_playlist_desired_state(
        _playlist(),
        (edition,),
        {edition.identity: _match(edition, duration)},
        now=NOW,
        source_timezones={SourceId("ser"): MADRID},
    )


def test_default_ceiling_accepts_480_and_rejects_481_seconds() -> None:
    accepted = _edition(9)
    state = _build(accepted, 480)
    assert state.uris == ("spotify:episode:ser-9",)

    rejected = _edition(10)
    state = _build(rejected, 481)
    assert state.uris == ()
    decision = state.duration_decisions[0]
    assert decision.reason == DURATION_EXCEEDS_DEFAULT_MAX
    assert decision.max_seconds == 480


def test_ser_0800_exception_uses_source_timezone_and_is_bounded() -> None:
    # 06:00 UTC is 08:00 Europe/Madrid in September.
    edition = _edition(6)
    accepted = _build(edition, 1200)
    assert accepted.uris == ("spotify:episode:ser-6",)
    assert accepted.duration_decisions[0].reason == DURATION_EXCEPTION
    assert accepted.duration_decisions[0].exception_id == "ser_morning_0800"

    rejected = _build(edition, 1801)
    assert rejected.uris == ()
    assert rejected.duration_decisions[0].reason == DURATION_EXCEEDS_EXCEPTION_MAX
    assert rejected.duration_decisions[0].max_seconds == 1800


def test_long_ser_non_0800_and_missing_semantic_time_do_not_match_exception() -> None:
    seven_local = _edition(5)
    assert _build(seven_local, 1200).duration_decisions[0].reason == (DURATION_EXCEEDS_DEFAULT_MAX)

    no_semantic = CanonicalEdition(
        source_id=SourceId("ser"),
        source_native_id="no-semantic",
        title="SER bulletin",
        published_at=NOW,
        edition_at=None,
    )
    assert _build(no_semantic, 1200).duration_decisions[0].reason == (DURATION_EXCEEDS_DEFAULT_MAX)


def test_unknown_spotify_duration_fails_closed() -> None:
    edition = _edition(9)
    with pytest.raises(DesiredStateError, match="Spotify duration unavailable"):
        _build(edition, None)


def test_builtin_catalog_and_managed_compile_inherit_ser_exception_without_local_copy() -> None:
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    exception = template.duration_policy.exceptions[0]
    assert template.duration_policy.default_max_seconds == 480
    assert exception.id == "ser_morning_0800"
    assert exception.max_seconds == 1800

    managed = activate_template(template, "destination")
    state = ManagedState(playlists=(managed,))
    compiled = compile_engine_config(BUILTIN_CATALOG, state)
    assert compiled.playlists[0].duration_policy == template.duration_policy


def _config_payload(exception_source: str = "ser", exception_max: int = 1800):
    return {
        "schema_version": 1,
        "sources": [
            {
                "id": "ser",
                "display_name": "SER",
                "countries": ["ES"],
                "languages": ["es"],
                "timezone": "Europe/Madrid",
                "enabled": True,
                "parser_id": "ser",
                "endpoint_url": "https://example.invalid/feed",
            }
        ],
        "playlists": [
            {
                "id": "test",
                "display_name": "Test",
                "description": "test",
                "countries": ["ES"],
                "languages": ["es"],
                "enabled": True,
                "source_selection": {"explicit": ["ser"]},
                "destination": {"adapter_id": "spotify", "external_id": "dest"},
                "duration_policy": {
                    "default_max_seconds": 480,
                    "exceptions": [
                        {
                            "id": "ser_morning_0800",
                            "source_id": exception_source,
                            "edition_local_time": "08:00",
                            "max_seconds": exception_max,
                        }
                    ],
                },
            }
        ],
    }


def test_yaml_duration_policy_validates_bounded_exception_and_known_source() -> None:
    parsed = parse_config(_config_payload())
    assert parsed.playlists[0].duration_policy.exceptions[0].edition_local_time == time(8, 0)

    with pytest.raises(ConfigError, match="must exceed duration policy default"):
        parse_config(_config_payload(exception_max=480))
    with pytest.raises(ConfigError, match="unknown source"):
        parse_config(_config_payload(exception_source="missing"))
