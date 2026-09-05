from __future__ import annotations

from dataclasses import replace
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
from news_bulletin_playlist.snapshot_pending import PendingSnapshotJournal

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
PLAYLIST_ID = PlaylistId("spain_spanish_news")
DESTINATION_ID = "destination"


def _playlist() -> PlaylistDefinition:
    return PlaylistDefinition(
        id=PLAYLIST_ID,
        display_name="Noticias en Español",
        description="test",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es-ES"),),
        enabled=True,
        source_selection=SourceSelection((SourceId("rne"),)),
        destination=DestinationReference(AdapterId("spotify"), DESTINATION_ID),
    )


def _desired(*, generated_at: datetime = NOW, count: int = 51) -> DesiredPlaylistState:
    items = tuple(
        DesiredPlaylistItem(
            source_id=SourceId("rne"),
            source_native_id=f"rne-{index}",
            published_at=generated_at - timedelta(seconds=index),
            spotify_episode_uri=f"spotify:episode:{index:03d}",
        )
        for index in range(count)
    )
    return DesiredPlaylistState(playlist_id=PLAYLIST_ID, generated_at=generated_at, items=items)


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    return store


class _LaggingSpotify:
    def __init__(self, *, count: int = 51) -> None:
        self.slots: list[str | None] = [
            f"spotify:episode:old-{index:03d}" for index in range(count)
        ]
        self.visible_snapshot = "snapshot-A"
        self.write_snapshot = "snapshot-B"
        self.writes: list[list[str]] = []

    def playlist_items(
        self,
        playlist_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        assert playlist_id == DESTINATION_ID
        page = self.slots[offset : offset + limit]
        return {
            "items": [{"item": None if uri is None else {"uri": uri}} for uri in page],
            "next": "next" if offset + limit < len(self.slots) else None,
            "total": len(self.slots),
        }

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]:
        assert playlist_id == DESTINATION_ID
        self.writes.append(list(uris))
        self.slots = list(uris)
        self.slots[17] = None
        # Spotify accepted the write and returned B while GET snapshot intentionally stays on A.
        return {"snapshot_id": self.write_snapshot}

    def playlist_snapshot(self, playlist_id: str) -> dict[str, Any]:
        assert playlist_id == DESTINATION_ID
        return {"snapshot_id": self.visible_snapshot}


class _ExactLaggingSpotify(_LaggingSpotify):
    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]:
        assert playlist_id == DESTINATION_ID
        self.writes.append(list(uris))
        self.slots = list(uris)
        return {"snapshot_id": self.write_snapshot}


def test_single_page_stale_snapshot_is_durable_across_restart(tmp_path: Path) -> None:
    store = _store(tmp_path)
    client = _ExactLaggingSpotify()
    desired = _desired(count=35)

    first = reconcile_spotify_playlist(client, _playlist(), desired, store=store)
    pending = PendingSnapshotJournal(store).get(PLAYLIST_ID)
    assert first.wrote is True and first.degraded_verification
    assert pending is not None
    assert pending.baseline_snapshot_id == "snapshot-A"
    assert pending.expected_snapshot_id == "snapshot-B"
    assert store.get_playlist_attestation(PLAYLIST_ID) is None

    restarted = SQLiteStore(store.path)
    restarted.initialize()
    stale = reconcile_spotify_playlist(
        client,
        _playlist(),
        _desired(count=35, generated_at=NOW + timedelta(minutes=10)),
        store=restarted,
    )
    assert stale.wrote is False and stale.degraded_verification
    assert len(client.writes) == 1

    client.visible_snapshot = "snapshot-B"
    promoted = reconcile_spotify_playlist(
        client,
        _playlist(),
        _desired(count=35, generated_at=NOW + timedelta(minutes=20)),
        store=restarted,
    )
    assert promoted.wrote is False
    assert PendingSnapshotJournal(restarted).get(PLAYLIST_ID) is None
    assert restarted.get_playlist_attestation(PLAYLIST_ID).snapshot_id == "snapshot-B"  # type: ignore[union-attr]


def test_single_page_pending_partial_read_stays_degraded_without_rewrite(tmp_path: Path) -> None:
    store = _store(tmp_path)
    client = _ExactLaggingSpotify()
    desired = _desired(count=35)
    reconcile_spotify_playlist(client, _playlist(), desired, store=store)

    client.slots[17] = None
    partial = reconcile_spotify_playlist(
        client,
        _playlist(),
        _desired(count=35, generated_at=NOW + timedelta(minutes=10)),
        store=store,
    )
    assert partial.wrote is False and partial.degraded_verification
    assert len(client.writes) == 1


