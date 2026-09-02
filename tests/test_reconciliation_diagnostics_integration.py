from __future__ import annotations

import io
import logging
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from news_bulletin_playlist.diagnostics import DiagnosticEventStore
from news_bulletin_playlist.diagnostics_web import (
    DiagnosticFilters,
    build_diagnostic_bundle,
    render_diagnostics_page,
)
from news_bulletin_playlist.engine import EngineCycleResult, PlaylistCycleOutcome
from news_bulletin_playlist.engine_observability import InstrumentedEngineCycleRunner
from news_bulletin_playlist.models import PlaylistId
from news_bulletin_playlist.runtime_diagnostics import OperationalDiagnostics

NOW = datetime(2026, 9, 2, 17, 51, 12, tzinfo=UTC)
SECRET = "access-token-sentinel-never-export"


class _Runner:
    def __init__(self, result: EngineCycleResult) -> None:
        self.result = result

    def run_cycle(self) -> EngineCycleResult:
        return self.result


def test_live_shape_failure_is_safe_in_store_ui_and_zip(tmp_path: Path) -> None:
    store = DiagnosticEventStore(tmp_path / "state.sqlite3")
    store.initialize()
    output = io.StringIO()
    logger = logging.Logger("test-reconciliation-diagnostics-integration")
    logger.setLevel(logging.INFO)
    logger.addHandler(logging.StreamHandler(output))
    diagnostics = OperationalDiagnostics(store, logger=logger)

    raw_error = (
        "Spotify playlist prewrite response pagination truncated before total "
        f"(offset=50 returned=24 total=80 next=null) {SECRET}"
    )
    result = EngineCycleResult(
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=4),
        ok=False,
        sources=(),
        playlists=(
            PlaylistCycleOutcome(
                playlist_id=PlaylistId("spain_spanish_news"),
                ok=False,
                desired_count=80,
                applied_count=None,
                wrote=None,
                last_success_at=NOW - timedelta(minutes=10),
                error=raw_error,
            ),
        ),
        error=f"cycle failure includes {SECRET}",
    )

    runner = InstrumentedEngineCycleRunner(
        _Runner(result),
        diagnostics,
        clock=lambda: NOW,
    )
    runner.run_cycle()

    events = store.list_events(limit=10)
    playlist_event = next(
        event for event in events if event.event_name == "playlist_reconciliation_failed"
    )
    assert playlist_event.details == {
        "desired_count": 80,
        "failure_class": "pagination_error",
        "phase": "prewrite",
        "offset": 50,
        "returned_count": 24,
        "total": 80,
        "next_state": "null",
        "verification_outcome": "failed",
        "write_decision": "blocked",
    }
    assert SECRET not in repr(events)
    assert SECRET not in output.getvalue()

    page = render_diagnostics_page(
        events=(playlist_event,),
        filters=DiagnosticFilters(hours=24, limit=200),
    )
    assert b"failure_class=pagination_error" in page
    assert b"phase=prewrite" in page
    assert SECRET.encode() not in page

    bundle = build_diagnostic_bundle(
        events=(playlist_event,),
        generated_at=NOW,
        last_cycle=result,
        retention_days=30,
        max_events=10_000,
    )
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        combined = b"\n".join(archive.read(name) for name in archive.namelist())
    assert b'"failure_class":"pagination_error"' in combined
    assert b'"offset":50' in combined
    assert SECRET.encode() not in combined
