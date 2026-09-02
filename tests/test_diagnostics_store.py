from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from news_bulletin_playlist.diagnostics import (
    DiagnosticEventStore,
    DiagnosticSeverity,
)
from news_bulletin_playlist.persistence import SQLiteStore

NOW = datetime(2026, 9, 2, 10, 0, tzinfo=UTC)


def _stores(
    tmp_path: Path,
    *,
    retention_days: int = 30,
    max_events: int = 10_000,
) -> tuple[SQLiteStore, DiagnosticEventStore]:
    path = tmp_path / "data" / "state.sqlite3"
    operational = SQLiteStore(path)
    operational.initialize()
    diagnostics = DiagnosticEventStore(
        path,
        retention_days=retention_days,
        max_events=max_events,
    )
    diagnostics.initialize()
    return operational, diagnostics


def test_diagnostics_survive_store_recreation_and_share_application_database(
    tmp_path: Path,
) -> None:
    operational, diagnostics = _stores(tmp_path)
    assert operational.schema_version() >= 2

    event_id = diagnostics.record(
        occurred_at=NOW,
        severity=DiagnosticSeverity.WARNING,
        component="spotify.reconcile",
        event_name="playlist_readback_degraded",
        cycle_id="cycle-20260902T100000Z",
        source_id="rne",
        playlist_id="spain_spanish_news",
        details={
            "phase": "readback",
            "unavailable_count": 1,
            "desired_count": 74,
            "verification_outcome": "attested",
        },
    )

    restarted = DiagnosticEventStore(diagnostics.path)
    restarted.initialize()
    events = restarted.list_events()

    assert len(events) == 1
    event = events[0]
    assert event.event_id == event_id
    assert event.occurred_at == NOW
    assert event.severity is DiagnosticSeverity.WARNING
    assert event.component == "spotify.reconcile"
    assert event.event_name == "playlist_readback_degraded"
    assert event.cycle_id == "cycle-20260902T100000Z"
    assert event.source_id == "rne"
    assert event.playlist_id == "spain_spanish_news"
    assert event.details == {
        "desired_count": 74,
        "phase": "readback",
        "unavailable_count": 1,
        "verification_outcome": "attested",
    }


def test_diagnostic_details_are_allow_listed_and_reject_secret_shaped_fields(
    tmp_path: Path,
) -> None:
    _, diagnostics = _stores(tmp_path)
    sentinel = "access-token-sentinel-never-persist"

    with pytest.raises(ValueError, match="unknown key: access_token"):
        diagnostics.record(
            occurred_at=NOW,
            severity=DiagnosticSeverity.ERROR,
            component="spotify.auth",
            event_name="authorization_failed",
            details={"access_token": sentinel},  # type: ignore[dict-item]
        )

    assert diagnostics.list_events() == ()
    assert sentinel.encode() not in diagnostics.path.read_bytes()


def test_allowed_text_key_still_rejects_unlisted_secret_value(tmp_path: Path) -> None:
    _, diagnostics = _stores(tmp_path)
    sentinel = "access-token-sentinel-never-persist"

    with pytest.raises(ValueError, match="phase contains an unsupported label"):
        diagnostics.record(
            occurred_at=NOW,
            severity=DiagnosticSeverity.ERROR,
            component="spotify.auth",
            event_name="authorization_failed",
            details={"phase": sentinel},
        )

    assert diagnostics.list_events() == ()
    assert sentinel.encode() not in diagnostics.path.read_bytes()


def test_diagnostic_row_cap_evicts_oldest_events_deterministically(tmp_path: Path) -> None:
    _, diagnostics = _stores(tmp_path, max_events=3)

    for index in range(5):
        diagnostics.record(
            occurred_at=NOW + timedelta(seconds=index),
            severity=DiagnosticSeverity.INFO,
            component="engine",
            event_name=f"cycle_{index}",
            details={"edition_count": index},
        )

    events = diagnostics.list_events(limit=10)
    assert [event.event_name for event in events] == ["cycle_4", "cycle_3", "cycle_2"]


def test_explicit_prune_enforces_age_and_overflow_bounds(tmp_path: Path) -> None:
    _, diagnostics = _stores(tmp_path, retention_days=365, max_events=20)
    diagnostics.record(
        occurred_at=NOW - timedelta(days=40),
        severity=DiagnosticSeverity.INFO,
        component="engine",
        event_name="old_event",
    )
    for index in range(4):
        diagnostics.record(
            occurred_at=NOW - timedelta(minutes=index),
            severity=DiagnosticSeverity.INFO,
            component="engine",
            event_name=f"recent_{index}",
        )

    result = diagnostics.prune(now=NOW, retention_days=30, max_events=2)

    assert result.age_deleted == 1
    assert result.overflow_deleted == 2
    assert result.total_deleted == 3
    assert [event.event_name for event in diagnostics.list_events(limit=10)] == [
        "recent_0",
        "recent_1",
    ]


def test_diagnostic_filters_are_bounded_and_newest_first(tmp_path: Path) -> None:
    _, diagnostics = _stores(tmp_path)
    diagnostics.record(
        occurred_at=NOW - timedelta(minutes=2),
        severity=DiagnosticSeverity.ERROR,
        component="source.collect",
        event_name="source_failed",
        source_id="rne",
        details={"http_status": 503},
    )
    diagnostics.record(
        occurred_at=NOW - timedelta(minutes=1),
        severity=DiagnosticSeverity.WARNING,
        component="spotify.reconcile",
        event_name="playlist_degraded",
        playlist_id="spain_spanish_news",
        details={"unavailable_count": 1},
    )
    diagnostics.record(
        occurred_at=NOW,
        severity=DiagnosticSeverity.INFO,
        component="source.collect",
        event_name="source_recovered",
        source_id="rne",
        details={"edition_count": 5},
    )

    rne_events = diagnostics.list_events(source_id="rne", limit=10)
    assert [event.event_name for event in rne_events] == [
        "source_recovered",
        "source_failed",
    ]
    warning_events = diagnostics.list_events(
        severity=DiagnosticSeverity.WARNING,
        since=NOW - timedelta(minutes=5),
        limit=10,
    )
    assert [event.event_name for event in warning_events] == ["playlist_degraded"]

    with pytest.raises(ValueError, match="between 1 and 500"):
        diagnostics.list_events(limit=501)


def test_diagnostics_initialize_is_idempotent(tmp_path: Path) -> None:
    _, diagnostics = _stores(tmp_path)
    diagnostics.initialize()
    diagnostics.initialize()

    with sqlite3.connect(diagnostics.path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert "diagnostic_events" in tables
    assert "source_runs" in tables
    assert "playlist_runs" in tables
