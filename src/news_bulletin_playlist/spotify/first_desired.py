"""Pure desired-state construction and legacy comparison for Spotify-first shadow mode."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from news_bulletin_playlist.desired_state import (
    DesiredPlaylistItem,
    DesiredPlaylistState,
    DurationEligibilityDecision,
    build_playlist_desired_state,
    duration_eligibility_decision,
)
from news_bulletin_playlist.models import PlaylistDefinition, PlaylistId, SourceDefinition, SourceId
from news_bulletin_playlist.persistence import EditionMatch, SQLiteStore
from news_bulletin_playlist.spotify.first_models import (
    SpotifyFirstEpisode,
    as_utc,
    episode_order_key,
    release_period,
)
from news_bulletin_playlist.spotify.first_persistence import SpotifyFirstJournal
from news_bulletin_playlist.spotify.public_detector import spotify_show_id
from news_bulletin_playlist.spotify_planner import stable_fingerprint


@dataclass(frozen=True, slots=True)
class ShadowDesiredState:
    playlist_id: PlaylistId
    items: tuple[DesiredPlaylistItem, ...]
    duration_decisions: tuple[DurationEligibilityDecision, ...]
    duration_unavailable_count: int

    @property
    def uris(self) -> tuple[str, ...]:
        return tuple(item.spotify_episode_uri for item in self.items)


@dataclass(frozen=True, slots=True)
class DesiredComparison:
    legacy_count: int
    shadow_count: int
    missing_count: int
    extra_count: int
    order_difference_count: int
    duration_unavailable_count: int
    legacy_fingerprint: str
    shadow_fingerprint: str
    equivalent: bool
    valid: bool
    differing_source_ids: tuple[SourceId, ...]


def build_shadow_desired_state(
    playlist: PlaylistDefinition,
    episodes: Sequence[SpotifyFirstEpisode],
    *,
    sources: Mapping[SourceId, SourceDefinition],
    now: datetime,
) -> ShadowDesiredState:
    """Build a complete playlist locally from persisted Spotify-first source state."""
    observed = as_utc(now)
    cutoff = observed - timedelta(hours=playlist.retention_hours)
    selected = set(playlist.source_selection.explicit)
    timezones = {source_id: source.timezone for source_id, source in sources.items()}
    candidates: list[tuple[DesiredPlaylistItem, SpotifyFirstEpisode]] = []
    decisions: list[DurationEligibilityDecision] = []
    unavailable = 0

    for episode in episodes:
        source = sources.get(episode.source_id)
        if (
            episode.source_id not in selected
            or source is None
            or episode.spotify_show_id != spotify_show_id(source)
            or not _retained(episode, cutoff=cutoff, now=observed)
        ):
            continue
        if episode.duration_seconds is None:
            unavailable += 1
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
            source_timezones=timezones,
        )
        decisions.append(decision)
        if decision.accepted:
            candidates.append((item, episode))

    candidates.sort(key=lambda pair: (str(pair[0].source_id), pair[0].source_native_id))
    candidates.sort(key=lambda pair: episode_order_key(pair[1]), reverse=True)

    unique: list[DesiredPlaylistItem] = []
    seen_uris: set[str] = set()
    for item, _episode in candidates:
        if item.spotify_episode_uri in seen_uris:
            continue
        seen_uris.add(item.spotify_episode_uri)
        unique.append(item)

    return ShadowDesiredState(
        playlist_id=playlist.id,
        items=tuple(unique[: min(playlist.max_episodes, 100)]),
        duration_decisions=tuple(decisions),
        duration_unavailable_count=unavailable,
    )


def build_legacy_desired_state(
    store: SQLiteStore,
    playlist: PlaylistDefinition,
    *,
    now: datetime,
    timezones: Mapping[SourceId, ZoneInfo],
) -> DesiredPlaylistState:
    """Rebuild current authoritative desired state without any remote Spotify I/O."""
    editions = store.list_editions()
    matches: dict[tuple[SourceId, str], EditionMatch] = {}
    for edition in editions:
        match = store.get_match_state(edition.source_id, edition.source_native_id)
        if match is not None:
            matches[edition.identity] = match
    return build_playlist_desired_state(
        playlist,
        editions,
        matches,
        now=now,
        source_timezones=timezones,
    )


def comparison_is_valid(
    journal: SpotifyFirstJournal,
    playlist: PlaylistDefinition,
    sources: Mapping[SourceId, SourceDefinition],
    *,
    now: datetime,
) -> bool:
    """Require proven Spotify-first coverage for every selected source before cutover evidence."""
    cutoff = as_utc(now) - timedelta(hours=playlist.retention_hours)
    for source_id in playlist.source_selection.explicit:
        source = sources.get(source_id)
        state = journal.get_source_state(source_id)
        if source is None or state is None or state.spotify_show_id != spotify_show_id(source):
            return False
        if not state.coverage_proven or state.coverage_cutoff_at > cutoff:
            return False
    return True


def compare_desired_states(
    legacy: DesiredPlaylistState,
    shadow: ShadowDesiredState,
    *,
    valid: bool,
) -> DesiredComparison:
    legacy_set = set(legacy.uris)
    shadow_set = set(shadow.uris)
    missing = legacy_set - shadow_set
    extra = shadow_set - legacy_set
    common = legacy_set & shadow_set
    legacy_position = {uri: index for index, uri in enumerate(legacy.uris)}
    shadow_position = {uri: index for index, uri in enumerate(shadow.uris)}
    legacy_source = {item.spotify_episode_uri: item.source_id for item in legacy.items}
    shadow_source = {item.spotify_episode_uri: item.source_id for item in shadow.items}
    differing_sources = sorted(
        {legacy_source[uri] for uri in missing} | {shadow_source[uri] for uri in extra},
        key=str,
    )
    return DesiredComparison(
        legacy_count=len(legacy.uris),
        shadow_count=len(shadow.uris),
        missing_count=len(missing),
        extra_count=len(extra),
        order_difference_count=sum(
            legacy_position[uri] != shadow_position[uri] for uri in common
        ),
        duration_unavailable_count=shadow.duration_unavailable_count,
        legacy_fingerprint=stable_fingerprint(legacy.uris),
        shadow_fingerprint=stable_fingerprint(shadow.uris),
        equivalent=legacy.uris == shadow.uris,
        valid=valid,
        differing_source_ids=tuple(differing_sources),
    )


def _retained(episode: SpotifyFirstEpisode, *, cutoff: datetime, now: datetime) -> bool:
    if episode.edition_at is not None:
        return cutoff < episode.edition_at <= now
    release_start, release_end = release_period(
        episode.release_date_raw,
        episode.release_date_precision,
    )
    return release_end >= cutoff.date() and release_start <= now.date()
