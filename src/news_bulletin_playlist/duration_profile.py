"""Canonical descriptive duration summaries; never playlist eligibility policy."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum


class DurationBucket(StrEnum):
    UNDER_10M = "under_10m"
    M10_TO_15M = "10_to_15m"
    M15_TO_30M = "15_to_30m"
    M30_TO_60M = "30_to_60m"
    M60_PLUS = "60m_plus"


CANONICAL_DURATION_BUCKETS = tuple(DurationBucket)
DurationProfile = tuple[int, int, int, int, int]


def duration_bucket(duration_seconds: int) -> DurationBucket:
    """Classify one exact non-negative duration into a half-open summary interval."""
    if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, int):
        raise TypeError("duration_seconds must be an integer")
    if duration_seconds < 0:
        raise ValueError("duration_seconds must be non-negative")
    if duration_seconds < 600:
        return DurationBucket.UNDER_10M
    if duration_seconds < 900:
        return DurationBucket.M10_TO_15M
    if duration_seconds < 1800:
        return DurationBucket.M15_TO_30M
    if duration_seconds < 3600:
        return DurationBucket.M30_TO_60M
    return DurationBucket.M60_PLUS


def duration_profile(durations: Iterable[int | None]) -> DurationProfile:
    """Count known durations; missing values belong to no bucket."""
    counts = dict.fromkeys(CANONICAL_DURATION_BUCKETS, 0)
    for seconds in durations:
        if seconds is not None:
            bucket = duration_bucket(seconds)
            counts[bucket] += 1
    return (
        counts[DurationBucket.UNDER_10M],
        counts[DurationBucket.M10_TO_15M],
        counts[DurationBucket.M15_TO_30M],
        counts[DurationBucket.M30_TO_60M],
        counts[DurationBucket.M60_PLUS],
    )


def profile_as_dict(profile: DurationProfile) -> dict[str, int]:
    """Project the fixed profile to ordered, stable JSON-compatible keys."""
    return dict(zip((bucket.value for bucket in CANONICAL_DURATION_BUCKETS), profile, strict=True))
