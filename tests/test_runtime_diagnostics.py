from __future__ import annotations

import io
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from news_bulletin_playlist.desired_state import (
    DURATION_EXCEEDS_DEFAULT_MAX,
    DURATION_EXCEPTION,
    DurationEligibilityDecision,
)
from news_bulletin_playlist.diagnostics import (
    DiagnosticEventStore,
    DiagnosticSeverity,
)
from news_bulletin_playlist.engine import (
    EngineCycleResult,
    PlaylistCycleOutcome,
    SourceCycleOutcome,
)
from news_bulletin_playlist.engine_observability import InstrumentedEngineCycleRunner
from news_bulletin_playlist.engine_runtime import serve
from news_bulletin_playlist.models import PlaylistId, SourceId
from news_bulletin_playlist.persistence import DEFAULT_DB_FILENAME
from news_bulletin_playlist.runtime_diagnostics import OperationalDiagnostics, cycle_id_for

NOW = datetime(2026, 9, 2, 10, 0, tzinfo=UTC)


@dataclass
class _Runner:
    result: EngineCycleResult

    def run_cycle(self) -> EngineCycleResult:
        return self.result


class _FailingRunner:
    def run_cycle(self) -> EngineCycleResult:
        raise RuntimeError("access-token-sentinel must never be logged")


def _diagnostics(
    tmp_path: Path,
) -> tuple[OperationalDiagnostics, DiagnosticEventStore, io.StringIO]:
    store = DiagnosticEventStore(tmp_path / "state.sqlite3")
    store.initialize()
    output = io.StringIO()
    logger = logging.Logger("test-runtime-diagnostics")
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(output)
    logger.addHandler(handler)
    return OperationalDiagnostics(store, logger=logger), store, output


def test_cycle_id_is_utc_and_identifier_safe() -> None:
    assert cycle_id_for(NOW) == "cycle-20260902T100000000000Z"
    with pytest.raises(ValueError, match="timezone-aware"):
        cycle_id_for(datetime(2026, 9, 2, 10, 0))


def test_instrumented_runner_records_correlated_source_playlist_and_cycle_events(
    tmp_path: Path,
) -> None:
    diagnostics, store, output = _diagnostics(tmp_path)
    result = EngineCycleResult(
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=2),
        ok=False,
        sources=(
            SourceCycleOutcome(
                source_id=SourceId("rne"),
                collection_ok=True,
                matching_ok=True,
                edition_count=6,
                matched_count=5,
                last_success_at=NOW,
            ),
            SourceCycleOutcome(
                source_id=SourceId("ser"),
                collection_ok=False,
                matching_ok=None,
                edition_count=0,
                matched_count=0,
                last_success_at=None,
                error="provider-body-sentinel",
            ),
        ),
        playlists=(
            PlaylistCycleOutcome(
                playlist_id=PlaylistId("spain_spanish_news"),
                ok=False,
                desired_count=42,
                applied_count=None,
                wrote=None,
                last_success_at=NOW,
                error=(
                    "destination preserved because no last-known-good state is available "
                    "for source(s): ser"
                ),
            ),
        ),
        error="cycle completed with 1 source failure(s) and 1 playlist failure(s)",
    )
    runner = InstrumentedEngineCycleRunner(
        _Runner(result),
        diagnostics,
        clock=lambda: NOW,
    )

    assert runner.run_cycle() is result

    events = store.list_events(limit=20)
    names = [event.event_name for event in reversed(events)]
    assert names == [
        "cycle_started",
        "source_cycle_completed",
        "source_collection_failed",
        "destination_preserved",
        "cycle_degraded",
    ]
    cycle_ids = {event.cycle_id for event in events}
    assert cycle_ids == {"cycle-20260902T100000000000Z"}
    rne = next(event for event in events if event.source_id == "rne")
    assert rne.details == {
        "edition_count": 6,
        "matched_count": 5,
        "phase": "complete",
    }
    preserved = next(event for event in events if event.event_name == "destination_preserved")
    assert preserved.details == {
        "desired_count": 42,
        "write_decision": "preserved",
    }
    assert "provider-body-sentinel" not in output.getvalue()


def test_degraded_playlist_verification_is_warning_without_raw_text(tmp_path: Path) -> None:
    diagnostics, store, output = _diagnostics(tmp_path)
    sentinel = "access-token-sentinel-never-persist"
    result = EngineCycleResult(
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=1),
        ok=True,
        sources=(),
        playlists=(
            PlaylistCycleOutcome(
                playlist_id=PlaylistId("spain_spanish_news"),
                ok=True,
                desired_count=82,
                applied_count=82,
                wrote=True,
                last_success_at=NOW,
                error=f"snapshot propagation pending {sentinel}",
            ),
        ),
    )
    runner = InstrumentedEngineCycleRunner(
        _Runner(result),
        diagnostics,
        clock=lambda: NOW,
    )

    assert runner.run_cycle() is result

    events = store.list_events(limit=10)
    playlist_event = next(event for event in events if event.component == "playlist")
    assert playlist_event.event_name == "playlist_reconciled"
    assert playlist_event.severity is DiagnosticSeverity.WARNING
    assert playlist_event.details == {
        "applied_count": 82,
        "desired_count": 82,
        "verification_outcome": "degraded",
        "write_decision": "applied",
    }
    assert sentinel not in output.getvalue()


