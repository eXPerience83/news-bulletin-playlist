from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from news_bulletin_playlist.desired_state import (
    DesiredPlaylistState,
    authoritative_playlist_time,
    build_playlist_desired_state,
)
from news_bulletin_playlist.models import OrderingPolicy, PlaylistDefinition, PlaylistId, SourceId
from news_bulletin_playlist.persistence import EditionMatch, SQLiteStore
from news_bulletin_playlist.spotify.client import SpotifyApiError, SpotifyTransportError

_SPOTIFY_PLAYLIST_PAGE_SIZE = 50
_MANAGED_PLAYLIST_MAX_ITEMS = 100


class SpotifyPlaylistClient(Protocol):
    """Small Spotify surface needed by playlist reconciliation."""

    def playlist_items(
        self,
        playlist_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]: ...

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]: ...


class SpotifyReconciliationError(RuntimeError):
    """Raised when Spotify playlist state cannot be read or verified safely."""


@dataclass(frozen=True, slots=True)
class PlaylistReconciliationResult:
    playlist_id: PlaylistId
    ok: bool
    desired_count: int
    applied_count: int | None
    wrote: bool | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _SpotifyPlaylistRead:
    uris: tuple[str, ...]
    had_unavailable_item: bool


def build_desired_state_from_store(
    store: SQLiteStore,
    playlist: PlaylistDefinition,
    *,
    now: datetime,
) -> DesiredPlaylistState:
    """Load only still-relevant durable state, then delegate to the pure builder."""
    observed_at = _as_utc(now)
    cutoff = observed_at - timedelta(hours=playlist.retention_hours)
    editions = []
    matches: dict[tuple[SourceId, str], EditionMatch] = {}

    for source_id in playlist.source_selection.explicit:
        for edition in store.list_editions(source_id=source_id):
            ordering_at = authoritative_playlist_time(edition, playlist.ordering)
            if playlist.ordering is OrderingPolicy.PUBLISHED_AT_DESC and ordering_at < cutoff:
                # list_editions(source_id=...) is publication-time descending, so only
                # the explicit legacy publication-order policy can safely stop here.
                break
            if ordering_at < cutoff or ordering_at > observed_at:
                continue
            editions.append(edition)
            match = store.get_match_state(edition.source_id, edition.source_native_id)
            if match is not None:
                matches[edition.identity] = match

    return build_playlist_desired_state(playlist, editions, matches, now=observed_at)


def read_spotify_playlist_uris(
    client: SpotifyPlaylistClient,
    playlist_id: str,
) -> tuple[str, ...]:
    """Read the complete bounded playlist and fail closed on unavailable media."""
    return _read_spotify_playlist(client, playlist_id, allow_unavailable=False).uris


def reconcile_playlist_items(
    client: SpotifyPlaylistClient,
    playlist_id: str,
    desired_uris: Sequence[str],
) -> bool:
    """Replace only on change and verify exact order/count/content after every write."""
    desired = tuple(desired_uris)
    if len(desired) > _MANAGED_PLAYLIST_MAX_ITEMS:
        raise ValueError("playlist reconciliation is limited to 100 items")

    current = _read_spotify_playlist(client, playlist_id, allow_unavailable=True)
    if current.uris == desired and not current.had_unavailable_item:
        return False

    client.replace_playlist_items(playlist_id, list(desired))
    readback = read_spotify_playlist_uris(client, playlist_id)
    if readback != desired:
        raise SpotifyReconciliationError(
            "Spotify playlist readback did not match desired order/count/content"
        )
    return True


def reconcile_spotify_playlist(
    client: SpotifyPlaylistClient,
    playlist: PlaylistDefinition,
    desired: DesiredPlaylistState,
) -> PlaylistReconciliationResult:
    """Reconcile one Spotify destination; errors are left to the batch isolator."""
    if not playlist.enabled:
        raise SpotifyReconciliationError(f"playlist {playlist.id!s} is disabled")
    if str(playlist.destination.adapter_id) != "spotify":
        raise SpotifyReconciliationError(
            f"playlist {playlist.id!s} destination is not Spotify"
        )
    if desired.playlist_id != playlist.id:
        raise SpotifyReconciliationError(
            f"desired state {desired.playlist_id!s} does not belong to playlist {playlist.id!s}"
        )

    wrote = reconcile_playlist_items(
        client,
        playlist.destination.external_id,
        desired.uris,
    )
    return PlaylistReconciliationResult(
        playlist_id=playlist.id,
        ok=True,
        desired_count=len(desired.items),
        applied_count=len(desired.items),
        wrote=wrote,
    )


