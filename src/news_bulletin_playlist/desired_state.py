from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from news_bulletin_playlist.models import (
    DurationPolicyException,
    PlaylistDefinition,
    PlaylistId,
    SourceId,
)

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
    spotify_episode_uri: str
    edition_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DurationEligibilityDecision:
    source_id: SourceId
    source_native_id: str
    duration_seconds: int
    accepted: bool
    reason: str
    max_seconds: int
    exception_id: str | None = None


@dataclass(frozen=True, slots=True)
class DesiredPlaylistState:
    playlist_id: PlaylistId
    generated_at: datetime
    items: tuple[DesiredPlaylistItem, ...]
    duration_decisions: tuple[DurationEligibilityDecision, ...] = ()

    @property
    def uris(self) -> tuple[str, ...]:
        return tuple(item.spotify_episode_uri for item in self.items)


def desired_fingerprint(uris: Sequence[str]) -> str:
    """Return the canonical SHA-256 fingerprint for an ordered desired URI sequence."""
    encoded = json.dumps(
        list(uris),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def duration_eligibility_decision(
    playlist: PlaylistDefinition,
    edition: DesiredPlaylistItem,
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
    edition: DesiredPlaylistItem,
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
