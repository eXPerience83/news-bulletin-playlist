"""Validate already observed public release evidence without performing requests."""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from news_bulletin_playlist.models import SourceId
from news_bulletin_playlist.spotify.first_models import as_utc, release_period
from news_bulletin_playlist.spotify.public_detector import (
    PublicDetectorState,
    PublicEpisodeObservation,
    PublicReleaseDateShape,
)

PUBLIC_RELEASE_CLOCK_SKEW = timedelta(minutes=5)
_AWARE_ISO = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])\Z"
)


class PublicReleaseOutcome(StrEnum):
    NO_EVIDENCE = "no_evidence"
    ACCEPTED = "accepted"
    IDENTITY_MISMATCH = "identity_mismatch"
    INVALID = "invalid_or_unsupported"
    CATALOGUE_CONFLICT = "catalogue_conflict"
    FUTURE = "rejected_future"
    SHADOWED = "shadowed_by_title"


@dataclass(frozen=True, slots=True)
class PublicReleaseEvidence:
    source_id: SourceId
    spotify_show_id: str
    spotify_episode_id: str
    release_date_raw: str | None
    release_date_shape: PublicReleaseDateShape | None
    observed_at: datetime

    @classmethod
    def from_state(cls, state: PublicDetectorState | None) -> PublicReleaseEvidence | None:
        if (
            state is None
            or state.consecutive_detector_failures
            or state.last_episode_id is None
            or state.last_public_observed_at is None
        ):
            return None
        return cls(
            state.source_id,
            state.spotify_show_id,
            state.last_episode_id,
            state.last_public_release_date_raw,
            state.last_public_release_date_shape,
            state.last_public_observed_at,
        )

    @classmethod
    def from_observation(cls, value: PublicEpisodeObservation) -> PublicReleaseEvidence:
        return cls(
            value.source_id,
            value.spotify_show_id,
            value.episode_id,
            value.release_date_raw,
            value.release_date_shape,
            value.observed_at,
        )


def validate_public_release(
    evidence: PublicReleaseEvidence | None,
    *,
    source_id: SourceId,
    show_id: str,
    episode_id: str,
    release_date: str,
    precision: str,
    timezone: ZoneInfo | None,
    semantic_exact: bool,
) -> tuple[datetime | None, PublicReleaseOutcome]:
    if evidence is None:
        return None, PublicReleaseOutcome.NO_EVIDENCE
    if (evidence.source_id, evidence.spotify_show_id, evidence.spotify_episode_id) != (
        source_id,
        show_id,
        episode_id,
    ):
        return None, PublicReleaseOutcome.IDENTITY_MISMATCH
    if semantic_exact:
        return None, PublicReleaseOutcome.SHADOWED
    # Only the observed exact object shape is enabled. STRING also includes dates
    # and arbitrary metadata in the detector contract, so it is not promoted here.
    raw = evidence.release_date_raw
    if (
        evidence.release_date_shape is not PublicReleaseDateShape.OBJECT_ISO_STRING
        or not isinstance(raw, str)
        or not _AWARE_ISO.fullmatch(raw)
        or timezone is None
    ):
        return None, PublicReleaseOutcome.INVALID
    try:
        instant = as_utc(datetime.fromisoformat(raw))
        observed = as_utc(evidence.observed_at)
        start, end = release_period(release_date, precision)
        local_day = instant.astimezone(timezone).date()
        if not start <= local_day <= end:
            return None, PublicReleaseOutcome.CATALOGUE_CONFLICT
        if instant > observed and instant - observed > PUBLIC_RELEASE_CLOCK_SKEW:
            return None, PublicReleaseOutcome.FUTURE
    except ValueError, OverflowError:
        return None, PublicReleaseOutcome.INVALID
    return instant, PublicReleaseOutcome.ACCEPTED
