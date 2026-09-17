from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from news_bulletin_playlist.diagnostics import DiagnosticEventStore
from news_bulletin_playlist.engine import (
    EngineCycleResult,
    PlaylistCycleOutcome,
    SourceCycleOutcome,
    SourceMatchingDiagnostics,
)
from news_bulletin_playlist.engine_runtime import DEFAULT_CONFIG_FILENAME
from news_bulletin_playlist.models import (
    AdapterId,
    CanonicalEdition,
    CountryCode,
    DestinationReference,
    EngineConfig,
    ExternalReference,
    LanguageTag,
    ParserId,
    PlaylistDefinition,
    PlaylistId,
    SourceDefinition,
    SourceId,
    SourceSelection,
)
from news_bulletin_playlist.persistence import MatchStatus, SQLiteStore
from news_bulletin_playlist.production_observability import (
    ProductionInstrumentedEngineCycleRunner,
)
from news_bulletin_playlist.runtime_diagnostics import OperationalDiagnostics
from news_bulletin_playlist.spotify_planner import (
    SpotifyPlannerJournal,
    matching_input_fingerprint,
)

NOW = datetime(2026, 9, 16, 7, 0, tzinfo=UTC)
URI = "spotify:episode:0000000000000000000001"


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
        external_references=(
            ExternalReference(
                system="spotify",
                resource_type="show",
                external_id="ser-show",
            ),
        ),
    )


def _playlist() -> PlaylistDefinition:
    return PlaylistDefinition(
        id=PlaylistId("spain"),
        display_name="Spain",
        description="test",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        enabled=True,
        source_selection=SourceSelection((SourceId("ser"),)),
        destination=DestinationReference(
            adapter_id=AdapterId("spotify"),
            external_id="destination-spain",
        ),
        retention_hours=48,
    )


def _edition(native_id: str) -> CanonicalEdition:
    return CanonicalEdition(
        source_id=SourceId("ser"),
        source_native_id=native_id,
        title="Las noticias de la SER, 08:00 (16/09/2026)",
        published_at=NOW - timedelta(hours=1),
        edition_at=NOW - timedelta(hours=1),
        duration_seconds=60,
    )


def _valid_config() -> str:
    return """schema_version: 1
sources:
  - id: ser
    display_name: Cadena SER
    countries: [ES]
    languages: [es]
    timezone: Europe/Madrid
    enabled: true
    parser_id: ser
    endpoint_url: https://example.test/ser.xml
    external_references:
      - system: spotify
        resource_type: show
        external_id: ser-show
playlists:
  - id: spain
    display_name: Spain
    description: test
    countries: [ES]
    languages: [es]
    enabled: true
    retention_hours: 48
    source_selection:
      explicit: [ser]
    destination:
      adapter_id: spotify
      external_id: destination-spain
"""


@dataclass
class _Runner:
    config: EngineConfig
    store: SQLiteStore
    planner_journal: SpotifyPlannerJournal
    result: EngineCycleResult

    def run_cycle(self) -> EngineCycleResult:
        return self.result


@dataclass
class _ReloadingRunnerShape:
    data_dir: Path
    environ: dict[str, str]
    store: SQLiteStore
    result: EngineCycleResult

    def run_cycle(self) -> EngineCycleResult:
        return self.result


@dataclass
class _PruningRunner:
    config: EngineConfig
    store: SQLiteStore
    result: EngineCycleResult
    fresh: CanonicalEdition

    def run_cycle(self) -> EngineCycleResult:
        self.store.upsert_editions((self.fresh,), observed_at=NOW)
        self.store.prune_operational_history(now=NOW, protected_identities=())
        return self.result


class _NoListEditionsStore(SQLiteStore):
    def list_editions(self, *args: object, **kwargs: object) -> tuple[CanonicalEdition, ...]:
        del args, kwargs
        raise AssertionError("production observability must not scan canonical_editions")


def _diagnostics(tmp_path: Path) -> tuple[OperationalDiagnostics, DiagnosticEventStore]:
    store = DiagnosticEventStore(tmp_path / "diagnostics.sqlite3")
    store.initialize()
    logger = logging.Logger("test-production-observability")
    logger.addHandler(logging.NullHandler())
    return OperationalDiagnostics(store, logger=logger), store


def test_production_diagnostics_expose_recovery_backoff_barrier_and_untracked_state(
    tmp_path: Path,
) -> None:
    source = _source()
    config = EngineConfig(schema_version=1, sources=(source,), playlists=(_playlist(),))
    store = SQLiteStore(tmp_path / "engine.sqlite3")
    store.initialize()
    backed_off = _edition("backed-off")
    untracked = _edition("untracked")
    store.upsert_editions((backed_off, untracked), observed_at=NOW)
    for edition in (backed_off, untracked):
        assert store.set_match_state(
            edition.source_id,
            edition.source_native_id,
            status=MatchStatus.MATCHED,
            spotify_episode_uri=URI,
            spotify_duration_seconds=60,
            diagnostics="matched",
            updated_at=NOW,
        )

    journal = SpotifyPlannerJournal(store)
    journal.record_matched_recovery(
        source_id="ser",
        source_native_id=backed_off.source_native_id,
        input_fingerprint=matching_input_fingerprint(source, backed_off),
        attempted_at=NOW,
    )
    durable = store.get_match_state(backed_off.source_id, backed_off.source_native_id)
    assert durable is not None
    journal.record_persistence_rejection_barrier(durable, rejected_at=NOW)

    result = EngineCycleResult(
        started_at=NOW + timedelta(minutes=10),
        finished_at=NOW + timedelta(minutes=10),
        ok=True,
        sources=(
            SourceCycleOutcome(
                source_id=source.id,
                collection_ok=True,
                matching_ok=True,
                edition_count=2,
                matched_count=2,
                last_success_at=NOW,
                matching_diagnostics=SourceMatchingDiagnostics(
                    matching_reason_evidence_recovery_count=1,
                ),
            ),
        ),
        playlists=(),
    )
    diagnostics, diagnostic_store = _diagnostics(tmp_path)
    runner = ProductionInstrumentedEngineCycleRunner(
        _Runner(config, store, journal, result),
        diagnostics,
        clock=lambda: NOW + timedelta(minutes=10),
    )

    assert runner.run_cycle() is result

    events = diagnostic_store.list_events(limit=50)
    by_name = {event.event_name: event for event in events}
    assert by_name["matching_recovery_attempted"].details == {"total": 1}
    assert by_name["matching_recovery_backoff_active"].details == {"total": 1}
    assert by_name["matching_recovery_untracked"].details == {"total": 1}
    assert by_name["matching_persistence_rejection_barrier_active"].details == {"total": 1}
    assert by_name["canonical_editions_pruned"].details == {"total": 0}
    assert "matching_recovery_due_active" not in by_name


