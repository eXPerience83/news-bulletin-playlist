"""Read-only, bounded SQLite queries used only by production diagnostics.

These helpers deliberately keep diagnostic evidence out of the planner safety path. They
replace per-edition N+1 reads with a fixed number of source-scoped queries while preserving
existing recovery/barrier classification semantics.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from news_bulletin_playlist.models import CanonicalEdition, SourceId
from news_bulletin_playlist.persistence import EditionMatch, MatchStatus, PersistenceError
from news_bulletin_playlist.spotify_planner import (
    AUDIT_INTERVAL,
    MatchedRecoveryReason,
    MatchedRecoveryState,
    PersistenceRejectionBarrierState,
)


@dataclass(frozen=True, slots=True)
class RecoveryDiagnosticRow:
    edition: CanonicalEdition
    recovery: MatchedRecoveryState | None


@dataclass(frozen=True, slots=True)
class RecoveryDiagnosticSnapshot:
    active_persistence_rejection_barrier_count: int
    matched_without_input: tuple[RecoveryDiagnosticRow, ...]


def read_recovery_diagnostic_snapshot(
    path: Path,
    source_id: SourceId,
    *,
    matching_cutoff: datetime,
    observed_at: datetime,
) -> RecoveryDiagnosticSnapshot:
    """Read all evidence needed for #214 recovery diagnostics in two SQL statements."""

    cutoff = _timestamp(matching_cutoff)
    observed = _timestamp(observed_at)
    try:
        connection = sqlite3.connect(path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            barrier_rows = connection.execute(
                """
                SELECT
                    b.source_id AS barrier_source_id,
                    b.source_native_id AS barrier_source_native_id,
                    b.protected_status,
                    b.protected_spotify_episode_uri,
                    b.protected_spotify_duration_seconds,
                    b.protected_match_updated_at,
                    b.first_rejected_at,
                    b.last_rejected_at,
                    m.source_id AS match_source_id,
                    m.source_native_id AS match_source_native_id,
                    m.status AS match_status,
                    m.spotify_episode_uri AS match_spotify_episode_uri,
                    m.spotify_duration_seconds AS match_spotify_duration_seconds,
                    m.updated_at AS match_updated_at
                FROM spotify_planner_persistence_rejection_state AS b
                LEFT JOIN spotify_matches AS m
                  ON m.source_id = b.source_id
                 AND m.source_native_id = b.source_native_id
                WHERE b.source_id = ?
                """,
                (str(source_id),),
            ).fetchall()
            recovery_rows = connection.execute(
                """
                SELECT
                    c.source_id,
                    c.source_native_id,
                    c.title,
                    c.published_at,
                    c.edition_at,
                    c.duration_seconds,
                    m.status AS match_status,
                    i.source_id AS input_source_id,
                    i.input_fingerprint AS input_fingerprint,
                    i.observed_at AS input_observed_at,
                    r.source_id AS recovery_source_id,
                    r.source_native_id AS recovery_source_native_id,
                    r.input_fingerprint AS recovery_input_fingerprint,
                    r.reason AS recovery_reason,
                    r.first_attempt_at AS recovery_first_attempt_at,
                    r.last_attempt_at AS recovery_last_attempt_at,
                    r.next_retry_at AS recovery_next_retry_at
                FROM canonical_editions AS c
                LEFT JOIN spotify_matches AS m
                  ON m.source_id = c.source_id
                 AND m.source_native_id = c.source_native_id
                LEFT JOIN spotify_planner_match_inputs AS i
                  ON i.source_id = c.source_id
                 AND i.source_native_id = c.source_native_id
                LEFT JOIN spotify_planner_matched_recovery_state AS r
                  ON r.source_id = c.source_id
                 AND r.source_native_id = c.source_native_id
                WHERE c.source_id = ?
                  AND c.published_at >= ?
                  AND c.published_at <= ?
                ORDER BY c.published_at, c.source_native_id
                """,
                (str(source_id), cutoff, observed),
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise PersistenceError(f"read production diagnostic snapshot failed for {path}: {exc}") from exc

    active_barriers = sum(_barrier_is_active(row) for row in barrier_rows)
    matched_without_input: list[RecoveryDiagnosticRow] = []
    for row in recovery_rows:
        match_status = row["match_status"]
        if match_status is None:
            continue
        status = _match_status(match_status)
        if status is not MatchStatus.MATCHED:
            continue

        if row["input_source_id"] is not None:
            _validate_match_input(row)
            continue

        edition = _canonical_edition(row)
        recovery = _matched_recovery(row)
        matched_without_input.append(RecoveryDiagnosticRow(edition, recovery))

    return RecoveryDiagnosticSnapshot(active_barriers, tuple(matched_without_input))


def _barrier_is_active(row: sqlite3.Row) -> bool:
    barrier = _barrier_from_row(row)
    match = _barrier_match_from_row(row)
    return barrier.protects(match)


def _barrier_from_row(row: sqlite3.Row) -> PersistenceRejectionBarrierState:
    status = _match_status(row["protected_status"])
    uri = _optional_nonempty_text(row["protected_spotify_episode_uri"], "protected URI")
    duration = _optional_nonnegative_int(
        row["protected_spotify_duration_seconds"], "protected duration"
    )
    state = PersistenceRejectionBarrierState(
        source_id=_nonempty_text(row["barrier_source_id"], "barrier source_id"),
        source_native_id=_nonempty_text(
            row["barrier_source_native_id"], "barrier source_native_id"
        ),
        protected_status=status,
        protected_spotify_episode_uri=uri,
        protected_spotify_duration_seconds=duration,
        protected_match_updated_at=_datetime(row["protected_match_updated_at"]),
        first_rejected_at=_datetime(row["first_rejected_at"]),
        last_rejected_at=_datetime(row["last_rejected_at"]),
    )
    if state.first_rejected_at > state.last_rejected_at:
        raise PersistenceError("production diagnostic barrier timestamps are inconsistent")
    return state


def _barrier_match_from_row(row: sqlite3.Row) -> EditionMatch | None:
    if row["match_source_id"] is None:
        return None
    return EditionMatch(
        source_id=SourceId(_nonempty_text(row["match_source_id"], "match source_id")),
        source_native_id=_nonempty_text(row["match_source_native_id"], "match source_native_id"),
        status=_match_status(row["match_status"]),
        spotify_episode_uri=_optional_nonempty_text(
            row["match_spotify_episode_uri"], "match Spotify URI"
        ),
        diagnostics=None,
        updated_at=_datetime(row["match_updated_at"]),
        spotify_duration_seconds=_optional_nonnegative_int(
            row["match_spotify_duration_seconds"], "match Spotify duration"
        ),
    )


def _validate_match_input(row: sqlite3.Row) -> None:
    source_id = _nonempty_text(row["input_source_id"], "match input source_id")
    if source_id != _nonempty_text(row["source_id"], "canonical source_id"):
        raise PersistenceError("production diagnostic match input identity is inconsistent")
    _fingerprint(row["input_fingerprint"])
    _datetime(row["input_observed_at"])


def _canonical_edition(row: sqlite3.Row) -> CanonicalEdition:
    edition_at = row["edition_at"]
    duration = _optional_nonnegative_int(row["duration_seconds"], "canonical duration")
    return CanonicalEdition(
        source_id=SourceId(_nonempty_text(row["source_id"], "canonical source_id")),
        source_native_id=_nonempty_text(row["source_native_id"], "canonical source_native_id"),
        title=_nonempty_text(row["title"], "canonical title"),
        published_at=_datetime(row["published_at"]),
        edition_at=None if edition_at is None else _datetime(edition_at),
        duration_seconds=duration,
    )


def _matched_recovery(row: sqlite3.Row) -> MatchedRecoveryState | None:
    if row["recovery_source_id"] is None:
        return None
    try:
        reason = MatchedRecoveryReason(
            _nonempty_text(row["recovery_reason"], "matched recovery reason")
        )
    except ValueError as exc:
        raise PersistenceError("production diagnostic matched recovery has invalid reason") from exc
    state = MatchedRecoveryState(
        source_id=_nonempty_text(row["recovery_source_id"], "matched recovery source_id"),
        source_native_id=_nonempty_text(
            row["recovery_source_native_id"], "matched recovery source_native_id"
        ),
        input_fingerprint=_fingerprint(row["recovery_input_fingerprint"]),
        reason=reason,
        first_attempt_at=_datetime(row["recovery_first_attempt_at"]),
        last_attempt_at=_datetime(row["recovery_last_attempt_at"]),
        next_retry_at=_datetime(row["recovery_next_retry_at"]),
    )
    if (
        state.first_attempt_at > state.last_attempt_at
        or state.next_retry_at - state.last_attempt_at != AUDIT_INTERVAL
    ):
        raise PersistenceError("production diagnostic matched recovery timestamps are inconsistent")
    return state


def _match_status(value: object) -> MatchStatus:
    try:
        return MatchStatus(_nonempty_text(value, "match status"))
    except ValueError as exc:
        raise PersistenceError("production diagnostic match state has invalid status") from exc


def _fingerprint(value: object) -> str:
    text = _nonempty_text(value, "fingerprint")
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise PersistenceError("production diagnostic state has invalid fingerprint")
    return text


def _datetime(value: object) -> datetime:
    text = _nonempty_text(value, "timestamp")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PersistenceError("production diagnostic state has invalid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PersistenceError("production diagnostic state has naive timestamp")
    return parsed.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("production diagnostic query timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _nonempty_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise PersistenceError(f"production diagnostic state has invalid {field}")
    return value


def _optional_nonempty_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _nonempty_text(value, field)


def _optional_nonnegative_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PersistenceError(f"production diagnostic state has invalid {field}")
    return value
