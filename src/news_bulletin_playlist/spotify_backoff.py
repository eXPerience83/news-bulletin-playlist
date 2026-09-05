"""Durable, fail-closed Spotify Web API rate-limit backoff for the engine runtime."""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Protocol, TypeVar

from news_bulletin_playlist.diagnostics import DiagnosticEventStore, DiagnosticSeverity
from news_bulletin_playlist.persistence import PersistenceError, SQLiteStore
from news_bulletin_playlist.runtime_diagnostics import OperationalDiagnostics
from news_bulletin_playlist.spotify.auth import SpotifyAuthError
from news_bulletin_playlist.spotify.client import SpotifyApiError, SpotifyTransportError

DEFAULT_SPOTIFY_RATE_LIMIT_FALLBACK_SECONDS = 30 * 60

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS spotify_rate_limit_backoff (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    observed_at TEXT NOT NULL,
    retry_not_before TEXT NOT NULL,
    retry_after_seconds INTEGER,
    backoff_source TEXT NOT NULL CHECK (backoff_source IN ('spotify_header', 'fallback'))
)
"""

_UPSERT_MAX_DEADLINE_SQL = """
INSERT INTO spotify_rate_limit_backoff (
    singleton, observed_at, retry_not_before, retry_after_seconds, backoff_source
) VALUES (1, ?, ?, ?, ?)
ON CONFLICT(singleton) DO UPDATE SET
    observed_at = excluded.observed_at,
    retry_not_before = excluded.retry_not_before,
    retry_after_seconds = excluded.retry_after_seconds,
    backoff_source = excluded.backoff_source
