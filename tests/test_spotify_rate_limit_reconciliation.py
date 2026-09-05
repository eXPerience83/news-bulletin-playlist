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
from news_bulletin_playlist.spotify.client import SpotifyApiError
from news_bulletin_playlist.spotify_backoff import (
    SpotifyRateLimitGuardClient,
    SpotifyRateLimitJournal,
)

NOW = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
URI = "spotify:episode:one"


class _PhaseSpotify:
    def __init__(self, fail_phase: str) -> None:
        self.fail_phase = fail_phase
        self.playlist_reads = 0
        self.replacements = 0
        self.snapshots = 0
        self.items: list[str] = []

    def show_episodes(
        self,
        show_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        raise AssertionError((show_id, limit, offset))

    def playlist_items(
        self,
        playlist_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        assert playlist_id == "destination"
        self.playlist_reads += 1
        if self.fail_phase == "readback" and self.playlist_reads == 2:
            raise SpotifyApiError(429, "rate limited", retry_after=120)
        selected = self.items[offset : offset + limit]
        return {
            "items": [{"item": {"uri": uri}} for uri in selected],
            "next": None,
            "total": len(self.items),
        }

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]:
        assert playlist_id == "destination"
        self.replacements += 1
        if self.fail_phase == "write":
            raise SpotifyApiError(429, "rate limited", retry_after=120)
        self.items = list(uris)
        return {"snapshot_id": "snapshot-write"}

    def playlist_snapshot(self, playlist_id: str) -> dict[str, Any]:
        assert playlist_id == "destination"
        self.snapshots += 1
        return {"snapshot_id": "snapshot-write"}


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "engine.sqlite3")
    store.initialize()
    return store


def _playlist() -> PlaylistDefinition:
    return PlaylistDefinition(
        id=PlaylistId("one"),
        display_name="One",
        description="test",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es-ES"),),
        enabled=True,
        source_selection=SourceSelection((SourceId("one"),)),
        destination=DestinationReference(AdapterId("spotify"), "destination"),
    )


def _desired() -> DesiredPlaylistState:
    return DesiredPlaylistState(
        playlist_id=PlaylistId("one"),
        generated_at=NOW,
        items=(
            DesiredPlaylistItem(
                source_id=SourceId("one"),
                source_native_id="native-one",
                published_at=NOW,
                spotify_episode_uri=URI,
            ),
        ),
    )


def test_write_429_persists_backoff_and_attempts_no_readback(tmp_path: Path) -> None:
    store = _store(tmp_path)
    journal = SpotifyRateLimitJournal(store)
    delegate = _PhaseSpotify("write")
    client = SpotifyRateLimitGuardClient(delegate, journal, clock=lambda: NOW)

    with pytest.raises(SpotifyReconciliationError, match="write replace_items API failure"):
        reconcile_spotify_playlist(client, _playlist(), _desired(), store=store)

    assert delegate.playlist_reads == 1
    assert delegate.replacements == 1
    assert delegate.snapshots == 1
    state = journal.active(now=NOW + timedelta(seconds=1))
    assert state is not None
    assert state.retry_not_before == NOW + timedelta(seconds=120)


def test_readback_429_stops_after_already_completed_write(tmp_path: Path) -> None:
    store = _store(tmp_path)
    journal = SpotifyRateLimitJournal(store)
    delegate = _PhaseSpotify("readback")
    client = SpotifyRateLimitGuardClient(delegate, journal, clock=lambda: NOW)

    with pytest.raises(SpotifyReconciliationError, match="readback playlist_items API failure"):
        reconcile_spotify_playlist(client, _playlist(), _desired(), store=store)

    assert delegate.playlist_reads == 2
    assert delegate.replacements == 1
    assert delegate.items == [URI]
    assert delegate.snapshots == 1
    state = journal.active(now=NOW + timedelta(seconds=1))
    assert state is not None
    assert state.retry_not_before == NOW + timedelta(seconds=120)
