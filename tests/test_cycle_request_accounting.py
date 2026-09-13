from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from news_bulletin_playlist.diagnostics import DiagnosticEventStore
from news_bulletin_playlist.engine import EngineCycleResult
from news_bulletin_playlist.engine_observability import InstrumentedEngineCycleRunner
from news_bulletin_playlist.persistence import PersistenceError, SQLiteStore
from news_bulletin_playlist.runtime_diagnostics import OperationalDiagnostics
from news_bulletin_playlist.spotify.client import SpotifyClient
from news_bulletin_playlist.spotify_backoff import (
    SpotifyRateLimitGuardClient,
    SpotifyRateLimitJournal,
    SpotifyRateLimitSuppressed,
)
from news_bulletin_playlist.spotify_metrics import (
    SpotifyOAuthOperation,
    SpotifyQuotaReason,
    SpotifyRequestMetrics,
    SpotifyRequestOutcome,
    SpotifyWebApiOperation,
)

NOW = datetime(2026, 9, 13, 6, 30, tzinfo=UTC)


def _metrics(tmp_path: Path) -> SpotifyRequestMetrics:
    metrics = SpotifyRequestMetrics(tmp_path / "state.sqlite3", clock=lambda: NOW)
    metrics.initialize()
    return metrics


def test_request_scope_counts_operations_nesting_and_429(tmp_path: Path) -> None:
    metrics = _metrics(tmp_path)

    with metrics.request_scope() as outer:
        metrics.record_web_api(
            SpotifyWebApiOperation.SHOW_EPISODES,
            SpotifyRequestOutcome.SUCCESS,
        )
        with metrics.request_scope() as inner:
            assert inner is outer
            metrics.record_web_api(
                SpotifyWebApiOperation.PLAYLIST_SNAPSHOT,
                SpotifyRequestOutcome.HTTP_ERROR,
                http_status=429,
                quota_reason=SpotifyQuotaReason.QUOTA_EXCEEDED,
            )
            metrics.record_oauth(
                SpotifyOAuthOperation.TOKEN_REFRESH,
                SpotifyRequestOutcome.SUCCESS,
            )
        snapshot = outer.snapshot()

    assert snapshot.total_requests == 3
    assert snapshot.web_api_requests == 2
    assert snapshot.oauth_requests == 1
    assert snapshot.web_api_count(SpotifyWebApiOperation.SHOW_EPISODES) == 1
    assert snapshot.web_api_count(SpotifyWebApiOperation.PLAYLIST_SNAPSHOT) == 1
    assert snapshot.oauth_count(SpotifyOAuthOperation.TOKEN_REFRESH) == 1
    assert snapshot.http_rate_limits == 1
    assert snapshot.quota_exceeded == 1


def test_request_scope_excludes_concurrent_context(tmp_path: Path) -> None:
    metrics = _metrics(tmp_path)

    with metrics.request_scope() as scope:
        metrics.record_web_api(
            SpotifyWebApiOperation.SHOW_EPISODES,
            SpotifyRequestOutcome.SUCCESS,
        )

        thread = threading.Thread(
            target=lambda: metrics.record_web_api(
                SpotifyWebApiOperation.PLAYLIST_ITEMS,
                SpotifyRequestOutcome.SUCCESS,
            )
        )
        thread.start()
        thread.join()
        snapshot = scope.snapshot()

    assert snapshot.web_api_requests == 1
    assert snapshot.web_api_count(SpotifyWebApiOperation.SHOW_EPISODES) == 1
    assert snapshot.web_api_count(SpotifyWebApiOperation.PLAYLIST_ITEMS) == 0
    assert metrics.last_24_hours(now=NOW).web_api_requests == 2


def test_request_scope_survives_durable_metrics_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = _metrics(tmp_path)

    def fail_connection(_operation: str):
        raise PersistenceError("synthetic metrics persistence failure")

    monkeypatch.setattr(metrics, "_connection", fail_connection)

    with metrics.request_scope() as scope:
        metrics.record_web_api(
            SpotifyWebApiOperation.SHOW_EPISODES,
            SpotifyRequestOutcome.SUCCESS,
        )
        snapshot = scope.snapshot()

    assert snapshot.total_requests == 1
    assert snapshot.web_api_count(SpotifyWebApiOperation.SHOW_EPISODES) == 1


def test_active_rate_limit_suppression_transmits_zero_scoped_requests(tmp_path: Path) -> None:
    metrics = _metrics(tmp_path)
    store = SQLiteStore(metrics.path)
    store.initialize()
    journal = SpotifyRateLimitJournal(store)
    journal.activate(observed_at=NOW, retry_after_seconds=600)
    guarded = SpotifyRateLimitGuardClient(
        SpotifyClient("access-token-sentinel", metrics=metrics),
        journal,
        clock=lambda: NOW,
    )

    with metrics.request_scope() as scope:
        with pytest.raises(SpotifyRateLimitSuppressed):
            guarded.show_episodes("show")
        snapshot = scope.snapshot()

    assert snapshot.total_requests == 0
    assert metrics.last_24_hours(now=NOW).web_api_requests == 0


