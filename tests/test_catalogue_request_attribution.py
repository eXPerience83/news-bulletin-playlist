from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from news_bulletin_playlist.diagnostics import DiagnosticEventStore
from news_bulletin_playlist.engine import (
    EngineCycleResult,
    SourceCycleOutcome,
    SourceMatchingDiagnostics,
)
from news_bulletin_playlist.engine_observability import InstrumentedEngineCycleRunner
from news_bulletin_playlist.models import SourceId
from news_bulletin_playlist.runtime_diagnostics import OperationalDiagnostics

NOW = datetime(2026, 9, 13, 8, 0, tzinfo=UTC)
ATTRIBUTION_KEYS = {
    "catalogue_matching_request_count",
    "catalogue_audit_request_count",
    "catalogue_prewrite_request_count",
}


@dataclass
class _Runner:
    result: EngineCycleResult

    def run_cycle(self) -> EngineCycleResult:
        return self.result


def _source_event_details(
    tmp_path: Path,
    matching: SourceMatchingDiagnostics,
) -> dict[str, str | int | bool | None]:
    store = DiagnosticEventStore(tmp_path / "state.sqlite3")
    store.initialize()
    logger = logging.getLogger(f"catalogue-attribution-{tmp_path.name}")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    diagnostics = OperationalDiagnostics(store, logger=logger)
    source = SourceCycleOutcome(
        source_id=SourceId("ser"),
        collection_ok=True,
        matching_ok=True,
        edition_count=1,
        matched_count=1,
        last_success_at=NOW,
        matching_diagnostics=matching,
    )
    result = EngineCycleResult(
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=1),
        ok=True,
        sources=(source,),
        playlists=(),
    )

    InstrumentedEngineCycleRunner(
        _Runner(result),
        diagnostics,
        clock=lambda: NOW,
    ).run_cycle()

    event = next(item for item in store.list_events(limit=10) if item.source_id == "ser")
    return event.details


@pytest.mark.parametrize(
    ("matching_work", "audit_due", "conservative", "total", "expected"),
    [
        (2, False, False, 7, (7, 0, 0)),
        (0, True, False, 5, (0, 5, 0)),
        (0, False, True, 4, (0, 4, 0)),
        (0, False, False, 6, (0, 0, 6)),
    ],
)
def test_catalogue_requests_are_attributed_to_first_requiring_phase(
    tmp_path: Path,
    matching_work: int,
    audit_due: bool,
    conservative: bool,
    total: int,
    expected: tuple[int, int, int],
) -> None:
    details = _source_event_details(
        tmp_path,
        SourceMatchingDiagnostics(
            matching_work_count=matching_work,
            catalogue_audit_due=audit_due,
            matching_plan_conservative=conservative,
            catalogue_requested=True,
            catalogue_inspected=True,
            catalogue_request_count=total,
        ),
    )

    observed = (
        details["catalogue_matching_request_count"],
        details["catalogue_audit_request_count"],
        details["catalogue_prewrite_request_count"],
    )
    assert observed == expected
    assert sum(observed) == details["catalogue_request_count"] == total


def test_zero_catalogue_requests_omit_attribution_fields(tmp_path: Path) -> None:
    details = _source_event_details(
        tmp_path,
        SourceMatchingDiagnostics(catalogue_request_count=0),
    )

    assert details["catalogue_request_count"] == 0
    assert ATTRIBUTION_KEYS.isdisjoint(details)