def test_production_diagnostics_load_config_from_reloading_runner_shape(tmp_path: Path) -> None:
    (tmp_path / DEFAULT_CONFIG_FILENAME).write_text(_valid_config(), encoding="utf-8")
    store = SQLiteStore(tmp_path / "engine.sqlite3")
    store.initialize()
    edition = _edition("untracked-production")
    store.upsert_editions((edition,), observed_at=NOW)
    assert store.set_match_state(
        edition.source_id,
        edition.source_native_id,
        status=MatchStatus.MATCHED,
        spotify_episode_uri=URI,
        spotify_duration_seconds=60,
        diagnostics="matched",
        updated_at=NOW,
    )
    result = EngineCycleResult(
        started_at=NOW,
        finished_at=NOW + timedelta(minutes=1),
        ok=True,
        sources=(
            SourceCycleOutcome(
                source_id=SourceId("ser"),
                collection_ok=True,
                matching_ok=True,
                edition_count=1,
                matched_count=1,
                last_success_at=NOW,
            ),
        ),
        playlists=(),
    )
    diagnostics, diagnostic_store = _diagnostics(tmp_path)
    runner = ProductionInstrumentedEngineCycleRunner(
        _ReloadingRunnerShape(tmp_path, {}, store, result),
        diagnostics,
        clock=lambda: NOW,
    )

    runner.run_cycle()

    events = diagnostic_store.list_events(limit=50)
    untracked = next(event for event in events if event.event_name == "matching_recovery_untracked")
    assert untracked.source_id == "ser"
    assert untracked.details == {"total": 1}


def test_production_diagnostics_make_soft_retention_and_barrier_preservation_explicit(
    tmp_path: Path,
) -> None:
    config = EngineConfig(schema_version=1, sources=(_source(),), playlists=(_playlist(),))
    store = SQLiteStore(tmp_path / "engine.sqlite3")
    store.initialize()
    journal = SpotifyPlannerJournal(store)
    result = EngineCycleResult(
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=1),
        ok=False,
        sources=(),
        playlists=(
            PlaylistCycleOutcome(
                playlist_id=PlaylistId("spain"),
                ok=True,
                desired_count=4,
                applied_count=5,
                wrote=False,
                last_success_at=NOW,
            ),
            PlaylistCycleOutcome(
                playlist_id=PlaylistId("blocked"),
                ok=False,
                desired_count=4,
                applied_count=None,
                wrote=None,
                last_success_at=NOW,
                error=(
                    "destination preserved because a Spotify matching repair was rejected by "
                    "durable persistence for source(s): ser"
                ),
            ),
        ),
    )
    diagnostics, diagnostic_store = _diagnostics(tmp_path)
    runner = ProductionInstrumentedEngineCycleRunner(
        _Runner(config, store, journal, result),
        diagnostics,
        clock=lambda: NOW,
    )

    runner.run_cycle()

    events = diagnostic_store.list_events(limit=50)
    deferred = next(event for event in events if event.event_name == "soft_retention_deferred")
    assert deferred.playlist_id == "spain"
    assert deferred.details == {"total": 1}
    preserved = next(
        event
        for event in events
        if event.event_name == "destination_preserved_matching_persistence_rejection"
    )
    assert preserved.playlist_id == "blocked"
    assert preserved.details == {"total": 1}


def test_production_prune_diagnostics_use_exact_prune_result_without_canonical_scans(
    tmp_path: Path,
) -> None:
    config = EngineConfig(schema_version=1, sources=(_source(),), playlists=(_playlist(),))
    store = _NoListEditionsStore(tmp_path / "engine.sqlite3")
    store.initialize()
    stale_at = NOW - timedelta(days=31)
    stale = CanonicalEdition(
        source_id=SourceId("ser"),
        source_native_id="stale",
        title="stale",
        published_at=stale_at,
        edition_at=stale_at,
        duration_seconds=60,
    )
    fresh = _edition("fresh")
    store.upsert_editions((stale,), observed_at=stale_at)
    result = EngineCycleResult(
        started_at=NOW,
        finished_at=NOW,
        ok=True,
        sources=(),
        playlists=(),
    )
    diagnostics, diagnostic_store = _diagnostics(tmp_path)
    runner = ProductionInstrumentedEngineCycleRunner(
        _PruningRunner(config, store, result, fresh),
        diagnostics,
        clock=lambda: NOW,
    )

    assert runner.run_cycle() is result

    event = next(
        event
        for event in diagnostic_store.list_events(limit=20)
        if event.event_name == "canonical_editions_pruned"
    )
    assert event.details == {"total": 1}
    assert store.get_edition(SourceId("ser"), "stale") is None
    assert store.get_edition(SourceId("ser"), "fresh") == fresh