@pytest.mark.parametrize("divergence", ["uri", "order", "count"])
def test_single_page_pending_content_divergence_is_repaired_not_accepted_as_cosmetic(
    tmp_path: Path,
    divergence: str,
) -> None:
    store = _store(tmp_path)
    client = _ExactLaggingSpotify(count=35)
    desired = _desired(count=35)
    reconcile_spotify_playlist(client, _playlist(), desired, store=store)

    if divergence == "uri":
        client.slots[17] = "spotify:episode:wrong"
    elif divergence == "order":
        client.slots[0], client.slots[1] = client.slots[1], client.slots[0]
    else:
        client.slots.pop()
    repaired = reconcile_spotify_playlist(
        client,
        _playlist(),
        _desired(count=35, generated_at=NOW + timedelta(minutes=20)),
        store=store,
    )
    assert repaired.wrote is True
    assert len(client.writes) == 2


def test_single_page_exact_cosmetic_third_snapshot_promotes_after_stable_recheck(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    client = _ExactLaggingSpotify(count=35)
    reconcile_spotify_playlist(client, _playlist(), _desired(count=35), store=store)
    client.visible_snapshot = "snapshot-C-metadata"

    result = reconcile_spotify_playlist(
        client,
        _playlist(),
        _desired(count=35, generated_at=NOW + timedelta(minutes=10)),
        store=SQLiteStore(store.path),
    )

    assert result.wrote is False and not result.degraded_verification
    assert len(client.writes) == 1
    assert PendingSnapshotJournal(store).get(PLAYLIST_ID) is None
    assert store.get_playlist_attestation(PLAYLIST_ID).snapshot_id == "snapshot-C-metadata"  # type: ignore[union-attr]


def test_single_page_partial_cosmetic_third_snapshot_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    client = _LaggingSpotify(count=35)
    reconcile_spotify_playlist(client, _playlist(), _desired(count=35), store=store)
    client.visible_snapshot = "snapshot-C-cover"

    with pytest.raises(SpotifyReconciliationError, match="outside the pending confirmation"):
        reconcile_spotify_playlist(
            client,
            _playlist(),
            _desired(count=35, generated_at=NOW + timedelta(minutes=10)),
            store=SQLiteStore(store.path),
        )

    assert len(client.writes) == 1


def test_partial_stale_snapshot_is_persisted_and_next_cycle_does_not_rewrite(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    client = _LaggingSpotify()

    first = reconcile_spotify_playlist(client, _playlist(), _desired(), store=store)
    second = reconcile_spotify_playlist(
        client,
        _playlist(),
        _desired(generated_at=NOW + timedelta(minutes=10)),
        store=store,
    )

    assert first.ok and first.wrote is True and first.degraded_verification
    assert second.ok and second.wrote is False and second.degraded_verification
    assert len(client.writes) == 1
    pending = PendingSnapshotJournal(store).get(PLAYLIST_ID)
    assert pending is not None
    assert pending.baseline_snapshot_id == "snapshot-A"
    assert pending.expected_snapshot_id == "snapshot-B"
    assert store.get_playlist_attestation(PLAYLIST_ID) is None


def test_exact_stale_snapshot_survives_restart_then_partial_readback_without_rewrite(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    client = _ExactLaggingSpotify()

    first = reconcile_spotify_playlist(client, _playlist(), _desired(), store=store)
    pending = PendingSnapshotJournal(store).get(PLAYLIST_ID)
    assert first.ok and first.wrote is True and first.degraded_verification
    assert pending is not None
    assert pending.baseline_snapshot_id == "snapshot-A"
    assert pending.expected_snapshot_id == "snapshot-B"

    restarted = SQLiteStore(store.path)
    restarted.initialize()
    client.slots[17] = None
    second = reconcile_spotify_playlist(
        client,
        _playlist(),
        _desired(generated_at=NOW + timedelta(minutes=10)),
        store=restarted,
    )

    assert second.ok and second.wrote is False and second.degraded_verification
    assert len(client.writes) == 1
    persisted = PendingSnapshotJournal(restarted).get(PLAYLIST_ID)
    assert persisted is not None
    assert persisted.baseline_snapshot_id == "snapshot-A"
    assert persisted.expected_snapshot_id == "snapshot-B"


def test_pending_confirmation_survives_restart_and_promotes_when_expected_snapshot_appears(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    client = _LaggingSpotify()
    reconcile_spotify_playlist(client, _playlist(), _desired(), store=store)

    restarted = SQLiteStore(store.path)
    restarted.initialize()
    client.visible_snapshot = "snapshot-B"
    result = reconcile_spotify_playlist(
        client,
        _playlist(),
        _desired(generated_at=NOW + timedelta(minutes=10)),
        store=restarted,
    )

    assert result.ok and result.wrote is False and result.degraded_verification
    assert len(client.writes) == 1
    assert PendingSnapshotJournal(restarted).get(PLAYLIST_ID) is None
    attestation = restarted.get_playlist_attestation(PLAYLIST_ID)
    assert attestation is not None
    assert attestation.snapshot_id == "snapshot-B"


def test_incompatible_snapshot_during_fresh_pending_transition_fails_closed(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    client = _LaggingSpotify()
    reconcile_spotify_playlist(client, _playlist(), _desired(), store=store)
    client.visible_snapshot = "snapshot-C"

    with pytest.raises(SpotifyReconciliationError, match="outside the pending confirmation"):
        reconcile_spotify_playlist(
            client,
            _playlist(),
            _desired(generated_at=NOW + timedelta(minutes=10)),
            store=store,
        )

    assert len(client.writes) == 1


@pytest.mark.parametrize("operation", ["metadata", "cover"])
def test_stable_cosmetic_snapshot_after_restart_promotes_exact_content(
    tmp_path: Path,
    operation: str,
) -> None:
    store = _store(tmp_path)
    client = _ExactLaggingSpotify()
    reconcile_spotify_playlist(client, _playlist(), _desired(), store=store)

    restarted = SQLiteStore(store.path)
    restarted.initialize()
    client.visible_snapshot = f"snapshot-C-{operation}"
    result = reconcile_spotify_playlist(
        client,
        _playlist(),
        _desired(generated_at=NOW + timedelta(minutes=10)),
        store=restarted,
    )

    assert result.ok and result.wrote is False and not result.degraded_verification
    assert len(client.writes) == 1
    assert PendingSnapshotJournal(restarted).get(PLAYLIST_ID) is None
    attestation = restarted.get_playlist_attestation(PLAYLIST_ID)
    assert attestation is not None
    assert attestation.snapshot_id == f"snapshot-C-{operation}"
    again = reconcile_spotify_playlist(client, _playlist(), _desired(), store=restarted)
    assert again.wrote is False
    assert len(client.writes) == 1


@pytest.mark.parametrize("change", ["uri", "order", "count", "partial", "snapshot"])
def test_third_snapshot_recheck_rejects_ambiguity(tmp_path: Path, change: str) -> None:
    class RacingSpotify(_ExactLaggingSpotify):
        armed = False
        reads = 0

        def playlist_items(
            self, playlist_id: str, *, limit: int = 50, offset: int = 0
        ) -> dict[str, Any]:
            if self.armed and offset == 0:
                self.reads += 1
                if self.reads == 2:
                    if change == "uri":
                        self.slots[0] = "spotify:episode:unexpected"
                    elif change == "order":
                        self.slots[0], self.slots[1] = self.slots[1], self.slots[0]
                    elif change == "count":
                        self.slots.pop()
                    elif change == "partial":
                        self.slots[17] = None
                    else:
                        self.visible_snapshot = "snapshot-D"
            return super().playlist_items(playlist_id, limit=limit, offset=offset)

    store = _store(tmp_path)
    client = RacingSpotify()
    reconcile_spotify_playlist(client, _playlist(), _desired(), store=store)
    client.visible_snapshot = "snapshot-C"
    client.armed = True
    with pytest.raises(SpotifyReconciliationError):
        reconcile_spotify_playlist(client, _playlist(), _desired(), store=SQLiteStore(store.path))
    assert client.reads == 2
    assert len(client.writes) == 1
    assert PendingSnapshotJournal(store).get(PLAYLIST_ID) is not None
    assert store.get_playlist_attestation(PLAYLIST_ID) is None


@pytest.mark.parametrize("field", ["destination_id", "desired_fingerprint"])
def test_third_snapshot_cannot_inherit_other_evidence(tmp_path: Path, field: str) -> None:
    store = _store(tmp_path)
    client = _ExactLaggingSpotify()
    reconcile_spotify_playlist(client, _playlist(), _desired(), store=store)
    journal = PendingSnapshotJournal(store)
    pending = journal.get(PLAYLIST_ID)
    assert pending is not None
    if field == "destination_id":
        journal.set(replace(pending, destination_id="other-destination"))
    else:
        journal.set(replace(pending, desired_fingerprint="0" * 64))
    client.visible_snapshot = "snapshot-C"
    result = reconcile_spotify_playlist(client, _playlist(), _desired(), store=store)
    assert result.wrote is False  # Independently exact state needs no repair.
    assert journal.get(PLAYLIST_ID) is None
    assert store.get_playlist_attestation(PLAYLIST_ID) is None


def test_cosmetic_snapshot_with_partial_read_still_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    client = _LaggingSpotify()
    reconcile_spotify_playlist(client, _playlist(), _desired(), store=store)
    client.visible_snapshot = "snapshot-C"

    with pytest.raises(SpotifyReconciliationError, match="outside the pending confirmation"):
        reconcile_spotify_playlist(
            client,
            _playlist(),
            _desired(generated_at=NOW + timedelta(minutes=10)),
            store=SQLiteStore(store.path),
        )

    assert len(client.writes) == 1


def test_expired_pending_transition_is_cleared_and_reconciled_normally(tmp_path: Path) -> None:
    store = _store(tmp_path)
    client = _LaggingSpotify()
    reconcile_spotify_playlist(client, _playlist(), _desired(), store=store)

    result = reconcile_spotify_playlist(
        client,
        _playlist(),
        _desired(generated_at=NOW + timedelta(hours=2)),
        store=store,
    )

    assert result.wrote is True
    assert len(client.writes) == 2
