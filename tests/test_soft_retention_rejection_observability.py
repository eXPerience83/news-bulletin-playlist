from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

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
from news_bulletin_playlist.soft_retention_diagnostics import (
    SoftRetentionRejectionReason,
    publish_soft_retention_decision,
    publish_soft_retention_rejection,
)

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
PLAYLIST_ID = PlaylistId("spain")


def _result() -> EngineCycleResult:
    return EngineCycleResult(
        started_at=NOW,
        finished_at=NOW,
        ok=True,
        sources=(),
        playlists=(
            PlaylistCycleOutcome(
                playlist_id=PLAYLIST_ID,
                ok=True,
                desired_count=3,
                applied_count=3,
                wrote=True,
                last_success_at=NOW,
            ),
        ),
    )


def _diagnostics(tmp_path: Path) -> tuple[OperationalDiagnostics, DiagnosticEventStore]:
    store = DiagnosticEventStore(tmp_path / "diagnostics.sqlite3")
    store.initialize()
    logger = logging.Logger("test-soft-retention-rejection-observability")
    logger.addHandler(logging.NullHandler())
    return OperationalDiagnostics(store, logger=logger), store


@dataclass
class _RejectionRunner:
    result: EngineCycleResult
    reason: SoftRetentionRejectionReason

    def run_cycle(self) -> EngineCycleResult:
        publish_soft_retention_rejection(PLAYLIST_ID, self.reason)
        return self.result


@dataclass
class _MixedRunner:
    result: EngineCycleResult

    def run_cycle(self) -> EngineCycleResult:
        publish_soft_retention_rejection(
            PLAYLIST_ID,
            SoftRetentionRejectionReason.MATCHING_INPUT_MISSING,
        )
        publish_soft_retention_decision(
            PLAYLIST_ID,
            SoftRetentionDecision(
                disposition=SoftRetentionDisposition.DEFER,
                soft_expired_count=2,
                oldest_soft_expired_age_seconds=180_000,
            ),
        )
        return self.result


def test_production_observability_emits_bounded_rejection_reason(tmp_path: Path) -> None:
    result = _result()
    diagnostics, diagnostic_store = _diagnostics(tmp_path)
    runner = ProductionInstrumentedEngineCycleRunner(
        _RejectionRunner(result, SoftRetentionRejectionReason.MATCHING_INPUT_MISSING),
        diagnostics,
        clock=lambda: NOW,
    )

    assert runner.run_cycle() is result

    event = next(
        event
        for event in diagnostic_store.list_events(limit=20)
        if event.event_name == "soft_retention_evidence_rejected_matching_input_missing"
    )
    assert event.playlist_id == "spain"
    assert event.details == {"total": 1}


def test_not_expiry_only_is_not_published_as_evidence_rejection(tmp_path: Path) -> None:
    result = _result()
    diagnostics, diagnostic_store = _diagnostics(tmp_path)
    runner = ProductionInstrumentedEngineCycleRunner(
        _RejectionRunner(result, SoftRetentionRejectionReason.NOT_EXPIRY_ONLY),
        diagnostics,
        clock=lambda: NOW,
    )

    runner.run_cycle()

    assert not any(
        event.event_name.startswith("soft_retention_evidence_rejected_")
        for event in diagnostic_store.list_events(limit=20)
    )


def test_positive_outcome_wins_over_earlier_rejection_in_same_cycle(tmp_path: Path) -> None:
    result = _result()
    diagnostics, diagnostic_store = _diagnostics(tmp_path)
    runner = ProductionInstrumentedEngineCycleRunner(
        _MixedRunner(result),
        diagnostics,
        clock=lambda: NOW,
    )

    runner.run_cycle()

    events = diagnostic_store.list_events(limit=20)
    names = {event.event_name for event in events}
    assert "soft_retention_deferred" in names
    assert "soft_retention_evidence_rejected_matching_input_missing" not in names
