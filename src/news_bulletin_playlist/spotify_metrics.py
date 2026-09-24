"""Bounded, fail-soft aggregates for outbound Spotify HTTP attempts."""

from __future__ import annotations

import sqlite3
import threading
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from news_bulletin_playlist.persistence import DEFAULT_DB_PATH, PersistenceError, SQLiteStore

SPOTIFY_METRICS_RETENTION_DAYS = 30
Clock = Callable[[], datetime]


class SpotifyWebApiOperation(StrEnum):
    SHOW_DETAILS = "show_details"
    SHOW_EPISODES = "show_episodes"
    SEARCH_SHOWS = "search_shows"
    CREATE_PLAYLIST = "create_playlist"
    CURRENT_USER = "current_user"
    PLAYLIST_DETAILS = "playlist_details"
    CHANGE_PLAYLIST_DETAILS = "change_playlist_details"
    UPLOAD_PLAYLIST_COVER = "upload_playlist_cover"
    REPLACE_PLAYLIST_ITEMS = "replace_playlist_items"
    PLAYLIST_ITEMS = "playlist_items"
    PLAYLIST_SNAPSHOT = "playlist_snapshot"


class SpotifyOAuthOperation(StrEnum):
    TOKEN_EXCHANGE = "token_exchange"
    TOKEN_REFRESH = "token_refresh"


class SpotifyPublicOperation(StrEnum):
    EMBED_SHOW = "embed_show"
    OEMBED = "oembed"


class SpotifyRequestOutcome(StrEnum):
    SUCCESS = "success"
    HTTP_ERROR = "http_error"
    TRANSPORT_ERROR = "transport_error"
    RESPONSE_ERROR = "response_error"


class SpotifyQuotaReason(StrEnum):
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"


@dataclass(frozen=True, slots=True)
class SpotifyRequestMetric:
    surface: str
    operation: str
    outcome: SpotifyRequestOutcome
    http_status: int | None
    quota_reason: SpotifyQuotaReason | None
    request_count: int


@dataclass(frozen=True, slots=True)
class SpotifyRequestMetricsSnapshot:
    metrics: tuple[SpotifyRequestMetric, ...]
    coverage_complete: bool

    @property
    def public_requests(self) -> int:
        return sum(metric.request_count for metric in self.metrics if metric.surface == "public")

    @property
    def web_api_requests(self) -> int:
        return sum(metric.request_count for metric in self.metrics if metric.surface == "web_api")

    @property
    def oauth_requests(self) -> int:
        return sum(metric.request_count for metric in self.metrics if metric.surface == "oauth")

    @property
    def oauth_refreshes(self) -> int:
        return sum(
            metric.request_count
            for metric in self.metrics
            if metric.surface == "oauth" and metric.operation == SpotifyOAuthOperation.TOKEN_REFRESH
        )

    @property
    def http_rate_limits(self) -> int:
        return sum(metric.request_count for metric in self.metrics if metric.http_status == 429)

    @property
    def quota_exceeded(self) -> int:
        return sum(
            metric.request_count
            for metric in self.metrics
            if metric.quota_reason is SpotifyQuotaReason.QUOTA_EXCEEDED
        )


@dataclass(frozen=True, slots=True)
class SpotifyRequestScopeSnapshot:
    """Process-local request counts for one explicitly scoped unit of work."""

    public: tuple[tuple[SpotifyPublicOperation, int], ...] = ()
    web_api: tuple[tuple[SpotifyWebApiOperation, int], ...] = ()
    oauth: tuple[tuple[SpotifyOAuthOperation, int], ...] = ()
    http_rate_limits: int = 0
    quota_exceeded: int = 0

    @property
    def public_requests(self) -> int:
        return sum(count for _operation, count in self.public)

    @property
    def web_api_requests(self) -> int:
        return sum(count for _operation, count in self.web_api)

    @property
    def oauth_requests(self) -> int:
        return sum(count for _operation, count in self.oauth)

    @property
    def total_requests(self) -> int:
        return self.public_requests + self.web_api_requests + self.oauth_requests

    def public_count(self, operation: SpotifyPublicOperation) -> int:
        return next((count for candidate, count in self.public if candidate is operation), 0)

    def web_api_count(self, operation: SpotifyWebApiOperation) -> int:
        return next((count for candidate, count in self.web_api if candidate is operation), 0)

    def oauth_count(self, operation: SpotifyOAuthOperation) -> int:
        return next((count for candidate, count in self.oauth if candidate is operation), 0)


