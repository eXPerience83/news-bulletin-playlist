from __future__ import annotations

import pytest

from news_bulletin_playlist.reconciliation import (
    SpotifyReconciliationError,
    reconcile_playlist_items,
)
from news_bulletin_playlist.spotify.client import SpotifyApiError, SpotifyTransportError

SECRET = "provider-body-sentinel-never-persist"


class _PrewriteApiFailure:
    def playlist_items(self, playlist_id: str, *, limit: int, offset: int):  # type: ignore[no-untyped-def]
        del playlist_id, limit, offset
        raise SpotifyApiError(503, SECRET)

    def replace_playlist_items(self, playlist_id: str, uris: list[str]):  # type: ignore[no-untyped-def]
        raise AssertionError((playlist_id, uris))

    def playlist_snapshot(self, playlist_id: str):  # type: ignore[no-untyped-def]
        raise AssertionError(playlist_id)


class _WriteTransportFailure:
    def playlist_items(self, playlist_id: str, *, limit: int, offset: int):  # type: ignore[no-untyped-def]
        del playlist_id, limit, offset
        return {"items": [], "next": None, "total": 0}

    def replace_playlist_items(self, playlist_id: str, uris: list[str]):  # type: ignore[no-untyped-def]
        del playlist_id, uris
        raise SpotifyTransportError(SECRET)

    def playlist_snapshot(self, playlist_id: str):  # type: ignore[no-untyped-def]
        raise AssertionError(playlist_id)


class _AppliedThen502:
    def __init__(self) -> None:
        self.items: list[str] = []
        self.writes = 0

    def playlist_items(self, playlist_id: str, *, limit: int, offset: int):  # type: ignore[no-untyped-def]
        del playlist_id, limit
        page = self.items[offset:]
        return {
            "items": [{"item": {"uri": uri}} for uri in page],
            "next": None,
            "total": len(self.items),
        }

    def replace_playlist_items(self, playlist_id: str, uris: list[str]):  # type: ignore[no-untyped-def]
        del playlist_id
        self.writes += 1
        self.items = list(uris)
        raise SpotifyApiError(502, SECRET)

    def playlist_snapshot(self, playlist_id: str):  # type: ignore[no-untyped-def]
        del playlist_id
        return {"snapshot_id": "applied-snapshot"}


class _ReadbackApiFailure:
    def __init__(self) -> None:
        self.reads = 0

    def playlist_items(self, playlist_id: str, *, limit: int, offset: int):  # type: ignore[no-untyped-def]
        del playlist_id, limit, offset
        self.reads += 1
        if self.reads == 1:
            return {"items": [], "next": None, "total": 0}
        raise SpotifyApiError(502, SECRET)

    def replace_playlist_items(self, playlist_id: str, uris: list[str]):  # type: ignore[no-untyped-def]
        del playlist_id, uris
        return {"snapshot_id": "snapshot-after-write"}

    def playlist_snapshot(self, playlist_id: str):  # type: ignore[no-untyped-def]
        raise AssertionError(playlist_id)


def test_prewrite_api_failure_has_safe_phase_and_status() -> None:
    with pytest.raises(SpotifyReconciliationError) as raised:
        reconcile_playlist_items(
            _PrewriteApiFailure(),  # type: ignore[arg-type]
            "playlist",
            ["spotify:episode:one"],
        )

    message = str(raised.value)
    assert message == "Spotify playlist prewrite playlist_items API failure (http_status=503)"
    assert SECRET not in message


def test_write_transport_failure_has_safe_phase() -> None:
    with pytest.raises(SpotifyReconciliationError) as raised:
        reconcile_playlist_items(
            _WriteTransportFailure(),  # type: ignore[arg-type]
            "playlist",
            ["spotify:episode:one"],
        )

    message = str(raised.value)
    assert message == (
        "Spotify playlist write replace_items transport failure (write_outcome=unknown)"
    )
    assert SECRET not in message


def test_5xx_after_applied_mutation_is_verified_by_readback_without_retry() -> None:
    client = _AppliedThen502()

    wrote = reconcile_playlist_items(
        client,  # type: ignore[arg-type]
        "playlist",
        ["spotify:episode:one"],
    )

    assert wrote is True
    assert client.writes == 1


def test_readback_api_failure_has_safe_phase_and_status() -> None:
    with pytest.raises(SpotifyReconciliationError) as raised:
        reconcile_playlist_items(
            _ReadbackApiFailure(),  # type: ignore[arg-type]
            "playlist",
            ["spotify:episode:one"],
        )

    message = str(raised.value)
    assert message == "Spotify playlist readback playlist_items API failure (http_status=502)"
    assert SECRET not in message
