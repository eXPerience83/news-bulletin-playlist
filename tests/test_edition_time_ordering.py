from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from news_bulletin_playlist.desired_state import build_playlist_desired_state
from news_bulletin_playlist.models import (
    AdapterId,
    CanonicalEdition,
    CountryCode,
    DestinationReference,
    LanguageTag,
    OrderingPolicy,
    PlaylistDefinition,
    PlaylistId,
    SourceId,
    SourceSelection,
)
from news_bulletin_playlist.persistence import EditionMatch, MatchStatus, SQLiteStore
from news_bulletin_playlist.reconciliation import build_desired_state_from_store

NOW = datetime(2026, 8, 31, 20, 30, tzinfo=UTC)


def _playlist(*sources: str) -> PlaylistDefinition:
    return PlaylistDefinition(
        id=PlaylistId("spain_spanish_news"),
        display_name="Noticias España",
        description="test",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        enabled=True,
        source_selection=SourceSelection(tuple(SourceId(source) for source in sources)),
        destination=DestinationReference(AdapterId("spotify"), "playlist"),
        ordering=OrderingPolicy.EDITION_AT_DESC,
    )


def _edition(
    source: str,
    native_id: str,
    *,
    published_at: datetime,
    edition_at: datetime | None,
) -> CanonicalEdition:
    return CanonicalEdition(
        source_id=SourceId(source),
        source_native_id=native_id,
        title=native_id,
        published_at=published_at,
        edition_at=edition_at,
    )


def _matches(*editions: CanonicalEdition) -> dict[tuple[SourceId, str], EditionMatch]:
    return {
        edition.identity: EditionMatch(
            source_id=edition.source_id,
            source_native_id=edition.source_native_id,
            status=MatchStatus.MATCHED,
            spotify_episode_uri=f"spotify:episode:{edition.source_id}-{edition.source_native_id}",
            diagnostics="matched",
            updated_at=NOW,
        )
        for edition in editions
    }


def test_semantic_edition_time_beats_later_rss_publication_time() -> None:
    ser_19 = _edition(
        "ser",
        "ser-19",
        published_at=NOW - timedelta(minutes=45),
        edition_at=NOW - timedelta(hours=1, minutes=30),
    )
    rne_1530 = _edition(
        "rne",
        "rne-1530",
        published_at=NOW - timedelta(minutes=5),
        edition_at=NOW - timedelta(hours=5),
    )

    desired = build_playlist_desired_state(
        _playlist("ser", "rne"),
        (rne_1530, ser_19),
        _matches(rne_1530, ser_19),
        now=NOW,
    )

    assert tuple(item.source_native_id for item in desired.items) == ("ser-19", "rne-1530")


def test_retention_uses_edition_time_even_when_feed_publication_is_recent() -> None:
    delayed_old_bulletin = _edition(
        "rne",
        "old-edition",
        published_at=NOW - timedelta(minutes=5),
        edition_at=NOW - timedelta(hours=48, seconds=1),
    )

    desired = build_playlist_desired_state(
        _playlist("rne"),
        (delayed_old_bulletin,),
        _matches(delayed_old_bulletin),
        now=NOW,
    )

    assert desired.items == ()


def test_missing_edition_time_falls_back_to_rss_publication_time() -> None:
    with_semantic_time = _edition(
        "ser",
        "semantic",
        published_at=NOW - timedelta(minutes=5),
        edition_at=NOW - timedelta(hours=2),
    )
    without_semantic_time = _edition(
        "cnn",
        "fallback",
        published_at=NOW - timedelta(hours=1),
        edition_at=None,
    )

    desired = build_playlist_desired_state(
        _playlist("ser", "cnn"),
        (with_semantic_time, without_semantic_time),
        _matches(with_semantic_time, without_semantic_time),
        now=NOW,
    )

    assert tuple(item.source_native_id for item in desired.items) == ("fallback", "semantic")


def test_store_scan_does_not_break_on_old_publication_when_edition_is_current(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    publication_older_than_cutoff = _edition(
        "rne",
        "late-canonical",
        published_at=NOW - timedelta(hours=49),
        edition_at=NOW - timedelta(hours=1),
    )
    store.upsert_editions((publication_older_than_cutoff,), observed_at=NOW)
    store.set_match_state(
        publication_older_than_cutoff.source_id,
        publication_older_than_cutoff.source_native_id,
        status=MatchStatus.MATCHED,
        spotify_episode_uri="spotify:episode:late-canonical",
        diagnostics="matched",
        updated_at=NOW,
    )

    desired = build_desired_state_from_store(store, _playlist("rne"), now=NOW)

    assert desired.uris == ("spotify:episode:late-canonical",)


def test_explicit_legacy_publication_order_remains_available() -> None:
    newer_publication = _edition(
        "rne",
        "newer-publication",
        published_at=NOW - timedelta(minutes=5),
        edition_at=NOW - timedelta(hours=5),
    )
    newer_edition = _edition(
        "ser",
        "newer-edition",
        published_at=NOW - timedelta(minutes=45),
        edition_at=NOW - timedelta(hours=1),
    )
    playlist = _playlist("ser", "rne")
    playlist = PlaylistDefinition(
        id=playlist.id,
        display_name=playlist.display_name,
        description=playlist.description,
        countries=playlist.countries,
        languages=playlist.languages,
        enabled=playlist.enabled,
        source_selection=playlist.source_selection,
        destination=playlist.destination,
        ordering=OrderingPolicy.PUBLISHED_AT_DESC,
    )

    desired = build_playlist_desired_state(
        playlist,
        (newer_publication, newer_edition),
        _matches(newer_publication, newer_edition),
        now=NOW,
    )

    assert tuple(item.source_native_id for item in desired.items) == (
        "newer-publication",
        "newer-edition",
    )