class SpotifyRequestScope:
    """Mutable process-local accumulator exposed only through immutable snapshots."""

    def __init__(self) -> None:
        self._public: Counter[SpotifyPublicOperation] = Counter()
        self._web_api: Counter[SpotifyWebApiOperation] = Counter()
        self._oauth: Counter[SpotifyOAuthOperation] = Counter()
        self._http_rate_limits = 0
        self._quota_exceeded = 0
        self._lock = threading.Lock()

    def record(
        self,
        *,
        surface: str,
        operation: str,
        http_status: int | None,
        quota_reason: SpotifyQuotaReason | None,
    ) -> None:
        with self._lock:
            if surface == "public":
                self._public[SpotifyPublicOperation(operation)] += 1
                if http_status == 429:
                    self._http_rate_limits += 1
            elif surface == "web_api":
                self._web_api[SpotifyWebApiOperation(operation)] += 1
                if http_status == 429:
                    self._http_rate_limits += 1
                if quota_reason is SpotifyQuotaReason.QUOTA_EXCEEDED:
                    self._quota_exceeded += 1
            elif surface == "oauth":
                self._oauth[SpotifyOAuthOperation(operation)] += 1
                if http_status == 429:
                    self._http_rate_limits += 1
            else:
                raise ValueError("invalid Spotify request metric surface")

    def snapshot(self) -> SpotifyRequestScopeSnapshot:
        with self._lock:
            return SpotifyRequestScopeSnapshot(
                public=tuple(sorted(self._public.items(), key=lambda item: item[0].value)),
                web_api=tuple(sorted(self._web_api.items(), key=lambda item: item[0].value)),
                oauth=tuple(sorted(self._oauth.items(), key=lambda item: item[0].value)),
                http_rate_limits=self._http_rate_limits,
                quota_exceeded=self._quota_exceeded,
            )


