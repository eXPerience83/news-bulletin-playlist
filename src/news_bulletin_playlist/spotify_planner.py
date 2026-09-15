"""Durable, local-only Spotify work planning for shadow-mode evaluation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Literal

from news_bulletin_playlist.models import CanonicalEdition, PlaylistDefinition, SourceDefinition
from news_bulletin_playlist.persistence import MatchStatus, PersistenceError, SQLiteStore
from news_bulletin_playlist.spotify.matching_policy import (
    duration_disambiguation_tolerance_seconds,
)

AUDIT_INTERVAL = timedelta(hours=6)
_FINGERPRINT_VERSION = 1


class PlannerReason(StrEnum):
    NEW_MATCH = "new_match"
    RETRY_DUE = "retry_due"
    INPUT_CHANGED = "input_changed"
    DIRTY = "dirty"
    CATALOGUE_AUDIT_DUE = "catalogue_audit_due"
    DESTINATION_AUDIT_DUE = "destination_audit_due"
    PENDING_VERIFICATION = "pending_verification"
    UNKNOWN_MUTATION = "unknown_mutation"
    MISSING_EVIDENCE = "missing_evidence"
    INVALIDATED_CONTEXT = "invalidated_context"
    CORRUPT_STATE = "corrupt_state"
    CLOCK_ROLLBACK = "clock_rollback"


class UnresolvedReason(StrEnum):
    NO_VIABLE_CANDIDATE = "no_viable_candidate"
    MULTIPLE_CANDIDATES = "multiple_candidates"


class MatchedRecoveryReason(StrEnum):
    INCONCLUSIVE_CATALOGUE = "inconclusive_catalogue"


_REASON_ORDER = tuple(PlannerReason)


@dataclass(frozen=True, slots=True)
class PlannerShadowResult:
    would_need_spotify: bool
    would_allow_local_skip: bool
    reasons: tuple[PlannerReason, ...]
    matching_work_count: int
    matching_source_count: int
    dirty_destination_count: int
    catalogue_audit_due_count: int
    destination_audit_due_count: int
    safety_blocker_count: int
    state_available: bool = True

    @classmethod
    def unsafe(cls, reason: PlannerReason = PlannerReason.CORRUPT_STATE) -> PlannerShadowResult:
        return cls(True, False, (reason,), 0, 0, 0, 0, 0, 1, False)


@dataclass(frozen=True, slots=True)
class CatalogueState:
    context_key: str
    contract_fingerprint: str
    last_verified_at: datetime | None
    next_audit_at: datetime | None


@dataclass(frozen=True, slots=True)
class DestinationState:
    playlist_id: str
    destination_context_fingerprint: str
    config_fingerprint: str
    auth_context_fingerprint: str
    observed_desired_fingerprint: str
    confirmed_desired_fingerprint: str | None
    dirty: bool
    unknown: bool
    last_verified_at: datetime | None
    next_audit_at: datetime | None


@dataclass(frozen=True, slots=True)
class DestinationVerificationContext:
    desired_fingerprint: str
    destination_context_fingerprint: str
    config_fingerprint: str
    auth_context_fingerprint: str


@dataclass(frozen=True, slots=True)
class UnresolvedState:
    source_id: str
    source_native_id: str
    input_fingerprint: str
    status: MatchStatus
    reason: UnresolvedReason
    first_unresolved_at: datetime
    last_attempt_at: datetime
    next_retry_at: datetime


@dataclass(frozen=True, slots=True)
class MatchInputState:
    source_id: str
    source_native_id: str
    input_fingerprint: str
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class MatchedRecoveryState:
    source_id: str
    source_native_id: str
    input_fingerprint: str
    reason: MatchedRecoveryReason
    first_attempt_at: datetime
    last_attempt_at: datetime
    next_retry_at: datetime


class SpotifyPlannerJournal:
    """Persistence boundary for future skip-authorizing evidence.

    Reads are strict: malformed rows raise ``PersistenceError`` so callers cannot turn
    corrupt evidence into permission to skip. Shadow integration catches that error.
    """

    def __init__(self, store: SQLiteStore) -> None:
        self.path = store.path

    def get_catalogue(self, context_key: str) -> CatalogueState | None:
        with self._connection("read Spotify planner catalogue state") as connection:
            row = connection.execute(
                """
                SELECT context_key, contract_fingerprint, last_verified_at, next_audit_at
                FROM spotify_planner_catalogue_state WHERE context_key = ?
                """,
                (context_key,),
            ).fetchone()
        if row is None:
            return None
        state = CatalogueState(
            context_key=_text(row, "context_key"),
            contract_fingerprint=_fingerprint(row, "contract_fingerprint"),
            last_verified_at=_optional_timestamp(row["last_verified_at"]),
            next_audit_at=_optional_timestamp(row["next_audit_at"]),
        )

        _validate_audit(state.last_verified_at, state.next_audit_at)
        return state

    def verify_catalogue(
        self, context_key: str, contract_fingerprint: str, *, verified_at: datetime
    ) -> None:
        verified = _as_utc(verified_at)
        with self._connection("persist Spotify planner catalogue verification") as connection:
            connection.execute(
                """
                INSERT INTO spotify_planner_catalogue_state (
                    context_key, contract_fingerprint, last_verified_at, next_audit_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(context_key) DO UPDATE SET
                    contract_fingerprint=excluded.contract_fingerprint,
                    last_verified_at=excluded.last_verified_at,
                    next_audit_at=excluded.next_audit_at
                """,
                (
                    context_key,
                    _require_fingerprint(contract_fingerprint),
                    _timestamp(verified),
                    _timestamp(verified + AUDIT_INTERVAL),
                ),
            )

    def get_destination(self, playlist_id: str) -> DestinationState | None:
        with self._connection("read Spotify planner destination state") as connection:
            row = self._select_destination(connection, playlist_id)
        if row is None:
            return None
        return _destination_from_row(row)

    def observe_destination(
        self,
        *,
        playlist_id: str,
        destination_context_fingerprint: str,
        config_fingerprint: str,
        auth_context_fingerprint: str,
        desired_fingerprint: str,
    ) -> DestinationState:
        destination_context_fingerprint = _require_fingerprint(destination_context_fingerprint)
        config_fingerprint = _require_fingerprint(config_fingerprint)
        auth_context_fingerprint = _require_fingerprint(auth_context_fingerprint)
        desired_fingerprint = _require_fingerprint(desired_fingerprint)
        with self._connection("observe Spotify planner destination") as connection:
            row = self._select_destination(connection, playlist_id)
            current = None if row is None else _destination_from_row(row)
            compatible = current is not None and (
                current.destination_context_fingerprint == destination_context_fingerprint
                and current.config_fingerprint == config_fingerprint
                and current.auth_context_fingerprint == auth_context_fingerprint
            )
            changed = current is None or current.observed_desired_fingerprint != desired_fingerprint
            confirmed = (
                current.confirmed_desired_fingerprint
                if compatible and not changed and current is not None
                else None
            )
            state = DestinationState(
                playlist_id=playlist_id,
                destination_context_fingerprint=destination_context_fingerprint,
                config_fingerprint=config_fingerprint,
                auth_context_fingerprint=auth_context_fingerprint,
                observed_desired_fingerprint=desired_fingerprint,
                confirmed_desired_fingerprint=confirmed,
                dirty=not compatible or changed or confirmed != desired_fingerprint,
                unknown=current.unknown if compatible and current is not None else False,
                last_verified_at=(
                    current.last_verified_at if compatible and current is not None else None
                ),
                next_audit_at=current.next_audit_at if compatible and current is not None else None,
            )
            self._put_destination_on(connection, state)
        return state

    def verify_destination(
        self, playlist_id: str, *, expected: DestinationVerificationContext, verified_at: datetime
    ) -> None:
        verified = _as_utc(verified_at)
        with self._connection("verify Spotify planner destination") as connection:
            cursor = connection.execute(
                """UPDATE spotify_planner_destination_state
                   SET confirmed_desired_fingerprint = observed_desired_fingerprint,
                       dirty = 0, unknown = 0, last_verified_at = ?, next_audit_at = ?
                   WHERE playlist_id = ? AND observed_desired_fingerprint = ?
                     AND destination_context_fingerprint = ? AND config_fingerprint = ?
                     AND auth_context_fingerprint = ?""",
                (
                    _timestamp(verified),
                    _timestamp(verified + AUDIT_INTERVAL),
                    playlist_id,
                    expected.desired_fingerprint,
                    expected.destination_context_fingerprint,
                    expected.config_fingerprint,
                    expected.auth_context_fingerprint,
                ),
            )
            if cursor.rowcount != 1:
                raise PersistenceError("Spotify planner verification context changed or missing")

    def begin_destination_verification(self, playlist_id: str) -> None:
        """Invalidate confirmation before a reconciliation that may mutate remotely."""
        with self._connection("begin Spotify destination verification") as connection:
            cursor = connection.execute(
                """UPDATE spotify_planner_destination_state
                   SET confirmed_desired_fingerprint=NULL, dirty=1
                   WHERE playlist_id=?""",
                (playlist_id,),
            )
            if cursor.rowcount != 1:
                raise PersistenceError("cannot begin verification for missing planner destination")

    def mark_destination_unknown(self, playlist_id: str) -> None:
        current = self.get_destination(playlist_id)
        if current is None:
            return
        self._put_destination(
            DestinationState(
                playlist_id=current.playlist_id,
                destination_context_fingerprint=current.destination_context_fingerprint,
                config_fingerprint=current.config_fingerprint,
                auth_context_fingerprint=current.auth_context_fingerprint,
                observed_desired_fingerprint=current.observed_desired_fingerprint,
                confirmed_desired_fingerprint=None,
                dirty=True,
                unknown=True,
                last_verified_at=None,
                next_audit_at=None,
            )
        )

    def get_unresolved(self, source_id: str, source_native_id: str) -> UnresolvedState | None:
        with self._connection("read Spotify planner unresolved state") as connection:
            row = connection.execute(
                """
                SELECT source_id, source_native_id, input_fingerprint, status, reason,
                       first_unresolved_at, last_attempt_at, next_retry_at
                FROM spotify_planner_unresolved_state
                WHERE source_id = ? AND source_native_id = ?
                """,
                (source_id, source_native_id),
            ).fetchone()
        if row is None:
            return None
        try:
            status = MatchStatus(_text(row, "status"))
        except ValueError as exc:
            raise PersistenceError("Spotify planner unresolved state has invalid status") from exc
        if status is MatchStatus.MATCHED:
            raise PersistenceError("Spotify planner unresolved state cannot be matched")
        try:
            reason = UnresolvedReason(_text(row, "reason"))
        except ValueError as exc:
            raise PersistenceError("Spotify planner unresolved state has invalid reason") from exc
        expected_reason = (
            UnresolvedReason.NO_VIABLE_CANDIDATE
            if status is MatchStatus.PENDING
            else UnresolvedReason.MULTIPLE_CANDIDATES
        )
        if reason is not expected_reason:
            raise PersistenceError("Spotify planner unresolved state has inconsistent reason")
        state = UnresolvedState(
            source_id=_text(row, "source_id"),
            source_native_id=_text(row, "source_native_id"),
            input_fingerprint=_fingerprint(row, "input_fingerprint"),
            status=status,
            reason=reason,
            first_unresolved_at=_timestamp_value(row, "first_unresolved_at"),
            last_attempt_at=_timestamp_value(row, "last_attempt_at"),
            next_retry_at=_timestamp_value(row, "next_retry_at"),
        )

        if (
            state.first_unresolved_at > state.last_attempt_at
            or state.next_retry_at <= state.last_attempt_at
            or state.next_retry_at - state.last_attempt_at
            != unresolved_retry_interval(state.last_attempt_at - state.first_unresolved_at)
        ):
            raise PersistenceError("Spotify planner retry timestamps are inconsistent")
        return state

    def get_matched_recovery(
        self, source_id: str, source_native_id: str
    ) -> MatchedRecoveryState | None:
        with self._connection("read Spotify planner matched recovery state") as connection:
            row = connection.execute(
                """
                SELECT source_id, source_native_id, input_fingerprint, reason,
                       first_attempt_at, last_attempt_at, next_retry_at
                FROM spotify_planner_matched_recovery_state
                WHERE source_id = ? AND source_native_id = ?
                """,
                (source_id, source_native_id),
            ).fetchone()
        if row is None:
            return None
        try:
            reason = MatchedRecoveryReason(_text(row, "reason"))
        except ValueError as exc:
            raise PersistenceError("Spotify planner matched recovery has invalid reason") from exc
        state = MatchedRecoveryState(
            source_id=_text(row, "source_id"),
            source_native_id=_text(row, "source_native_id"),
            input_fingerprint=_fingerprint(row, "input_fingerprint"),
            reason=reason,
            first_attempt_at=_timestamp_value(row, "first_attempt_at"),
            last_attempt_at=_timestamp_value(row, "last_attempt_at"),
            next_retry_at=_timestamp_value(row, "next_retry_at"),
        )
        if (
            state.first_attempt_at > state.last_attempt_at
            or state.next_retry_at - state.last_attempt_at != AUDIT_INTERVAL
        ):
            raise PersistenceError("Spotify planner matched recovery timestamps are inconsistent")
        return state

    def record_matched_recovery(
        self,
        *,
        source_id: str,
        source_native_id: str,
        input_fingerprint: str,
        attempted_at: datetime,
    ) -> MatchedRecoveryState:
        attempted = _as_utc(attempted_at)
        input_fingerprint = _require_fingerprint(input_fingerprint)
        current = self.get_matched_recovery(source_id, source_native_id)
        first = (
            current.first_attempt_at
            if current is not None and current.input_fingerprint == input_fingerprint
            else attempted
        )
        baseline = max(attempted, current.last_attempt_at) if current is not None else attempted
        state = MatchedRecoveryState(
            source_id=source_id,
            source_native_id=source_native_id,
            input_fingerprint=input_fingerprint,
            reason=MatchedRecoveryReason.INCONCLUSIVE_CATALOGUE,
            first_attempt_at=first,
            last_attempt_at=baseline,
            next_retry_at=baseline + AUDIT_INTERVAL,
        )
        with self._connection("persist Spotify planner matched recovery state") as connection:
            connection.execute(
                """
                INSERT INTO spotify_planner_matched_recovery_state (
                    source_id, source_native_id, input_fingerprint, reason,
                    first_attempt_at, last_attempt_at, next_retry_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, source_native_id) DO UPDATE SET
                    input_fingerprint=excluded.input_fingerprint,
                    reason=excluded.reason,
                    first_attempt_at=excluded.first_attempt_at,
                    last_attempt_at=excluded.last_attempt_at,
                    next_retry_at=excluded.next_retry_at
                """,
                (
                    source_id,
                    source_native_id,
                    input_fingerprint,
                    state.reason.value,
                    _timestamp(first),
                    _timestamp(baseline),
                    _timestamp(state.next_retry_at),
                ),
            )
        return state

    def clear_matched_recovery(self, source_id: str, source_native_id: str) -> None:
        with self._connection("clear Spotify planner matched recovery state") as connection:
            connection.execute(
                """DELETE FROM spotify_planner_matched_recovery_state
                   WHERE source_id = ? AND source_native_id = ?""",
                (source_id, source_native_id),
            )

    def get_match_input(self, source_id: str, source_native_id: str) -> MatchInputState | None:
        with self._connection("read Spotify planner match input") as connection:
            row = connection.execute(
                """
                SELECT source_id, source_native_id, input_fingerprint, observed_at
                FROM spotify_planner_match_inputs
                WHERE source_id = ? AND source_native_id = ?
                """,
                (source_id, source_native_id),
            ).fetchone()
        if row is None:
            return None
        return MatchInputState(
            source_id=_text(row, "source_id"),
            source_native_id=_text(row, "source_native_id"),
            input_fingerprint=_fingerprint(row, "input_fingerprint"),
            observed_at=_timestamp_value(row, "observed_at"),
        )

    def observe_match_input(
        self,
        *,
        source_id: str,
        source_native_id: str,
        input_fingerprint: str,
        observed_at: datetime,
    ) -> None:
        with self._connection("persist Spotify planner match input") as connection:
            connection.execute(
                """
                INSERT INTO spotify_planner_match_inputs (
                    source_id, source_native_id, input_fingerprint, observed_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(source_id, source_native_id) DO UPDATE SET
                    input_fingerprint=excluded.input_fingerprint,
                    observed_at=excluded.observed_at
                """,
                (
                    source_id,
                    source_native_id,
                    _require_fingerprint(input_fingerprint),
                    _timestamp(observed_at),
                ),
            )

    def clear_match_input(self, source_id: str, source_native_id: str) -> None:
        with self._connection("clear Spotify planner match input") as connection:
            connection.execute(
                """DELETE FROM spotify_planner_match_inputs
                   WHERE source_id = ? AND source_native_id = ?""",
                (source_id, source_native_id),
            )

    def record_unresolved(
        self,
        *,
        source_id: str,
        source_native_id: str,
        input_fingerprint: str,
        status: MatchStatus,
        attempted_at: datetime,
    ) -> UnresolvedState:
        if status not in {MatchStatus.PENDING, MatchStatus.AMBIGUOUS}:
            raise ValueError("unresolved planner status must be pending or ambiguous")
        attempted = _as_utc(attempted_at)
        input_fingerprint = _require_fingerprint(input_fingerprint)
        current = self.get_unresolved(source_id, source_native_id)
        first = (
            current.first_unresolved_at
            if current is not None and current.input_fingerprint == input_fingerprint
            else attempted
        )
        baseline = max(attempted, current.last_attempt_at) if current is not None else attempted
        next_retry = baseline + unresolved_retry_interval(max(baseline - first, timedelta(0)))
        state = UnresolvedState(
            source_id,
            source_native_id,
            input_fingerprint,
            status,
            (
                UnresolvedReason.NO_VIABLE_CANDIDATE
                if status is MatchStatus.PENDING
                else UnresolvedReason.MULTIPLE_CANDIDATES
            ),
            first,
            baseline,
            next_retry,
        )
        with self._connection("persist Spotify planner unresolved state") as connection:
            connection.execute(
                """
                INSERT INTO spotify_planner_unresolved_state (
                    source_id, source_native_id, input_fingerprint, status, reason,
                    first_unresolved_at, last_attempt_at, next_retry_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, source_native_id) DO UPDATE SET
                    input_fingerprint=excluded.input_fingerprint,
                    status=excluded.status,
                    reason=excluded.reason,
                    first_unresolved_at=excluded.first_unresolved_at,
                    last_attempt_at=excluded.last_attempt_at,
                    next_retry_at=excluded.next_retry_at
                """,
                (
                    source_id,
                    source_native_id,
                    input_fingerprint,
                    status.value,
                    state.reason.value,
                    _timestamp(first),
                    _timestamp(baseline),
                    _timestamp(next_retry),
                ),
            )
        return state

    def clear_unresolved(self, source_id: str, source_native_id: str) -> None:
        with self._connection("clear Spotify planner unresolved state") as connection:
            connection.execute(
                """DELETE FROM spotify_planner_unresolved_state
                   WHERE source_id = ? AND source_native_id = ?""",
                (source_id, source_native_id),
            )

    def retain_contexts(
        self, *, catalogue_contexts: Iterable[str], playlist_ids: Iterable[str]
    ) -> None:
        """Bound state growth to currently configured catalogue and destination contexts."""
        catalogue = tuple(sorted(set(catalogue_contexts)))
        playlists = tuple(sorted(set(playlist_ids)))
        with self._connection("prune Spotify planner contexts") as connection:
            stale_catalogues = [
                (str(row["context_key"]),)
                for row in connection.execute(
                    "SELECT context_key FROM spotify_planner_catalogue_state"
                ).fetchall()
                if str(row["context_key"]) not in catalogue
            ]
            connection.executemany(
                "DELETE FROM spotify_planner_catalogue_state WHERE context_key = ?",
                stale_catalogues,
            )
            stale_playlists = [
                (str(row["playlist_id"]),)
                for row in connection.execute(
                    "SELECT playlist_id FROM spotify_planner_destination_state"
                ).fetchall()
                if str(row["playlist_id"]) not in playlists
            ]
            connection.executemany(
                "DELETE FROM spotify_planner_destination_state WHERE playlist_id = ?",
                stale_playlists,
            )

    def _put_destination(self, state: DestinationState) -> None:
        with self._connection("persist Spotify planner destination state") as connection:
            self._put_destination_on(connection, state)

    @staticmethod
    def _select_destination(connection: sqlite3.Connection, playlist_id: str) -> sqlite3.Row | None:
        row = connection.execute(
            """SELECT playlist_id, destination_context_fingerprint, config_fingerprint,
                      auth_context_fingerprint, observed_desired_fingerprint,
                      confirmed_desired_fingerprint, dirty, unknown,
                      last_verified_at, next_audit_at
               FROM spotify_planner_destination_state WHERE playlist_id=?""",
            (playlist_id,),
        ).fetchone()
        return row if isinstance(row, sqlite3.Row) else None

    @staticmethod
    def _put_destination_on(connection: sqlite3.Connection, state: DestinationState) -> None:
        connection.execute(
            """
                INSERT INTO spotify_planner_destination_state (
                    playlist_id, destination_context_fingerprint, config_fingerprint,
                    auth_context_fingerprint, observed_desired_fingerprint,
                    confirmed_desired_fingerprint, dirty, unknown,
                    last_verified_at, next_audit_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(playlist_id) DO UPDATE SET
                    destination_context_fingerprint=excluded.destination_context_fingerprint,
                    config_fingerprint=excluded.config_fingerprint,
                    auth_context_fingerprint=excluded.auth_context_fingerprint,
                    observed_desired_fingerprint=excluded.observed_desired_fingerprint,
                    confirmed_desired_fingerprint=excluded.confirmed_desired_fingerprint,
                    dirty=excluded.dirty, unknown=excluded.unknown,
                    last_verified_at=excluded.last_verified_at,
                    next_audit_at=excluded.next_audit_at
                """,
            (
                state.playlist_id,
                state.destination_context_fingerprint,
                state.config_fingerprint,
                state.auth_context_fingerprint,
                state.observed_desired_fingerprint,
                state.confirmed_desired_fingerprint,
                int(state.dirty),
                int(state.unknown),
                _optional_timestamp_text(state.last_verified_at),
                _optional_timestamp_text(state.next_audit_at),
            ),
        )

    def _connection(self, operation: str) -> _PlannerConnection:
        try:
            connection = sqlite3.connect(self.path, timeout=5.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
        except sqlite3.Error as exc:
            raise PersistenceError(f"{operation} failed for {self.path}: {exc}") from exc
        return _PlannerConnection(connection, operation, self.path)


class _PlannerConnection:
    def __init__(self, connection: sqlite3.Connection, operation: str, path: Path) -> None:
        self.connection = connection
        self.operation = operation
        self.path = path

    def __enter__(self) -> sqlite3.Connection:
        return self.connection

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        del traceback
        db_error = exc_value if isinstance(exc_value, sqlite3.Error) else None
        try:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
        except sqlite3.Error as cleanup_error:
            db_error = cleanup_error
        finally:
            try:
                self.connection.close()
            except sqlite3.Error as close_error:
                if db_error is None:
                    db_error = close_error
        if db_error is not None:
            raise PersistenceError(
                f"{self.operation} failed for {self.path}: {db_error}"
            ) from db_error
        return False


def plan_shadow_cycle(
    *,
    source_reasons: Iterable[tuple[str, PlannerReason]],
    destination_states: Sequence[DestinationState],
    catalogue_states: Sequence[CatalogueState | None],
    pending_verification_count: int,
    now: datetime,
    additional_reasons: Iterable[PlannerReason] = (),
) -> PlannerShadowResult:
    observed = _as_utc(now)
    source_reason_list = tuple(source_reasons)
    reasons = {reason for _, reason in source_reason_list}
    reasons.update(additional_reasons)
    matching_identities = {identity for identity, _ in source_reason_list}
    matching_sources = {identity.split("\0", 1)[0] for identity in matching_identities}
    dirty_count = sum(state.dirty for state in destination_states)
    unknown_count = sum(state.unknown for state in destination_states)
    catalogue_due = sum(
        state is None
        or state.last_verified_at is None
        or state.next_audit_at is None
        or observed < state.last_verified_at
        or observed >= state.next_audit_at
        for state in catalogue_states
    )
    destination_due = sum(
        state.last_verified_at is None
        or state.next_audit_at is None
        or observed < state.last_verified_at
        or observed >= state.next_audit_at
        for state in destination_states
    )
    if dirty_count:
        reasons.add(PlannerReason.DIRTY)
    if catalogue_due:
        reasons.add(PlannerReason.CATALOGUE_AUDIT_DUE)
    if destination_due:
        reasons.add(PlannerReason.DESTINATION_AUDIT_DUE)
    if pending_verification_count:
        reasons.add(PlannerReason.PENDING_VERIFICATION)
    if unknown_count:
        reasons.add(PlannerReason.UNKNOWN_MUTATION)
    ordered = tuple(reason for reason in _REASON_ORDER if reason in reasons)
    blocker_reasons = {
        PlannerReason.MISSING_EVIDENCE,
        PlannerReason.INVALIDATED_CONTEXT,
        PlannerReason.CORRUPT_STATE,
        PlannerReason.CLOCK_ROLLBACK,
    }
    blockers = (
        pending_verification_count
        + unknown_count
        + sum(reason in blocker_reasons for reason in reasons)
    )
    need = bool(ordered)
    return PlannerShadowResult(
        would_need_spotify=need,
        would_allow_local_skip=not need,
        reasons=ordered,
        matching_work_count=len(matching_identities),
        matching_source_count=len(matching_sources),
        dirty_destination_count=dirty_count,
        catalogue_audit_due_count=catalogue_due,
        destination_audit_due_count=destination_due,
        safety_blocker_count=blockers,
    )


def unresolved_retry_interval(age: timedelta) -> timedelta:
    if age < timedelta(hours=1):
        return timedelta(minutes=15)
    if age < timedelta(hours=3):
        return timedelta(minutes=30)
    if age < timedelta(hours=6):
        return timedelta(hours=1)
    return timedelta(hours=6)


def matched_recovery_planning_reason(
    state: MatchedRecoveryState | None, *, input_fingerprint: str, now: datetime
) -> PlannerReason | None:
    observed = _as_utc(now)
    if state is None:
        return PlannerReason.MISSING_EVIDENCE
    if state.input_fingerprint != input_fingerprint:
        return PlannerReason.INPUT_CHANGED
    if observed < state.last_attempt_at:
        return PlannerReason.CLOCK_ROLLBACK
    if observed >= state.next_retry_at:
        return PlannerReason.RETRY_DUE
    return None


def unresolved_planning_reason(
    state: UnresolvedState | None, *, input_fingerprint: str, now: datetime
) -> PlannerReason | None:
    observed = _as_utc(now)
    if state is None:
        return PlannerReason.MISSING_EVIDENCE
    if state.input_fingerprint != input_fingerprint:
        return PlannerReason.INPUT_CHANGED
    if observed < state.last_attempt_at:
        return PlannerReason.CLOCK_ROLLBACK
    if observed >= state.next_retry_at:
        return PlannerReason.RETRY_DUE
    return None


def edition_input_fingerprint(edition: CanonicalEdition) -> str:
    return _digest(
        {
            "v": _FINGERPRINT_VERSION,
            "source_id": str(edition.source_id),
            "source_native_id": edition.source_native_id,
            "title": edition.title,
            "published_at": _timestamp(edition.published_at),
            "edition_at": _optional_timestamp_text(edition.edition_at),
            "duration_seconds": edition.duration_seconds,
        }
    )


def matching_input_fingerprint(source: SourceDefinition, edition: CanonicalEdition) -> str:
    """Bind an edition input to the source rules used to evaluate it."""
    return stable_fingerprint(
        (
            "spotify-matching-input-v2",
            source_contract_fingerprint(source),
            edition_input_fingerprint(edition),
        )
    )


def source_contract_fingerprint(source: SourceDefinition) -> str:
    references = sorted(
        (ref.system.casefold(), ref.resource_type.casefold(), ref.external_id)
        for ref in source.external_references
    )
    return _digest(
        {
            "v": _FINGERPRINT_VERSION,
            "source_id": str(source.id),
            "parser_id": str(source.parser_id),
            "timezone": str(source.timezone),
            "spotify_release_delay_days": source.spotify_release_delay_days,
            "spotify_duration_disambiguation_tolerance_seconds": (
                duration_disambiguation_tolerance_seconds(source)
            ),
            "external_references": references,
        }
    )


def playlist_config_fingerprint(playlist: PlaylistDefinition) -> str:
    return _digest(
        {
            "v": _FINGERPRINT_VERSION,
            "playlist_id": str(playlist.id),
            "enabled": playlist.enabled,
            "sources": [str(value) for value in playlist.source_selection.explicit],
            "retention_hours": playlist.retention_hours,
            "max_episodes": playlist.max_episodes,
            "ordering": playlist.ordering.value,
            "duration_default": playlist.duration_policy.default_max_seconds,
            "duration_exceptions": [
                {
                    "id": value.id,
                    "source_id": str(value.source_id),
                    "edition_local_time": value.edition_local_time.isoformat(),
                    "max_seconds": value.max_seconds,
                }
                for value in playlist.duration_policy.exceptions
            ],
        }
    )


def destination_context_fingerprint(playlist: PlaylistDefinition) -> str:
    return _digest(
        {
            "v": _FINGERPRINT_VERSION,
            "adapter_id": str(playlist.destination.adapter_id),
            "external_id": playlist.destination.external_id,
        }
    )


def stable_fingerprint(values: Sequence[str]) -> str:
    return _digest({"v": _FINGERPRINT_VERSION, "values": list(values)})


def _digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _require_fingerprint(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise PersistenceError("Spotify planner state has invalid fingerprint")
    return value


def _fingerprint(row: sqlite3.Row, key: str) -> str:
    return _require_fingerprint(_text(row, key))


def _optional_fingerprint(value: object) -> str | None:
    return None if value is None else _require_fingerprint(str(value))


def _text(row: sqlite3.Row, key: str) -> str:
    value = row[key]
    if not isinstance(value, str) or not value:
        raise PersistenceError(f"Spotify planner state has invalid {key}")
    return value


def _boolean(row: sqlite3.Row, key: str) -> bool:
    value = row[key]
    if value not in {0, 1}:
        raise PersistenceError(f"Spotify planner state has invalid {key}")
    return bool(value)


def _timestamp_value(row: sqlite3.Row, key: str) -> datetime:
    value = row[key]
    if not isinstance(value, str):
        raise PersistenceError(f"Spotify planner state has invalid {key}")
    return _parse_timestamp(value)


def _optional_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PersistenceError("Spotify planner state has invalid timestamp")
    return _parse_timestamp(value)


def _timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _optional_timestamp_text(value: datetime | None) -> str | None:
    return None if value is None else _timestamp(value)


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PersistenceError("Spotify planner state has invalid timestamp") from exc
    return _as_utc(parsed)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Spotify planner timestamps must be timezone-aware")
    return value.astimezone(UTC)


def authorization_context_fingerprint(authorized_at: datetime, *, market: str | None = None) -> str:
    market_context = "user-token/account" if market is None else f"explicit:{market}"
    return stable_fingerprint(
        ("spotify-authorization-v2", _timestamp(authorized_at), market_context)
    )


def catalogue_context_fingerprint(source: SourceDefinition, auth_context: str) -> str:
    return stable_fingerprint(
        (
            "spotify-catalogue-v2",
            source_contract_fingerprint(source),
            _require_fingerprint(auth_context),
            "page-size=50;max-pages=2",
        )
    )


def _validate_audit(verified: datetime | None, deadline: datetime | None) -> None:
    if (verified is None) != (deadline is None) or (
        verified is not None and deadline is not None and deadline - verified != AUDIT_INTERVAL
    ):
        raise PersistenceError("Spotify planner audit timestamps are inconsistent")


def _validate_destination(state: DestinationState) -> None:
    _validate_audit(state.last_verified_at, state.next_audit_at)
    if state.dirty and state.confirmed_desired_fingerprint is not None:
        raise PersistenceError("Spotify planner dirty destination has confirmation")
    if not state.dirty and (
        state.unknown
        or state.confirmed_desired_fingerprint is None
        or state.confirmed_desired_fingerprint != state.observed_desired_fingerprint
        or state.last_verified_at is None
    ):
        raise PersistenceError("Spotify planner clean destination is inconsistent")
    if state.unknown and state.confirmed_desired_fingerprint is not None:
        raise PersistenceError("Spotify planner unknown destination has confirmation")


def _destination_from_row(row: sqlite3.Row) -> DestinationState:
    state = DestinationState(
        playlist_id=_text(row, "playlist_id"),
        destination_context_fingerprint=_fingerprint(row, "destination_context_fingerprint"),
        config_fingerprint=_fingerprint(row, "config_fingerprint"),
        auth_context_fingerprint=_fingerprint(row, "auth_context_fingerprint"),
        observed_desired_fingerprint=_fingerprint(row, "observed_desired_fingerprint"),
        confirmed_desired_fingerprint=_optional_fingerprint(row["confirmed_desired_fingerprint"]),
        dirty=_boolean(row, "dirty"),
        unknown=_boolean(row, "unknown"),
        last_verified_at=_optional_timestamp(row["last_verified_at"]),
        next_audit_at=_optional_timestamp(row["next_audit_at"]),
    )
    _validate_destination(state)
    return state
