from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from news_bulletin_playlist.collection import CollectionDiagnostics
from news_bulletin_playlist.diagnostics import DiagnosticEventStore
from news_bulletin_playlist.engine import EngineCycleResult, SourceCycleOutcome
from news_bulletin_playlist.models import (
    CountryCode,
    EngineConfig,
    LanguageTag,
    ParserId,
    SourceDefinition,
    SourceId,
)
from news_bulletin_playlist.persistence import SQLiteStore
from news_bulletin_playlist.production_observability import (
    ProductionInstrumentedEngineCycleRunner,
)
from news_bulletin_playlist.runtime_diagnostics import OperationalDiagnostics
from news_bulletin_playlist.spotify_planner import SpotifyPlannerJournal

NOW = datetime(2026, 9, 17, 10, 0, tzinfo=UTC)


def _source() -> SourceDefinition:
    return SourceDefinition(
        id=SourceId("ser"),
        display_name="SER",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        timezone=ZoneInfo("Europe/Madrid"),
        enabled=True,
        parser_id=ParserId("ser"),
        endpoint_url="https://example.test/ser.xml",
    )


@dataclass
class _Runner:
    config: EngineConfig
    store: SQLiteStore
    planner_journal: SpotifyPlannerJournal
    result: EngineCycleResult

    def run_cycle(self) -> EngineCycleResult:
        return self.result


def _diagnostics(tmp_path: Path) -> tuple[OperationalDiagnostics, DiagnosticEventStore]:
    store = DiagnosticEventStore(tmp_path / "diagnostics.sqlite3")
    store.initialize()
    logger = logging.Logger("title-recovery-observability")
    logger.addHandler(logging.NullHandler())
    return OperationalDiagnostics(store, logger=logger), store


def test_production_emits_bounded_recovery_mode_counts_and_unresolved_drift(
    tmp_path: Path,
) -> None:
    source = _source()
    config = EngineConfig(schema_version=1, sources=(source,), playlists=())
    store = SQLiteStore(tmp_path / "engine.sqlite3")
    store.initialize()
    journal = SpotifyPlannerJournal(store)
    collection = CollectionDiagnostics(
        rss_item_count=6,
        rss_title_parser_attempted_count=6,
        rss_title_parser_exact_count=2,
        rss_title_parser_recovered_count=2,
        rss_title_parser_normalized_recovered_count=1,
        rss_title_parser_provider_compat_recovered_count=1,
        rss_title_parser_rejected_count=2,
        failure_reason="title_parser_drift",
    )
    result = EngineCycleResult(
        started_at=NOW,
        finished_at=NOW,
        ok=False,
        sources=(
            SourceCycleOutcome(
                source_id=source.id,
                collection_ok=False,
                matching_ok=None,
                edition_count=0,
                matched_count=0,
                last_success_at=None,
                error="title parser contract degraded",
                collection_diagnostics=collection,
            ),
        ),
        playlists=(),
    )
    diagnostics, diagnostic_store = _diagnostics(tmp_path)
    runner = ProductionInstrumentedEngineCycleRunner(
        _Runner(config, store, journal, result),
        diagnostics,
        clock=lambda: NOW,
    )

    assert runner.run_cycle() is result

    events = diagnostic_store.list_events(limit=100)
    by_name = {event.event_name: event for event in events}
    assert by_name["title_parse_exact"].details == {"total": 2}
    assert by_name["title_parse_normalized_recovered"].details == {"total": 1}
    assert by_name["title_parse_provider_compat_recovered"].details == {"total": 1}
    assert by_name["title_parse_rejected"].details == {"total": 2}
    assert by_name["title_format_drift_unresolved"].details == {"total": 2}
    assert by_name["title_format_drift_unresolved"].source_id == "ser"

    serialized = "\n".join(str(event.details) for event in events)
    assert "Las noticias" not in serialized
    assert "https://example.test" not in serialized