class SpotifyRequestMetrics:
    """Persist minute-level aggregates without letting observability alter provider behavior."""

    def __init__(self, path: Path | str = DEFAULT_DB_PATH, *, clock: Clock | None = None) -> None:
        self.path = Path(path)
        self._clock = _utc_now if clock is None else clock
        self._pending_incomplete_at: datetime | None = None
        self._lock = threading.Lock()
        self._active_scope: ContextVar[SpotifyRequestScope | None] = ContextVar(
            f"spotify_request_scope_{id(self)}",
            default=None,
        )

    def initialize(self) -> None:
        try:
            SQLiteStore(self.path).initialize()
        except PersistenceError, ValueError:
            self._mark_incomplete(_as_utc(self._clock()))

    @contextmanager
    def request_scope(self) -> Iterator[SpotifyRequestScope]:
        """Count requests in this execution context without capturing concurrent admin traffic."""
        current = self._active_scope.get()
        if current is not None:
            # Nested instrumentation belongs to the same unit of work. Sharing the accumulator
            # avoids double counting while keeping nesting harmless to provider behavior.
            yield current
            return
        scope = SpotifyRequestScope()
        token = self._active_scope.set(scope)
        try:
            yield scope
        finally:
            self._active_scope.reset(token)

    def record_public(
        self,
        operation: SpotifyPublicOperation,
        outcome: SpotifyRequestOutcome,
        *,
        http_status: int | None = None,
        occurred_at: datetime | None = None,
    ) -> None:
        self._record(
            surface="public",
            operation=operation.value,
            outcome=outcome,
            http_status=http_status,
            quota_reason=None,
            occurred_at=occurred_at,
        )

    def record_web_api(
        self,
        operation: SpotifyWebApiOperation,
        outcome: SpotifyRequestOutcome,
        *,
        http_status: int | None = None,
        quota_reason: SpotifyQuotaReason | None = None,
        occurred_at: datetime | None = None,
    ) -> None:
        self._record(
            surface="web_api",
            operation=operation.value,
            outcome=outcome,
            http_status=http_status,
            quota_reason=quota_reason,
            occurred_at=occurred_at,
        )

    def record_oauth(
        self,
        operation: SpotifyOAuthOperation,
        outcome: SpotifyRequestOutcome,
        *,
        http_status: int | None = None,
        occurred_at: datetime | None = None,
    ) -> None:
        self._record(
            surface="oauth",
            operation=operation.value,
            outcome=outcome,
            http_status=http_status,
            quota_reason=None,
            occurred_at=occurred_at,
        )

    def last_24_hours(self, *, now: datetime | None = None) -> SpotifyRequestMetricsSnapshot:
        observed = _as_utc(now or self._clock())
        coverage_since = observed - timedelta(hours=24)
        since = _bucket(coverage_since)
        try:
            with self._connection("read Spotify request metrics") as connection:
                persisted_pending = self._persist_pending_incomplete(connection)
                rows = connection.execute(
                    """
                    SELECT surface, operation, outcome, http_status, quota_reason,
                           SUM(request_count) AS request_count
                    FROM spotify_request_metrics
                    WHERE bucket_at >= ?
                    GROUP BY surface, operation, outcome, http_status, quota_reason
                    ORDER BY surface, operation, outcome, http_status, quota_reason
                    """,
                    (since,),
                ).fetchall()
                coverage_row = connection.execute(
                    "SELECT incomplete_at FROM spotify_request_metrics_coverage WHERE singleton = 1"
                ).fetchone()
            self._clear_persisted_pending(persisted_pending)
        except PersistenceError:
            self._mark_incomplete(observed)
            return SpotifyRequestMetricsSnapshot((), coverage_complete=False)
        try:
            metrics = tuple(_metric_from_row(row) for row in rows)
            durable_gap = (
                coverage_row is not None
                and _parse_timestamp(coverage_row["incomplete_at"]) >= coverage_since
            )
        except KeyError, TypeError, ValueError:
            self._mark_incomplete(observed)
            return SpotifyRequestMetricsSnapshot((), coverage_complete=False)
        pending_gap = self._pending_incomplete_in_window(coverage_since)
        return SpotifyRequestMetricsSnapshot(
            metrics,
            coverage_complete=not durable_gap and not pending_gap,
        )

    def _record(
        self,
        *,
        surface: str,
        operation: str,
        outcome: SpotifyRequestOutcome,
        http_status: int | None,
        quota_reason: SpotifyQuotaReason | None,
        occurred_at: datetime | None,
    ) -> None:
        self._record_active_scope(
            surface=surface,
            operation=operation,
            http_status=http_status,
            quota_reason=quota_reason,
        )
        if http_status is not None and not 100 <= http_status <= 599:
            self._mark_incomplete(_as_utc(occurred_at or self._clock()))
            return
        observed = _as_utc(occurred_at or self._clock())
        bucket = _bucket(observed)
        try:
            with self._connection("record Spotify request metric") as connection:
                persisted_pending = self._persist_pending_incomplete(connection)
                connection.execute(
                    """
                    INSERT INTO spotify_request_metrics (
                        bucket_at, surface, operation, outcome, http_status,
                        quota_reason, request_count
                    ) VALUES (?, ?, ?, ?, ?, ?, 1)
                    ON CONFLICT(bucket_at, surface, operation, outcome, http_status, quota_reason)
                    DO UPDATE SET request_count = request_count + 1
                    """,
                    (
                        bucket,
                        surface,
                        operation,
                        outcome.value,
                        -1 if http_status is None else http_status,
                        "" if quota_reason is None else quota_reason.value,
                    ),
                )
                connection.execute(
                    "DELETE FROM spotify_request_metrics WHERE bucket_at < ?",
                    (_bucket(observed - timedelta(days=SPOTIFY_METRICS_RETENTION_DAYS)),),
                )
                connection.execute(
                    "DELETE FROM spotify_request_metrics_coverage WHERE incomplete_at < ?",
                    (
                        _coverage_timestamp(
                            observed - timedelta(days=SPOTIFY_METRICS_RETENTION_DAYS)
                        ),
                    ),
                )
            self._clear_persisted_pending(persisted_pending)
        except PersistenceError:
            self._mark_incomplete(observed)

    def _record_active_scope(
        self,
        *,
        surface: str,
        operation: str,
        http_status: int | None,
        quota_reason: SpotifyQuotaReason | None,
    ) -> None:
        scope = self._active_scope.get()
        if scope is None:
            return
        try:
            scope.record(
                surface=surface,
                operation=operation,
                http_status=http_status,
                quota_reason=quota_reason,
            )
        except TypeError, ValueError:
            # Cycle accounting is observability only and may never alter provider behavior.
            return

    def _connection(self, operation: str) -> _MetricsConnection:
        return _MetricsConnection(self.path, operation)

    def _mark_incomplete(self, observed_at: datetime) -> None:
        observed = _as_utc(observed_at)
        with self._lock:
            pending = self._pending_incomplete_at
            if pending is None or observed > pending:
                self._pending_incomplete_at = observed
        try:
            with self._connection("persist Spotify request metric coverage") as connection:
                persisted_pending = self._persist_pending_incomplete(connection)
            self._clear_persisted_pending(persisted_pending)
        except PersistenceError:
            return

    def _persist_pending_incomplete(self, connection: sqlite3.Connection) -> datetime | None:
        with self._lock:
            pending = self._pending_incomplete_at
        if pending is None:
            return None
        connection.execute(
            """
            INSERT INTO spotify_request_metrics_coverage (singleton, incomplete_at)
            VALUES (1, ?)
            ON CONFLICT(singleton) DO UPDATE SET incomplete_at = excluded.incomplete_at
            WHERE excluded.incomplete_at > spotify_request_metrics_coverage.incomplete_at
            """,
            (_coverage_timestamp(pending),),
        )
        return pending

    def _clear_persisted_pending(self, persisted: datetime | None) -> None:
        if persisted is None:
            return
        with self._lock:
            if self._pending_incomplete_at == persisted:
                self._pending_incomplete_at = None

    def _pending_incomplete_in_window(self, coverage_since: datetime) -> bool:
        with self._lock:
            pending = self._pending_incomplete_at
        return pending is not None and pending >= coverage_since


