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
from news_bulletin_playlist.spotify.client import SpotifyTransportError

NOW = datetime(2026, 9, 1, 20, 0, tzinfo=UTC)
PLAYLIST_ID = PlaylistId("spain_spanish_news")
DESTINATION_ID = "spotify-playlist"


def _playlist() -> PlaylistDefinition:
    return PlaylistDefinition(
        id=PLAYLIST_ID,
        display_name="Spain Spanish News",
        description="test",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        enabled=True,
        source_selection=SourceSelection((SourceId("rne"),)),
        destination=DestinationReference(AdapterId("spotify"), DESTINATION_ID),
    )


def _desired(*uris: str, generated_at: datetime = NOW) -> DesiredPlaylistState:
    items = tuple(
        DesiredPlaylistItem(
            source_id=SourceId("rne"),
            source_native_id=f"rne-{index}",
            published_at=generated_at - timedelta(minutes=index),
            spotify_episode_uri=uri,
        )
        for index, uri in enumerate(uris)
    )
    return DesiredPlaylistState(
        playlist_id=PLAYLIST_ID,
        generated_at=generated_at,
        items=items,
    )


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    return store


class _AttestedSpotify:
    def __init__(
        self,
        slots: list[str | None],
        *,
        snapshot: str = "snapshot-initial",
    ) -> None:
        self.slots = list(slots)
        self.snapshot = snapshot
        self.writes: list[list[str]] = []
        self.reads: list[tuple[int, int]] = []
        self.snapshot_reads = 0
        self.opaque_after_write: set[int] = set()
        self.visible_after_write: dict[int, str] = {}
        self.write_response_override: object | None = None
        self.snapshot_responses: list[object] = []

    def playlist_items(
        self,
        playlist_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        assert playlist_id == DESTINATION_ID
        self.reads.append((limit, offset))
        page = self.slots[offset : offset + limit]
        next_url = "next" if offset + limit < len(self.slots) else None
        return {
            "items": [{"item": None if uri is None else {"uri": uri}} for uri in page],
            "next": next_url,
            "total": len(self.slots),
        }

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]:
        assert playlist_id == DESTINATION_ID
        self.writes.append(list(uris))
        self.snapshot = f"snapshot-write-{len(self.writes)}"
        self.slots = [
            None if index in self.opaque_after_write else uri for index, uri in enumerate(uris)
        ]
        for index, uri in self.visible_after_write.items():
            if 0 <= index < len(self.slots):
                self.slots[index] = uri
        response = (
            {"snapshot_id": self.snapshot}
            if self.write_response_override is None
            else self.write_response_override
        )
        assert isinstance(response, dict)
        return dict(response)

    def playlist_snapshot(self, playlist_id: str) -> dict[str, Any]:
        assert playlist_id == DESTINATION_ID
        self.snapshot_reads += 1
        if self.snapshot_responses:
            response = self.snapshot_responses.pop(0)
            if isinstance(response, Exception):
                raise response
            assert isinstance(response, dict)
            return dict(response)
        return {"snapshot_id": self.snapshot}


def _apply(
    store: SQLiteStore,
    client: _AttestedSpotify,
    *uris: str,
    generated_at: datetime = NOW,
):
    return reconcile_spotify_playlist(
        client,
        _playlist(),
        _desired(*uris, generated_at=generated_at),
        store=store,
    )


def test_exact_readback_confirms_write_snapshot_for_single_page(tmp_path: Path) -> None:
    store = _store(tmp_path)
    client = _AttestedSpotify(["spotify:episode:old"])

    result = _apply(store, client, "spotify:episode:new")

    assert result.ok
    assert result.wrote is True
    assert result.degraded_verification is False
    assert result.warning is None
    assert client.snapshot_reads == 2
    assert client.writes == [["spotify:episode:new"]]
    attestation = store.get_playlist_attestation(PLAYLIST_ID)
    assert attestation is not None
    assert attestation.snapshot_id == "snapshot-write-1"


def test_write_with_unavailable_slot_is_attested_as_degraded_success(tmp_path: Path) -> None:
    store = _store(tmp_path)
    client = _AttestedSpotify(["spotify:episode:old"])
    client.opaque_after_write = {1}

    result = _apply(
        store,
        client,
        "spotify:episode:one",
        "spotify:episode:two",
    )

    assert result.ok
    assert result.wrote is True
    assert result.applied_count == 2
    assert result.degraded_verification is True
    assert result.warning is not None
    assert "[1]" in result.warning
    assert client.snapshot_reads == 2
    attestation = store.get_playlist_attestation(PLAYLIST_ID)
    assert attestation is not None
    assert attestation.destination_id == DESTINATION_ID
    assert attestation.snapshot_id == "snapshot-write-1"
    assert len(attestation.desired_fingerprint) == 64


