"""One durable seconds-based contract for managed playlist duration ceilings."""

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
