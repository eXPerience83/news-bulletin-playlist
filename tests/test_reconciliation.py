from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from news_bulletin_playlist.desired_state import DesiredPlaylistItem, DesiredPlaylistState
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
from news_bulletin_playlist.persistence import MatchStatus, SQLiteStore
from news_bulletin_playlist.reconciliation import (
    SpotifyReconciliationError,
    build_desired_state_from_store,
    reconcile_playlist_items,
    reconcile_spotify_destinations,
)
from news_bulletin_playlist.spotify.client import SpotifyTransportError

NOW = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)


def _playlist(playlist_id: str, *, source: str = "ser") -> PlaylistDefinition:
    return PlaylistDefinition(
        id=PlaylistId(playlist_id),
        display_name=playlist_id,
        description="test",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        enabled=True,
        source_selection=SourceSelection((SourceId(source),)),
        destination=DestinationReference(AdapterId("spotify"), playlist_id),
    )


def _desired(playlist: PlaylistDefinition, *uris: str) -> DesiredPlaylistState:
    items = tuple(
        DesiredPlaylistItem(
            source_id=SourceId("ser"),
            source_native_id=str(index),
            published_at=NOW - timedelta(minutes=index),
            spotify_episode_uri=uri,
        )
        for index, uri in enumerate(uris)
    )
    return DesiredPlaylistState(playlist_id=playlist.id, generated_at=NOW, items=items)


class _FakeSpotify:
    def __init__(self, states: dict[str, list[str]]) -> None:
        self.states = {playlist_id: list(uris) for playlist_id, uris in states.items()}
        self.writes: list[tuple[str, list[str]]] = []
        self.reads: list[tuple[str, int, int]] = []
        self.fail_reads: set[str] = set()
        self.corrupt_after_write: set[str] = set()

    def playlist_items(
        self,
        playlist_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        self.reads.append((playlist_id, limit, offset))
        if playlist_id in self.fail_reads:
            raise SpotifyTransportError("network error")
        state = self.states.get(playlist_id, [])
        page = state[offset : offset + limit]
        next_url = "next" if offset + limit < len(state) else None
        return {
            "items": [{"item": {"uri": uri}} for uri in page],
            "next": next_url,
        }

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]:
        self.writes.append((playlist_id, list(uris)))
        self.states[playlist_id] = list(uris)
        if playlist_id in self.corrupt_after_write and uris:
            self.states[playlist_id] = list(reversed(uris))
        return {"snapshot_id": "snapshot"}


