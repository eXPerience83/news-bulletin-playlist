from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

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
from news_bulletin_playlist.persistence import SQLiteStore
from news_bulletin_playlist.reconciliation import (
    SpotifyReconciliationError,
    reconcile_spotify_playlist,
)

NOW = datetime(2026, 9, 2, 20, 37, tzinfo=UTC)


def _playlist() -> PlaylistDefinition:
    return PlaylistDefinition(
        id=PlaylistId("spain_spanish_news"),
        display_name="Noticias España",
        description="test",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        enabled=True,
        source_selection=SourceSelection((SourceId("ser"),)),
        destination=DestinationReference(AdapterId("spotify"), "playlist"),
    )


def _desired(playlist: PlaylistDefinition, count: int = 75) -> DesiredPlaylistState:
    items = tuple(
        DesiredPlaylistItem(
            source_id=SourceId("ser"),
            source_native_id=str(index),
            published_at=NOW - timedelta(minutes=index),
            spotify_episode_uri=f"spotify:episode:{index}",
        )
        for index in range(count)
    )
    return DesiredPlaylistState(
        playlist_id=playlist.id,
        generated_at=NOW,
        items=items,
    )


def _store(path: Path) -> SQLiteStore:
    store = SQLiteStore(path / "state.sqlite3")
    store.initialize()
    return store


class _SnapshotPropagationSpotify:
    def __init__(self, snapshot_responses: list[str]) -> None:
        self.state = ["spotify:episode:old"]
        self.snapshot_responses = list(snapshot_responses)
        self.snapshot_calls = 0
        self.writes = 0

    def playlist_items(
        self,
        playlist_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        del playlist_id
        page = self.state[offset : offset + limit]
        return {
            "items": [{"item": {"uri": uri}} for uri in page],
            "next": "next" if offset + limit < len(self.state) else None,
            "total": len(self.state),
        }

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]:
        del playlist_id
        self.writes += 1
        self.state = list(uris)
        return {"snapshot_id": "snapshot-write"}

    def playlist_snapshot(self, playlist_id: str) -> dict[str, Any]:
        del playlist_id
        index = min(self.snapshot_calls, len(self.snapshot_responses) - 1)
        response = self.snapshot_responses[index]
        self.snapshot_calls += 1
        return {"snapshot_id": response}


class _SnapshotContentRaceSpotify(_SnapshotPropagationSpotify):
    def playlist_items(
        self,
        playlist_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        if self.snapshot_calls == 1 and offset == 0 and self.writes:
            self.state[0] = "spotify:episode:concurrent"
        return super().playlist_items(playlist_id, limit=limit, offset=offset)


def test_stable_stale_snapshot_becomes_degraded_without_attestation(tmp_path: Path) -> None:
    playlist = _playlist()
    desired = _desired(playlist)
    store = _store(tmp_path)
    client = _SnapshotPropagationSpotify(["snapshot-stale", "snapshot-stale"])

    result = reconcile_spotify_playlist(client, playlist, desired, store=store)

    assert result.ok is True
    assert result.wrote is True
    assert result.degraded_verification is True
    assert result.warning == (
        "Spotify verification degraded: snapshot propagation pending after exact readback"
    )
    assert client.writes == 1
    assert client.snapshot_calls == 2
    assert store.get_playlist_attestation(playlist.id) is None


def test_snapshot_that_converges_on_recheck_is_fully_verified_and_attested(
    tmp_path: Path,
) -> None:
    playlist = _playlist()
    desired = _desired(playlist)
    store = _store(tmp_path)
    client = _SnapshotPropagationSpotify(["snapshot-stale", "snapshot-write"])

    result = reconcile_spotify_playlist(client, playlist, desired, store=store)

    assert result.ok is True
    assert result.wrote is True
    assert result.degraded_verification is False
    assert result.warning is None
    attestation = store.get_playlist_attestation(playlist.id)
    assert attestation is not None
    assert attestation.snapshot_id == "snapshot-write"


def test_content_race_during_snapshot_recheck_still_fails_closed(tmp_path: Path) -> None:
    playlist = _playlist()
    desired = _desired(playlist)
    store = _store(tmp_path)
    client = _SnapshotContentRaceSpotify(["snapshot-stale", "snapshot-stale"])

    with pytest.raises(SpotifyReconciliationError, match="content changed"):
        reconcile_spotify_playlist(client, playlist, desired, store=store)

    assert store.get_playlist_attestation(playlist.id) is None
