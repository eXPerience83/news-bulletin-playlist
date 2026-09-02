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
        self.snapshot = "snapshot-initial"

    def playlist_items(
        self,
        playlist_id: str,
        *,
        limit: int = 50,
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
            "total": len(state),
        }

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]:
        self.writes.append((playlist_id, list(uris)))
        self.states[playlist_id] = list(uris)
        self.snapshot = f"snapshot-write-{len(self.writes)}"
        if playlist_id in self.corrupt_after_write and uris:
            self.states[playlist_id] = list(reversed(uris))
        return {"snapshot_id": self.snapshot}

    def playlist_snapshot(self, playlist_id: str) -> dict[str, Any]:
        return {"snapshot_id": self.snapshot}


class _UnavailableMediaSpotify(_FakeSpotify):
    def __init__(self, media_key: str) -> None:
        super().__init__({"playlist": ["spotify:episode:old"]})
        self.media_key = media_key

    def playlist_items(
        self,
        playlist_id: str,
        *,
        limit: int = 50,
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
                "total": 2,
            }
        return super().playlist_items(playlist_id, limit=limit, offset=offset)


class _UnavailableAfterWriteSpotify(_FakeSpotify):
    def playlist_items(
        self,
        playlist_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        if self.writes:
            self.reads.append((playlist_id, limit, offset))
            return {"items": [{"item": None}], "next": None, "total": 1}
        return super().playlist_items(playlist_id, limit=limit, offset=offset)


class _MissingMediaSpotify(_FakeSpotify):
    def playlist_items(
        self,
        playlist_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        self.reads.append((playlist_id, limit, offset))
        return {"items": [{}], "next": None, "total": 1}


class _MalformedPagingSpotify(_FakeSpotify):
    def __init__(self, page: dict[str, Any]) -> None:
        super().__init__({"playlist": []})
        self.page = page

    def playlist_items(
        self,
        playlist_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        self.reads.append((playlist_id, limit, offset))
        return dict(self.page)


class _FalseTerminalSpotify(_FakeSpotify):
    def playlist_items(
        self,
        playlist_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        if not self.writes and offset == 0:
            self.reads.append((playlist_id, limit, offset))
            state = self.states[playlist_id]
            return {
                "items": [{"item": {"uri": uri}} for uri in state[:limit]],
                "next": None,
                "total": len(state),
            }
        return super().playlist_items(playlist_id, limit=limit, offset=offset)


class _PrewritePaginationRaceSpotify(_FakeSpotify):
    def __init__(self, desired: list[str]) -> None:
        initial = list(desired)
        initial[60] = "spotify:episode:old-at-60"
        super().__init__({"playlist": initial})
        self.desired = desired
        self.snapshot = "snapshot-initial"
        self.mutated = False

    def playlist_items(
        self,
        playlist_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        response = super().playlist_items(playlist_id, limit=limit, offset=offset)
        if offset == 0 and not self.mutated:
            self.states[playlist_id][0] = "spotify:episode:concurrent-at-0"
            self.states[playlist_id][60] = self.desired[60]
            self.snapshot = "snapshot-concurrent"
            self.mutated = True
        return response

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]:
        super().replace_playlist_items(playlist_id, uris)
        self.snapshot = "snapshot-write"
        return {"snapshot_id": self.snapshot}

    def playlist_snapshot(self, playlist_id: str) -> dict[str, Any]:
        return {"snapshot_id": self.snapshot}


class _PostwritePaginationRaceSpotify(_FakeSpotify):
    def __init__(self) -> None:
        super().__init__({"playlist": ["spotify:episode:old"]})
        self.snapshot = "snapshot-initial"
        self.mutated = False

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]:
        super().replace_playlist_items(playlist_id, uris)
        self.snapshot = "snapshot-write"
        return {"snapshot_id": self.snapshot}

    def playlist_items(
        self,
        playlist_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        response = super().playlist_items(playlist_id, limit=limit, offset=offset)
        if self.writes and offset == 0 and not self.mutated:
            self.states[playlist_id][0] = "spotify:episode:concurrent-at-0"
            self.snapshot = "snapshot-concurrent"
            self.mutated = True
        return response

    def playlist_snapshot(self, playlist_id: str) -> dict[str, Any]:
        return {"snapshot_id": self.snapshot}


def test_unchanged_desired_state_performs_zero_writes() -> None:
    client = _FakeSpotify({"playlist": ["spotify:episode:one", "spotify:episode:two"]})

    wrote = reconcile_playlist_items(
        client,
        "playlist",
        ("spotify:episode:one", "spotify:episode:two"),
    )

    assert wrote is False
    assert client.writes == []
    assert client.reads == [("playlist", 50, 0)]


def test_unchanged_state_over_50_items_uses_multiple_pages() -> None:
    desired = tuple(f"spotify:episode:{index}" for index in range(75))
    client = _FakeSpotify({"playlist": list(desired)})

    wrote = reconcile_playlist_items(client, "playlist", desired)

    assert wrote is False
    assert client.writes == []
    assert client.reads == [
        ("playlist", 50, 0),
        ("playlist", 50, 50),
        ("playlist", 50, 0),
        ("playlist", 50, 50),
    ]


def test_changed_state_is_replaced_and_exactly_read_back() -> None:
    client = _FakeSpotify({"playlist": ["spotify:episode:old"]})
    desired = ("spotify:episode:new", "spotify:episode:second")

    wrote = reconcile_playlist_items(client, "playlist", desired)

    assert wrote is True
    assert client.writes == [("playlist", list(desired))]
    assert client.reads == [("playlist", 50, 0), ("playlist", 50, 0)]
    assert client.states["playlist"] == list(desired)


def test_current_state_over_100_forces_bounded_replacement() -> None:
    current = [f"spotify:episode:{index}" for index in range(101)]
    desired = tuple(current[:100])
    client = _FakeSpotify({"playlist": current})

    wrote = reconcile_playlist_items(client, "playlist", desired)

    assert wrote is True
    assert client.writes == [("playlist", list(desired))]
    assert client.states["playlist"] == list(desired)
    assert client.reads == [
        ("playlist", 50, 0),
        ("playlist", 50, 50),
        ("playlist", 1, 100),
        ("playlist", 50, 0),
        ("playlist", 50, 50),
        ("playlist", 1, 100),
    ]


def test_false_terminal_page_with_total_fails_closed() -> None:
    current = [f"spotify:episode:{index}" for index in range(51)]
    desired = tuple(current[:50])
    client = _FalseTerminalSpotify({"playlist": current})

    with pytest.raises(SpotifyReconciliationError, match="truncated before total"):
        reconcile_playlist_items(client, "playlist", desired)

    assert client.writes == []


def test_short_false_terminal_page_without_total_never_reports_unchanged() -> None:
    client = _MalformedPagingSpotify(
        {"items": [{"item": {"uri": "spotify:episode:A"}}], "next": None}
    )

    with pytest.raises(SpotifyReconciliationError, match="missing total"):
        reconcile_playlist_items(client, "playlist", ("spotify:episode:A",))

    assert client.writes == []


def test_prewrite_multi_page_comparison_cannot_mix_snapshots() -> None:
    desired = [f"spotify:episode:{index}" for index in range(75)]
    client = _PrewritePaginationRaceSpotify(desired)

    wrote = reconcile_playlist_items(client, "playlist", desired)

    assert wrote is True
    assert client.states["playlist"] == desired


def test_postwrite_multi_page_readback_cannot_mix_snapshots() -> None:
    desired = [f"spotify:episode:{index}" for index in range(75)]
    client = _PostwritePaginationRaceSpotify()

    with pytest.raises(SpotifyReconciliationError, match="snapshot changed"):
        reconcile_playlist_items(client, "playlist", desired)

    assert client.writes == [("playlist", desired)]


@pytest.mark.parametrize(
    ("page", "message"),
    [
        ({"items": []}, "pagination was missing next"),
        ({"items": [], "next": "", "total": 0}, "pagination contained invalid next"),
        (
            {"items": [{"item": {"uri": "spotify:episode:one"}}], "next": None, "total": 2},
            "pagination truncated before total",
        ),
        (
            {"items": [], "next": None, "total": -1},
            "pagination contained invalid total",
        ),
    ],
)
def test_malformed_pagination_fails_closed_without_writing(
    page: dict[str, Any],
    message: str,
) -> None:
    client = _MalformedPagingSpotify(page)

    with pytest.raises(SpotifyReconciliationError, match=message) as error:
        reconcile_playlist_items(client, "playlist", ("spotify:episode:new",))

    assert "prewrite" in str(error.value)
    assert "offset=0" in str(error.value)
    assert client.writes == []


@pytest.mark.parametrize("media_key", ["item", "track"])
def test_unavailable_media_item_does_not_block_replacement(media_key: str) -> None:
    client = _UnavailableMediaSpotify(media_key)
    desired = ("spotify:episode:new", "spotify:episode:second")

    wrote = reconcile_playlist_items(client, "playlist", desired)

    assert wrote is True
    assert client.writes == [("playlist", list(desired))]
    assert client.states["playlist"] == list(desired)
    assert client.reads == [("playlist", 50, 0), ("playlist", 50, 0)]


def test_unavailable_media_item_forces_healing_when_visible_uris_match() -> None:
    # Opaque prewrite slots are not wildcard equality: they force a complete replacement.
    client = _UnavailableMediaSpotify("item")
    desired = ("spotify:episode:old",)

    wrote = reconcile_playlist_items(client, "playlist", desired)

    assert wrote is True
    assert client.writes == [("playlist", list(desired))]
    assert client.states["playlist"] == list(desired)


@pytest.mark.parametrize(
    "wrapper",
    [
        {
            "item": {"uri": "spotify:episode:one"},
            "track": {"uri": "spotify:episode:two"},
        },
        {"item": {"uri": "spotify:episode:one"}, "track": "malformed"},
        {"item": {"uri": "spotify:episode:one"}, "track": {}},
    ],
)
def test_contradictory_wrapper_media_fails_closed(wrapper: dict[str, object]) -> None:
    client = _MalformedPagingSpotify({"items": [wrapper], "next": None, "total": 1})

    with pytest.raises(SpotifyReconciliationError):
        reconcile_playlist_items(client, "playlist", ("spotify:episode:one",))

    assert client.writes == []


def test_matching_dual_wrapper_media_is_accepted() -> None:
    uri = "spotify:episode:one"
    client = _MalformedPagingSpotify(
        {
            "items": [{"item": {"uri": uri}, "track": {"uri": uri}}],
            "next": None,
            "total": 1,
        }
    )

    assert reconcile_playlist_items(client, "playlist", (uri,)) is False
    assert client.writes == []


@pytest.mark.parametrize("null_field", ["item", "track"])
def test_null_dual_wrapper_field_falls_back_to_valid_media(null_field: str) -> None:
    uri = "spotify:episode:one"
    wrapper = {"item": {"uri": uri}, "track": {"uri": uri}}
    wrapper[null_field] = None
    client = _MalformedPagingSpotify({"items": [wrapper], "next": None, "total": 1})

    assert reconcile_playlist_items(client, "playlist", (uri,)) is False
    assert client.writes == []


def test_unavailable_media_item_after_write_fails_exact_readback() -> None:
    client = _UnavailableAfterWriteSpotify({"playlist": ["spotify:episode:old"]})

    with pytest.raises(SpotifyReconciliationError, match="unavailable media item") as error:
        reconcile_playlist_items(client, "playlist", ("spotify:episode:new",))

    assert "readback" in str(error.value)
    assert "offset=0" in str(error.value)
    assert client.writes == [("playlist", ["spotify:episode:new"])]


def test_missing_media_field_still_fails_closed() -> None:
    client = _MissingMediaSpotify({"playlist": []})

    with pytest.raises(SpotifyReconciliationError, match="without a media object") as error:
        reconcile_playlist_items(client, "playlist", ("spotify:episode:new",))

    assert "prewrite" in str(error.value)
    assert "offset=0" in str(error.value)
    assert "item_index=0" in str(error.value)
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
