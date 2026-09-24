from __future__ import annotations

import io
import logging
import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from news_bulletin_playlist.desired_state import (
    DURATION_EXCEEDS_DEFAULT_MAX,
    DURATION_EXCEPTION,
    DURATION_WITHIN_DEFAULT_MAX,
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


class _NestedDiagnosticRunner:
    def __init__(
        self,
        diagnostics: OperationalDiagnostics,
        result: EngineCycleResult,
    ) -> None:
        self.diagnostics = diagnostics
        self.result = result

    def run_cycle(self) -> EngineCycleResult:
        self.diagnostics.emit(
            occurred_at=NOW,
            severity=DiagnosticSeverity.INFO,
            component="spotify.public_detector",
            event_name="public_detector_cycle_completed",
            details={
                "detector_source_count": 1,
                "detector_changed_count": 0,
                "detector_unchanged_count": 1,
                "detector_failure_count": 0,
            },
        )
        return self.result


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
                ok=True,
                episode_count=6,
                refreshed=True,
                refresh_reason="change",
                api_request_count=2,
                last_detector_success_at=NOW,
                last_catalogue_refresh_at=NOW - timedelta(minutes=1),
                detector_ok=True,
                detector_changed=True,
                parser_fallback_count=1,
                filter_rejected_count=0,
                removed_count=1,
            ),
            SourceCycleOutcome(
                source_id=SourceId("ser"),
                ok=False,
                episode_count=4,
                refreshed=False,
                refresh_reason="inactivity_audit",
                api_request_count=0,
                last_detector_success_at=None,
                last_catalogue_refresh_at=None,
                detector_ok=False,
                detector_changed=False,
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
                error="destination preserved because Spotify-first source state is incomplete",
            ),
        ),
        error="cycle completed with preserved or degraded work",
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
        "source_state_preserved",
        "destination_preserved",
        "cycle_degraded",
    ]
    cycle_ids = {event.cycle_id for event in events}
    assert cycle_ids == {"cycle-20260902T100000000000Z"}
    rne = next(event for event in events if event.source_id == "rne")
    assert rne.details == {
        "api_request_count": 2,
        "detector_changed": True,
        "detector_false_negative": False,
        "detector_ok": True,
        "episode_count": 6,
        "filter_rejected_count": 0,
        "parser_fallback_count": 1,
        "phase": "complete",
        "refresh_reason": "change",
        "refreshed": True,
        "removed_count": 1,
    }
    ser = next(event for event in events if event.source_id == "ser")
    assert ser.details["episode_count"] == 4
    assert ser.details["refresh_reason"] == "inactivity_audit"
    assert ser.details["detector_ok"] is False
    assert "error" not in ser.details
    preserved = next(event for event in events if event.event_name == "destination_preserved")
    assert preserved.details == {
        "desired_count": 42,
        "duration_unavailable_count": 0,
        "local_skip": False,
        "stale_escalation": False,
        "write_decision": "preserved",
    }
    assert "provider-body-sentinel" not in output.getvalue()


def test_nested_detector_event_inherits_engine_cycle_id(tmp_path: Path) -> None:
    diagnostics, store, _output = _diagnostics(tmp_path)
    result = EngineCycleResult(
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=1),
        ok=True,
        sources=(),
        playlists=(),
    )
    runner = InstrumentedEngineCycleRunner(
        _NestedDiagnosticRunner(diagnostics, result),
        diagnostics,
        clock=lambda: NOW,
    )

    assert runner.run_cycle() is result

    detector = next(
        event
        for event in store.list_events(limit=10)
        if event.event_name == "public_detector_cycle_completed"
    )
    assert detector.cycle_id == "cycle-20260902T100000000000Z"


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
                pending_snapshot_created="exact",
                pending_snapshot_resolution="still_baseline",
                unavailable_media_count=1,
                unavailable_media_phase="pending_confirmation",
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
        "duration_unavailable_count": 0,
        "local_skip": False,
        "pending_snapshot_created": "exact",
        "pending_snapshot_resolution": "still_baseline",
        "stale_escalation": False,
        "unavailable_media_count": 1,
        "unavailable_media_phase": "pending_confirmation",
        "verification_outcome": "degraded",
        "write_decision": "applied",
    }
    assert sentinel not in output.getvalue()


