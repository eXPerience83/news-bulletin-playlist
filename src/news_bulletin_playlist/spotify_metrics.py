"""Bounded, fail-soft aggregates for outbound Spotify HTTP attempts."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from news_bulletin_playlist.persistence import DEFAULT_DB_PATH, PersistenceError, SQLiteStore

SPOTIFY_METRICS_RETENTION_DAYS = 30


class SpotifyWebApiOperation(StrEnum):
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
    def web_api_requests(self) -> int:
        return sum(metric.request_count for metric in self.metrics if metric.surface == "web_api")

    @property
    def oauth_refreshes(self) -> int:
        return sum(
            metric.request_count
            for metric in self.metrics
            if metric.surface == "oauth" and metric.operation == SpotifyOAuthOperation.TOKEN_REFRESH
        )

    @property
    def http_rate_limits(self) -> int:
        return sum(
            metric.request_count
            for metric in self.metrics
            if metric.surface == "web_api" and metric.http_status == 429
        )

    @property
    def quota_exceeded(self) -> int:
        return sum(
            metric.request_count
            for metric in self.metrics
            if metric.quota_reason is SpotifyQuotaReason.QUOTA_EXCEEDED
        )


class SpotifyRequestMetrics:
    """Persist minute-level aggregates without letting observability alter provider behavior."""

    def __init__(self, path: Path | str = DEFAULT_DB_PATH) -> None:
        self.path = Path(path)
        self._coverage_complete = True
        self._lock = threading.Lock()

    def initialize(self) -> None:
        try:
            SQLiteStore(self.path).initialize()
            with self._connection("read Spotify request metric coverage") as connection:
                row = connection.execute(
                    "SELECT incomplete_at FROM spotify_request_metrics_coverage WHERE singleton = 1"
                ).fetchone()
            if row is not None and _parse_timestamp(row["incomplete_at"]) >= datetime.now(
                UTC
            ) - timedelta(days=SPOTIFY_METRICS_RETENTION_DAYS):
                self._mark_incomplete(persist=False)
        except PersistenceError, ValueError:
            self._mark_incomplete()

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
        observed = _as_utc(now or datetime.now(UTC))
        coverage_since = observed - timedelta(hours=24)
        since = _bucket(coverage_since)
        try:
            with self._connection("read Spotify request metrics") as connection:
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
        except PersistenceError:
            self._mark_incomplete()
            return SpotifyRequestMetricsSnapshot((), coverage_complete=False)
        try:
            metrics = tuple(_metric_from_row(row) for row in rows)
            durable_gap = (
                coverage_row is not None
                and _parse_timestamp(coverage_row["incomplete_at"]) >= coverage_since
            )
        except KeyError, TypeError, ValueError:
            self._mark_incomplete()
            return SpotifyRequestMetricsSnapshot((), coverage_complete=False)
        return SpotifyRequestMetricsSnapshot(
            metrics,
            coverage_complete=self.coverage_complete and not durable_gap,
        )

    @property
    def coverage_complete(self) -> bool:
        with self._lock:
            return self._coverage_complete

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
        if http_status is not None and not 100 <= http_status <= 599:
            self._mark_incomplete()
            return
        observed = _as_utc(occurred_at or datetime.now(UTC))
        bucket = _bucket(observed)
        try:
            with self._connection("record Spotify request metric") as connection:
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
                    (_bucket(observed - timedelta(days=SPOTIFY_METRICS_RETENTION_DAYS)),),
                )
        except PersistenceError:
            self._mark_incomplete()

    def _connection(self, operation: str) -> _MetricsConnection:
        return _MetricsConnection(self.path, operation)

    def _mark_incomplete(self, *, persist: bool = True) -> None:
        with self._lock:
            self._coverage_complete = False
        if not persist:
            return
        try:
            with self._connection("persist Spotify request metric coverage") as connection:
                connection.execute(
                    """
                    INSERT INTO spotify_request_metrics_coverage (singleton, incomplete_at)
                    VALUES (1, ?)
                    ON CONFLICT(singleton) DO UPDATE SET incomplete_at = excluded.incomplete_at
                    """,
                    (_bucket(datetime.now(UTC)),),
                )
        except PersistenceError:
            return


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
        del exc, tb
        connection = self.connection
        if connection is None:
            return
        try:
            if exc_type is None:
                connection.commit()
            else:
                connection.rollback()
        except sqlite3.Error as db_exc:
            raise PersistenceError(f"{self.operation} failed for {self.path}: {db_exc}") from db_exc
        finally:
            connection.close()


def _metric_from_row(row: sqlite3.Row) -> SpotifyRequestMetric:
    surface = row["surface"]
    operation = row["operation"]
    if not isinstance(surface, str) or not isinstance(operation, str):
        raise ValueError("invalid Spotify request metric operation")
    if surface == "web_api":
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


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Spotify metric timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("invalid Spotify request metric coverage timestamp")
    return _as_utc(datetime.fromisoformat(value))
