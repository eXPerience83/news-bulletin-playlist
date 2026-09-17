from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from news_bulletin_playlist.diagnostics import DiagnosticEventStore
from news_bulletin_playlist.engine import EngineCycleResult, PlaylistCycleOutcome
from news_bulletin_playlist.models import PlaylistId
from news_bulletin_playlist.production_observability import (
    ProductionInstrumentedEngineCycleRunner,
)
from news_bulletin_playlist.runtime_diagnostics import OperationalDiagnostics
from news_bulletin_playlist.soft_retention import (
    SoftRetentionDecision,
    SoftRetentionDisposition,
)
from news_bulletin_playlist.soft_retention_diagnostics import publish_soft_retention_decision

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


@dataclass
class _Runner:
    result: EngineCycleResult
    playlist_id: PlaylistId
    decision: SoftRetentionDecision

    def run_cycle(self) -> EngineCycleResult:
        publish_soft_retention_decision(self.playlist_id, self.decision)
        return self.result


def _diagnostics(tmp_path: Path) -> tuple[OperationalDiagnostics, DiagnosticEventStore]:
    store = DiagnosticEventStore(tmp_path / "diagnostics.sqlite3")
    store.initialize()
    logger = logging.Logger("test-soft-retention-outcome-observability")
    logger.addHandler(logging.NullHandler())
    return OperationalDiagnostics(store, logger=logger), store


@pytest.mark.parametrize(
    ("disposition", "event_name", "wrote", "desired_count", "applied_count"),
    [
        (SoftRetentionDisposition.DEFER, "soft_retention_deferred", False, 3, 5),
        (
            SoftRetentionDisposition.FORCE_HARD_EXPIRY,
            "soft_retention_forced_hard_expiry",
            True,
            3,
            3,
        ),
    ],
)
def test_production_observability_emits_bounded_soft_retention_outcome(
    tmp_path: Path,
    disposition: SoftRetentionDisposition,
    event_name: str,
    wrote: bool,
    desired_count: int,
    applied_count: int,
) -> None:
    playlist_id = PlaylistId("spain")
    result = EngineCycleResult(
        started_at=NOW,
        finished_at=NOW,
        ok=True,
        sources=(),
        playlists=(
            PlaylistCycleOutcome(
                playlist_id=playlist_id,
                ok=True,
                desired_count=desired_count,
                applied_count=applied_count,
                wrote=wrote,
                last_success_at=NOW,
            ),
        ),
    )
    decision = SoftRetentionDecision(
        disposition=disposition,
        soft_expired_count=2,
        oldest_soft_expired_age_seconds=201_600,
    )
    diagnostics, diagnostic_store = _diagnostics(tmp_path)
    runner = ProductionInstrumentedEngineCycleRunner(
        _Runner(result, playlist_id, decision),
        diagnostics,
        clock=lambda: NOW,
    )

    assert runner.run_cycle() is result

    event = next(
        event
        for event in diagnostic_store.list_events(limit=20)
        if event.event_name == event_name
    )
    assert event.playlist_id == "spain"
    assert event.details == {"total": 2, "duration_seconds": 201_600}


def test_positive_soft_retention_outcome_does_not_leak_into_later_cycle(tmp_path: Path) -> None:
    playlist_id = PlaylistId("spain")
    result = EngineCycleResult(
        started_at=NOW,
        finished_at=NOW,
        ok=True,
        sources=(),
        playlists=(
            PlaylistCycleOutcome(
                playlist_id=playlist_id,
                ok=True,
                desired_count=3,
                applied_count=3,
                wrote=False,
                last_success_at=NOW,
            ),
        ),
    )
    diagnostics, diagnostic_store = _diagnostics(tmp_path)
    runner = ProductionInstrumentedEngineCycleRunner(
        _Runner(
            result,
            playlist_id,
            SoftRetentionDecision(
                disposition=SoftRetentionDisposition.FORCE_HARD_EXPIRY,
                soft_expired_count=1,
                oldest_soft_expired_age_seconds=202_000,
            ),
        ),
        diagnostics,
        clock=lambda: NOW,
    )

    runner.run_cycle()
    runner.runner.decision = SoftRetentionDecision(SoftRetentionDisposition.NOT_EXPIRY_ONLY)
    runner.run_cycle()

    events = [
        event
        for event in diagnostic_store.list_events(limit=50)
        if event.event_name == "soft_retention_forced_hard_expiry"
    ]
    assert len(events) == 1
