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
from news_bulletin_playlist.snapshot_pending import (
    PendingSnapshotConfirmation,
    PendingSnapshotJournal,
)
from news_bulletin_playlist.spotify_destination_audit import (
    audit_spotify_playlist_snapshot_first,
    playlist_attestation_fingerprint,
)

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


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
    return DesiredPlaylistState(
        playlist_id=playlist.id,
        generated_at=NOW,
        items=tuple(
            DesiredPlaylistItem(
                source_id=SourceId("ser"),
                source_native_id=str(index),
                published_at=NOW - timedelta(minutes=index),
                spotify_episode_uri=f"spotify:episode:{index}",
            )
            for index in range(count)
        ),
    )


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    return store


def _attest(
    store: SQLiteStore,
    playlist: PlaylistDefinition,
    desired: DesiredPlaylistState,
    *,
    snapshot: str = "snapshot-a",
) -> datetime:
    updated_at = NOW - timedelta(hours=6)
    store.set_playlist_attestation(
        playlist.id,
        destination_id=playlist.destination.external_id,
        snapshot_id=snapshot,
        desired_fingerprint=playlist_attestation_fingerprint(desired.uris),
        updated_at=updated_at,
    )
    return updated_at


class _Spotify:
    def __init__(self, uris: tuple[str, ...], snapshots: list[str]) -> None:
        self.uris = list(uris)
        self.snapshots = snapshots
        self.snapshot_calls = 0
        self.item_calls = 0
        self.writes = 0

    def playlist_snapshot(self, playlist_id: str) -> dict[str, Any]:
        del playlist_id
        index = min(self.snapshot_calls, len(self.snapshots) - 1)
        self.snapshot_calls += 1
        return {"snapshot_id": self.snapshots[index]}

    def playlist_items(
        self,
        playlist_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        del playlist_id
        self.item_calls += 1
        page = self.uris[offset : offset + limit]
        return {
            "items": [{"item": {"uri": uri}} for uri in page],
            "next": "next" if offset + len(page) < len(self.uris) else None,
            "total": len(self.uris),
        }

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]:
        del playlist_id
        self.writes += 1
        self.uris = list(uris)
        return {"snapshot_id": "write"}


@pytest.mark.parametrize("count", [49, 50, 51, 75, 100])
def test_matching_attested_snapshot_costs_exactly_one_request(
    tmp_path: Path,
    count: int,
) -> None:
    playlist = _playlist()
    desired = _desired(playlist, count=count)
    store = _store(tmp_path)
    attested_at = _attest(store, playlist, desired)
    client = _Spotify(desired.uris, ["snapshot-a"])

    result = audit_spotify_playlist_snapshot_first(
        client,
        playlist,
        desired,
        store=store,
        observed_at=NOW,
    )

    assert result is not None
    assert result.remote_evidence_qualified is True
    assert result.verified_desired_uris == desired.uris
    assert result.wrote is False
    assert client.snapshot_calls == 1
    assert client.item_calls == 0
    assert client.writes == 0
    attestation = store.get_playlist_attestation(playlist.id)
    assert attestation is not None
    assert attestation.snapshot_id == "snapshot-a"
    assert attestation.updated_at == attested_at


def test_stable_new_snapshot_with_exact_items_rotates_attestation_without_write(
    tmp_path: Path,
) -> None:
    playlist = _playlist()
    desired = _desired(playlist)
    store = _store(tmp_path)
    _attest(store, playlist, desired)
    client = _Spotify(desired.uris, ["snapshot-c", "snapshot-c"])

    result = audit_spotify_playlist_snapshot_first(
        client,
        playlist,
        desired,
        store=store,
        observed_at=NOW,
    )

    assert result is not None and result.remote_evidence_qualified
    assert client.snapshot_calls == 2
    assert client.item_calls == 2
    assert client.writes == 0
    attestation = store.get_playlist_attestation(playlist.id)
    assert attestation is not None
    assert attestation.snapshot_id == "snapshot-c"
    assert attestation.updated_at == NOW