def test_degraded_local_skip_is_explicit_without_warning_noise(tmp_path: Path) -> None:
    diagnostics, store, _output = _diagnostics(tmp_path)
    result = EngineCycleResult(
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=1),
        ok=True,
        sources=(),
        playlists=(
            PlaylistCycleOutcome(
                playlist_id=PlaylistId("spain_spanish_news"),
                ok=True,
                desired_count=75,
                applied_count=75,
                wrote=False,
                last_success_at=NOW,
                local_skip=True,
                write_outcome="skipped",
                destination_verification_kind="degraded",
            ),
        ),
    )
    runner = InstrumentedEngineCycleRunner(
        _Runner(result),
        diagnostics,
        clock=lambda: NOW,
    )

    assert runner.run_cycle() is result

    playlist_event = next(
        event for event in store.list_events(limit=10) if event.component == "playlist"
    )
    assert playlist_event.severity is DiagnosticSeverity.INFO
    assert playlist_event.details["write_decision"] == "skipped"
    assert playlist_event.details["destination_verification_kind"] == "degraded"
    assert playlist_event.details["verification_outcome"] == "degraded"


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


def test_runtime_emitter_rejects_unknown_refresh_reason(tmp_path: Path) -> None:
    diagnostics, store, output = _diagnostics(tmp_path)
    sentinel = "provider-body-sentinel-never-log"

    diagnostics.emit(
        occurred_at=NOW,
        severity=DiagnosticSeverity.ERROR,
        component="source",
        event_name="source_refresh_failed",
        details={"refresh_reason": sentinel},
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
                desired_count=3,
                applied_count=3,
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
                    DurationEligibilityDecision(
                        source_id=SourceId("rne"),
                        source_native_id="short-not-persisted",
                        duration_seconds=245,
                        accepted=True,
                        reason=DURATION_WITHIN_DEFAULT_MAX,
                        max_seconds=1800,
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
        event for event in store.list_events(limit=30) if event.component == "playlist.eligibility"
    )
    assert {event.event_name for event in policy_events} == {
        "duration_profile",
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
    rne_profile = next(
        event
        for event in policy_events
        if event.event_name == "duration_profile" and event.source_id == "rne"
    )
    assert rne_profile.details == {
        "10_to_15m": 0,
        "15_to_30m": 0,
        "30_to_60m": 0,
        "60m_plus": 0,
        "accepted_count": 1,
        "excluded_count": 1,
        "sample_count": 2,
        "under_10m": 2,
    }
    ser_profile = next(
        event
        for event in policy_events
        if event.event_name == "duration_profile" and event.source_id == "ser"
    )
    assert ser_profile.details["15_to_30m"] == 1
    assert ser_profile.details["sample_count"] == 1
    rendered = output.getvalue()
    assert "must-not-be-persisted" not in rendered
    assert "also-not-persisted" not in rendered
    assert "short-not-persisted" not in rendered


def test_unchanged_duration_diagnostics_are_not_repeated_but_new_identity_is_emitted(
    tmp_path: Path,
) -> None:
    diagnostics, store, output = _diagnostics(tmp_path)
    decision = DurationEligibilityDecision(
        source_id=SourceId("ser"),
        source_native_id="episode-a-private",
        duration_seconds=1201,
        accepted=True,
        reason=DURATION_WITHIN_DEFAULT_MAX,
        max_seconds=1800,
    )
    playlist = PlaylistCycleOutcome(
        playlist_id=PlaylistId("spain_spanish_news"),
        ok=True,
        desired_count=1,
        applied_count=1,
        wrote=False,
        last_success_at=NOW,
        duration_decisions=(decision,),
    )
    result = EngineCycleResult(
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=1),
        ok=True,
        sources=(),
        playlists=(playlist,),
    )
    base_runner = _Runner(result)
    runner = InstrumentedEngineCycleRunner(
        base_runner,
        diagnostics,
        clock=lambda: NOW,
    )

    runner.run_cycle()
    runner.run_cycle()

    policy_events = tuple(
        event for event in store.list_events(limit=30) if event.component == "playlist.eligibility"
    )
    assert [event.event_name for event in reversed(policy_events)] == [
        "duration_profile",
        "duration_long_episode_accepted",
    ]

    base_runner.result = replace(
        result,
        playlists=(
            replace(
                playlist,
                duration_decisions=(replace(decision, source_native_id="episode-b-private"),),
            ),
        ),
    )
    runner.run_cycle()

    policy_events = tuple(
        event for event in store.list_events(limit=30) if event.component == "playlist.eligibility"
    )
    assert [event.event_name for event in reversed(policy_events)] == [
        "duration_profile",
        "duration_long_episode_accepted",
        "duration_profile",
        "duration_long_episode_accepted",
    ]
    rendered = output.getvalue()
    assert "episode-a-private" not in rendered
    assert "episode-b-private" not in rendered