def test_identical_next_cycle_with_same_snapshot_performs_zero_writes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    client = _AttestedSpotify(["spotify:episode:old"])
    client.opaque_after_write = {1}
    uris = ("spotify:episode:one", "spotify:episode:two")

    first = _apply(store, client, *uris)
    second = _apply(store, client, *uris, generated_at=NOW + timedelta(minutes=10))

    assert first.wrote is True
    assert second.ok
    assert second.wrote is False
    assert second.degraded_verification is True
    assert client.writes == [list(uris)]


def test_restart_reuses_durable_attestation_without_rewriting(tmp_path: Path) -> None:
    store = _store(tmp_path)
    client = _AttestedSpotify(["spotify:episode:old"])
    client.opaque_after_write = {0}
    uris = ("spotify:episode:one", "spotify:episode:two")
    _apply(store, client, *uris)

    restarted = SQLiteStore(store.path)
    restarted.initialize()
    result = _apply(
        restarted,
        client,
        *uris,
        generated_at=NOW + timedelta(minutes=10),
    )

    assert result.ok
    assert result.wrote is False
    assert result.degraded_verification is True
    assert len(client.writes) == 1


def test_desired_change_in_opaque_position_forces_write(tmp_path: Path) -> None:
    store = _store(tmp_path)
    client = _AttestedSpotify(["spotify:episode:old"])
    client.opaque_after_write = {1}
    _apply(store, client, "spotify:episode:one", "spotify:episode:two")

    result = _apply(
        store,
        client,
        "spotify:episode:one",
        "spotify:episode:changed",
        generated_at=NOW + timedelta(minutes=10),
    )

    assert result.wrote is True
    assert len(client.writes) == 2
    assert client.writes[-1][1] == "spotify:episode:changed"


def test_external_snapshot_change_invalidates_attestation_and_forces_write(tmp_path: Path) -> None:
    store = _store(tmp_path)
    client = _AttestedSpotify(["spotify:episode:old"])
    client.opaque_after_write = {1}
    uris = ("spotify:episode:one", "spotify:episode:two")
    _apply(store, client, *uris)
    client.snapshot = "snapshot-manual-edit"

    result = _apply(
        store,
        client,
        *uris,
        generated_at=NOW + timedelta(minutes=10),
    )

    assert result.wrote is True
    assert len(client.writes) == 2


def test_visible_uri_mismatch_is_never_accepted_by_attestation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    client = _AttestedSpotify(["spotify:episode:old"])
    client.opaque_after_write = {1}
    uris = ("spotify:episode:one", "spotify:episode:two")
    _apply(store, client, *uris)
    client.slots[0] = "spotify:episode:manual"

    result = _apply(
        store,
        client,
        *uris,
        generated_at=NOW + timedelta(minutes=10),
    )

    assert result.wrote is True
    assert len(client.writes) == 2


def test_visible_order_mismatch_forces_reconciliation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    client = _AttestedSpotify(["spotify:episode:old"])
    uris = (
        "spotify:episode:one",
        "spotify:episode:two",
        "spotify:episode:three",
    )
    client.opaque_after_write = {2}
    _apply(store, client, *uris)
    client.slots[:2] = [uris[1], uris[0]]

    result = _apply(
        store,
        client,
        *uris,
        generated_at=NOW + timedelta(minutes=10),
    )

    assert result.wrote is True
    assert len(client.writes) == 2


def test_wrapper_count_mismatch_forces_reconciliation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    client = _AttestedSpotify(["spotify:episode:old"])
    client.opaque_after_write = {1}
    uris = ("spotify:episode:one", "spotify:episode:two")
    _apply(store, client, *uris)
    client.slots.append(None)

    result = _apply(
        store,
        client,
        *uris,
        generated_at=NOW + timedelta(minutes=10),
    )

    assert result.wrote is True
    assert len(client.writes) == 2


def test_multiple_unavailable_slots_are_attested_positionally(tmp_path: Path) -> None:
    store = _store(tmp_path)
    client = _AttestedSpotify(["spotify:episode:old"])
    client.opaque_after_write = {0, 2}
    uris = (
        "spotify:episode:one",
        "spotify:episode:two",
        "spotify:episode:three",
    )

    first = _apply(store, client, *uris)
    second = _apply(
        store,
        client,
        *uris,
        generated_at=NOW + timedelta(minutes=10),
    )

    assert first.degraded_verification is True
    assert first.warning is not None and "[0,2]" in first.warning
    assert second.wrote is False
    assert len(client.writes) == 1


