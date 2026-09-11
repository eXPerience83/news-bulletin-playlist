from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from news_bulletin_playlist.diagnostics import (
    DEFAULT_DIAGNOSTIC_MAX_EVENTS,
    MAX_DIAGNOSTIC_EXPORT_EVENTS,
    DiagnosticEventStore,
    DiagnosticSeverity,
)
from news_bulletin_playlist.diagnostics_runtime import DiagnosticOperationalHealthHandler
from news_bulletin_playlist.diagnostics_web import DiagnosticFilters, build_diagnostic_bundle
from news_bulletin_playlist.reconciliation_diagnostics import (
    classify_reconciliation_failure,
    classify_reconciliation_warning,
)

NOW = datetime(2026, 9, 10, 20, 0, tzinfo=UTC)


def _record_events(store: DiagnosticEventStore, count: int) -> None:
    for index in range(count):
        store.record(
            occurred_at=NOW + timedelta(seconds=index),
            severity=DiagnosticSeverity.INFO,
            component="engine",
            event_name="cycle_completed",
            cycle_id=f"cycle-{index}",
        )


def test_retention_and_export_have_separate_bounded_capacities(tmp_path: Path) -> None:
    assert DEFAULT_DIAGNOSTIC_MAX_EVENTS == 100_000
    assert MAX_DIAGNOSTIC_EXPORT_EVENTS == 10_000

    store = DiagnosticEventStore(tmp_path / "state.sqlite3")
    store.initialize()
    _record_events(store, 600)

    ui_events, ui_truncated = store.list_events_with_truncation(limit=500)
    export_events, export_truncated = store.list_events_for_export_with_truncation(limit=10_000)

    assert len(ui_events) == 500
    assert ui_truncated is True
    assert len(export_events) == 600
    assert export_truncated is False


def test_export_runtime_path_ignores_ui_limit_but_keeps_filters(tmp_path: Path) -> None:
    store = DiagnosticEventStore(tmp_path / "state.sqlite3")
    store.initialize()
    _record_events(store, 600)
    handler = type("DiagnosticHandler", (), {"diagnostic_store": store})()

    filters, events, truncated = DiagnosticOperationalHealthHandler._diagnostic_export_events(
        handler,
        "hours=all&limit=2",
        observed_at=NOW + timedelta(hours=1),
    )

    assert filters.limit == 2
    assert len(events) == 600
    assert truncated is False


def test_capacity_status_surfaces_when_emergency_row_cap_is_active(tmp_path: Path) -> None:
    store = DiagnosticEventStore(tmp_path / "state.sqlite3", max_events=3)
    store.initialize()
    _record_events(store, 5)

    status = store.retention_status()

    assert status.event_count == 3
    assert status.capacity_limit_reached is True
    assert status.oldest_occurred_at == NOW + timedelta(seconds=2)
    assert status.newest_occurred_at == NOW + timedelta(seconds=4)


def test_actionable_failure_reasons_do_not_infer_playlist_items_from_phase() -> None:
    mapping = classify_reconciliation_failure(
        "Spotify prewrite mapping validation failed; destination preserved"
    )
    assert mapping.failure_reason == "prewrite_mapping_validation_failed"
    assert mapping.phase == "prewrite"
    assert mapping.operation is None
    assert mapping.write_decision == "blocked"

    pagination = classify_reconciliation_failure(
        "Spotify playlist prewrite response pagination truncated before total "
        "(offset=50 returned=24 total=80 next=null)"
    )
    assert pagination.failure_reason == "playlist_pagination_truncated"
    assert pagination.operation == "playlist_items"
    assert pagination.offset == 50
    assert pagination.returned_count == 24
    assert pagination.total == 80

    api_error = classify_reconciliation_failure(
        "Spotify playlist readback playlist_items API failure (http_status=503)"
    )
    assert api_error.failure_reason == "playlist_items_api_error"
    assert api_error.operation == "playlist_items"
    assert api_error.http_status == 503

    transport = classify_reconciliation_failure(
        "Spotify playlist write replace_items transport failure (write_outcome=unknown)"
    )
    assert transport.failure_reason == "replace_items_transport_error"
    assert transport.operation == "replace_items"
    assert transport.write_outcome == "unknown"

    unknown = classify_reconciliation_failure("opaque safe wrapper")
    assert unknown.failure_reason == "unknown_reconciliation_error"
    assert unknown.operation is None


def test_snapshot_pending_warning_has_stable_secret_safe_reason(tmp_path: Path) -> None:
    warning = "Spotify verification degraded: snapshot propagation pending after exact readback"
    assert classify_reconciliation_warning(warning) == "snapshot_propagation_pending"

    store = DiagnosticEventStore(tmp_path / "state.sqlite3")
    store.initialize()
    store.record(
        occurred_at=NOW,
        severity=DiagnosticSeverity.WARNING,
        component="playlist",
        event_name="playlist_reconciled",
        playlist_id="spain_spanish_news",
        details={
            "failure_reason": "snapshot_propagation_pending",
            "verification_outcome": "degraded",
            "write_decision": "applied",
        },
    )
    event = store.list_events(limit=1)[0]
    assert event.details["failure_reason"] == "snapshot_propagation_pending"


def test_bundle_reports_true_export_and_retention_capacity(tmp_path: Path) -> None:
    store = DiagnosticEventStore(tmp_path / "state.sqlite3", max_events=100_000)
    store.initialize()
    _record_events(store, 3)
    events, truncated = store.list_events_for_export_with_truncation(limit=10_000)
    status = store.retention_status()

    payload = build_diagnostic_bundle(
        events=events,
        generated_at=NOW + timedelta(hours=1),
        last_cycle=None,
        retention_days=store.retention_days,
        max_events=store.max_events,
        filters=DiagnosticFilters(hours=24, limit=200),
        result_truncated=truncated,
        retention_status=status,
        export_limit=MAX_DIAGNOSTIC_EXPORT_EVENTS,
    )

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        runtime = json.loads(archive.read("runtime.json"))

    assert runtime["diagnostic_retention_days"] == 30
    assert runtime["diagnostic_store_max_events"] == 100_000
    assert runtime["diagnostic_store_event_count"] == 3
    assert runtime["diagnostic_store_capacity_limit_reached"] is False
    assert runtime["diagnostic_store_oldest_occurred_at"] == "2026-09-10T20:00:00Z"
    assert runtime["diagnostic_store_newest_occurred_at"] == "2026-09-10T20:00:02Z"
    assert runtime["ui_max_query_limit"] == 500
    assert runtime["export_requested_limit"] == 10_000
    assert runtime["export_max_allowed_limit"] == 10_000
    assert runtime["export_result_truncated"] is False
