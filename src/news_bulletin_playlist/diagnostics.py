"""Bounded, structured operational diagnostics persisted under the existing data model."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from news_bulletin_playlist.persistence import DEFAULT_DB_PATH, PersistenceError

DEFAULT_DIAGNOSTIC_RETENTION_DAYS = 30
DEFAULT_DIAGNOSTIC_MAX_EVENTS = 10_000
DEFAULT_DIAGNOSTIC_QUERY_LIMIT = 200
MAX_DIAGNOSTIC_QUERY_LIMIT = 500

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]+$")
_ALLOWED_DETAIL_KEYS = frozenset(
    {
        "applied_count",
        "candidate_count",
        "catalogue_calls",
        "desired_count",
        "duration_ms",
        "edition_count",
        "failure_class",
        "http_status",
        "limit",
        "match_reason",
        "matched_count",
        "next_state",
        "offset",
        "operation",
        "phase",
        "returned_count",
        "total",
        "unavailable_count",
        "verification_outcome",
        "write_decision",
    }
)
_ALLOWED_DETAIL_STRING_VALUES = {
    "failure_class": frozenset(
        {
            "api_error",
            "configuration_error",
            "desired_state_error",
            "pagination_error",
            "reconciliation_error",
            "response_shape_error",
            "snapshot_error",
            "transport_error",
            "unavailable_media",
            "verification_mismatch",
        }
    ),
    "match_reason": frozenset(
        {
            "ambiguous",
            "delayed_release_matched",
            "matched",
            "pending",
            "release_date_mismatch",
            "release_date_skew_rejected",
            "semantic_time_mismatch",
            "title_mismatch",
            "title_parse_failed",
        }
    ),
    "next_state": frozenset(
        {
            "connected",
            "disabled",
            "disconnected",
            "enabled",
            "expired",
            "invalid",
            "missing",
            "null",
            "present",
            "running",
            "scheduled",
            "stopped",
        }
    ),
    "operation": frozenset(
        {
            "playlist_items",
            "replace_items",
            "snapshot",
        }
    ),
    "phase": frozenset(
        {
            "authorization",
            "collection",
            "complete",
            "configuration",
            "desired_state",
            "matching",
            "persistence",
            "prewrite",
            "readback",
            "reconciliation",
            "retention",
            "scheduler",
            "verification",
            "write",
        }
    ),
    "verification_outcome": frozenset(
        {
            "attested",
            "degraded",
            "failed",
            "mismatch",
            "skipped",
            "unavailable",
            "verified",
        }
    ),
    "write_decision": frozenset(
        {
            "applied",
            "blocked",
            "preserved",
            "retry",
            "skipped",
            "unchanged",
        }
    ),
}

type DiagnosticValue = str | int | bool | None


class DiagnosticSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class DiagnosticEvent:
    event_id: int
    occurred_at: datetime
    severity: DiagnosticSeverity
    component: str
    event_name: str
    cycle_id: str | None
    source_id: str | None
    playlist_id: str | None
    details: dict[str, DiagnosticValue]


@dataclass(frozen=True, slots=True)
class DiagnosticRetentionResult:
    age_deleted: int
    overflow_deleted: int

    @property
    def total_deleted(self) -> int:
        return self.age_deleted + self.overflow_deleted


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS diagnostic_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('INFO', 'WARNING', 'ERROR')),
    component TEXT NOT NULL,
    event_name TEXT NOT NULL,
    cycle_id TEXT,
    source_id TEXT,
    playlist_id TEXT,
    details_json TEXT NOT NULL
)
"""

_INDEXES = (
    """
    CREATE INDEX IF NOT EXISTS diagnostic_events_occurred_at_idx
    ON diagnostic_events (occurred_at DESC, event_id DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS diagnostic_events_source_idx
    ON diagnostic_events (source_id, occurred_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS diagnostic_events_playlist_idx
    ON diagnostic_events (playlist_id, occurred_at DESC)
    """,
)


