from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from news_bulletin_playlist.diagnostics import DiagnosticEventStore, DiagnosticSeverity
from news_bulletin_playlist.engine import EngineCycleResult, PlaylistCycleOutcome
from news_bulletin_playlist.engine_observability import InstrumentedEngineCycleRunner
from news_bulletin_playlist.models import PlaylistId
from news_bulletin_playlist.runtime_diagnostics import OperationalDiagnostics

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