class _UnavailableMediaSpotify(_FakeSpotify):
    def __init__(self, media_key: str) -> None:
        super().__init__({"playlist": ["spotify:episode:old"]})
        self.media_key = media_key

    def playlist_items(
        self,
        playlist_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        if not self.writes:
            self.reads.append((playlist_id, limit, offset))
            return {
                "items": [
                    {self.media_key: None},
                    {self.media_key: {"uri": "spotify:episode:old"}},
                ],
                "next": None,
            }
        return super().playlist_items(playlist_id, limit=limit, offset=offset)


class _MissingMediaSpotify(_FakeSpotify):
    def playlist_items(
        self,
        playlist_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        self.reads.append((playlist_id, limit, offset))
        return {"items": [{}], "next": None}


def test_unchanged_desired_state_performs_zero_writes() -> None:
    client = _FakeSpotify({"playlist": ["spotify:episode:one", "spotify:episode:two"]})

    wrote = reconcile_playlist_items(
        client,
        "playlist",
        ("spotify:episode:one", "spotify:episode:two"),
    )

    assert wrote is False
    assert client.writes == []
    assert client.reads == [("playlist", 100, 0)]


def test_changed_state_is_replaced_and_exactly_read_back() -> None:
    client = _FakeSpotify({"playlist": ["spotify:episode:old"]})
    desired = ("spotify:episode:new", "spotify:episode:second")

    wrote = reconcile_playlist_items(client, "playlist", desired)

    assert wrote is True
    assert client.writes == [("playlist", list(desired))]
    assert client.reads == [("playlist", 100, 0), ("playlist", 100, 0)]
    assert client.states["playlist"] == list(desired)


@pytest.mark.parametrize("media_key", ["item", "track"])
def test_unavailable_media_item_does_not_block_replacement(media_key: str) -> None:
    client = _UnavailableMediaSpotify(media_key)
    desired = ("spotify:episode:new", "spotify:episode:second")

    wrote = reconcile_playlist_items(client, "playlist", desired)

    assert wrote is True
    assert client.writes == [("playlist", list(desired))]
    assert client.states["playlist"] == list(desired)
    assert client.reads == [("playlist", 100, 0), ("playlist", 100, 0)]


def test_missing_media_field_still_fails_closed() -> None:
    client = _MissingMediaSpotify({"playlist": []})

    with pytest.raises(SpotifyReconciliationError, match="without a media object"):
        reconcile_playlist_items(client, "playlist", ("spotify:episode:new",))

    assert client.writes == []


def test_post_write_order_mismatch_fails_closed() -> None:
    client = _FakeSpotify({"playlist": ["spotify:episode:old"]})
    client.corrupt_after_write.add("playlist")

    with pytest.raises(SpotifyReconciliationError, match="readback did not match"):
        reconcile_playlist_items(
            client,
            "playlist",
            ("spotify:episode:one", "spotify:episode:two"),
        )


def test_desired_state_over_100_is_rejected_before_writing() -> None:
    client = _FakeSpotify({"playlist": []})
    desired = tuple(f"spotify:episode:{index}" for index in range(101))

    with pytest.raises(ValueError, match="limited to 100"):
        reconcile_playlist_items(client, "playlist", desired)

    assert client.reads == []
    assert client.writes == []


def test_one_destination_failure_does_not_block_another() -> None:
    first = _playlist("first")
    second = _playlist("second")
    client = _FakeSpotify({"first": [], "second": ["spotify:episode:old"]})
    client.fail_reads.add("first")

    results = reconcile_spotify_destinations(
        client,
        (
            (first, _desired(first, "spotify:episode:first")),
            (second, _desired(second, "spotify:episode:second")),
        ),
    )

    assert results[0].ok is False
    assert results[0].wrote is None
    assert results[1].ok is True
    assert results[1].wrote is True
    assert client.states["second"] == ["spotify:episode:second"]


def test_failed_source_keeps_last_known_good_until_retention_expires(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    playlist = _playlist("playlist")
    edition = CanonicalEdition(
        source_id=SourceId("ser"),
        source_native_id="edition-1",
        title="Bulletin",
        published_at=NOW - timedelta(hours=2),
        edition_at=NOW - timedelta(hours=2),
    )
    store.upsert_editions((edition,), observed_at=NOW - timedelta(hours=2))
    store.set_match_state(
        edition.source_id,
        edition.source_native_id,
        status=MatchStatus.MATCHED,
        spotify_episode_uri="spotify:episode:edition-1",
        diagnostics="matched",
        updated_at=NOW - timedelta(hours=2),
    )
    store.record_source_run(
        edition.source_id,
        started_at=NOW - timedelta(hours=2),
        finished_at=NOW - timedelta(hours=2) + timedelta(seconds=1),
        ok=True,
        edition_count=1,
    )
    store.record_source_run(
        edition.source_id,
        started_at=NOW - timedelta(minutes=1),
        finished_at=NOW,
        ok=False,
        edition_count=0,
        error="temporary upstream failure",
    )

    during_failure = build_desired_state_from_store(store, playlist, now=NOW)
    after_expiry = build_desired_state_from_store(
        store,
        playlist,
        now=NOW + timedelta(hours=47),
    )

    assert during_failure.uris == ("spotify:episode:edition-1",)
    assert after_expiry.uris == ()
