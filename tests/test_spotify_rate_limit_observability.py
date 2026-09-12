from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from news_bulletin_playlist.diagnostics import DiagnosticEventStore, DiagnosticSeverity
from news_bulletin_playlist.engine import (
    EngineCycleResult,
    PlaylistCycleOutcome,
    SourceCycleOutcome,
)
from news_bulletin_playlist.engine_observability import InstrumentedEngineCycleRunner
from news_bulletin_playlist.models import PlaylistId, SourceId
from news_bulletin_playlist.runtime_diagnostics import OperationalDiagnostics
from news_bulletin_playlist.spotify_backoff import SPOTIFY_RATE_LIMIT_PRESERVED_MESSAGE

NOW = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)


@dataclass
class _Runner:
    result: EngineCycleResult

    def run_cycle(self) -> EngineCycleResult:
        return self.result


def test_active_spotify_backoff_is_reported_as_destination_preserved(tmp_path: Path) -> None:
    store = DiagnosticEventStore(tmp_path / "state.sqlite3")
    store.initialize()
    diagnostics = OperationalDiagnostics(store)
    message = "Spotify rate-limit backoff active until 2026-09-05T10:10:00Z"
    result = EngineCycleResult(
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=1),
        ok=False,
        sources=(),
        playlists=(
            PlaylistCycleOutcome(
                playlist_id=PlaylistId("spain_spanish_news"),
                ok=False,
                desired_count=79,
                applied_count=None,
                wrote=None,
                last_success_at=NOW - timedelta(minutes=10),
                error=message,
            ),
        ),
        error=message,
    )

    assert (
        InstrumentedEngineCycleRunner(
            _Runner(result),
            diagnostics,
            clock=lambda: NOW,
        ).run_cycle()
        is result
    )

    events = store.list_events(limit=10)
    assert [event.event_name for event in reversed(events)] == [
        "cycle_started",
        "destination_preserved",
        "cycle_degraded",
    ]
    playlist_event = next(event for event in events if event.component == "playlist")
    assert playlist_event.severity is DiagnosticSeverity.WARNING
    assert playlist_event.details == {
        "desired_count": 79,
        "write_decision": "preserved",
    }
    assert all(event.event_name != "playlist_reconciliation_failed" for event in events)


def test_same_cycle_backoff_reports_deferred_matching_and_preserved_later_destination(
    tmp_path: Path,
) -> None:
    store = DiagnosticEventStore(tmp_path / "state.sqlite3")
    store.initialize()
    diagnostics = OperationalDiagnostics(store)
    result = EngineCycleResult(
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=1),
        ok=False,
        sources=(
            SourceCycleOutcome(
                source_id=SourceId("two"),
                collection_ok=True,
                matching_ok=None,
                edition_count=1,
                matched_count=0,
                last_success_at=NOW,
                error="Spotify matching skipped because rate-limit backoff is active",
            ),
        ),
        playlists=(
            PlaylistCycleOutcome(
                playlist_id=PlaylistId("later"),
                ok=False,
                desired_count=10,
                applied_count=None,
                wrote=None,
                last_success_at=NOW - timedelta(minutes=10),
                error=SPOTIFY_RATE_LIMIT_PRESERVED_MESSAGE,
            ),
        ),
        error="cycle completed with deferred Spotify work",
    )

    assert (
        InstrumentedEngineCycleRunner(
            _Runner(result),
            diagnostics,
            clock=lambda: NOW,
        ).run_cycle()
        is result
    )

    events = store.list_events(limit=10)
    assert [event.event_name for event in reversed(events)] == [
        "cycle_started",
        "source_matching_deferred",
        "destination_preserved",
        "cycle_degraded",
    ]
    source_event = next(event for event in events if event.component == "source")
    assert source_event.severity is DiagnosticSeverity.WARNING
    assert source_event.details == {
        "catalogue_audit_due": False,
        "catalogue_inspected": False,
        "catalogue_request_count": 0,
        "catalogue_requested": False,
        "edition_count": 1,
        "matched_count": 0,
        "matching_evaluated_count": 0,
        "matching_plan_conservative": False,
        "matching_reason_coalesced_unresolved_count": 0,
        "matching_reason_evidence_recovery_count": 0,
        "matching_reason_input_changed_count": 0,
        "matching_reason_missing_evidence_count": 0,
        "matching_reason_new_match_count": 0,
        "matching_reason_retry_due_count": 0,
        "matching_work_count": 0,
        "phase": "matching",
        "unresolved_relevant_count": 0,
    }
    playlist_event = next(event for event in events if event.component == "playlist")
    assert playlist_event.severity is DiagnosticSeverity.WARNING
    assert playlist_event.details == {
        "desired_count": 10,
        "write_decision": "preserved",
    }
