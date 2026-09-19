"""Pure desired-state construction from persisted Spotify-first episode state."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from news_bulletin_playlist.desired_state import (
    DesiredPlaylistItem,
    DesiredPlaylistState,
    duration_eligibility_decision,
)
from news_bulletin_playlist.models import PlaylistDefinition, SourceDefinition, SourceId
from news_bulletin_playlist.spotify.first_models import (
    SpotifyFirstEpisode,
    as_utc,
    episode_order_key,
    release_period,
)
from news_bulletin_playlist.spotify.public_detector import spotify_show_id


@dataclass(frozen=True, slots=True)
class SpotifyFirstDesiredBuild:
    desired: DesiredPlaylistState
    duration_unavailable_count: int


def build_spotify_first_desired_state(
    playlist: PlaylistDefinition,
    episodes: Sequence[SpotifyFirstEpisode],
    *,
    sources: Mapping[SourceId, SourceDefinition],
    now: datetime,
) -> SpotifyFirstDesiredBuild:
    """Build one complete deterministic playlist from Spotify-first persisted state."""
    observed = as_utc(now)
    cutoff = observed - timedelta(hours=playlist.retention_hours)
    selected = set(playlist.source_selection.explicit)
    source_timezones = {source_id: source.timezone for source_id, source in sources.items()}
    candidates: list[tuple[DesiredPlaylistItem, SpotifyFirstEpisode]] = []
    decisions = []
    duration_unavailable = 0

    for episode in episodes:
        source = sources.get(episode.source_id)
        if source is None or episode.source_id not in selected:
            continue
        if episode.spotify_show_id != spotify_show_id(source):
            continue
        if not _retained(episode, cutoff=cutoff, now=observed):
            continue
        if episode.duration_seconds is None:
            duration_unavailable += 1
            continue

        item = DesiredPlaylistItem(
            source_id=episode.source_id,
            source_native_id=episode.spotify_episode_id,
            published_at=episode.first_seen_at,
            spotify_episode_uri=episode.spotify_episode_uri,
            edition_at=episode.edition_at,
        )
        decision = duration_eligibility_decision(
            playlist,
            item,
            episode.duration_seconds,
            source_timezones=source_timezones,
        )
        decisions.append(decision)
        if decision.accepted:
            candidates.append((item, episode))

    candidates.sort(key=lambda pair: (str(pair[0].source_id), pair[0].source_native_id))
    candidates.sort(key=lambda pair: episode_order_key(pair[1]), reverse=True)

    unique_items: list[DesiredPlaylistItem] = []
    seen_uris: set[str] = set()
    for item, _episode in candidates:
        if item.spotify_episode_uri in seen_uris:
            continue
        seen_uris.add(item.spotify_episode_uri)
        unique_items.append(item)

    desired = DesiredPlaylistState(
        playlist_id=playlist.id,
        generated_at=observed,
        items=tuple(unique_items[: min(playlist.max_episodes, 100)]),
        duration_decisions=tuple(decisions),
    )
    return SpotifyFirstDesiredBuild(
        desired=desired,
        duration_unavailable_count=duration_unavailable,
    )


def desired_fingerprint(uris: Sequence[str]) -> str:
    encoded = json.dumps(
        list(uris),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _retained(
    episode: SpotifyFirstEpisode,
    *,
    cutoff: datetime,
    now: datetime,
) -> bool:
    if episode.edition_at is not None:
        return cutoff < episode.edition_at <= now
    release_start, release_end = release_period(
        episode.release_date_raw,
        episode.release_date_precision,
    )
    return release_end >= cutoff.date() and release_start <= now.date()