def reconcile_spotify_destinations(
    client: SpotifyPlaylistClient,
    plans: Sequence[tuple[PlaylistDefinition, DesiredPlaylistState]],
) -> tuple[PlaylistReconciliationResult, ...]:
    """Reconcile destinations independently so one Spotify failure cannot block another."""
    results: list[PlaylistReconciliationResult] = []
    for playlist, desired in plans:
        try:
            result = reconcile_spotify_playlist(client, playlist, desired)
        except (SpotifyApiError, SpotifyTransportError, SpotifyReconciliationError) as exc:
            result = PlaylistReconciliationResult(
                playlist_id=playlist.id,
                ok=False,
                desired_count=len(desired.items),
                applied_count=None,
                wrote=None,
                error=str(exc),
            )
        results.append(result)
    return tuple(results)


def _read_spotify_playlist(
    client: SpotifyPlaylistClient,
    playlist_id: str,
    *,
    allow_unavailable: bool,
) -> _SpotifyPlaylistRead:
    uris: list[str] = []
    had_unavailable = False
    offset = 0

    while offset < _MANAGED_PLAYLIST_MAX_ITEMS:
        limit = min(_SPOTIFY_PLAYLIST_PAGE_SIZE, _MANAGED_PLAYLIST_MAX_ITEMS - offset)
        page = client.playlist_items(playlist_id, limit=limit, offset=offset)
        items = _require_items(page, context="Spotify playlist response")
        if not items and page.get("next"):
            raise SpotifyReconciliationError(
                "Spotify playlist pagination advanced without returning an item"
            )
        page_uris, page_unavailable = _extract_playlist_uris(
            items,
            allow_unavailable=allow_unavailable,
        )
        uris.extend(page_uris)
        had_unavailable = had_unavailable or page_unavailable
        offset += len(items)

        if not page.get("next"):
            return _SpotifyPlaylistRead(tuple(uris), had_unavailable)

    overflow = client.playlist_items(playlist_id, limit=1, offset=offset)
    overflow_items = _require_items(overflow, context="Spotify playlist overflow response")
    if not overflow_items:
        raise SpotifyReconciliationError(
            "Spotify playlist pagination reported an item that was not returned"
        )
    overflow_uris, overflow_unavailable = _extract_playlist_uris(
        overflow_items,
        allow_unavailable=allow_unavailable,
    )
    had_unavailable = had_unavailable or overflow_unavailable
    if overflow_uris:
        uris.append(overflow_uris[0])
    return _SpotifyPlaylistRead(tuple(uris), had_unavailable)


def _require_items(container: object, *, context: str) -> list[object]:
    if not isinstance(container, dict):
        raise SpotifyReconciliationError(f"{context} was not an object")
    items = container.get("items")
    if not isinstance(items, list):
        raise SpotifyReconciliationError(f"{context} did not contain an item list")
    return items


def _extract_playlist_uris(
    items: Sequence[object],
    *,
    allow_unavailable: bool,
) -> tuple[list[str], bool]:
    uris: list[str] = []
    had_unavailable = False
    for item in items:
        if not isinstance(item, dict):
            raise SpotifyReconciliationError(
                "Spotify playlist response contained an invalid item"
            )
        has_item = "item" in item
        has_track = "track" in item
        if not has_item and not has_track:
            raise SpotifyReconciliationError(
                "Spotify playlist response contained an item without a media object"
            )
        value = item.get("item") if has_item else item.get("track")
        if value is None and has_item and has_track:
            value = item.get("track")
        if value is None:
            if not allow_unavailable:
                raise SpotifyReconciliationError(
                    "Spotify playlist response contained an unavailable media item"
                )
            had_unavailable = True
            continue
        if not isinstance(value, dict):
            raise SpotifyReconciliationError(
                "Spotify playlist response contained an invalid media object"
            )
        uri = value.get("uri")
        if not isinstance(uri, str) or not uri.strip():
            raise SpotifyReconciliationError(
                "Spotify playlist response contained an item without a URI"
            )
        uris.append(uri)
    return uris, had_unavailable


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC)
