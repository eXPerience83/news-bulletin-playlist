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
from news_bulletin_playlist.persistence import PersistenceError, SQLiteStore
from news_bulletin_playlist.reconciliation import (
    SpotifyReconciliationError,
    reconcile_spotify_playlist,
)
from news_bulletin_playlist.snapshot_pending import PendingSnapshotJournal

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
        self.playlist_item_calls = 0
        self.writes = 0

    def playlist_items(
        self,
        playlist_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        del playlist_id
        self.playlist_item_calls += 1
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


class _LateSnapshotContentRaceSpotify(_SnapshotPropagationSpotify):
    def playlist_items(
        self,
        playlist_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        if self.snapshot_calls == 2 and offset == 0 and self.writes:
            self.state[0] = "spotify:episode:late-concurrent"
        return super().playlist_items(playlist_id, limit=limit, offset=offset)


def test_stable_stale_snapshot_becomes_durable_pending_without_second_recheck(
    tmp_path: Path,
) -> None:
    playlist = _playlist()
    desired = _desired(playlist)
    store = _store(tmp_path)
    client = _SnapshotPropagationSpotify(["snapshot-stale", "snapshot-stale"])

    result = reconcile_spotify_playlist(client, playlist, desired, store=store)

    assert result.ok is True
    assert result.wrote is True
    assert result.degraded_verification is True
    assert result.remote_evidence_qualified is False
    assert result.warning == (
        "Spotify verification degraded: snapshot propagation pending after exact readback"
    )
    assert client.writes == 1
    # One prewrite page + two post-write pages for 75 items.  The former second
    # exact readback (two more pages) and second snapshot request are deliberately absent.
    assert client.playlist_item_calls == 3
    assert client.snapshot_calls == 2
    assert store.get_playlist_attestation(playlist.id) is None
    pending = PendingSnapshotJournal(store).get(playlist.id)
    assert pending is not None
    assert pending.baseline_snapshot_id == "snapshot-stale"
    assert pending.expected_snapshot_id == "snapshot-write"


@pytest.mark.parametrize(
    ("count", "expected_item_calls"),
    ((49, 2), (50, 3), (75, 3), (100, 4)),
)
def test_stale_snapshot_request_boundaries_omit_second_immediate_pass(
    tmp_path: Path,
    count: int,
    expected_item_calls: int,
) -> None:
    playlist = _playlist()
    desired = _desired(playlist, count=count)
    store = _store(tmp_path)
    client = _SnapshotPropagationSpotify(["snapshot-stale"])

    result = reconcile_spotify_playlist(client, playlist, desired, store=store)

    assert result.wrote is True
    assert result.degraded_verification is True
    assert result.remote_evidence_qualified is False
    assert client.writes == 1
    assert client.playlist_item_calls == expected_item_calls
    assert client.snapshot_calls == 2
    assert store.get_playlist_attestation(playlist.id) is None
    assert PendingSnapshotJournal(store).get(playlist.id) is not None


def test_pending_persistence_failure_after_exact_readback_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    playlist = _playlist()
    desired = _desired(playlist)
    store = _store(tmp_path)
    client = _SnapshotPropagationSpotify(["snapshot-stale"])

    def fail_set(self: PendingSnapshotJournal, confirmation: object) -> None:
        del self, confirmation
        raise PersistenceError("persist pending Spotify snapshot failed")

    monkeypatch.setattr(PendingSnapshotJournal, "set", fail_set)

    with pytest.raises(PersistenceError, match="persist pending Spotify snapshot failed"):
        reconcile_spotify_playlist(client, playlist, desired, store=store)

    assert client.writes == 1
    assert client.playlist_item_calls == 3
    assert client.snapshot_calls == 2
    assert store.get_playlist_attestation(playlist.id) is None


def test_unexpected_advancing_snapshot_fails_closed(tmp_path: Path) -> None:
    playlist = _playlist()
    desired = _desired(playlist)
    store = _store(tmp_path)
    client = _SnapshotPropagationSpotify(["snapshot-a", "snapshot-b"])

    with pytest.raises(SpotifyReconciliationError, match="known prewrite baseline"):
        reconcile_spotify_playlist(client, playlist, desired, store=store)

    assert client.writes == 1
    assert store.get_playlist_attestation(playlist.id) is None
    assert PendingSnapshotJournal(store).get(playlist.id) is None


def test_snapshot_that_is_immediately_visible_is_fully_verified_and_attested(
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
    assert client.snapshot_calls == 2
    attestation = store.get_playlist_attestation(playlist.id)
    assert attestation is not None
    assert attestation.snapshot_id == "snapshot-write"
    assert PendingSnapshotJournal(store).get(playlist.id) is None


def test_unexpected_snapshot_can_converge_to_write_snapshot_on_strict_recheck(
    tmp_path: Path,
) -> None:
    playlist = _playlist()
    desired = _desired(playlist)
    store = _store(tmp_path)
    client = _SnapshotPropagationSpotify(["snapshot-a", "snapshot-c", "snapshot-write"])

    result = reconcile_spotify_playlist(client, playlist, desired, store=store)

    assert result.ok is True
    assert result.wrote is True
    assert result.degraded_verification is False
    assert client.snapshot_calls == 3
    # The C path intentionally retains the second full readback.
    assert client.playlist_item_calls == 5
    attestation = store.get_playlist_attestation(playlist.id)
    assert attestation is not None
    assert attestation.snapshot_id == "snapshot-write"


def test_content_race_during_snapshot_recheck_still_fails_closed(tmp_path: Path) -> None:
    playlist = _playlist()
    desired = _desired(playlist)
    store = _store(tmp_path)
    client = _SnapshotContentRaceSpotify(["snapshot-stale", "snapshot-stale"])

    with pytest.raises(SpotifyReconciliationError):
        reconcile_spotify_playlist(client, playlist, desired, store=store)

    assert client.writes == 1
    assert store.get_playlist_attestation(playlist.id) is None


def test_late_content_race_after_advancing_snapshot_still_fails_closed(
    tmp_path: Path,
) -> None:
    playlist = _playlist()
    desired = _desired(playlist)
    store = _store(tmp_path)
    client = _LateSnapshotContentRaceSpotify(["snapshot-a", "snapshot-b"])

    with pytest.raises(SpotifyReconciliationError):
        reconcile_spotify_playlist(client, playlist, desired, store=store)

    assert client.writes == 1
    assert store.get_playlist_attestation(playlist.id) is None
