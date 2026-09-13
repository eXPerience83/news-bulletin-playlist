from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

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
from news_bulletin_playlist.reconciliation import reconcile_spotify_playlist
from news_bulletin_playlist.snapshot_pending import (
    PendingSnapshotConfirmation,
    PendingSnapshotJournal,
)

NOW = datetime(2026, 9, 13, 7, 30, tzinfo=UTC)
PLAYLIST_ID = PlaylistId("spain_spanish_news")
DESTINATION_ID = "destination"


def _playlist() -> PlaylistDefinition:
    return PlaylistDefinition(
        id=PLAYLIST_ID,
        display_name="Noticias España",
        description="test",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        enabled=True,
        source_selection=SourceSelection((SourceId("rne"),)),
        destination=DestinationReference(AdapterId("spotify"), DESTINATION_ID),
    )


def _desired() -> DesiredPlaylistState:
    items = tuple(
        DesiredPlaylistItem(
            source_id=SourceId("rne"),
            source_native_id=f"rne-{index}",
            published_at=NOW - timedelta(seconds=index),
            spotify_episode_uri=f"spotify:episode:{index:03d}",
        )
        for index in range(35)
    )
    return DesiredPlaylistState(playlist_id=PLAYLIST_ID, generated_at=NOW, items=items)


def _fingerprint(desired: DesiredPlaylistState) -> str:
    encoded = json.dumps(list(desired.uris), ensure_ascii=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    return store


def _pending(store: SQLiteStore, desired: DesiredPlaylistState, *, exact: bool) -> None:
    PendingSnapshotJournal(store).set(
        PendingSnapshotConfirmation(
            playlist_id=PLAYLIST_ID,
            destination_id=DESTINATION_ID,
            baseline_snapshot_id="snapshot-A",
            expected_snapshot_id="snapshot-B",
            desired_fingerprint=_fingerprint(desired),
            created_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(hours=1),
            readback_exact=exact,
        )
    )


class _Client:
    def __init__(self, desired: DesiredPlaylistState, snapshot: str) -> None:
        self.state = list(desired.uris)
        self.snapshot = snapshot
        self.item_calls = 0
        self.snapshot_calls = 0
        self.writes = 0

    def playlist_items(
        self,
        playlist_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        assert playlist_id == DESTINATION_ID
        self.item_calls += 1
        page = self.state[offset : offset + limit]
        return {
            "items": [{"item": {"uri": uri}} for uri in page],
            "next": "next" if offset + limit < len(self.state) else None,
            "total": len(self.state),
        }

    def playlist_snapshot(self, playlist_id: str) -> dict[str, Any]:
        assert playlist_id == DESTINATION_ID
        self.snapshot_calls += 1
        return {"snapshot_id": self.snapshot}

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]:
        assert playlist_id == DESTINATION_ID
        self.writes += 1
        self.state = list(uris)
        return {"snapshot_id": "snapshot-write"}


def test_exact_pending_expected_snapshot_uses_one_snapshot_and_zero_item_reads(
    tmp_path: Path,
) -> None:
    desired = _desired()
    store = _store(tmp_path)
    _pending(store, desired, exact=True)
    client = _Client(desired, "snapshot-B")

    result = reconcile_spotify_playlist(client, _playlist(), desired, store=store)

    assert result.wrote is False and result.remote_evidence_qualified is True
    assert (client.snapshot_calls, client.item_calls, client.writes) == (1, 0, 0)
    assert PendingSnapshotJournal(store).get(PLAYLIST_ID) is None
    attestation = store.get_playlist_attestation(PLAYLIST_ID)
    assert attestation is not None
    assert attestation.snapshot_id == "snapshot-B"


def test_exact_pending_baseline_snapshot_keeps_divergence_read(
    tmp_path: Path,
) -> None:
    desired = _desired()
    store = _store(tmp_path)
    _pending(store, desired, exact=True)
    client = _Client(desired, "snapshot-A")

    result = reconcile_spotify_playlist(client, _playlist(), desired, store=store)

    assert result.wrote is False and result.degraded_verification is True
    assert result.remote_evidence_qualified is False
    assert (client.snapshot_calls, client.item_calls, client.writes) == (1, 1, 0)
    assert PendingSnapshotJournal(store).get(PLAYLIST_ID) is not None
    assert store.get_playlist_attestation(PLAYLIST_ID) is None


def test_exact_pending_third_snapshot_keeps_two_content_observations(
    tmp_path: Path,
) -> None:
    desired = _desired()
    store = _store(tmp_path)
    _pending(store, desired, exact=True)
    client = _Client(desired, "snapshot-C")

    result = reconcile_spotify_playlist(client, _playlist(), desired, store=store)

    assert result.wrote is False and result.remote_evidence_qualified is True
    assert (client.snapshot_calls, client.item_calls, client.writes) == (2, 2, 0)
    attestation = store.get_playlist_attestation(PLAYLIST_ID)
    assert attestation is not None
    assert attestation.snapshot_id == "snapshot-C"


def test_non_exact_pending_keeps_item_first_resolution(tmp_path: Path) -> None:
    desired = _desired()
    store = _store(tmp_path)
    _pending(store, desired, exact=False)
    client = _Client(desired, "snapshot-B")

    result = reconcile_spotify_playlist(client, _playlist(), desired, store=store)

    assert result.wrote is False
    assert (client.snapshot_calls, client.item_calls, client.writes) == (1, 1, 0)
