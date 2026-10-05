"""Bounded, structured operational diagnostics persisted under the existing data model."""

from __future__ import annotations

import base64
import binascii
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from news_bulletin_playlist.persistence import DEFAULT_DB_PATH, PersistenceError
from news_bulletin_playlist.spotify.identifiers import is_spotify_id

DEFAULT_DIAGNOSTIC_RETENTION_DAYS = 30
DEFAULT_DIAGNOSTIC_MAX_EVENTS = 100_000
DEFAULT_DIAGNOSTIC_QUERY_LIMIT = 200
MAX_DIAGNOSTIC_QUERY_LIMIT = 500
MAX_DIAGNOSTIC_EXPORT_EVENTS = 10_000

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]+$")
_ALLOWED_DETAIL_KEYS = frozenset(
    {
        "10_to_15m",
        "15_to_30m",
        "30_to_60m",
        "60m_plus",
        "accepted_count",
        "applied_count",
        "backoff_source",
        "backoff_state",
        "api_request_count",
        "desired_count",
        "duration_ms",
        "duration_unavailable_count",
        "episode_count",
        "excluded_count",
        "exception_class",
        "filter_rejected_count",
        "duration_seconds",
        "eligibility_reason",
        "failure_class",
        "failure_reason",
        "http_status",
        "parser_fallback_count",
        "temporal_strategy",
        "public_release_mode",
        "public_release_disabled_count",
        "temporal_supporting_rows",
        "temporal_conflicts",
        "timestamp_title_count",
        "timestamp_public_count",
        "timestamp_first_seen_count",
        "public_release_accepted_count",
        "public_release_rejected_future_count",
        "public_release_catalogue_conflict_count",
        "public_release_invalid_count",
        "public_release_identity_mismatch_count",
        "public_release_shadowed_count",
        "pending_snapshot_created",
        "pending_snapshot_resolution",
        "provider",
        "refresh_reason",
        "service",
        "false_negative_reason",
        "request_item_count",
        "request_payload_bytes",
        "valid_uri_count",
        "invalid_uri_count",
        "duplicate_count",
        "desired_fingerprint",
        "destination_verification_kind",
        "detector_changed",
        "detector_false_negative",
        "detector_ok",
        "local_skip",
        "refreshed",
        "removed_count",
        "stale_escalation",
        "stage",
        "detector_changed_count",
        "detector_failure_count",
        "detector_degraded_change_count",
        "detector_fallback_outcome",
        "detector_outcome",
        "detector_response_reason",
        "detector_schema_version",
        "public_response_capture",
        "public_response_surface",
        "public_response_content_type",
        "public_response_size_bytes",
        "public_response_sha256",
        "public_response_truncated",
        "public_response_body_b64",
        "detector_source_count",
        "detector_unchanged_count",
        "oembed_matches",
        "public_release_date",
        "public_release_date_shape",
        "spotify_episode_id",
        "spotify_episode_title",
        "spotify_oembed_title",
        "spotify_previous_episode_id",
        "spotify_previous_episode_title",
        "consecutive_failure_count",
        "last_failure_at",
        "limit",
        "metadata_outcome",
        "max_seconds",
        "min_seconds",
        "next_state",
        "offset",
        "operation",
        "outcome",
        "phase",
        "policy_exception",
        "retry_after_seconds",
        "retry_not_before",
        "returned_count",
        "sample_count",
        "total",
        "under_10m",
        "unavailable_count",
        "unavailable_media_count",
        "unavailable_media_phase",
        "verification_outcome",
        "write_decision",
        "write_outcome",
    }
)
_ALLOWED_DETAIL_STRING_VALUES = {
    "public_release_mode": frozenset({"instant", "wall_time", "disabled"}),
    "temporal_strategy": frozenset(
        {
            "auto",
            "full_datetime",
            "clock_catalogue_day",
            "partial_date_catalogue_day",
            "explicit_utc",
            "disabled",
        }
    ),
    "backoff_source": frozenset({"fallback", "spotify_header"}),
    "backoff_state": frozenset({"activated", "active", "expired"}),
    "exception_class": frozenset(
        {
            "io_error",
            "persistence_error",
            "runtime_error",
            "sqlite_error",
            "timeout_error",
            "type_error",
            "unexpected_error",
            "value_error",
        }
    ),
    "failure_class": frozenset(
        {
            "api_error",
            "configuration_error",
            "desired_state_error",
            "pagination_error",
            "persistence_error",
            "reconciliation_error",
            "response_shape_error",
            "snapshot_error",
            "transport_error",
            "unavailable_media",
            "verification_mismatch",
        }
    ),
    "failure_reason": frozenset(
        {
            "authorization_context_changed",
            "configuration_invalid",
            "desired_state_invalid",
            "local_skip_authority_changed",
            "playlist_items_api_error",
            "playlist_items_transport_error",
            "playlist_pagination_invalid",
            "playlist_pagination_truncated",
            "prewrite_desired_iteration_limit",
            "prewrite_mapping_validation_failed",
            "replace_items_api_error",
            "replace_items_transport_error",
            "response_shape_invalid",
            "snapshot_api_error",
            "snapshot_mismatch",
            "snapshot_propagation_pending",
            "snapshot_transport_error",
            "unavailable_media",
            "unknown_reconciliation_error",
            "verification_mismatch",
        }
    ),
    "refresh_reason": frozenset({"bootstrap", "change", "inactivity_audit", "stale_escalation"}),
    "false_negative_reason": frozenset(
        {"api_semantic_newer", "api_release_day_newer", "detector_id_missing"}
    ),
    "destination_verification_kind": frozenset({"exact", "degraded"}),
    "detector_outcome": frozenset(
        {
            "bootstrap",
            "changed",
            "configuration_failure",
            "degraded_audit",
            "http_failure",
            "metadata_drift",
            "oembed_failure",
            "persistence_failure",
            "recovered",
            "response_failure",
            "stale_ignored",
            "transport_failure",
            "unchanged",
        }
    ),
    "detector_fallback_outcome": frozenset(
        {"oembed_failed", "title_baseline", "title_changed", "title_unchanged"}
    ),
    "public_response_surface": frozenset({"embed", "oembed"}),
    "public_release_date_shape": frozenset(
        {
            "missing",
            "string",
            "object_iso_string",
            "unsupported_string",
            "unsupported_object",
            "unsupported_array",
            "unsupported_number",
            "unsupported_boolean",
        }
    ),
    "detector_response_reason": frozenset(
        {
            "response_too_large",
            "embed_not_utf8",
            "embed_html_parse",
            "next_data_count",
            "next_data_json",
            "embed_shape",
            "embed_entity_type",
            "embed_field",
            "embed_entity_validation",
            "oembed_json",
            "oembed_shape",
            "oembed_title",
        }
    ),
    "eligibility_reason": frozenset(
        {
            "duration_exception",
            "duration_exceeds_default_max",
            "duration_below_default_min",
            "duration_exceeds_exception_max",
            "duration_metadata_unavailable",
            "duration_within_default_max",
        }
    ),
    "policy_exception": frozenset({"ser_morning_0800"}),
    "next_state": frozenset(
        {
            "connected",
            "disabled",
            "deleted",
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
            "updated",
        }
    ),
    "operation": frozenset(
        {
            "playlist_configuration",
            "source_configuration",
            "playlist_cover_upload",
            "playlist_items",
            "playlist_metadata",
            "replace_items",
            "snapshot",
        }
    ),
    # Keep the pre-inline label readable in durable history; the runtime no longer emits it.
    "metadata_outcome": frozenset({"applied_with_attribution", "applied_without_attribution"}),
    "outcome": frozenset({"applied", "failed"}),
    "pending_snapshot_created": frozenset({"exact", "partial"}),
    "pending_snapshot_resolution": frozenset(
        {
            "confirmed",
            "still_baseline",
            "superseded_desired",
            "expired",
            "invalidated_context",
            "unexpected_snapshot",
        }
    ),
    "stage": frozenset({"engine_execution"}),
    "unavailable_media_phase": frozenset({"prewrite", "readback", "pending_confirmation"}),
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
    "write_outcome": frozenset(
        {"not_attempted", "applied", "verified", "unknown", "skipped", "rejected"}
    ),
    "provider": frozenset({"spotify"}),
    "service": frozenset({"spotify"}),
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
class DiagnosticRetentionStatus:
    event_count: int
    oldest_occurred_at: datetime | None
    newest_occurred_at: datetime | None
    capacity_limit_reached: bool


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

_DELETE_LEGACY_DURATION_PROFILES_SQL = """
DELETE FROM diagnostic_events
WHERE event_name = 'duration_profile'
  AND instr(details_json, '"under_10m":') = 0
"""


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
            raise PersistenceError(f"initialize diagnostics failed for {self.path}: {exc}") from exc
        # Existing CREATE IF NOT EXISTS checks establish a WAL read snapshot. Reserve
        # the writer before them so a concurrent scheduler/Admin event cannot make
        # that snapshot stale before the legacy-profile DELETE (SQLITE_BUSY_SNAPSHOT).
        with self._connection("initialize diagnostics", immediate=True) as connection:
            connection.execute(_SCHEMA_SQL)
            for statement in _INDEXES:
                connection.execute(statement)
            # The legacy seven-bucket aggregate cannot be losslessly converted to the
            # canonical five-bucket profile. Drop only non-canonical duration profiles;
            # exact accepted-episode duration evidence is retained separately by #398.
            connection.execute(_DELETE_LEGACY_DURATION_PROFILES_SQL)

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
        clauses, values = _event_filter_query(
            since=since,
            severity=severity,
            source_id=source_id,
            playlist_id=playlist_id,
        )

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

    def list_events_with_truncation(
        self,
        *,
        since: datetime | None = None,
        severity: DiagnosticSeverity | None = None,
        source_id: str | None = None,
        playlist_id: str | None = None,
        limit: int = DEFAULT_DIAGNOSTIC_QUERY_LIMIT,
    ) -> tuple[tuple[DiagnosticEvent, ...], bool]:
        """Return one UI-bounded event snapshot and whether it contained an additional row."""
        return self._list_events_with_truncation(
            since=since,
            severity=severity,
            source_id=source_id,
            playlist_id=playlist_id,
            limit=limit,
            maximum=MAX_DIAGNOSTIC_QUERY_LIMIT,
            operation="list diagnostic events with truncation",
            label="diagnostic query limit",
        )

    def list_events_for_export_with_truncation(
        self,
        *,
        since: datetime | None = None,
        severity: DiagnosticSeverity | None = None,
        source_id: str | None = None,
        playlist_id: str | None = None,
        limit: int = MAX_DIAGNOSTIC_EXPORT_EVENTS,
    ) -> tuple[tuple[DiagnosticEvent, ...], bool]:
        """Return a larger sanitized export snapshot without changing the bounded UI query."""
        return self._list_events_with_truncation(
            since=since,
            severity=severity,
            source_id=source_id,
            playlist_id=playlist_id,
            limit=limit,
            maximum=MAX_DIAGNOSTIC_EXPORT_EVENTS,
            operation="list diagnostic events for export",
            label="diagnostic export limit",
        )

    def _list_events_with_truncation(
        self,
        *,
        since: datetime | None,
        severity: DiagnosticSeverity | None,
        source_id: str | None,
        playlist_id: str | None,
        limit: int,
        maximum: int,
        operation: str,
        label: str,
    ) -> tuple[tuple[DiagnosticEvent, ...], bool]:
        if not 1 <= limit <= maximum:
            raise ValueError(f"{label} must be between 1 and {maximum}")
        clauses, values = _event_filter_query(
            since=since,
            severity=severity,
            source_id=source_id,
            playlist_id=playlist_id,
        )
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        query = (
            "SELECT event_id, occurred_at, severity, component, event_name, "
            "cycle_id, source_id, playlist_id, details_json "
            f"FROM diagnostic_events{where} "
            "ORDER BY occurred_at DESC, event_id DESC LIMIT ?"
        )
        values.append(limit + 1)
        with self._connection(operation) as connection:
            rows = connection.execute(query, values).fetchall()
        return tuple(_event_from_row(row) for row in rows[:limit]), len(rows) > limit

    def retention_status(self) -> DiagnosticRetentionStatus:
        """Describe effective stored coverage and whether the emergency row cap is saturated."""
        with self._connection("read diagnostic retention status") as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS event_count,
                       MIN(occurred_at) AS oldest_occurred_at,
                       MAX(occurred_at) AS newest_occurred_at
                FROM diagnostic_events
                """
            ).fetchone()
        if row is None or not isinstance(row["event_count"], int):
            raise PersistenceError("diagnostic retention status returned an invalid result")
        event_count = int(row["event_count"])
        oldest_raw = row["oldest_occurred_at"]
        newest_raw = row["newest_occurred_at"]
        oldest = None if oldest_raw is None else _parse_timestamp(str(oldest_raw))
        newest = None if newest_raw is None else _parse_timestamp(str(newest_raw))
        return DiagnosticRetentionStatus(
            event_count=event_count,
            oldest_occurred_at=oldest,
            newest_occurred_at=newest,
            capacity_limit_reached=event_count >= self.max_events,
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

    def _connection(self, operation: str, *, immediate: bool = False) -> _DiagnosticConnection:
        return _DiagnosticConnection(self.path, operation, immediate=immediate)


def _event_filter_query(
    *,
    since: datetime | None,
    severity: DiagnosticSeverity | None,
    source_id: str | None,
    playlist_id: str | None,
) -> tuple[list[str], list[object]]:
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
    return clauses, values


class _DiagnosticConnection:
    def __init__(self, path: Path, operation: str, *, immediate: bool = False) -> None:
        self.path = path
        self.operation = operation
        self.connection: sqlite3.Connection | None = None
        self.immediate = immediate

    def __enter__(self) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path, timeout=5.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("BEGIN IMMEDIATE" if self.immediate else "BEGIN")
        except sqlite3.Error as exc:
            if connection is not None:
                connection.close()
            raise PersistenceError(f"{self.operation} failed for {self.path}: {exc}") from exc
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
            if key in {"retry_not_before", "last_failure_at"}:
                normalized[key] = _normalize_detail_timestamp(value)
                continue
            if key in {
                "desired_fingerprint",
                "public_response_sha256",
            } and re.fullmatch(r"[0-9a-f]{64}", value):
                normalized[key] = value
                continue
            if key == "public_response_content_type":
                normalized[key] = _normalize_bounded_text(value, key, max_length=256)
                continue
            if key == "public_response_body_b64":
                if len(value) > 699_052:
                    raise ValueError("diagnostic public response capture is too large")
                try:
                    decoded = base64.b64decode(value, validate=True)
                except binascii.Error as exc:
                    raise ValueError(
                        "diagnostic public response capture is not valid base64"
                    ) from exc
                if len(decoded) > 512 * 1024:
                    raise ValueError("diagnostic public response capture is too large")
                normalized[key] = value
                continue
            if key in {"spotify_episode_id", "spotify_previous_episode_id"}:
                if not is_spotify_id(value):
                    raise ValueError(f"diagnostic detail {key} contains an invalid Spotify id")
                normalized[key] = value
                continue
            if key in {
                "spotify_episode_title",
                "spotify_previous_episode_title",
                "spotify_oembed_title",
            }:
                normalized[key] = _normalize_bounded_text(value, key, max_length=1000)
                continue
            if key == "public_release_date":
                normalized[key] = _normalize_bounded_text(value, key, max_length=128)
                continue
            allowed_values = _ALLOWED_DETAIL_STRING_VALUES.get(key)
            if allowed_values is None or value not in allowed_values:
                raise ValueError(f"diagnostic detail {key} contains an unsupported label")
            normalized[key] = value
        else:
            raise ValueError(f"diagnostic detail {key} has unsupported value type")
    return normalized


def _normalize_bounded_text(value: str, key: str, *, max_length: int) -> str:
    if not value or len(value) > max_length:
        raise ValueError(f"diagnostic detail {key} contains invalid text")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"diagnostic detail {key} contains control characters")
    return value


def _normalize_detail_timestamp(value: str) -> str:
    if len(value) > 64:
        raise ValueError("diagnostic detail retry_not_before contains an invalid timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            "diagnostic detail retry_not_before contains an invalid timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("diagnostic detail retry_not_before contains an invalid timestamp")
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


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