class _Runner:
    def __init__(self, metrics: SpotifyRequestMetrics, *, make_requests: bool) -> None:
        self.request_metrics = metrics
        self.make_requests = make_requests

    def run_cycle(self) -> EngineCycleResult:
        if self.make_requests:
            self.request_metrics.record_web_api(
                SpotifyWebApiOperation.SHOW_EPISODES,
                SpotifyRequestOutcome.SUCCESS,
            )
            self.request_metrics.record_web_api(
                SpotifyWebApiOperation.SHOW_EPISODES,
                SpotifyRequestOutcome.SUCCESS,
            )
            self.request_metrics.record_web_api(
                SpotifyWebApiOperation.PLAYLIST_ITEMS,
                SpotifyRequestOutcome.SUCCESS,
            )
            self.request_metrics.record_web_api(
                SpotifyWebApiOperation.PLAYLIST_SNAPSHOT,
                SpotifyRequestOutcome.SUCCESS,
            )
            self.request_metrics.record_web_api(
                SpotifyWebApiOperation.REPLACE_PLAYLIST_ITEMS,
                SpotifyRequestOutcome.SUCCESS,
            )
            self.request_metrics.record_oauth(
                SpotifyOAuthOperation.TOKEN_REFRESH,
                SpotifyRequestOutcome.SUCCESS,
            )
        return EngineCycleResult(
            started_at=NOW,
            finished_at=NOW + timedelta(seconds=1),
            ok=True,
            sources=(),
            playlists=(),
        )


class _FailingRunner:
    def __init__(self, metrics: SpotifyRequestMetrics) -> None:
        self.request_metrics = metrics

    def run_cycle(self) -> EngineCycleResult:
        self.request_metrics.record_web_api(
            SpotifyWebApiOperation.SHOW_EPISODES,
            SpotifyRequestOutcome.SUCCESS,
        )
        raise RuntimeError("synthetic engine failure")


def _diagnostics(tmp_path: Path) -> tuple[OperationalDiagnostics, DiagnosticEventStore]:
    store = DiagnosticEventStore(tmp_path / "diagnostics.sqlite3")
    store.initialize()
    logger = logging.getLogger(f"cycle-request-accounting-{tmp_path.name}")
    logger.handlers.clear()
    logger.propagate = False
    return OperationalDiagnostics(store, logger=logger), store


def test_instrumented_cycle_emits_exact_request_aggregates(tmp_path: Path) -> None:
    metrics = _metrics(tmp_path)
    diagnostics, store = _diagnostics(tmp_path)

    result = InstrumentedEngineCycleRunner(
        _Runner(metrics, make_requests=True),
        diagnostics,
        clock=lambda: NOW,
    ).run_cycle()

    assert result.ok
    events = store.list_events(limit=100)
    request_events = {
        event.event_name: event.details["total"]
        for event in events
        if event.component == "spotify.requests"
    }
    assert request_events == {
        "cycle_request_accounting": 6,
        "cycle_request_web_api_total": 5,
        "cycle_request_web_api_show_episodes": 2,
        "cycle_request_web_api_playlist_items": 1,
        "cycle_request_web_api_playlist_snapshot": 1,
        "cycle_request_web_api_replace_playlist_items": 1,
        "cycle_request_oauth_total": 1,
        "cycle_request_oauth_token_refresh": 1,
    }
    cycle_ids = {event.cycle_id for event in events if event.component == "spotify.requests"}
    assert len(cycle_ids) == 1
    assert next(iter(cycle_ids)) is not None


def test_unexpected_engine_failure_keeps_requests_already_transmitted(tmp_path: Path) -> None:
    metrics = _metrics(tmp_path)
    diagnostics, store = _diagnostics(tmp_path)

    with pytest.raises(RuntimeError, match="synthetic engine failure"):
        InstrumentedEngineCycleRunner(
            _FailingRunner(metrics),
            diagnostics,
            clock=lambda: NOW,
        ).run_cycle()

    events = store.list_events(limit=100)
    request_events = {
        event.event_name: event.details["total"]
        for event in events
        if event.component == "spotify.requests"
    }
    assert request_events == {
        "cycle_request_accounting": 1,
        "cycle_request_web_api_total": 1,
        "cycle_request_web_api_show_episodes": 1,
    }
    assert any(event.event_name == "cycle_unexpected_failure" for event in events)


def test_zero_request_cycle_has_explicit_accounting_marker(tmp_path: Path) -> None:
    metrics = _metrics(tmp_path)
    diagnostics, store = _diagnostics(tmp_path)

    InstrumentedEngineCycleRunner(
        _Runner(metrics, make_requests=False),
        diagnostics,
        clock=lambda: NOW,
    ).run_cycle()

    request_events = [
        event for event in store.list_events(limit=100) if event.component == "spotify.requests"
    ]
    assert [(event.event_name, event.details) for event in request_events] == [
        ("cycle_request_accounting", {"total": 0})
    ]