@pytest.mark.parametrize(
    "write_response",
    [{}, {"snapshot_id": None}, {"snapshot_id": ""}, {"snapshot_id": 123}],
)
def test_missing_or_invalid_write_snapshot_fails_closed(
    tmp_path: Path,
    write_response: dict[str, object],
) -> None:
    store = _store(tmp_path)
    client = _AttestedSpotify(["spotify:episode:old"])
    client.opaque_after_write = {0}
    client.write_response_override = write_response

    with pytest.raises(SpotifyReconciliationError, match="valid snapshot_id"):
        _apply(store, client, "spotify:episode:new")

    assert len(client.writes) == 1
    assert store.get_playlist_attestation(PLAYLIST_ID) is None


def test_malformed_snapshot_check_on_existing_attestation_preserves_destination(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    client = _AttestedSpotify(["spotify:episode:old"])
    client.opaque_after_write = {1}
    uris = ("spotify:episode:one", "spotify:episode:two")
    _apply(store, client, *uris)
    attestation_before = store.get_playlist_attestation(PLAYLIST_ID)
    assert attestation_before is not None
    client.snapshot_responses = [{}]

    with pytest.raises(SpotifyReconciliationError, match="valid snapshot_id"):
        _apply(
            store,
            client,
            *uris,
            generated_at=NOW + timedelta(minutes=10),
        )

    assert len(client.writes) == 1
    assert store.get_playlist_attestation(PLAYLIST_ID) == attestation_before


def test_snapshot_change_between_write_and_degraded_readback_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    client = _AttestedSpotify(["spotify:episode:old"])
    client.opaque_after_write = {0}
    client.snapshot_responses = [
        {"snapshot_id": "snapshot-initial"},
        {"snapshot_id": "snapshot-concurrent-edit"},
    ]

    with pytest.raises(SpotifyReconciliationError, match="snapshot changed"):
        _apply(store, client, "spotify:episode:new")

    assert len(client.writes) == 1
    assert store.get_playlist_attestation(PLAYLIST_ID) is None


def test_snapshot_network_error_never_declares_degraded_success(tmp_path: Path) -> None:
    store = _store(tmp_path)
    client = _AttestedSpotify(["spotify:episode:old"])
    client.opaque_after_write = {0}
    client.snapshot_responses = [
        {"snapshot_id": "snapshot-initial"},
        SpotifyTransportError("safe network error"),
    ]

    with pytest.raises(SpotifyTransportError, match="safe network error"):
        _apply(store, client, "spotify:episode:new")

    assert len(client.writes) == 1
    assert store.get_playlist_attestation(PLAYLIST_ID) is None


def test_network_error_checking_existing_attestation_preserves_destination(tmp_path: Path) -> None:
    store = _store(tmp_path)
    client = _AttestedSpotify(["spotify:episode:old"])
    client.opaque_after_write = {0}
    _apply(store, client, "spotify:episode:new")
    client.snapshot_responses = [SpotifyTransportError("safe network error")]

    with pytest.raises(SpotifyTransportError, match="safe network error"):
        _apply(
            store,
            client,
            "spotify:episode:new",
            generated_at=NOW + timedelta(minutes=10),
        )

    assert len(client.writes) == 1


def test_post_write_visible_mismatch_still_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    client = _AttestedSpotify(["spotify:episode:old"])
    client.opaque_after_write = {1}
    client.visible_after_write = {0: "spotify:episode:wrong"}

    with pytest.raises(SpotifyReconciliationError, match="did not match desired"):
        _apply(
            store,
            client,
            "spotify:episode:one",
            "spotify:episode:two",
        )

    assert store.get_playlist_attestation(PLAYLIST_ID) is None


def test_exercised_sensitive_inputs_do_not_reach_attestation_or_operator_surfaces(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _store(tmp_path)
    client = _AttestedSpotify(["spotify:episode:old"])
    access_token = "ACCESS_TOKEN_SENTINEL"
    client.access_token = access_token
    client.opaque_after_write = {0}
    provider_material = "RAW_PROVIDER_BODY_SENTINEL"
    desired_uri = f"spotify:episode:{provider_material}"

    result = _apply(store, client, desired_uri)
    attestation = store.get_playlist_attestation(PLAYLIST_ID)
    captured = capsys.readouterr()

    assert result.warning is not None
    assert attestation is not None
    operator_surfaces = " ".join(
        (
            captured.out,
            captured.err,
            str(result),
            result.warning,
            attestation.destination_id,
            attestation.snapshot_id,
            attestation.desired_fingerprint,
        )
    )
    persisted = store.path.read_bytes()
    assert client.access_token == access_token
    assert client.writes == [[desired_uri]]
    for sentinel in (access_token, provider_material):
        assert sentinel not in operator_surfaces
        assert sentinel.encode() not in persisted
