from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from news_bulletin_playlist.diagnostics import DiagnosticEventStore
from news_bulletin_playlist.engine import (
    EngineCycleResult,
    PlaylistCycleOutcome,
    SourceCycleOutcome,
)
from news_bulletin_playlist.engine_observability import InstrumentedEngineCycleRunner
from news_bulletin_playlist.models import PlaylistId, SourceId
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


def _diagnostics(tmp_path: Path) -> tuple[OperationalDiagnostics, DiagnosticEventStore, io.StringIO]:
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
        severity=store.list_events.__annotations__.get("severity", None)  # type: ignore[arg-type]
        if False
        else __import__(
            "news_bulletin_playlist.diagnostics",
            fromlist=["DiagnosticSeverity"],
        ).DiagnosticSeverity.ERROR,
        component="spotify.auth",
        event_name="authorization_failed",
        details={"phase": sentinel},
    )

    assert store.list_events() == ()
    rendered = output.getvalue()
    assert "diagnostic_persistence_failed" in rendered
    assert sentinel not in rendered
