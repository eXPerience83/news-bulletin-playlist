from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from news_bulletin_playlist.desired_state import (
    DesiredStateError,
    build_multi_playlist_desired_states,
    build_playlist_desired_state,
)
from news_bulletin_playlist.models import (
    AdapterId,
    CanonicalEdition,
    CountryCode,
    DestinationReference,
    LanguageTag,
    PlaylistDefinition,
    PlaylistId,
    SourceId,
    SourceSelection,
)
from news_bulletin_playlist.persistence import EditionMatch, MatchStatus

NOW = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)


def _playlist(
    playlist_id: str = "spain-es",
    *,
    sources: tuple[str, ...] = ("ser",),
    max_episodes: int = 100,
    retention_hours: int = 48,
    enabled: bool = True,
) -> PlaylistDefinition:
    return PlaylistDefinition(
        id=PlaylistId(playlist_id),
        display_name=playlist_id,
        description="test",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        enabled=enabled,
        source_selection=SourceSelection(tuple(SourceId(source) for source in sources)),
        destination=DestinationReference(AdapterId("spotify"), f"spotify-{playlist_id}"),
        retention_hours=retention_hours,
        max_episodes=max_episodes,
    )


def _edition(native_id: str, published_at: datetime, *, source: str = "ser") -> CanonicalEdition:
    return CanonicalEdition(
        source_id=SourceId(source),
        source_native_id=native_id,
        title=f"Bulletin {native_id}",
        published_at=published_at,
        edition_at=published_at,
    )


def _match(
    edition: CanonicalEdition,
    *,
    status: MatchStatus = MatchStatus.MATCHED,
    uri: str | None = None,
) -> EditionMatch:
    spotify_uri = (
        uri or f"spotify:episode:{edition.source_id!s}-{edition.source_native_id}"
        if status is MatchStatus.MATCHED
        else None
    )
    return EditionMatch(
        source_id=edition.source_id,
        source_native_id=edition.source_native_id,
        status=status,
        spotify_episode_uri=spotify_uri,
        diagnostics=None,
        updated_at=NOW,
        spotify_duration_seconds=60 if status is MatchStatus.MATCHED else None,
    )


def test_default_window_order_and_unmatched_exclusion() -> None:
    cutoff = NOW - timedelta(hours=48)
    newest = _edition("newest", NOW - timedelta(minutes=1))
    boundary = _edition("boundary", cutoff)
    too_old = _edition("old", cutoff - timedelta(seconds=1))
    future = _edition("future", NOW + timedelta(seconds=1))
    other_source = _edition("other", NOW - timedelta(minutes=2), source="rne")
    pending = _edition("pending", NOW - timedelta(minutes=3))
    ambiguous = _edition("ambiguous", NOW - timedelta(minutes=4))
    editions = (boundary, too_old, newest, future, other_source, pending, ambiguous)
    matches = {
        edition.identity: _match(edition)
        for edition in (boundary, too_old, newest, future, other_source)
    }
    matches[pending.identity] = _match(pending, status=MatchStatus.PENDING)
    matches[ambiguous.identity] = _match(ambiguous, status=MatchStatus.AMBIGUOUS)

    desired = build_playlist_desired_state(_playlist(), editions, matches, now=NOW)

    assert tuple(item.source_native_id for item in desired.items) == ("newest", "boundary")


def test_newest_100_are_kept_deterministically() -> None:
    editions = tuple(_edition(str(index), NOW - timedelta(minutes=index)) for index in range(105))
    matches = {edition.identity: _match(edition) for edition in editions}

    desired = build_playlist_desired_state(_playlist(), editions, matches, now=NOW)

    assert len(desired.items) == 100
    assert desired.items[0].source_native_id == "0"
    assert desired.items[-1].source_native_id == "99"


def test_configured_max_below_spotify_limit_is_honored() -> None:
    editions = tuple(_edition(str(index), NOW - timedelta(minutes=index)) for index in range(4))
    matches = {edition.identity: _match(edition) for edition in editions}

    desired = build_playlist_desired_state(
        _playlist(max_episodes=2),
        editions,
        matches,
        now=NOW,
    )

    assert tuple(item.source_native_id for item in desired.items) == ("0", "1")


def test_equal_publication_times_have_stable_identity_tiebreaker() -> None:
    published_at = NOW - timedelta(hours=1)
    editions = (
        _edition("b", published_at, source="ser"),
        _edition("a", published_at, source="rne"),
        _edition("a", published_at, source="ser"),
    )
    matches = {edition.identity: _match(edition) for edition in editions}

    desired = build_playlist_desired_state(
        _playlist(sources=("ser", "rne")), editions, matches, now=NOW
    )

    assert tuple(item.identity for item in desired.items) == (
        (SourceId("rne"), "a"),
        (SourceId("ser"), "a"),
        (SourceId("ser"), "b"),
    )


def test_duplicate_spotify_uri_keeps_only_newest_canonical_occurrence() -> None:
    newest = _edition("new", NOW - timedelta(minutes=1), source="rne")
    older = _edition("old", NOW - timedelta(minutes=2), source="rne")
    duplicate_uri = "spotify:episode:shared"
    matches = {
        newest.identity: _match(newest, uri=duplicate_uri),
        older.identity: _match(older, uri=duplicate_uri),
    }

    desired = build_playlist_desired_state(
        _playlist(sources=("rne",)),
        (older, newest),
        matches,
        now=NOW,
    )

    assert desired.uris == (duplicate_uri,)
    assert desired.items[0].identity == newest.identity


def test_overlapping_playlists_share_canonical_input_but_not_destination_state() -> None:
    shared = _edition("shared", NOW - timedelta(minutes=1), source="rne")
    ser = _edition("ser-only", NOW - timedelta(minutes=2), source="ser")
    cnn = _edition("cnn-only", NOW - timedelta(minutes=3), source="cnn")
    editions = (shared, ser, cnn)
    matches = {edition.identity: _match(edition) for edition in editions}
    playlists = (
        _playlist("a", sources=("ser", "rne")),
        _playlist("b", sources=("rne", "cnn")),
    )

    states = build_multi_playlist_desired_states(playlists, editions, matches, now=NOW)

    assert states[0].playlist_id == PlaylistId("a")
    assert states[1].playlist_id == PlaylistId("b")
    assert shared.identity in tuple(item.identity for item in states[0].items)
    assert shared.identity in tuple(item.identity for item in states[1].items)
    assert ser.identity in tuple(item.identity for item in states[0].items)
    assert ser.identity not in tuple(item.identity for item in states[1].items)
    assert cnn.identity not in tuple(item.identity for item in states[0].items)
    assert cnn.identity in tuple(item.identity for item in states[1].items)


def test_conflicting_duplicate_canonical_identity_fails_closed() -> None:
    first = _edition("same", NOW - timedelta(minutes=1))
    second = CanonicalEdition(
        source_id=first.source_id,
        source_native_id=first.source_native_id,
        title="Conflicting title",
        published_at=first.published_at,
        edition_at=first.edition_at,
    )

    with pytest.raises(DesiredStateError, match="conflicting canonical editions"):
        build_playlist_desired_state(
            _playlist(),
            (first, second),
            {first.identity: _match(first)},
            now=NOW,
        )


def test_disabled_playlist_cannot_accidentally_generate_an_empty_write_plan() -> None:
    with pytest.raises(DesiredStateError, match="disabled"):
        build_playlist_desired_state(_playlist(enabled=False), (), {}, now=NOW)