class DiagnosticEventStore:
    """Persist a compact allow-listed event stream in the application's SQLite database."""

    def __init__(
        self,
        path: Path | str = DEFAULT_DB_PATH,
        *,
        retention_days: int = DEFAULT_DIAGNOSTIC_RETENTION_DAYS,
        max_events: int = DEFAULT_DIAGNOSTIC_MAX_EVENTS,
    ) -> None:
        if retention_days <= 0:
            raise ValueError("diagnostic retention_days must be positive")
        if max_events <= 0:
            raise ValueError("diagnostic max_events must be positive")
        self.path = Path(path)
        self.retention_days = retention_days
        self.max_events = max_events

    def initialize(self) -> None:
        """Create the diagnostic table and indexes idempotently in the existing DB file."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PersistenceError(
                f"initialize diagnostics failed for {self.path}: {exc}"
            ) from exc
        with self._connection("initialize diagnostics") as connection:
            connection.execute(_SCHEMA_SQL)
            for statement in _INDEXES:
                connection.execute(statement)

    def record(
        self,
        *,
        occurred_at: datetime,
        severity: DiagnosticSeverity,
        component: str,
        event_name: str,
        cycle_id: str | None = None,
        source_id: str | None = None,
        playlist_id: str | None = None,
        details: dict[str, DiagnosticValue] | None = None,
    ) -> int:
        """Append one sanitized event and enforce both age and row-count bounds."""
        timestamp = _format_timestamp(occurred_at)
        component_value = _identifier(component, "component", max_length=64)
        event_value = _identifier(event_name, "event_name", max_length=96)
        cycle_value = _optional_identifier(cycle_id, "cycle_id", max_length=128)
        source_value = _optional_identifier(source_id, "source_id", max_length=128)
        playlist_value = _optional_identifier(playlist_id, "playlist_id", max_length=128)
        normalized_details = _normalize_details(details or {})
        details_json = json.dumps(
            normalized_details,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        cutoff = _format_timestamp(occurred_at - timedelta(days=self.retention_days))
        with self._connection("record diagnostic event") as connection:
            cursor = connection.execute(
                """
                INSERT INTO diagnostic_events (
                    occurred_at,
                    severity,
                    component,
                    event_name,
                    cycle_id,
                    source_id,
                    playlist_id,
                    details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    severity.value,
                    component_value,
                    event_value,
                    cycle_value,
                    source_value,
                    playlist_value,
                    details_json,
                ),
            )
            event_id = cursor.lastrowid
            if not isinstance(event_id, int):
                raise PersistenceError("diagnostic event insert did not return an integer id")
            connection.execute(
                "DELETE FROM diagnostic_events WHERE occurred_at < ?",
                (cutoff,),
            )
            self._delete_overflow(connection, self.max_events)
        return event_id

    def list_events(
        self,
        *,
        since: datetime | None = None,
        severity: DiagnosticSeverity | None = None,
        source_id: str | None = None,
        playlist_id: str | None = None,
        limit: int = DEFAULT_DIAGNOSTIC_QUERY_LIMIT,
    ) -> tuple[DiagnosticEvent, ...]:
        """Return newest-first bounded diagnostic events with simple operational filters."""
        if not 1 <= limit <= MAX_DIAGNOSTIC_QUERY_LIMIT:
            raise ValueError(
                f"diagnostic query limit must be between 1 and {MAX_DIAGNOSTIC_QUERY_LIMIT}"
            )
        clauses: list[str] = []
        values: list[object] = []
        if since is not None:
            clauses.append("occurred_at >= ?")
            values.append(_format_timestamp(since))
        if severity is not None:
            clauses.append("severity = ?")
            values.append(severity.value)
        if source_id is not None:
            clauses.append("source_id = ?")
            values.append(_identifier(source_id, "source_id", max_length=128))
        if playlist_id is not None:
            clauses.append("playlist_id = ?")
            values.append(_identifier(playlist_id, "playlist_id", max_length=128))

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(limit)
        query = (
            "SELECT event_id, occurred_at, severity, component, event_name, "
            "cycle_id, source_id, playlist_id, details_json "
            f"FROM diagnostic_events{where} "
            "ORDER BY occurred_at DESC, event_id DESC LIMIT ?"
        )
        with self._connection("list diagnostic events") as connection:
            rows = connection.execute(query, values).fetchall()
        return tuple(_event_from_row(row) for row in rows)

    def prune(
        self,
        *,
        now: datetime,
        retention_days: int | None = None,
        max_events: int | None = None,
    ) -> DiagnosticRetentionResult:
        """Apply explicit time and row bounds, returning deterministic deletion counts."""
        days = self.retention_days if retention_days is None else retention_days
        maximum = self.max_events if max_events is None else max_events
        if days <= 0:
            raise ValueError("diagnostic retention_days must be positive")
        if maximum <= 0:
            raise ValueError("diagnostic max_events must be positive")
        cutoff = _format_timestamp(now - timedelta(days=days))
        with self._connection("prune diagnostic events") as connection:
            age_cursor = connection.execute(
                "DELETE FROM diagnostic_events WHERE occurred_at < ?",
                (cutoff,),
            )
            age_deleted = max(age_cursor.rowcount, 0)
            overflow_deleted = self._delete_overflow(connection, maximum)
        return DiagnosticRetentionResult(
            age_deleted=age_deleted,
            overflow_deleted=overflow_deleted,
        )

    @staticmethod
    def _delete_overflow(connection: sqlite3.Connection, max_events: int) -> int:
        cursor = connection.execute(
            """
            DELETE FROM diagnostic_events
            WHERE event_id IN (
                SELECT event_id
                FROM diagnostic_events
                ORDER BY occurred_at DESC, event_id DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (max_events,),
        )
        return max(cursor.rowcount, 0)

    def _connection(self, operation: str):  # type: ignore[no-untyped-def]
        return _DiagnosticConnection(self.path, operation)


class _DiagnosticConnection:
    def __init__(self, path: Path, operation: str) -> None:
        self.path = path
        self.operation = operation
        self.connection: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(self.path, timeout=5.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("BEGIN")
        except sqlite3.Error as exc:
            raise PersistenceError(
                f"{self.operation} failed for {self.path}: {exc}"
            ) from exc
        self.connection = connection
        return connection

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        connection = self.connection
        if connection is None:
            return
        try:
            if exc_type is None:
                connection.commit()
            else:
                connection.rollback()
        except sqlite3.Error as sqlite_exc:
            raise PersistenceError(
                f"{self.operation} failed for {self.path}: {sqlite_exc}"
            ) from sqlite_exc
        finally:
            connection.close()


def _normalize_details(
    details: dict[str, DiagnosticValue],
) -> dict[str, DiagnosticValue]:
    unknown = sorted(set(details) - _ALLOWED_DETAIL_KEYS)
    if unknown:
        raise ValueError(f"diagnostic details contain unknown key: {unknown[0]}")
    normalized: dict[str, DiagnosticValue] = {}
    for key, value in details.items():
        if isinstance(value, bool) or value is None:
            normalized[key] = value
        elif isinstance(value, int):
            if value < 0:
                raise ValueError(f"diagnostic detail {key} must be non-negative")
            normalized[key] = value
        elif isinstance(value, str):
            allowed_values = _ALLOWED_DETAIL_STRING_VALUES.get(key)
            if allowed_values is None or value not in allowed_values:
                raise ValueError(f"diagnostic detail {key} contains an unsupported label")
            normalized[key] = value
        else:
            raise ValueError(f"diagnostic detail {key} has unsupported value type")
    return normalized


def _event_from_row(row: sqlite3.Row) -> DiagnosticEvent:
    event_id = row["event_id"]
    if not isinstance(event_id, int):
        raise PersistenceError("diagnostic event id is not an integer")
    raw_details = row["details_json"]
    if not isinstance(raw_details, str):
        raise PersistenceError("diagnostic details are not text")
    try:
        decoded = json.loads(raw_details)
    except json.JSONDecodeError as exc:
        raise PersistenceError("diagnostic details contain invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise PersistenceError("diagnostic details JSON is not an object")
    details = _normalize_details(decoded)
    return DiagnosticEvent(
        event_id=event_id,
        occurred_at=_parse_timestamp(_row_text(row, "occurred_at")),
        severity=DiagnosticSeverity(_row_text(row, "severity")),
        component=_row_text(row, "component"),
        event_name=_row_text(row, "event_name"),
        cycle_id=_optional_text(row["cycle_id"]),
        source_id=_optional_text(row["source_id"]),
        playlist_id=_optional_text(row["playlist_id"]),
        details=details,
    )


def _identifier(value: str, label: str, *, max_length: int) -> str:
    result = value.strip()
    if not result or len(result) > max_length or _IDENTIFIER.fullmatch(result) is None:
        raise ValueError(f"diagnostic {label} contains an invalid identifier")
    return result


def _optional_identifier(value: str | None, label: str, *, max_length: int) -> str | None:
    return None if value is None else _identifier(value, label, max_length=max_length)


def _format_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("diagnostic timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PersistenceError("diagnostic timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PersistenceError("diagnostic timestamp is not timezone-aware")
    return parsed.astimezone(UTC)


def _row_text(row: sqlite3.Row, key: str) -> str:
    value = row[key]
    if not isinstance(value, str):
        raise PersistenceError(f"diagnostic column {key} is not text")
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PersistenceError("diagnostic optional column is not text")
    return value
