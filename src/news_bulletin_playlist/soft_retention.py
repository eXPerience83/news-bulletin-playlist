"""Pure per-playlist classification for quota-friendly soft retention."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from news_bulletin_playlist.desired_state import (
    DesiredPlaylistState,
    authoritative_playlist_time,
)
from news_bulletin_playlist.models import PlaylistDefinition

SOFT_RETENTION_GRACE = timedelta(hours=8)


class SoftRetentionDisposition(StrEnum):
    NOT_EXPIRY_ONLY = "not_expiry_only"
    DEFER = "defer"
    FORCE_HARD_EXPIRY = "force_hard_expiry"


@dataclass(frozen=True, slots=True)
class SoftRetentionDecision:
    disposition: SoftRetentionDisposition
    soft_expired_count: int = 0
    oldest_soft_expired_age_seconds: int | None = None

    @property
    def expiry_only(self) -> bool:
        return self.disposition is not SoftRetentionDisposition.NOT_EXPIRY_ONLY

    @property
    def should_defer(self) -> bool:
        return self.disposition is SoftRetentionDisposition.DEFER

    @property
    def force_cleanup(self) -> bool:
        return self.disposition is SoftRetentionDisposition.FORCE_HARD_EXPIRY


def classify_soft_retention(
    playlist: PlaylistDefinition,
    physical_baseline: DesiredPlaylistState,
    logical_desired: DesiredPlaylistState,
    *,
    now: datetime,
) -> SoftRetentionDecision:
    """Classify a desired transition that may consist only of age-based removals.

    ``physical_baseline`` is the last strongly verified ordered state of this exact playlist.
    ``logical_desired`` is the current strict state built with ``retention_hours``. The caller is
    responsible for proving that the baseline itself is durable and still trustworthy.

    A transition is expiry-only only when deleting every baseline item at or beyond the soft
    retention boundary reproduces the current logical desired state exactly. Additions, remaps,
    reorders, or clock rollback therefore fail closed into the normal reconciliation path.
    """
    observed_at = _as_utc(now)
    if physical_baseline.playlist_id != playlist.id or logical_desired.playlist_id != playlist.id:
        return SoftRetentionDecision(SoftRetentionDisposition.NOT_EXPIRY_ONLY)
    if observed_at < physical_baseline.generated_at:
        return SoftRetentionDecision(SoftRetentionDisposition.NOT_EXPIRY_ONLY)
    if physical_baseline.items == logical_desired.items:
        return SoftRetentionDecision(SoftRetentionDisposition.NOT_EXPIRY_ONLY)

    soft_cutoff = observed_at - timedelta(hours=playlist.retention_hours)
    retained = []
    expired = []
    for item in physical_baseline.items:
        item_time = authoritative_playlist_time(item, playlist.ordering)
        if item_time > observed_at:
            return SoftRetentionDecision(SoftRetentionDisposition.NOT_EXPIRY_ONLY)
        if item_time <= soft_cutoff:
            expired.append((item, item_time))
        else:
            retained.append(item)

    if not expired or tuple(retained) != logical_desired.items:
        return SoftRetentionDecision(SoftRetentionDisposition.NOT_EXPIRY_ONLY)

    oldest_time = min(item_time for _, item_time in expired)
    oldest_age = observed_at - oldest_time
    disposition = (
        SoftRetentionDisposition.FORCE_HARD_EXPIRY
        if oldest_age >= timedelta(hours=playlist.retention_hours) + SOFT_RETENTION_GRACE
        else SoftRetentionDisposition.DEFER
    )
    return SoftRetentionDecision(
        disposition,
        soft_expired_count=len(expired),
        oldest_soft_expired_age_seconds=max(round(oldest_age.total_seconds()), 0),
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("soft-retention timestamps must be timezone-aware")
    return value.astimezone(UTC)