def test_unexpected_runner_exception_logs_only_safe_classification(tmp_path: Path) -> None:
    diagnostics, store, output = _diagnostics(tmp_path)
    runner = InstrumentedEngineCycleRunner(
        _FailingRunner(),
        diagnostics,
        clock=lambda: NOW,
    )

    with pytest.raises(RuntimeError, match="access-token-sentinel"):
        runner.run_cycle()

    events = store.list_events(limit=10)
    assert [event.event_name for event in reversed(events)] == [
        "cycle_started",
        "cycle_unexpected_failure",
    ]
    assert events[0].details == {"phase": "scheduler"}
    assert "access-token-sentinel" not in output.getvalue()


def test_runtime_emitter_rejects_unsafe_allowed_field_without_leaking_it(tmp_path: Path) -> None:
    diagnostics, store, output = _diagnostics(tmp_path)
    sentinel = "access-token-sentinel-never-log"

    diagnostics.emit(
        occurred_at=NOW,
        severity=DiagnosticSeverity.ERROR,
        component="spotify.auth",
        event_name="authorization_failed",
        details={"phase": sentinel},
    )

    assert store.list_events() == ()
    rendered = output.getvalue()
    assert "diagnostic_persistence_failed" in rendered
    assert sentinel not in rendered


def test_serve_persists_runtime_lifecycle_events(tmp_path: Path) -> None:
    stop = threading.Event()
    stop.set()

    assert (
        serve(
            host="127.0.0.1",
            port=0,
            data_dir=tmp_path,
            stop_event=stop,
            environ={},
        )
        == 0
    )

    store = DiagnosticEventStore(tmp_path / DEFAULT_DB_FILENAME)
    store.initialize()
    events = store.list_events(limit=10)
    assert [event.event_name for event in reversed(events)] == [
        "runtime_ready",
        "runtime_stopping",
        "runtime_stopped",
    ]
    assert all(event.component == "runtime" for event in events)


def test_duration_policy_decisions_emit_sanitized_eligibility_events(tmp_path: Path) -> None:
    diagnostics, store, output = _diagnostics(tmp_path)
    result = EngineCycleResult(
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=1),
        ok=True,
        sources=(),
        playlists=(
            PlaylistCycleOutcome(
                playlist_id=PlaylistId("spain_spanish_news"),
                ok=True,
                desired_count=1,
                applied_count=1,
                wrote=False,
                last_success_at=NOW,
                duration_decisions=(
                    DurationEligibilityDecision(
                        source_id=SourceId("ser"),
                        source_native_id="must-not-be-persisted",
                        duration_seconds=1200,
                        accepted=True,
                        reason=DURATION_EXCEPTION,
                        max_seconds=1800,
                        exception_id="ser_morning_0800",
                    ),
                    DurationEligibilityDecision(
                        source_id=SourceId("rne"),
                        source_native_id="also-not-persisted",
                        duration_seconds=481,
                        accepted=False,
                        reason=DURATION_EXCEEDS_DEFAULT_MAX,
                        max_seconds=480,
                    ),
                ),
            ),
        ),
    )

    InstrumentedEngineCycleRunner(
        _Runner(result),
        diagnostics,
        clock=lambda: NOW,
    ).run_cycle()

    policy_events = tuple(
        event for event in store.list_events(limit=20) if event.component == "playlist.eligibility"
    )
    assert {event.event_name for event in policy_events} == {
        "duration_exception_applied",
        "duration_episode_excluded",
    }
    exception = next(
        event for event in policy_events if event.event_name == "duration_exception_applied"
    )
    assert exception.source_id == "ser"
    assert exception.details == {
        "duration_seconds": 1200,
        "eligibility_reason": "duration_exception",
        "max_seconds": 1800,
        "policy_exception": "ser_morning_0800",
    }
    excluded = next(
        event for event in policy_events if event.event_name == "duration_episode_excluded"
    )
    assert excluded.source_id == "rne"
    assert excluded.details == {
        "duration_seconds": 481,
        "eligibility_reason": "duration_exceeds_default_max",
        "max_seconds": 480,
    }
    rendered = output.getvalue()
    assert "must-not-be-persisted" not in rendered
    assert "also-not-persisted" not in rendered
