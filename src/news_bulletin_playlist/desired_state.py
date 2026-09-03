from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from news_bulletin_playlist.models import (
    CanonicalEdition,
    DurationPolicyException,
    OrderingPolicy,
    PlaylistDefinition,
    PlaylistId,
    SourceId,
)
from news_bulletin_playlist.persistence import EditionMatch, MatchStatus

SPOTIFY_PLAYLIST_ITEM_LIMIT = 100
DURATION_WITHIN_DEFAULT_MAX = "duration_within_default_max"
DURATION_EXCEPTION = "duration_exception"
DURATION_EXCEEDS_DEFAULT_MAX = "duration_exceeds_default_max"
DURATION_EXCEEDS_EXCEPTION_MAX = "duration_exceeds_exception_max"


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


@dataclass(frozen=True, slots=True)
class DurationEligibilityDecision:
    source_id: SourceId
    source_native_id: str
    duration_seconds: int
    accepted: bool
    reason: str
    max_seconds: int
    exception_id: str | None = None


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
    duration_decisions: tuple[DurationEligibilityDecision, ...] = ()

    @property
    def uris(self) -> tuple[str, ...]:
        return tuple(item.spotify_episode_uri for item in self.items)


def build_playlist_desired_state(
    playlist: PlaylistDefinition,
    editions: Sequence[CanonicalEdition],
    matches: Mapping[tuple[SourceId, str], EditionMatch],
    *,
    now: datetime,
    source_timezones: Mapping[SourceId, ZoneInfo] | None = None,
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
    decisions: list[DurationEligibilityDecision] = []
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
        duration = match.spotify_duration_seconds
        if duration is None:
            raise DesiredStateError(
                "desired state Spotify duration unavailable for matched edition "
                f"{edition.source_id!s}/{edition.source_native_id}"
            )
        decision = _duration_decision(
            playlist,
            edition,
            duration,
            source_timezones=source_timezones or {},
        )
        decisions.append(decision)
        if not decision.accepted:
            continue
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
        duration_decisions=tuple(decisions),
    )


def build_multi_playlist_desired_states(
    playlists: Sequence[PlaylistDefinition],
    editions: Sequence[CanonicalEdition],
    matches: Mapping[tuple[SourceId, str], EditionMatch],
    *,
    now: datetime,
    source_timezones: Mapping[SourceId, ZoneInfo] | None = None,
) -> tuple[DesiredPlaylistState, ...]:
    """Build every enabled playlist independently from the same canonical input set."""
    return tuple(
        build_playlist_desired_state(
            playlist,
            editions,
            matches,
            now=now,
            source_timezones=source_timezones,
        )
        for playlist in playlists
        if playlist.enabled
    )


def _duration_decision(
    playlist: PlaylistDefinition,
    edition: CanonicalEdition,
    duration_seconds: int,
    *,
    source_timezones: Mapping[SourceId, ZoneInfo],
) -> DurationEligibilityDecision:
    policy = playlist.duration_policy
    if duration_seconds <= policy.default_max_seconds:
        return DurationEligibilityDecision(
            source_id=edition.source_id,
            source_native_id=edition.source_native_id,
            duration_seconds=duration_seconds,
            accepted=True,
            reason=DURATION_WITHIN_DEFAULT_MAX,
            max_seconds=policy.default_max_seconds,
        )

    exceptions = tuple(
        exception for exception in policy.exceptions if exception.source_id == edition.source_id
    )
    matching_exception = _matching_duration_exception(
        edition,
        exceptions,
        source_timezones=source_timezones,
    )
    if matching_exception is None:
        return DurationEligibilityDecision(
            source_id=edition.source_id,
            source_native_id=edition.source_native_id,
            duration_seconds=duration_seconds,
            accepted=False,
            reason=DURATION_EXCEEDS_DEFAULT_MAX,
            max_seconds=policy.default_max_seconds,
        )

    accepted = duration_seconds <= matching_exception.max_seconds
    return DurationEligibilityDecision(
        source_id=edition.source_id,
        source_native_id=edition.source_native_id,
        duration_seconds=duration_seconds,
        accepted=accepted,
        reason=(DURATION_EXCEPTION if accepted else DURATION_EXCEEDS_EXCEPTION_MAX),
        max_seconds=matching_exception.max_seconds,
        exception_id=matching_exception.id,
    )


def _matching_duration_exception(
    edition: CanonicalEdition,
    exceptions: Sequence[DurationPolicyException],
    *,
    source_timezones: Mapping[SourceId, ZoneInfo],
) -> DurationPolicyException | None:
    if not exceptions or edition.edition_at is None:
        return None
    timezone = source_timezones.get(edition.source_id)
    if timezone is None:
        raise DesiredStateError(
            "desired state source timezone unavailable for duration exception evaluation"
        )
    local = edition.edition_at.astimezone(timezone)
    for exception in exceptions:
        target = exception.edition_local_time
        if (local.hour, local.minute) == (target.hour, target.minute):
            return exception
    return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC)
