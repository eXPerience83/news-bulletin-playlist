from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from news_bulletin_playlist.models import (
    CanonicalEdition,
    OrderingPolicy,
    PlaylistDefinition,
    PlaylistId,
    SourceId,
)
from news_bulletin_playlist.persistence import EditionMatch, MatchStatus

SPOTIFY_PLAYLIST_ITEM_LIMIT = 100


class DesiredStateError(RuntimeError):
    """Raised when deterministic desired-state construction cannot proceed safely."""


@dataclass(frozen=True, slots=True)
class DesiredPlaylistItem:
    source_id: SourceId
    source_native_id: str
    published_at: datetime
    spotify_episode_uri: str
    edition_at: datetime | None = None

    @property
    def identity(self) -> tuple[SourceId, str]:
        return (self.source_id, self.source_native_id)


def authoritative_playlist_time(
    edition: CanonicalEdition | DesiredPlaylistItem,
    ordering: OrderingPolicy,
) -> datetime:
    """Return the semantic timestamp used for retention and playlist chronology."""
    if ordering is OrderingPolicy.EDITION_AT_DESC:
        return edition.edition_at or edition.published_at
    if ordering is OrderingPolicy.PUBLISHED_AT_DESC:
        return edition.published_at
    raise DesiredStateError(f"unsupported ordering {ordering!s}")


@dataclass(frozen=True, slots=True)
class DesiredPlaylistState:
    playlist_id: PlaylistId
    generated_at: datetime
    items: tuple[DesiredPlaylistItem, ...]

    @property
    def uris(self) -> tuple[str, ...]:
        return tuple(item.spotify_episode_uri for item in self.items)


def build_playlist_desired_state(
    playlist: PlaylistDefinition,
    editions: Sequence[CanonicalEdition],
    matches: Mapping[tuple[SourceId, str], EditionMatch],
    *,
    now: datetime,
) -> DesiredPlaylistState:
    """Build one deterministic playlist state from canonical editions and match state.

    This function is intentionally pure: callers provide durable canonical/match data and
    it performs no persistence or Spotify I/O. Reading from durable state, rather than only
    the current fetch batch, naturally carries still-valid last-known-good editions across
    transient source failures.
    """
    generated_at = _as_utc(now)
    if not playlist.enabled:
        raise DesiredStateError(f"playlist {playlist.id!s} is disabled")
    if playlist.ordering not in {
        OrderingPolicy.EDITION_AT_DESC,
        OrderingPolicy.PUBLISHED_AT_DESC,
    }:
        raise DesiredStateError(
            f"playlist {playlist.id!s} uses unsupported ordering {playlist.ordering!s}"
        )

    selected_sources = set(playlist.source_selection.explicit)
    cutoff = generated_at - timedelta(hours=playlist.retention_hours)
    canonical_by_identity: dict[tuple[SourceId, str], CanonicalEdition] = {}

    for edition in editions:
        if edition.source_id not in selected_sources:
            continue
        ordering_at = authoritative_playlist_time(edition, playlist.ordering)
        if ordering_at < cutoff or ordering_at > generated_at:
            continue
        existing = canonical_by_identity.get(edition.identity)
        if existing is not None and existing != edition:
            raise DesiredStateError(
                "conflicting canonical editions share identity "
                f"{edition.source_id!s}/{edition.source_native_id}"
            )
        canonical_by_identity[edition.identity] = edition

    items: list[DesiredPlaylistItem] = []
    for identity, edition in canonical_by_identity.items():
        match = matches.get(identity)
        if match is None or match.status is not MatchStatus.MATCHED:
            continue
        uri = match.spotify_episode_uri
        if uri is None or not uri.strip():
            raise DesiredStateError(
                "matched edition is missing its Spotify episode URI for "
                f"{edition.source_id!s}/{edition.source_native_id}"
            )
        items.append(
            DesiredPlaylistItem(
                source_id=edition.source_id,
                source_native_id=edition.source_native_id,
                published_at=edition.published_at,
                edition_at=edition.edition_at,
                spotify_episode_uri=uri,
            )
        )

    # Stable identity ordering provides a deterministic tie-breaker when two providers
    # have the exact same authoritative bulletin timestamp. The second stable sort makes
    # semantic bulletin time authoritative and descending for managed playlists.
    items.sort(key=lambda item: (str(item.source_id), item.source_native_id))
    items.sort(
        key=lambda item: authoritative_playlist_time(item, playlist.ordering),
        reverse=True,
    )

    # Distinct canonical source assets may legitimately converge on the same Spotify
    # episode URI. A destination playlist should contain that Spotify episode once,
    # keeping the newest canonical occurrence under the configured ordering policy.
    unique_items: list[DesiredPlaylistItem] = []
    seen_uris: set[str] = set()
    for item in items:
        if item.spotify_episode_uri in seen_uris:
            continue
        seen_uris.add(item.spotify_episode_uri)
        unique_items.append(item)

    limit = min(playlist.max_episodes, SPOTIFY_PLAYLIST_ITEM_LIMIT)
    return DesiredPlaylistState(
        playlist_id=playlist.id,
        generated_at=generated_at,
        items=tuple(unique_items[:limit]),
    )


def build_multi_playlist_desired_states(
    playlists: Sequence[PlaylistDefinition],
    editions: Sequence[CanonicalEdition],
    matches: Mapping[tuple[SourceId, str], EditionMatch],
    *,
    now: datetime,
) -> tuple[DesiredPlaylistState, ...]:
    """Build every enabled playlist independently from the same canonical input set."""
    return tuple(
        build_playlist_desired_state(playlist, editions, matches, now=now)
        for playlist in playlists
        if playlist.enabled
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC)
