"""Deterministic scheduler-drain regressions for issue #421 (no real provider I/O)."""

from __future__ import annotations

import io
import logging
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from news_bulletin_playlist import engine_runtime
from news_bulletin_playlist.diagnostics import DiagnosticEventStore, DiagnosticSeverity
from news_bulletin_playlist.engine import EngineCycleResult
from news_bulletin_playlist.engine_runtime import EngineLifecycleController, _engine_label
from news_bulletin_playlist.runtime_diagnostics import OperationalDiagnostics

NOW = datetime(2026, 11, 4, 20, 0, tzinfo=UTC)


class _ControlledCycleRunner:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.completions = 0

    def run_cycle(self) -> EngineCycleResult:
        self.calls += 1
        self.started.set()
        if not self.release.wait(timeout=3):
            raise AssertionError("test did not release the in-flight engine cycle")
        self.completions += 1
        return EngineCycleResult(
            started_at=NOW,
            finished_at=NOW,
            ok=True,
            sources=(),
            playlists=(),
        )


def _diagnostics(
    tmp_path: Path,
) -> tuple[OperationalDiagnostics, DiagnosticEventStore, io.StringIO]:
    store = DiagnosticEventStore(tmp_path / "events.sqlite3")
    store.initialize()
    output = io.StringIO()
    logger = logging.Logger("test-scheduler-shutdown")
    logger.setLevel(logging.INFO)
    logger.addHandler(logging.StreamHandler(output))
    return OperationalDiagnostics(store, logger=logger), store, output


def test_shutdown_timeout_reports_in_flight_cycle_without_interrupting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_runtime, "_SCHEDULER_SHUTDOWN_WAIT_SECONDS", 0.02)
    diagnostics, store, output = _diagnostics(tmp_path)
    runner = _ControlledCycleRunner()
    controller = EngineLifecycleController(  # type: ignore[arg-type]
        runner, interval=timedelta(hours=1), diagnostics=diagnostics
    )
    controller.reconcile(configured=True)
    assert runner.started.wait(timeout=2)
    worker = controller._scheduler_thread
    assert worker is not None

    try:
        controller.shutdown()

        assert worker.is_alive()
        assert controller.scheduler is None
        snapshot = controller.status.snapshot()
        assert snapshot.configured is False
        assert snapshot.running is True
        assert _engine_label(snapshot) == "Finishing in-flight cycle"
        assert snapshot.last_cycle is None
        assert runner.calls == 1
        assert runner.completions == 0
        events = list(reversed(store.list_events(limit=10)))
        assert [event.event_name for event in events] == [
            "scheduler_started",
            "scheduler_stop_requested",
            "scheduler_shutdown_timeout",
        ]
        timeout = events[-1]
        assert timeout.severity is DiagnosticSeverity.WARNING
        assert timeout.details == {"phase": "scheduler", "cycle_in_progress": True}
        assert "scheduler_shutdown_timeout" in output.getvalue()
        assert "cycle_in_progress=true" in output.getvalue()
        assert "access-token-sentinel" not in output.getvalue()
    finally:
        runner.release.set()
        worker.join(timeout=2)

    assert not worker.is_alive()
    assert runner.completions == 1
    assert runner.calls == 1
    assert controller.status.snapshot().running is False
    assert controller.status.snapshot().next_run_at is None
    assert _engine_label(controller.status.snapshot()) == "Not configured"


def test_final_shutdown_cancels_queued_restart_until_blocked_cycle_drains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_runtime, "_SCHEDULER_SHUTDOWN_WAIT_SECONDS", 0.02)
    diagnostics, store, _ = _diagnostics(tmp_path)
    runner = _ControlledCycleRunner()
    controller = EngineLifecycleController(  # type: ignore[arg-type]
        runner, interval=timedelta(hours=1), diagnostics=diagnostics
    )
    controller.reconcile(configured=True)
    assert runner.started.wait(timeout=2)
    worker = controller._scheduler_thread
    assert worker is not None

    try:
        controller.reconcile(configured=False)
        controller.reconcile(configured=True)
        waiter = controller._restart_waiter
        assert waiter is not None
        assert controller.scheduler is None
        assert runner.calls == 1

        controller.shutdown()
        assert controller.status.snapshot().configured is False
        assert controller.status.snapshot().running is True
        assert controller.scheduler is None
        assert worker.is_alive()

        runner.release.set()
        worker.join(timeout=2)
        waiter.join(timeout=2)
        assert not worker.is_alive()
        assert not waiter.is_alive()
        assert controller.scheduler is None
        assert controller.status.snapshot().configured is False
        assert runner.calls == runner.completions == 1
        assert [
            event.event_name
            for event in store.list_events(limit=10)
            if event.event_name == "scheduler_started"
        ] == ["scheduler_started"]
    finally:
        runner.release.set()
        worker.join(timeout=2)


def test_scheduler_shutdown_cycle_flag_rejects_untrusted_text(tmp_path: Path) -> None:
    _, store, _ = _diagnostics(tmp_path)
    with pytest.raises(ValueError, match="diagnostic cycle state must be boolean"):
        store.record(
            occurred_at=NOW,
            severity=DiagnosticSeverity.WARNING,
            component="scheduler",
            event_name="scheduler_shutdown_timeout",
            details={
                "phase": "scheduler",
                "cycle_in_progress": "access-token-sentinel",
            },
        )
    assert store.list_events() == ()
    assert b"access-token-sentinel" not in store.path.read_bytes()