def test_changed_items_fall_back_without_rotating_attestation(tmp_path: Path) -> None:
    playlist = _playlist()
    desired = _desired(playlist)
    store = _store(tmp_path)
    _attest(store, playlist, desired)
    changed = list(desired.uris)
    changed[0] = "spotify:episode:external"
    client = _Spotify(tuple(changed), ["snapshot-c", "snapshot-c"])

    result = audit_spotify_playlist_snapshot_first(
        client,
        playlist,
        desired,
        store=store,
        observed_at=NOW,
    )

    assert result is None
    assert client.snapshot_calls == 2
    assert client.item_calls == 2
    assert client.writes == 0
    attestation = store.get_playlist_attestation(playlist.id)
    assert attestation is not None and attestation.snapshot_id == "snapshot-a"


def test_unstable_new_snapshot_falls_back_without_rotating_attestation(tmp_path: Path) -> None:
    playlist = _playlist()
    desired = _desired(playlist)
    store = _store(tmp_path)
    _attest(store, playlist, desired)
    client = _Spotify(desired.uris, ["snapshot-c", "snapshot-d"])

    result = audit_spotify_playlist_snapshot_first(
        client,
        playlist,
        desired,
        store=store,
        observed_at=NOW,
    )

    assert result is None
    assert client.snapshot_calls == 2
    assert client.item_calls == 2
    attestation = store.get_playlist_attestation(playlist.id)
    assert attestation is not None and attestation.snapshot_id == "snapshot-a"


def test_pending_transition_blocks_shortcut_before_provider_io(tmp_path: Path) -> None:
    playlist = _playlist()
    desired = _desired(playlist)
    store = _store(tmp_path)
    _attest(store, playlist, desired)
    PendingSnapshotJournal(store).set(
        PendingSnapshotConfirmation(
            playlist_id=playlist.id,
            destination_id=playlist.destination.external_id,
            baseline_snapshot_id="snapshot-a",
            expected_snapshot_id="snapshot-b",
            desired_fingerprint=playlist_attestation_fingerprint(desired.uris),
            created_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(minutes=59),
        )
    )
    client = _Spotify(desired.uris, ["snapshot-a"])

    result = audit_spotify_playlist_snapshot_first(
        client,
        playlist,
        desired,
        store=store,
        observed_at=NOW,
    )

    assert result is None
    assert client.snapshot_calls == 0
    assert client.item_calls == 0
    assert client.writes == 0


def test_missing_or_mismatched_attestation_blocks_shortcut_without_io(tmp_path: Path) -> None:
    playlist = _playlist()
    desired = _desired(playlist)
    store = _store(tmp_path)
    client = _Spotify(desired.uris, ["snapshot-a"])

    assert (
        audit_spotify_playlist_snapshot_first(
            client, playlist, desired, store=store, observed_at=NOW
        )
        is None
    )
    assert client.snapshot_calls == 0

    other = _desired(playlist, count=74)
    _attest(store, playlist, other)
    assert (
        audit_spotify_playlist_snapshot_first(
            client, playlist, desired, store=store, observed_at=NOW
        )
        is None
    )
    assert client.snapshot_calls == 0


def test_eventual_detection_boundary_is_explicit_and_deterministic(tmp_path: Path) -> None:
    playlist = _playlist()
    desired = _desired(playlist)
    store = _store(tmp_path)
    _attest(store, playlist, desired)
    changed = list(desired.uris)
    changed[0] = "spotify:episode:external"
    client = _Spotify(tuple(changed), ["snapshot-a"])

    first = audit_spotify_playlist_snapshot_first(
        client,
        playlist,
        desired,
        store=store,
        observed_at=NOW,
    )

    # Accepted residual: Spotify can expose changed items while the snapshot endpoint still
    # temporarily returns the previously attested A, so this audit intentionally does not read.
    assert first is not None and first.remote_evidence_qualified
    assert client.snapshot_calls == 1
    assert client.item_calls == 0

    client.snapshots = ["snapshot-c", "snapshot-c"]
    client.snapshot_calls = 0
    later = audit_spotify_playlist_snapshot_first(
        client,
        playlist,
        desired,
        store=store,
        observed_at=NOW + timedelta(hours=6),
    )

    assert later is None
    assert client.snapshot_calls == 2
    assert client.item_calls == 2
    assert client.writes == 0
