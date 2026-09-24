from __future__ import annotations

import pytest

from news_bulletin_playlist.duration_profile import (
    CANONICAL_DURATION_BUCKETS,
    DurationBucket,
    duration_bucket,
    duration_profile,
    profile_as_dict,
)


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, DurationBucket.UNDER_10M),
        (1, DurationBucket.UNDER_10M),
        (599, DurationBucket.UNDER_10M),
        (600, DurationBucket.M10_TO_15M),
        (899, DurationBucket.M10_TO_15M),
        (900, DurationBucket.M15_TO_30M),
        (1799, DurationBucket.M15_TO_30M),
        (1800, DurationBucket.M30_TO_60M),
        (3599, DurationBucket.M30_TO_60M),
        (3600, DurationBucket.M60_PLUS),
        (7200, DurationBucket.M60_PLUS),
    ],
)
def test_exact_half_open_duration_boundaries(seconds: int, expected: DurationBucket) -> None:
    assert duration_bucket(seconds) is expected
    assert duration_bucket(seconds) in CANONICAL_DURATION_BUCKETS


def test_profile_excludes_missing_duration_and_uses_each_bucket_once() -> None:
    samples = [0, 599, 600, 899, 900, 1799, 1800, 3599, 3600, 7200, None]
    profile = duration_profile(samples)
    assert profile == (2, 2, 2, 2, 2)
    assert sum(profile) == len(samples) - 1
    assert profile_as_dict(profile) == {
        "under_10m": 2,
        "10_to_15m": 2,
        "15_to_30m": 2,
        "30_to_60m": 2,
        "60m_plus": 2,
    }
    assert duration_profile([None]) == (0, 0, 0, 0, 0)


def test_every_second_in_bounded_range_has_exactly_one_bucket() -> None:
    samples = range(7201)
    assert all(duration_bucket(seconds) in CANONICAL_DURATION_BUCKETS for seconds in samples)
    assert sum(duration_profile(samples)) == len(samples)


@pytest.mark.parametrize("value", [-1, -600])
def test_negative_duration_is_not_bucketized(value: int) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        duration_bucket(value)


@pytest.mark.parametrize("value", [None, True, 0.0])
def test_non_integer_duration_is_not_bucketized(value: object) -> None:
    with pytest.raises(TypeError, match="integer"):
        duration_bucket(value)  # type: ignore[arg-type]