WHERE excluded.retry_not_before > spotify_rate_limit_backoff.retry_not_before
"""

T = TypeVar("T")
Clock = Callable[[], datetime]


class SpotifyBackoffClient(Protocol):
    def show_episodes(
        self,
        show_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]: ...

    def playlist_items(
        self,
        playlist_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]: ...

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]: ...

    def playlist_snapshot(self, playlist_id: str) -> dict[str, Any]: ...


class SpotifyBackoffAuthProvider(Protocol):
    def get_access_token(self, *, now: datetime | None = None) -> str: ...


class SpotifyRateLimitBackoffActive(SpotifyAuthError):
    """Raised before engine authorization when durable Spotify backoff is still active."""


class SpotifyRateLimitStateUnavailable(SpotifyAuthError):
    """Raised when the durable backoff state cannot be read safely."""


class SpotifyRateLimitSuppressed(SpotifyTransportError):
    """Raised locally when a later same-cycle Spotify request is suppressed."""


@dataclass(frozen=True, slots=True)
class SpotifyRateLimitState:
    observed_at: datetime
    retry_not_before: datetime
    retry_after_seconds: int | None
    backoff_source: str

    def __post_init__(self) -> None:
        observed = _as_utc(self.observed_at)
        retry_not_before = _as_utc(self.retry_not_before)
        if retry_not_before < observed:
            raise ValueError("Spotify retry_not_before must not precede observed_at")
        if self.retry_after_seconds is not None and self.retry_after_seconds < 0:
            raise ValueError("Spotify retry_after_seconds must not be negative")
        if self.backoff_source not in {"spotify_header", "fallback"}:
            raise ValueError("Spotify backoff_source is invalid")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "retry_not_before", retry_not_before)


class _ConnectionContext:
    def __init__(self, connection: sqlite3.Connection, *, operation: str, path: str) -> None:
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
    ) -> None:
        del traceback
        body_db_exc = exc_value if isinstance(exc_value, sqlite3.Error) else None
        try:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
        except sqlite3.Error as db_exc:
            raise PersistenceError(f"{self.operation} failed for {self.path}: {db_exc}") from db_exc
        finally:
            self.connection.close()
        if body_db_exc is not None:
            raise PersistenceError(
                f"{self.operation} failed for {self.path}: {body_db_exc}"
            ) from body_db_exc


class SpotifyRateLimitJournal:
    """Persist one runtime-wide Spotify Web API cooldown in the existing SQLite database."""

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store
        self.path = store.path
        self.diagnostics = self._build_diagnostics()

    def get(self) -> SpotifyRateLimitState | None:
        with self._connection("read Spotify rate-limit backoff") as connection:
            self._ensure_table(connection)
            row = self._select_row(connection)
        return None if row is None else _state_from_row(row)

    def active(self, *, now: datetime) -> SpotifyRateLimitState | None:
        observed = _as_utc(now)
        while True:
            state = self.get()
            if state is None:
                return None
            if observed < state.retry_not_before:
                return state

            with self._connection("expire Spotify rate-limit backoff") as connection:
                self._ensure_table(connection)
                cursor = connection.execute(
                    """
                    DELETE FROM spotify_rate_limit_backoff
                    WHERE singleton = 1
                      AND retry_not_before <= ?
                    """,
                    (_format_storage_timestamp(observed),),
                )
                deleted = max(cursor.rowcount, 0)
            if deleted:
                return None
            # A concurrent activation extended the singleton after our read. Re-read it
            # instead of clearing or ignoring the newer, later deadline.

    def activate(
        self,
        *,
        observed_at: datetime,
        retry_after_seconds: int | None,
    ) -> SpotifyRateLimitState:
        observed = _as_utc(observed_at)
        deadline, effective_retry_after, source = _retry_deadline(
            observed,
            retry_after_seconds=retry_after_seconds,
        )
        candidate = SpotifyRateLimitState(
            observed_at=observed,
            retry_not_before=deadline,
            retry_after_seconds=effective_retry_after,
            backoff_source=source,
        )

        with self._connection("persist Spotify rate-limit backoff") as connection:
            self._ensure_table(connection)
            connection.execute(
                _UPSERT_MAX_DEADLINE_SQL,
                (
                    _format_storage_timestamp(candidate.observed_at),
                    _format_storage_timestamp(candidate.retry_not_before),
                    candidate.retry_after_seconds,
                    candidate.backoff_source,
                ),
            )
            row = self._select_row(connection)
        if row is None:
            raise PersistenceError("Spotify rate-limit backoff disappeared during activation")
        return _state_from_row(row)

    def clear(self) -> None:
        with self._connection("clear Spotify rate-limit backoff") as connection:
            self._ensure_table(connection)
            connection.execute("DELETE FROM spotify_rate_limit_backoff WHERE singleton = 1")

    def prune_run_history(self, *, now: datetime) -> None:
        """Bound run-history growth while Spotify is intentionally skipped."""
        self.store.prune_operational_history(now=_as_utc(now))

    @staticmethod
    def _ensure_table(connection: sqlite3.Connection) -> None:
        connection.execute(_TABLE_SQL)

    @staticmethod
    def _select_row(connection: sqlite3.Connection) -> sqlite3.Row | None:
        row = connection.execute(
            """
            SELECT observed_at, retry_not_before, retry_after_seconds, backoff_source
            FROM spotify_rate_limit_backoff
            WHERE singleton = 1
            """
        ).fetchone()
        if row is not None and not isinstance(row, sqlite3.Row):
            raise PersistenceError("Spotify rate-limit backoff row had an invalid shape")
        return row

    def _connection(self, operation: str) -> _ConnectionContext:
        try:
            connection = sqlite3.connect(self.path, timeout=10.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 10000")
        except sqlite3.Error as exc:
            raise PersistenceError(f"{operation} failed for {self.path}: {exc}") from exc
        return _ConnectionContext(connection, operation=operation, path=str(self.path))

    def _build_diagnostics(self) -> OperationalDiagnostics | None:
        try:
            diagnostic_store = DiagnosticEventStore(self.path)
            diagnostic_store.initialize()
        except PersistenceError, ValueError:
            return None
        return OperationalDiagnostics(diagnostic_store)


class SpotifyRateLimitGuardAuth:
    """Skip token work after RSS collection while a durable Web API cooldown is active."""

    def __init__(
        self,
        provider: SpotifyBackoffAuthProvider,
        journal: SpotifyRateLimitJournal,
        *,
        clock: Clock,
        diagnostics: OperationalDiagnostics | None = None,
    ) -> None:
        self.provider = provider
        self.journal = journal
        self.clock = clock
        self.diagnostics = journal.diagnostics if diagnostics is None else diagnostics

    def get_access_token(self, *, now: datetime | None = None) -> str:
        observed = _as_utc(self.clock() if now is None else now)
        try:
            state = self.journal.active(now=observed)
            if state is not None:
                self.journal.prune_run_history(now=observed)
        except PersistenceError as exc:
            raise SpotifyRateLimitStateUnavailable(
                "Spotify rate-limit state is unavailable"
            ) from exc
        if state is not None:
            _emit_backoff(self.diagnostics, state, backoff_state="active", occurred_at=observed)
            raise SpotifyRateLimitBackoffActive(_active_message(state))
        return self.provider.get_access_token(now=now)


class SpotifyRateLimitGuardClient:
    """Persist the first live 429 and suppress every later Spotify request in that cycle."""

    def __init__(
        self,
        client: SpotifyBackoffClient,
        journal: SpotifyRateLimitJournal,
        *,
        clock: Clock,
        diagnostics: OperationalDiagnostics | None = None,
    ) -> None:
        self.client = client
        self.journal = journal
        self.clock = clock
        self.diagnostics = journal.diagnostics if diagnostics is None else diagnostics
        self._cycle_limited: SpotifyRateLimitState | None = None

    def show_episodes(
        self,
        show_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        return self._call(lambda: self.client.show_episodes(show_id, limit=limit, offset=offset))

    def playlist_items(
        self,
        playlist_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        return self._call(
            lambda: self.client.playlist_items(playlist_id, limit=limit, offset=offset)
        )

    def replace_playlist_items(self, playlist_id: str, uris: list[str]) -> dict[str, Any]:
        return self._call(lambda: self.client.replace_playlist_items(playlist_id, uris))

    def playlist_snapshot(self, playlist_id: str) -> dict[str, Any]:
        return self._call(lambda: self.client.playlist_snapshot(playlist_id))

    def _call(self, operation: Callable[[], T]) -> T:
        observed = _as_utc(self.clock())
        state = self._cycle_limited
        if state is None:
            state = self.journal.active(now=observed)
        if state is not None:
            self._cycle_limited = state
            raise SpotifyRateLimitSuppressed(
                "Spotify request skipped because rate-limit backoff is active"
            )
        try:
            return operation()
        except SpotifyApiError as exc:
            if exc.status != 429:
                raise
            rate_limited_at = _as_utc(self.clock())
            state = self.journal.activate(
                observed_at=rate_limited_at,
                retry_after_seconds=exc.retry_after,
            )
            self._cycle_limited = state
            _emit_backoff(
                self.diagnostics,
                state,
                backoff_state="activated",
                occurred_at=rate_limited_at,
            )
            raise


def _emit_backoff(
    diagnostics: OperationalDiagnostics | None,
    state: SpotifyRateLimitState,
    *,
    backoff_state: str,
    occurred_at: datetime,
) -> None:
    if diagnostics is None:
        return
    details: dict[str, str | int] = {
        "http_status": 429,
        "retry_not_before": _format_display_timestamp(state.retry_not_before),
        "backoff_source": state.backoff_source,
        "backoff_state": backoff_state,
        "write_decision": "skipped",
    }
    if state.retry_after_seconds is not None:
        details["retry_after_seconds"] = state.retry_after_seconds
    diagnostics.emit(
        occurred_at=occurred_at,
        severity=DiagnosticSeverity.WARNING,
        component="spotify",
        event_name="spotify_rate_limit_backoff",
        details=details,
    )


def _active_message(state: SpotifyRateLimitState) -> str:
    return (
        "Spotify rate-limit backoff active until "
        f"{_format_display_timestamp(state.retry_not_before)}"
    )


def _retry_deadline(
    observed: datetime,
    *,
    retry_after_seconds: int | None,
) -> tuple[datetime, int | None, str]:
    if retry_after_seconds is not None and retry_after_seconds >= 0:
        try:
            return (
                observed + timedelta(seconds=retry_after_seconds),
                retry_after_seconds,
                "spotify_header",
            )
        except OverflowError:
            pass
    return (
        observed + timedelta(seconds=DEFAULT_SPOTIFY_RATE_LIMIT_FALLBACK_SECONDS),
        None,
        "fallback",
    )


def _state_from_row(row: sqlite3.Row) -> SpotifyRateLimitState:
    retry_after = row["retry_after_seconds"]
    if retry_after is not None and not isinstance(retry_after, int):
        raise PersistenceError("Spotify rate-limit backoff contained invalid retry_after_seconds")
    source = row["backoff_source"]
    if not isinstance(source, str):
        raise PersistenceError("Spotify rate-limit backoff contained invalid backoff_source")
    return SpotifyRateLimitState(
        observed_at=_parse_timestamp(_row_text(row, "observed_at")),
        retry_not_before=_parse_timestamp(_row_text(row, "retry_not_before")),
        retry_after_seconds=retry_after,
        backoff_source=source,
    )


def _format_storage_timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _format_display_timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PersistenceError("Spotify rate-limit backoff contained an invalid timestamp") from exc
    return _as_utc(parsed)


def _row_text(row: sqlite3.Row, key: str) -> str:
    value = row[key]
    if not isinstance(value, str):
        raise PersistenceError(f"Spotify rate-limit backoff contained invalid {key}")
    return value


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Spotify rate-limit timestamps must be timezone-aware")
    return value.astimezone(UTC)


def remaining_retry_after_seconds(state: SpotifyRateLimitState, *, now: datetime) -> int:
    """Return a non-negative whole-second remainder for status/testing purposes."""
    remaining = (state.retry_not_before - _as_utc(now)).total_seconds()
    return max(0, math.ceil(remaining))
