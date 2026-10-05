"""One durable seconds-based contract for managed playlist duration ranges."""

from __future__ import annotations

MAX_NEW_MANAGED_DURATION_SECONDS = 24 * 60 * 60


def validate_persisted_duration_seconds(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("maximum duration must be a positive integer number of seconds")
    return value


def validate_new_duration_seconds(value: object) -> int:
    seconds = validate_persisted_duration_seconds(value)
    if seconds > MAX_NEW_MANAGED_DURATION_SECONDS:
        raise ValueError(
            f"maximum duration must be at most {MAX_NEW_MANAGED_DURATION_SECONDS} seconds"
        )
    return seconds


def validate_duration_update(value: object, *, current_seconds: int) -> int:
    seconds = validate_persisted_duration_seconds(value)
    # Historical explicit values are loadable and can round-trip unchanged even if they
    # predate the interactive ceiling for newly selected values.
    return seconds if seconds == current_seconds else validate_new_duration_seconds(seconds)


def validate_persisted_min_duration_seconds(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("minimum duration must be a non-negative integer number of seconds")
    return value


def validate_duration_range(min_seconds: object, max_seconds: object) -> None:
    minimum = validate_persisted_min_duration_seconds(min_seconds)
    maximum = validate_persisted_duration_seconds(max_seconds)
    if minimum > maximum:
        raise ValueError("minimum duration must not exceed maximum duration")


def validate_min_duration_update(value: object, *, current_seconds: int | None) -> int:
    seconds = validate_persisted_min_duration_seconds(value)
    if seconds != current_seconds and seconds > MAX_NEW_MANAGED_DURATION_SECONDS:
        raise ValueError(
            f"minimum duration must be at most {MAX_NEW_MANAGED_DURATION_SECONDS} seconds"
        )
    return seconds


def duration_seconds_from_minutes(minutes: int, *, current_seconds: int) -> int:
    """Preserve exact legacy seconds only when the visible whole minutes are unchanged."""
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 0:
        raise ValueError("duration minutes must be a non-negative integer")
    return current_seconds if minutes == current_seconds // 60 else minutes * 60
