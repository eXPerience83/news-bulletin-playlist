from __future__ import annotations

from datetime import UTC, datetime, timedelta

from news_bulletin_playlist.desired_state import DesiredPlaylistItem, DesiredPlaylistState
from news_bulletin_playlist.models import (
    AdapterId,
    CountryCode,
    DestinationReference,
    LanguageTag,
    PlaylistDefinition,
    PlaylistId,
    SourceId,
    SourceSelection,
)
from news_bulletin_playlist.soft_retention import (
    SOFT_RETENTION_GRACE,
    SoftRetentionDisposition,
    classify_soft_retention,
)

NOW = datetime(2026, 9, 13, 20, 0, tzinfo=UTC)
PLAYLIST_ID = PlaylistId("playlist")
SOURCE_ID = SourceId("source")


def _playlist() -> PlaylistDefinition:
    return PlaylistDefinition(
        id=PLAYLIST_ID,
        display_name="Playlist",
        description="",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        enabled=True,
        source_selection=SourceSelection((SOURCE_ID,)),
        destination=DestinationReference(AdapterId("spotify"), "destination"),
        retention_hours=48,
    )


def _item(native_id: str, *, age: timedelta, uri_suffix: str | None = None) -> DesiredPlaylistItem:
    published_at = NOW - age
    return DesiredPlaylistItem(
        source_id=SOURCE_ID,
        source_native_id=native_id,
        published_at=published_at,
        edition_at=published_at,
        spotify_episode_uri=f"spotify:episode:{uri_suffix or native_id}",
    )


def _state(
    *items: DesiredPlaylistItem,
    generated_at: datetime | None = None,
) -> DesiredPlaylistState:
    return DesiredPlaylistState(
        playlist_id=PLAYLIST_ID,
        generated_at=NOW - timedelta(hours=1) if generated_at is None else generated_at,
        items=tuple(items),
    )


def test_expiry_only_removal_is_deferred_per_playlist() -> None:
    current = _item("current", age=timedelta(hours=1))
    expired = _item("expired", age=timedelta(hours=49))

    decision = classify_soft_retention(
        _playlist(),
        _state(current, expired),
        _state(current),
        now=NOW,
    )

    assert decision.disposition is SoftRetentionDisposition.DEFER
    assert decision.expiry_only
    assert decision.should_defer
    assert not decision.force_cleanup
    assert decision.soft_expired_count == 1
    assert decision.oldest_soft_expired_age_seconds == 49 * 60 * 60


def test_exact_48_hour_boundary_is_soft_expired_and_deferred() -> None:
    current = _item("current", age=timedelta(hours=1))
    boundary = _item("boundary", age=timedelta(hours=48))

    decision = classify_soft_retention(
        _playlist(),
        _state(current, boundary),
        _state(current),
        now=NOW,
    )

    assert decision.disposition is SoftRetentionDisposition.DEFER
    assert decision.soft_expired_count == 1
    assert decision.oldest_soft_expired_age_seconds == 48 * 60 * 60


def test_multiple_expired_items_are_coalesced_into_one_deferred_cleanup() -> None:
    current = _item("current", age=timedelta(hours=2))
    expired_1 = _item("expired-1", age=timedelta(hours=49))
    expired_2 = _item("expired-2", age=timedelta(hours=52))
    expired_3 = _item("expired-3", age=timedelta(hours=55))

    decision = classify_soft_retention(
        _playlist(),
        _state(current, expired_1, expired_2, expired_3),
        _state(current),
        now=NOW,
    )

    assert decision.should_defer
    assert decision.soft_expired_count == 3
    assert decision.oldest_soft_expired_age_seconds == 55 * 60 * 60


def test_cleanup_is_forced_after_48_plus_8_hours() -> None:
    current = _item("current", age=timedelta(hours=1))
    expired = _item("expired", age=timedelta(hours=56, seconds=1))

    decision = classify_soft_retention(
        _playlist(),
        _state(current, expired),
        _state(current),
        now=NOW,
    )

    assert timedelta(hours=8) == SOFT_RETENTION_GRACE
    assert decision.disposition is SoftRetentionDisposition.FORCE_HARD_EXPIRY
    assert decision.expiry_only
    assert decision.force_cleanup
    assert not decision.should_defer


def test_exact_56_hour_boundary_forces_cleanup() -> None:
    current = _item("current", age=timedelta(hours=1))
    expired = _item("expired", age=timedelta(hours=56))

    decision = classify_soft_retention(
        _playlist(),
        _state(current, expired),
        _state(current),
        now=NOW,
    )

    assert decision.disposition is SoftRetentionDisposition.FORCE_HARD_EXPIRY
    assert decision.force_cleanup
    assert not decision.should_defer


def test_new_episode_plus_expiry_is_not_expiry_only() -> None:
    previous = _item("previous", age=timedelta(hours=2))
    expired = _item("expired", age=timedelta(hours=49))
    new = _item("new", age=timedelta(minutes=5))

    decision = classify_soft_retention(
        _playlist(),
        _state(previous, expired),
        _state(new, previous),
        now=NOW,
    )

    assert decision.disposition is SoftRetentionDisposition.NOT_EXPIRY_ONLY


def test_mapping_change_is_not_misclassified_as_expiry_only() -> None:
    previous = _item("previous", age=timedelta(hours=2))
    expired = _item("expired", age=timedelta(hours=49))
    remapped = _item("previous", age=timedelta(hours=2), uri_suffix="remapped")

    decision = classify_soft_retention(
        _playlist(),
        _state(previous, expired),
        _state(remapped),
        now=NOW,
    )

    assert decision.disposition is SoftRetentionDisposition.NOT_EXPIRY_ONLY


def test_reorder_is_not_misclassified_as_expiry_only() -> None:
    first = _item("first", age=timedelta(hours=1))
    second = _item("second", age=timedelta(hours=2))
    expired = _item("expired", age=timedelta(hours=49))

    decision = classify_soft_retention(
        _playlist(),
        _state(first, second, expired),
        _state(second, first),
        now=NOW,
    )

    assert decision.disposition is SoftRetentionDisposition.NOT_EXPIRY_ONLY


def test_clock_rollback_fails_closed() -> None:
    current = _item("current", age=timedelta(hours=1))
    expired = _item("expired", age=timedelta(hours=49))

    decision = classify_soft_retention(
        _playlist(),
        _state(current, expired, generated_at=NOW + timedelta(minutes=1)),
        _state(current),
        now=NOW,
    )

    assert decision.disposition is SoftRetentionDisposition.NOT_EXPIRY_ONLY
