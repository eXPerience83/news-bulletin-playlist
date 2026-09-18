from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from _spotify_catalogue import catalogue_page

from news_bulletin_playlist.collection import CollectionDiagnostics
from news_bulletin_playlist.diagnostics import DiagnosticEventStore
from news_bulletin_playlist.engine import EngineCycleResult, EngineRunner, SourceCycleOutcome
from news_bulletin_playlist.models import (
    AdapterId,
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


def test_real_engine_drift_observability_adds_no_spotify_or_oauth_work(
    tmp_path: Path,
) -> None:
    title = "Las noticias de la SER, 11:00 (17/09/2026)"
    episode_uri = "spotify:episode:0000000000000000000001"
    clock = [NOW]
    drift = [False]
    rss_calls = 0

    source = replace(
        _source(),
        external_references=(
            ExternalReference(
                system="spotify",
                resource_type="show",
                external_id="ser-show",
            ),
        ),
    )
    playlist = PlaylistDefinition(
        id=PlaylistId("ser-news"),
        display_name="SER News",
        description="test",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        enabled=True,
        source_selection=SourceSelection((source.id,)),
        destination=DestinationReference(
            adapter_id=AdapterId("spotify"),
            external_id="destination-ser",
        ),
    )
    config = EngineConfig(schema_version=1, sources=(source,), playlists=(playlist,))

    class CountingAuth:
        def __init__(self) -> None:
            self.calls = 0

        def planner_authorization_context(self) -> str:
            return "a" * 64

        def get_access_token(self, *, now: datetime | None = None) -> str:
            assert now is not None
            self.calls += 1
            return "access-token"

    class CountingSpotify:
        def __init__(self) -> None:
            self.show_calls = 0
            self.playlist_reads: list[str] = []
            self.snapshot_read_calls: list[str] = []
            self.replacements: list[tuple[str, tuple[str, ...]]] = []
            self.items: dict[str, list[str]] = {}
            self.snapshots: dict[str, str] = {}

        def show_episodes(
            self,
            show_id: str,
            *,
            limit: int = 50,
            offset: int = 0,
        ):
            assert show_id == "ser-show"
            self.show_calls += 1
            return catalogue_page(
                [
                    {
                        "uri": episode_uri,
                        "name": title,
                        "release_date": "2026-09-17",
                        "release_date_precision": "day",
                        "duration_ms": 60_000,
                    }
                ],
                offset=offset,
                limit=limit,
            )

        def playlist_items(
            self,
            playlist_id: str,
            *,
            limit: int = 50,
            offset: int = 0,
        ):
            self.playlist_reads.append(playlist_id)
            uris = self.items.get(playlist_id, [])
            selected = uris[offset : offset + limit]
            return {
                "items": [{"item": {"uri": uri}} for uri in selected],
                "next": None,
                "total": len(uris),
            }

        def replace_playlist_items(self, playlist_id: str, uris: list[str]):
            self.items[playlist_id] = list(uris)
            self.replacements.append((playlist_id, tuple(uris)))
            snapshot = f"snapshot-{len(self.replacements)}"
            self.snapshots[playlist_id] = snapshot
            return {"snapshot_id": snapshot}

        def playlist_snapshot(self, playlist_id: str):
            self.snapshot_read_calls.append(playlist_id)
            return {"snapshot_id": self.snapshots.get(playlist_id, "snapshot-initial")}

    def healthy_rss() -> bytes:
        return (
            "<rss><channel><item>"
            "<guid>ser-1</guid>"
            f"<title>{title}</title>"
            "<pubDate>Thu, 17 Sep 2026 09:05:00 +0000</pubDate>"
            "</item></channel></rss>"
        ).encode()

    def drifted_rss() -> bytes:
        items = (
            ("ser-1", "Las noticias de la SER, 11:01 (17/09/2026)"),
            ("ser-2", "SER DEPORTES - 12:00 (17/09/2026)"),
            ("ser-3", "new unreviewed SER title shape"),
        )
        body = "".join(
            "<item>"
            f"<guid>{native_id}</guid>"
            f"<title>{candidate_title}</title>"
            "<pubDate>Thu, 17 Sep 2026 10:05:00 +0000</pubDate>"
            "</item>"
            for native_id, candidate_title in items
        )
        return f"<rss><channel>{body}</channel></rss>".encode()

    def fetcher(_url: str) -> bytes:
        nonlocal rss_calls
        rss_calls += 1
        return drifted_rss() if drift[0] else healthy_rss()

    auth = CountingAuth()
    spotify = CountingSpotify()
    store = SQLiteStore(tmp_path / "real-engine.sqlite3")
    store.initialize()
    engine = EngineRunner(
        config,
        store,
        auth,
        fetcher=fetcher,
        client_factory=lambda _token: spotify,
        clock=lambda: clock[0],
    )

    first = engine.run_cycle()
    assert first.ok
    provider_counts = (
        auth.calls,
        spotify.show_calls,
        len(spotify.playlist_reads),
        len(spotify.snapshot_read_calls),
        len(spotify.replacements),
    )
    rss_before = rss_calls

    diagnostics, diagnostic_store = _diagnostics(tmp_path)
    wrapped = ProductionInstrumentedEngineCycleRunner(
        engine,
        diagnostics,
        clock=lambda: clock[0],
    )
    drift[0] = True
    clock[0] = NOW + timedelta(minutes=10)

    result = wrapped.run_cycle()

    assert not result.ok
    assert result.sources[0].collection_diagnostics.failure_reason == "title_parser_drift"
    assert rss_calls == rss_before + 1
    assert (
        auth.calls,
        spotify.show_calls,
        len(spotify.playlist_reads),
        len(spotify.snapshot_read_calls),
        len(spotify.replacements),
    ) == provider_counts

    events = diagnostic_store.list_events(limit=100)
    by_name = {event.event_name: event for event in events}
    assert by_name["title_parse_rejected"].details == {"total": 2}
    assert by_name["title_format_drift_unresolved"].details == {"total": 2}
    assert by_name["title_format_drift_unresolved"].source_id == "ser"