class _MetricsConnection:
    def __init__(self, path: Path, operation: str) -> None:
        self.path = path
        self.operation = operation
        self.connection: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(self.path, timeout=5.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("BEGIN")
        except sqlite3.Error as exc:
            raise PersistenceError(f"{self.operation} failed for {self.path}: {exc}") from exc
        self.connection = connection
        return connection

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        del tb
        connection = self.connection
        if connection is None:
            return
        db_error = exc if isinstance(exc, sqlite3.Error) else None
        try:
            if exc_type is None:
                connection.commit()
            else:
                connection.rollback()
        except sqlite3.Error as cleanup_error:
            db_error = cleanup_error
        finally:
            try:
                connection.close()
            except sqlite3.Error as close_error:
                if db_error is None:
                    db_error = close_error
        if db_error is not None:
            raise PersistenceError(
                f"{self.operation} failed for {self.path}: {db_error}"
            ) from db_error


def _metric_from_row(row: sqlite3.Row) -> SpotifyRequestMetric:
    surface = row["surface"]
    operation = row["operation"]
    if not isinstance(surface, str) or not isinstance(operation, str):
        raise ValueError("invalid Spotify request metric operation")
    if surface == "public":
        SpotifyPublicOperation(operation)
    elif surface == "web_api":
        SpotifyWebApiOperation(operation)
    elif surface == "oauth":
        SpotifyOAuthOperation(operation)
    else:
        raise ValueError("invalid Spotify request metric surface")
    status = row["http_status"]
    if isinstance(status, bool) or not isinstance(status, int) or not (-1 <= status <= 599):
        raise ValueError("invalid Spotify request metric status")
    reason = row["quota_reason"]
    if not isinstance(reason, str):
        raise ValueError("invalid Spotify request metric reason")
    raw_count = row["request_count"]
    if isinstance(raw_count, bool) or not isinstance(raw_count, int) or raw_count <= 0:
        raise ValueError("invalid Spotify request metric count")
    return SpotifyRequestMetric(
        surface=surface,
        operation=operation,
        outcome=SpotifyRequestOutcome(str(row["outcome"])),
        http_status=None if status == -1 else status,
        quota_reason=None if reason == "" else SpotifyQuotaReason(reason),
        request_count=raw_count,
    )


def _bucket(value: datetime) -> str:
    return _as_utc(value).replace(second=0, microsecond=0).isoformat(timespec="seconds")


def _coverage_timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat(timespec="microseconds")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Spotify metric timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("invalid Spotify request metric coverage timestamp")
    return _as_utc(datetime.fromisoformat(value))
