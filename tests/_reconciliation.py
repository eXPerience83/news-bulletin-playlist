from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
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
from news_bulletin_playlist.reconciliation import _reconcile_playlist_items
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


def strict_reconcile_playlist_items(
    client: Any,
    playlist_id: str,
    desired_uris: Sequence[str],
) -> bool:
    """Exercise the strict internal reconciliation primitive without durable attestation state."""
    return _reconcile_playlist_items(
        client,
        playlist_id,
        desired_uris,
        store=None,
        logical_playlist_id=None,
        attestation_updated_at=None,
        mutation_guard=None,
    ).wrote
