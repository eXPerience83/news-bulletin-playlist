from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from news_bulletin_playlist.diagnostics import DiagnosticEventStore
from news_bulletin_playlist.models import (
    CountryCode,
    ExternalReference,
    LanguageTag,
    ParserId,
    SourceDefinition,
    SourceId,
)
from news_bulletin_playlist.persistence import PersistenceError, SQLiteStore
from news_bulletin_playlist.runtime_diagnostics import OperationalDiagnostics
from news_bulletin_playlist.spotify.public_detector import (
    DETECTOR_SCHEMA_VERSION,
    PublicDetector,
    PublicDetectorJournal,
    PublicDetectorOutcome,
    PublicEpisodeObservation,
    PublicHttpResponse,
    PublicReleaseDateShape,
    PublicShowClient,
    PublicShowConfigurationError,
    PublicShowHttpError,
    PublicShowResponseError,
    PublicShowResponseFailureReason,
    PublicShowTransportError,
    parse_public_show_embed,
    spotify_show_id,
)
from news_bulletin_playlist.spotify_metrics import (
    SpotifyPublicOperation,
    SpotifyRequestMetrics,
    SpotifyRequestOutcome,
)

NOW = datetime(2026, 9, 19, 10, 0, tzinfo=UTC)
SHOW_ID = "S" * 22
EPISODE_A = "A" * 22
EPISODE_B = "B" * 22


def _embed_html(
    episode_id: str = EPISODE_A,
    *,
    title: str = "Bulletin 12:00",
    show_id: str = SHOW_ID,
    entity_type: str = "episode",
    release_date: object = "2026-09-19T10:00:00Z",
) -> str:
    payload = {
        "props": {
            "pageProps": {
                "state": {
                    "data": {
                        "entity": {
                            "type": entity_type,
                            "id": episode_id,
                            "uri": f"spotify:episode:{episode_id}",
                            "name": title,
                            "title": title,
                            "releaseDate": release_date,
                            "duration": 180000,
                            "relatedEntityUri": f"spotify:show:{show_id}",
                        }
                    }
                }
            }
        }
    }
    return (
        "<!doctype html><html><body>"
        '<script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps(payload)}"
        "</script></body></html>"
    )


def _observation(
    episode_id: str,
    *,
    title: str = "Bulletin",
    observed_at: datetime = NOW,
) -> PublicEpisodeObservation:
    return PublicEpisodeObservation(
        source_id=SourceId("source"),
        spotify_show_id=SHOW_ID,
        episode_id=episode_id,
        episode_uri=f"spotify:episode:{episode_id}",
        name=title,
        title=title,
        release_date_raw="2026-09-19T10:00:00Z",
        duration_ms=180000,
        related_entity_uri=f"spotify:show:{SHOW_ID}",
        observed_at=observed_at,
    )


def _source(*, show_ids: tuple[str, ...] = (SHOW_ID,)) -> SourceDefinition:
    return SourceDefinition(
        id=SourceId("source"),
        display_name="Source",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        timezone=ZoneInfo("Europe/Madrid"),
        enabled=True,
        parser_id=ParserId("ser"),
        external_references=tuple(
            ExternalReference("spotify", "show", show_id) for show_id in show_ids
        ),
    )


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    return store


def _diagnostics(store: SQLiteStore) -> tuple[OperationalDiagnostics, DiagnosticEventStore]:
    diagnostic_store = DiagnosticEventStore(store.path)
    diagnostic_store.initialize()
    logger = logging.getLogger(f"public-detector-test-{id(store)}")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return OperationalDiagnostics(diagnostic_store, logger=logger), diagnostic_store


class _StaticTransport:
    def __init__(self, responses: dict[str, PublicHttpResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    def get(self, url: str, *, accept: str) -> PublicHttpResponse:
        self.calls.append((url, accept))
        return self.responses[url]


class _SequenceClient:
    def __init__(
        self,
        observations: list[PublicEpisodeObservation | Exception],
        *,
        oembed_titles: list[str | Exception] | None = None,
    ) -> None:
        self.observations = list(observations)
        self.oembed_titles = list(oembed_titles or [])
        self.observe_calls = 0
        self.oembed_calls = 0

    def observe_episode(
        self,
        source_id: SourceId,
        spotify_show_id: str,
        *,
        observed_at: datetime,
    ) -> PublicEpisodeObservation:
        del source_id, spotify_show_id, observed_at
        self.observe_calls += 1
        result = self.observations.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def oembed_title(self, spotify_show_id: str) -> str:
        del spotify_show_id
        self.oembed_calls += 1
        result = self.oembed_titles.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_parse_public_show_embed_extracts_valid_episode_entity() -> None:
    observation = parse_public_show_embed(
        _embed_html(),
        source_id=SourceId("source"),
        spotify_show_id=SHOW_ID,
        observed_at=NOW,
    )

    assert observation.episode_id == EPISODE_A
    assert observation.episode_uri == f"spotify:episode:{EPISODE_A}"
    assert observation.original_title == "Bulletin 12:00"
    assert observation.release_date_raw == "2026-09-19T10:00:00Z"
    assert observation.duration_ms == 180000
    assert observation.related_entity_uri == f"spotify:show:{SHOW_ID}"
    assert observation.detector_schema_version == DETECTOR_SCHEMA_VERSION


@pytest.mark.parametrize(
    ("html", "message", "reason"),
    [
        (
            "<html></html>",
            "__NEXT_DATA__",
            PublicShowResponseFailureReason.NEXT_DATA_COUNT,
        ),
        (
            '<script id="__NEXT_DATA__">{bad json</script>',
            "invalid JSON",
            PublicShowResponseFailureReason.NEXT_DATA_JSON,
        ),
        (
            _embed_html(entity_type="show"),
            "not an episode",
            PublicShowResponseFailureReason.EMBED_ENTITY_TYPE,
        ),
    ],
)
def test_parse_public_show_embed_rejects_invalid_contract(
    html: str,
    message: str,
    reason: PublicShowResponseFailureReason,
) -> None:
    with pytest.raises(PublicShowResponseError, match=message) as exc_info:
        parse_public_show_embed(
            html,
            source_id=SourceId("source"),
            spotify_show_id=SHOW_ID,
            observed_at=NOW,
        )

    assert exc_info.value.reason is reason


def test_parse_public_show_embed_supports_release_date_iso_string_object() -> None:
    observation = parse_public_show_embed(
        _embed_html(release_date={"isoString": "2026-09-19T18:31:00Z"}),
        source_id=SourceId("source"),
        spotify_show_id=SHOW_ID,
        observed_at=NOW,
    )

    assert observation.episode_id == EPISODE_A
    assert observation.release_date_raw == "2026-09-19T18:31:00Z"
    assert observation.release_date_shape is PublicReleaseDateShape.OBJECT_ISO_STRING


@pytest.mark.parametrize(
    "release_date",
    [
        "",
        123,
        {"isoString": ""},
        {"isoString": 123},
        {"unexpected": "2026-09-19T10:00:00Z"},
        {"isoString": "2026-09-19T10:00:00Z", "extra": "field"},
    ],
)
def test_parse_public_show_embed_tolerates_unsupported_release_date_metadata(
    release_date: object,
) -> None:
    observation = parse_public_show_embed(
        _embed_html(release_date=release_date),
        source_id=SourceId("source"),
        spotify_show_id=SHOW_ID,
        observed_at=NOW,
    )

    assert observation.episode_id == EPISODE_A
    assert observation.release_date_raw is None
    assert observation.release_date_shape in {
        PublicReleaseDateShape.UNSUPPORTED_STRING,
        PublicReleaseDateShape.UNSUPPORTED_OBJECT,
        PublicReleaseDateShape.UNSUPPORTED_NUMBER,
    }


def test_parse_public_show_embed_rejects_duplicate_next_data() -> None:
    html = _embed_html() + _embed_html()
    with pytest.raises(PublicShowResponseError, match="exactly one") as exc_info:
        parse_public_show_embed(
            html,
            source_id=SourceId("source"),
            spotify_show_id=SHOW_ID,
            observed_at=NOW,
        )

    assert exc_info.value.reason is PublicShowResponseFailureReason.NEXT_DATA_COUNT


def test_parse_public_show_embed_wraps_invalid_episode_id() -> None:
    html = _embed_html(episode_id="invalid")
    with pytest.raises(PublicShowResponseError, match="episode id was invalid") as exc_info:
        parse_public_show_embed(
            html,
            source_id=SourceId("source"),
            spotify_show_id=SHOW_ID,
            observed_at=NOW,
        )

    assert exc_info.value.reason is PublicShowResponseFailureReason.EMBED_FIELD


def test_public_show_client_reads_embed_and_oembed_without_auth_headers() -> None:
    embed_url = f"https://open.spotify.com/embed/show/{SHOW_ID}"
    show_url = f"https://open.spotify.com/show/{SHOW_ID}"
    oembed_url = "https://open.spotify.com/oembed?url=" + (
        "https%3A%2F%2Fopen.spotify.com%2Fshow%2F" + SHOW_ID
    )
    transport = _StaticTransport(
        {
            embed_url: PublicHttpResponse(
                status=200,
                content_type="text/html",
                body=_embed_html().encode(),
            ),
            oembed_url: PublicHttpResponse(
                status=200,
                content_type="application/json",
                body=json.dumps({"title": "Bulletin 12:00"}).encode(),
            ),
        }
    )
    client = PublicShowClient(transport)

    observation = client.observe_episode(
        SourceId("source"),
        SHOW_ID,
        observed_at=NOW,
    )
    title = client.oembed_title(SHOW_ID)

    assert observation.episode_id == EPISODE_A
    assert title == "Bulletin 12:00"
    assert transport.calls == [
        (embed_url, "text/html,application/xhtml+xml"),
        (oembed_url, "application/json"),
    ]
    assert show_url in oembed_url.replace("%3A", ":").replace("%2F", "/")


def test_public_metrics_count_unchanged_embed_without_oembed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    diagnostics, _ = _diagnostics(store)
    journal = PublicDetectorJournal(store)
    journal.initialize()
    journal.record_success(_observation(EPISODE_A))
    metrics = SpotifyRequestMetrics(store.path, clock=lambda: NOW)
    metrics.initialize()
    embed_url = f"https://open.spotify.com/embed/show/{SHOW_ID}"
    transport = _StaticTransport(
        {
            embed_url: PublicHttpResponse(
                status=200,
                content_type="text/html",
                body=_embed_html(EPISODE_A).encode(),
            )
        }
    )
    detector = PublicDetector(
        journal,
        diagnostics,
        client=PublicShowClient(transport, metrics=metrics),
    )

    with metrics.request_scope() as scope:
        summary = detector.observe_sources((_source(),), observed_at=NOW + timedelta(minutes=1))
        snapshot = scope.snapshot()

    assert (summary.changed_count, summary.unchanged_count, summary.failure_count) == (0, 1, 0)
    assert snapshot.public_requests == 1
    assert snapshot.public_count(SpotifyPublicOperation.EMBED_SHOW) == 1
    assert snapshot.public_count(SpotifyPublicOperation.OEMBED) == 0
    assert transport.calls == [(embed_url, "text/html,application/xhtml+xml")]


def test_public_metrics_count_embed_and_oembed_on_identity_change(tmp_path: Path) -> None:
    store = _store(tmp_path)
    diagnostics, _ = _diagnostics(store)
    journal = PublicDetectorJournal(store)
    journal.initialize()
    journal.record_success(_observation(EPISODE_A))
    metrics = SpotifyRequestMetrics(store.path, clock=lambda: NOW)
    metrics.initialize()
    embed_url = f"https://open.spotify.com/embed/show/{SHOW_ID}"
    oembed_url = "https://open.spotify.com/oembed?url=" + (
        "https%3A%2F%2Fopen.spotify.com%2Fshow%2F" + SHOW_ID
    )
    transport = _StaticTransport(
        {
            embed_url: PublicHttpResponse(
                status=200,
                content_type="text/html",
                body=_embed_html(EPISODE_B, title="Bulletin 13:00").encode(),
            ),
            oembed_url: PublicHttpResponse(
                status=200,
                content_type="application/json",
                body=json.dumps({"title": "Bulletin 13:00"}).encode(),
            ),
        }
    )
    detector = PublicDetector(
        journal,
        diagnostics,
        client=PublicShowClient(transport, metrics=metrics),
    )

    with metrics.request_scope() as scope:
        summary = detector.observe_sources((_source(),), observed_at=NOW + timedelta(minutes=1))
        snapshot = scope.snapshot()

    assert (summary.changed_count, summary.unchanged_count, summary.failure_count) == (1, 0, 0)
    assert snapshot.public_requests == 2
    assert snapshot.public_count(SpotifyPublicOperation.EMBED_SHOW) == 1
    assert snapshot.public_count(SpotifyPublicOperation.OEMBED) == 1
    assert len(transport.calls) == 2


def test_public_http_failure_is_counted_with_bounded_status(tmp_path: Path) -> None:
    class _HttpFailureTransport:
        def get(self, url: str, *, accept: str) -> PublicHttpResponse:
            del url, accept
            raise PublicShowHttpError(503)

    store = _store(tmp_path)
    metrics = SpotifyRequestMetrics(store.path, clock=lambda: NOW)
    metrics.initialize()
    client = PublicShowClient(_HttpFailureTransport(), metrics=metrics)

    with pytest.raises(PublicShowHttpError):
        client.observe_episode(SourceId("source"), SHOW_ID, observed_at=NOW)

    metric = next(
        metric
        for metric in metrics.last_24_hours(now=NOW).metrics
        if metric.surface == "public"
        and metric.operation == SpotifyPublicOperation.EMBED_SHOW
        and metric.outcome is SpotifyRequestOutcome.HTTP_ERROR
    )
    assert metric.request_count == 1
    assert metric.http_status == 503


def test_public_transport_failure_is_counted_once(tmp_path: Path) -> None:
    class _TransportFailure:
        def get(self, url: str, *, accept: str) -> PublicHttpResponse:
            del url, accept
            raise PublicShowTransportError("offline")

    store = _store(tmp_path)
    metrics = SpotifyRequestMetrics(store.path, clock=lambda: NOW)
    metrics.initialize()
    client = PublicShowClient(_TransportFailure(), metrics=metrics)

    with pytest.raises(PublicShowTransportError):
        client.observe_episode(SourceId("source"), SHOW_ID, observed_at=NOW)

    matching = [
        metric
        for metric in metrics.last_24_hours(now=NOW).metrics
        if metric.surface == "public"
        and metric.operation == SpotifyPublicOperation.EMBED_SHOW
        and metric.outcome is SpotifyRequestOutcome.TRANSPORT_ERROR
    ]
    assert len(matching) == 1
    assert matching[0].request_count == 1
    assert matching[0].http_status is None


def test_public_response_validation_failure_is_counted_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    metrics = SpotifyRequestMetrics(store.path, clock=lambda: NOW)
    metrics.initialize()
    embed_url = f"https://open.spotify.com/embed/show/{SHOW_ID}"
    client = PublicShowClient(
        _StaticTransport(
            {
                embed_url: PublicHttpResponse(
                    status=200,
                    content_type="text/html",
                    body=b"<html></html>",
                )
            }
        ),
        metrics=metrics,
    )

    with pytest.raises(PublicShowResponseError):
        client.observe_episode(SourceId("source"), SHOW_ID, observed_at=NOW)

    matching = [
        metric
        for metric in metrics.last_24_hours(now=NOW).metrics
        if metric.surface == "public"
        and metric.operation == SpotifyPublicOperation.EMBED_SHOW
        and metric.outcome is SpotifyRequestOutcome.RESPONSE_ERROR
    ]
    assert len(matching) == 1
    assert matching[0].request_count == 1
    assert matching[0].http_status == 200


def test_response_failure_oembed_fallback_is_counted_at_public_transport_boundary(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    diagnostics, _ = _diagnostics(store)
    journal = PublicDetectorJournal(store)
    journal.initialize()
    journal.record_success(
        _observation(EPISODE_A, title="Bulletin 12:00"),
        oembed_title="Bulletin 12:00",
    )
    metrics = SpotifyRequestMetrics(store.path, clock=lambda: NOW)
    metrics.initialize()
    embed_url = f"https://open.spotify.com/embed/show/{SHOW_ID}"
    oembed_url = "https://open.spotify.com/oembed?url=" + (
        "https%3A%2F%2Fopen.spotify.com%2Fshow%2F" + SHOW_ID
    )
    transport = _StaticTransport(
        {
            embed_url: PublicHttpResponse(
                status=200,
                content_type="text/html",
                body=b"<html></html>",
            ),
            oembed_url: PublicHttpResponse(
                status=200,
                content_type="application/json",
                body=json.dumps({"title": "Bulletin 13:00"}).encode(),
            ),
        }
    )
    detector = PublicDetector(
        journal,
        diagnostics,
        client=PublicShowClient(transport, metrics=metrics),
    )

    with metrics.request_scope() as scope:
        summary = detector.observe_sources(
            (_source(),),
            observed_at=NOW + timedelta(minutes=1),
        )
        snapshot = scope.snapshot()

    assert summary.degraded_change_source_ids == (SourceId("source"),)
    assert snapshot.public_requests == 2
    assert snapshot.public_count(SpotifyPublicOperation.EMBED_SHOW) == 1
    assert snapshot.public_count(SpotifyPublicOperation.OEMBED) == 1


def test_public_metric_persistence_failure_is_fail_soft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    metrics = SpotifyRequestMetrics(store.path, clock=lambda: NOW)
    metrics.initialize()
    embed_url = f"https://open.spotify.com/embed/show/{SHOW_ID}"
    client = PublicShowClient(
        _StaticTransport(
            {
                embed_url: PublicHttpResponse(
                    status=200,
                    content_type="text/html",
                    body=_embed_html().encode(),
                )
            }
        ),
        metrics=metrics,
    )

    def unavailable(_operation: str) -> object:
        raise PersistenceError("synthetic metrics persistence failure")

    monkeypatch.setattr(metrics, "_connection", unavailable)

    observation = client.observe_episode(SourceId("source"), SHOW_ID, observed_at=NOW)

    assert observation.episode_id == EPISODE_A
    assert metrics.last_24_hours(now=NOW).coverage_complete is False


def test_spotify_show_id_requires_one_valid_show_reference() -> None:
    assert spotify_show_id(_source()) == SHOW_ID

    with pytest.raises(PublicShowConfigurationError, match="exactly one"):
        spotify_show_id(_source(show_ids=()))

    with pytest.raises(PublicShowConfigurationError, match="exactly one"):
        spotify_show_id(_source(show_ids=(SHOW_ID, "T" * 22)))


def test_journal_additively_upgrades_existing_detector_table(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            CREATE TABLE spotify_public_detector_state (
                source_id TEXT NOT NULL,
                spotify_show_id TEXT NOT NULL,
                last_episode_id TEXT,
                last_episode_title TEXT,
                last_public_release_date_raw TEXT,
                last_public_observed_at TEXT,
                last_detector_success_at TEXT,
                last_change_at TEXT,
                last_failure_at TEXT,
                last_catalogue_refresh_at TEXT,
                consecutive_detector_failures INTEGER NOT NULL DEFAULT 0,
                detector_schema_version INTEGER NOT NULL,
                last_oembed_title TEXT,
                PRIMARY KEY (source_id, spotify_show_id)
            )
            """
        )

    journal = PublicDetectorJournal(store)
    journal.initialize()

    with sqlite3.connect(store.path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(spotify_public_detector_state)"
            ).fetchall()
        }

    assert "last_public_release_date_shape" in columns
    assert "last_degraded_audit_at" in columns


def test_journal_detects_identity_changes_even_when_title_is_identical(tmp_path: Path) -> None:
    journal = PublicDetectorJournal(_store(tmp_path))
    journal.initialize()

    first = journal.record_success(_observation(EPISODE_A, title="12:00 Fakty"))
    unchanged = journal.record_success(
        _observation(
            EPISODE_A,
            title="12:00 Fakty",
            observed_at=NOW + timedelta(minutes=1),
        )
    )
    changed = journal.record_success(
        _observation(
            EPISODE_B,
            title="12:00 Fakty",
            observed_at=NOW + timedelta(minutes=2),
        )
    )

    assert first.outcome is PublicDetectorOutcome.BOOTSTRAP
    assert unchanged.outcome is PublicDetectorOutcome.UNCHANGED
    assert changed.outcome is PublicDetectorOutcome.CHANGED
    assert changed.previous is not None
    assert changed.previous.last_episode_id == EPISODE_A
    assert changed.current.last_episode_id == EPISODE_B
    assert changed.current.last_episode_title == "12:00 Fakty"
    assert changed.current.last_change_at == NOW + timedelta(minutes=2)


def test_journal_preserves_last_known_good_on_failure_and_recovers(tmp_path: Path) -> None:
    journal = PublicDetectorJournal(_store(tmp_path))
    journal.initialize()
    journal.record_success(_observation(EPISODE_A))

    failed = journal.record_failure(
        SourceId("source"),
        SHOW_ID,
        failed_at=NOW + timedelta(minutes=1),
    )
    assert failed.last_episode_id == EPISODE_A
    assert failed.consecutive_detector_failures == 1

    recovered = journal.record_success(
        _observation(EPISODE_A, observed_at=NOW + timedelta(minutes=2))
    )
    assert recovered.current.last_episode_id == EPISODE_A
    assert recovered.current.consecutive_detector_failures == 0
    assert recovered.current.last_failure_at == NOW + timedelta(minutes=1)


def test_journal_ignores_stale_success_and_failure_replays(tmp_path: Path) -> None:
    journal = PublicDetectorJournal(_store(tmp_path))
    journal.initialize()
    journal.record_success(_observation(EPISODE_B, observed_at=NOW + timedelta(minutes=10)))

    stale_success = journal.record_success(
        _observation(EPISODE_A, observed_at=NOW + timedelta(minutes=5))
    )
    stale_failure = journal.record_failure(
        SourceId("source"),
        SHOW_ID,
        failed_at=NOW + timedelta(minutes=6),
    )

    assert stale_success.outcome is PublicDetectorOutcome.STALE_IGNORED
    assert stale_success.current.last_episode_id == EPISODE_B
    assert stale_failure.last_episode_id == EPISODE_B
    assert stale_failure.consecutive_detector_failures == 0


def test_inactivity_audit_uses_change_or_catalogue_refresh_not_unchanged_poll(
    tmp_path: Path,
) -> None:
    journal = PublicDetectorJournal(_store(tmp_path))
    journal.initialize()
    journal.record_success(_observation(EPISODE_A))
    journal.record_success(_observation(EPISODE_A, observed_at=NOW + timedelta(hours=20)))

    state = journal.get(SourceId("source"), SHOW_ID)
    assert state is not None
    assert state.last_change_at == NOW
    assert state.audit_due(now=NOW + timedelta(hours=24), inactivity=timedelta(hours=24))

    assert journal.mark_catalogue_refresh(
        SourceId("source"),
        SHOW_ID,
        refreshed_at=NOW + timedelta(hours=24),
    )
    refreshed = journal.get(SourceId("source"), SHOW_ID)
    assert refreshed is not None
    assert not refreshed.audit_due(
        now=NOW + timedelta(hours=47),
        inactivity=timedelta(hours=24),
    )
    assert refreshed.audit_due(
        now=NOW + timedelta(hours=48),
        inactivity=timedelta(hours=24),
    )


def test_public_detector_logs_change_only_on_identity_transition_and_oembed_on_change(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    diagnostics, diagnostic_store = _diagnostics(store)
    journal = PublicDetectorJournal(store)
    journal.initialize()
    client = _SequenceClient(
        [
            _observation(EPISODE_A),
            _observation(
                EPISODE_A,
                observed_at=NOW + timedelta(minutes=1),
            ),
            _observation(
                EPISODE_B,
                title="Bulletin 13:00",
                observed_at=NOW + timedelta(minutes=2),
            ),
        ],
        oembed_titles=["Bulletin", "Bulletin 13:00"],
    )
    detector = PublicDetector(journal, diagnostics, client=client)  # type: ignore[arg-type]
    source = _source()

    first = detector.observe_sources((source,), observed_at=NOW)
    second = detector.observe_sources((source,), observed_at=NOW + timedelta(minutes=1))
    third = detector.observe_sources((source,), observed_at=NOW + timedelta(minutes=2))

    assert (first.changed_count, first.unchanged_count, first.failure_count) == (1, 0, 0)
    assert first.changed_source_ids == (SourceId("source"),)
    assert (second.changed_count, second.unchanged_count, second.failure_count) == (0, 1, 0)
    assert second.changed_source_ids == ()
    assert (third.changed_count, third.unchanged_count, third.failure_count) == (1, 0, 0)
    assert third.changed_source_ids == (SourceId("source"),)
    assert client.observe_calls == 3
    assert client.oembed_calls == 2

    changed = [
        event
        for event in diagnostic_store.list_events(limit=20)
        if event.event_name == "public_detector_changed"
    ]
    assert len(changed) == 2
    latest = changed[0]
    assert latest.details["spotify_previous_episode_id"] == EPISODE_A
    assert latest.details["spotify_episode_id"] == EPISODE_B
    assert latest.details["spotify_episode_title"] == "Bulletin 13:00"
    assert latest.details["oembed_matches"] is True


def test_public_schema_failure_captures_first_exact_response_without_stdout_storm(
    tmp_path: Path,
) -> None:
    raw_body = b"<html><body>simulated-schema-v2-without-next-data</body></html>"
    store = _store(tmp_path)
    diagnostic_store = DiagnosticEventStore(store.path)
    diagnostic_store.initialize()
    output = io.StringIO()
    logger = logging.Logger("public-detector-capture-simulation")
    logger.setLevel(logging.INFO)
    logger.addHandler(logging.StreamHandler(output))
    diagnostics = OperationalDiagnostics(diagnostic_store, logger=logger)
    journal = PublicDetectorJournal(store)
    journal.initialize()
    journal.record_success(
        _observation(EPISODE_A, title="Same title"),
        oembed_title="Same title",
    )
    embed_url = f"https://open.spotify.com/embed/show/{SHOW_ID}"
    oembed_url = "https://open.spotify.com/oembed?url=" + (
        "https%3A%2F%2Fopen.spotify.com%2Fshow%2F" + SHOW_ID
    )
    transport = _StaticTransport(
        {
            embed_url: PublicHttpResponse(
                status=200,
                content_type="text/html; charset=utf-8",
                body=raw_body,
            ),
            oembed_url: PublicHttpResponse(
                status=200,
                content_type="application/json",
                body=json.dumps({"title": "Same title"}).encode(),
            ),
        }
    )
    detector = PublicDetector(
        journal,
        diagnostics,
        client=PublicShowClient(transport),
    )

    first = detector.observe_sources((_source(),), observed_at=NOW + timedelta(minutes=1))
    second = detector.observe_sources((_source(),), observed_at=NOW + timedelta(minutes=11))

    assert first.failure_count == 1
    assert second.failure_count == 1
    failures = [
        event
        for event in reversed(diagnostic_store.list_events(limit=20))
        if event.event_name == "public_detector_failed"
        and event.details.get("detector_outcome") == "response_failure"
    ]
    assert len(failures) == 2

    first_failure, second_failure = failures
    assert first_failure.details["detector_response_reason"] == "next_data_count"
    assert first_failure.details["detector_fallback_outcome"] == "title_unchanged"
    assert first_failure.details["public_response_capture"] is True
    assert first_failure.details["public_response_surface"] == "embed"
    assert first_failure.details["public_response_content_type"] == "text/html; charset=utf-8"
    assert first_failure.details["public_response_size_bytes"] == len(raw_body)
    assert first_failure.details["public_response_sha256"] == hashlib.sha256(raw_body).hexdigest()
    assert first_failure.details["public_response_truncated"] is False
    encoded = first_failure.details["public_response_body_b64"]
    assert isinstance(encoded, str)
    assert base64.b64decode(encoded, validate=True) == raw_body

    assert "public_response_body_b64" not in second_failure.details
    assert second_failure.details["consecutive_failure_count"] == 2
    assert raw_body.decode() not in output.getvalue()
    assert "public_response_body=<captured>" in output.getvalue()


def test_public_detector_response_failure_emits_bounded_reason(tmp_path: Path) -> None:
    store = _store(tmp_path)
    diagnostics, diagnostic_store = _diagnostics(store)
    journal = PublicDetectorJournal(store)
    journal.initialize()
    journal.record_success(_observation(EPISODE_A))
    client = _SequenceClient(
        [
            PublicShowResponseError(
                "Spotify Embed __NEXT_DATA__ shape changed",
                reason=PublicShowResponseFailureReason.EMBED_SHAPE,
            )
        ],
        oembed_titles=["Bulletin"],
    )
    detector = PublicDetector(journal, diagnostics, client=client)  # type: ignore[arg-type]

    summary = detector.observe_sources((_source(),), observed_at=NOW + timedelta(minutes=1))

    assert summary.failure_count == 1
    event = next(
        event
        for event in diagnostic_store.list_events(limit=10)
        if event.event_name == "public_detector_failed"
    )
    assert event.details == {
        "consecutive_failure_count": 1,
        "detector_outcome": "response_failure",
        "detector_response_reason": "embed_shape",
        "detector_fallback_outcome": "title_unchanged",
    }
    assert client.oembed_calls == 1


def test_response_failure_changed_oembed_title_triggers_only_degraded_change(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    diagnostics, diagnostic_store = _diagnostics(store)
    journal = PublicDetectorJournal(store)
    journal.initialize()
    journal.record_success(
        _observation(EPISODE_A, title="Bulletin 12:00"),
        oembed_title="Bulletin 12:00",
    )
    client = _SequenceClient(
        [
            PublicShowResponseError(
                "Spotify Embed __NEXT_DATA__ shape changed",
                reason=PublicShowResponseFailureReason.EMBED_SHAPE,
            )
        ],
        oembed_titles=["Bulletin 13:00"],
    )
    detector = PublicDetector(journal, diagnostics, client=client)  # type: ignore[arg-type]

    summary = detector.observe_sources((_source(),), observed_at=NOW + timedelta(minutes=1))

    assert summary.changed_count == 0
    assert summary.failure_count == 1
    assert summary.changed_source_ids == ()
    assert summary.degraded_change_source_ids == (SourceId("source"),)
    state = journal.get(SourceId("source"), SHOW_ID)
    assert state is not None
    assert state.last_episode_id == EPISODE_A
    assert state.last_oembed_title == "Bulletin 13:00"
    event = next(
        event
        for event in diagnostic_store.list_events(limit=10)
        if event.event_name == "public_detector_failed"
    )
    assert event.details["detector_fallback_outcome"] == "title_changed"


def test_repeated_degraded_title_change_triggers_only_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    diagnostics, _ = _diagnostics(store)
    journal = PublicDetectorJournal(store)
    journal.initialize()
    journal.record_success(
        _observation(EPISODE_A, title="Bulletin 12:00"),
        oembed_title="Bulletin 12:00",
    )
    response_error = PublicShowResponseError(
        "Spotify Embed __NEXT_DATA__ shape changed",
        reason=PublicShowResponseFailureReason.EMBED_SHAPE,
    )
    client = _SequenceClient(
        [response_error, response_error],
        oembed_titles=["Bulletin 13:00", "Bulletin 13:00"],
    )
    detector = PublicDetector(journal, diagnostics, client=client)  # type: ignore[arg-type]

    first = detector.observe_sources((_source(),), observed_at=NOW + timedelta(minutes=1))
    second = detector.observe_sources((_source(),), observed_at=NOW + timedelta(minutes=2))

    assert first.degraded_change_source_ids == (SourceId("source"),)
    assert second.degraded_change_source_ids == ()
    state = journal.get(SourceId("source"), SHOW_ID)
    assert state is not None
    assert state.last_episode_id == EPISODE_A
    assert state.last_oembed_title == "Bulletin 13:00"
    assert state.consecutive_detector_failures == 2


def test_response_failure_same_oembed_title_is_inconclusive_not_unchanged(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    diagnostics, _ = _diagnostics(store)
    journal = PublicDetectorJournal(store)
    journal.initialize()
    journal.record_success(_observation(EPISODE_A, title="Same title"), oembed_title="Same title")
    client = _SequenceClient(
        [
            PublicShowResponseError(
                "Spotify Embed __NEXT_DATA__ shape changed",
                reason=PublicShowResponseFailureReason.EMBED_SHAPE,
            )
        ],
        oembed_titles=["Same title"],
    )
    detector = PublicDetector(journal, diagnostics, client=client)  # type: ignore[arg-type]

    summary = detector.observe_sources((_source(),), observed_at=NOW + timedelta(minutes=1))

    assert summary.changed_count == 0
    assert summary.unchanged_count == 0
    assert summary.failure_count == 1
    assert summary.degraded_change_source_ids == ()
    state = journal.get(SourceId("source"), SHOW_ID)
    assert state is not None
    assert state.last_episode_id == EPISODE_A
    assert state.consecutive_detector_failures == 1


def test_http_failure_does_not_double_public_requests_with_oembed_fallback(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    diagnostics, _ = _diagnostics(store)
    journal = PublicDetectorJournal(store)
    journal.initialize()
    journal.record_success(_observation(EPISODE_A))
    client = _SequenceClient(
        [PublicShowHttpError(504)],
        oembed_titles=["must not be consumed"],
    )
    detector = PublicDetector(journal, diagnostics, client=client)  # type: ignore[arg-type]

    summary = detector.observe_sources((_source(),), observed_at=NOW + timedelta(minutes=1))

    assert summary.failure_count == 1
    assert client.oembed_calls == 0


def test_degraded_audit_cadence_is_persisted_and_primary_recovery_clears_it(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    journal = PublicDetectorJournal(store)
    journal.initialize()
    journal.record_success(_observation(EPISODE_A))
    assert journal.mark_catalogue_refresh(
        SourceId("source"),
        SHOW_ID,
        refreshed_at=NOW,
    )
    journal.record_failure(
        SourceId("source"),
        SHOW_ID,
        failed_at=NOW + timedelta(minutes=10),
    )

    state = journal.get(SourceId("source"), SHOW_ID)
    assert state is not None
    assert not state.degraded_audit_due(
        now=NOW + timedelta(minutes=29),
        interval=timedelta(minutes=30),
        fallback_anchor=NOW,
    )
    assert state.degraded_audit_due(
        now=NOW + timedelta(minutes=30),
        interval=timedelta(minutes=30),
        fallback_anchor=NOW,
    )
    assert journal.mark_degraded_audit_attempt(
        SourceId("source"),
        SHOW_ID,
        attempted_at=NOW + timedelta(minutes=30),
    )

    restarted = PublicDetectorJournal(store)
    persisted = restarted.get(SourceId("source"), SHOW_ID)
    assert persisted is not None
    assert persisted.last_degraded_audit_at == NOW + timedelta(minutes=30)
    assert not persisted.degraded_audit_due(
        now=NOW + timedelta(minutes=59),
        interval=timedelta(minutes=30),
        fallback_anchor=NOW,
    )
    assert persisted.degraded_audit_due(
        now=NOW + timedelta(minutes=60),
        interval=timedelta(minutes=30),
        fallback_anchor=NOW,
    )

    restarted.record_success(
        _observation(
            EPISODE_A,
            observed_at=NOW + timedelta(minutes=61),
        )
    )
    recovered = restarted.get(SourceId("source"), SHOW_ID)
    assert recovered is not None
    assert recovered.last_degraded_audit_at is None
    assert recovered.consecutive_detector_failures == 0


def test_unsupported_release_date_shape_emits_one_bounded_drift_event(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    diagnostics, diagnostic_store = _diagnostics(store)
    journal = PublicDetectorJournal(store)
    journal.initialize()
    journal.record_success(_observation(EPISODE_A))
    embed_url = f"https://open.spotify.com/embed/show/{SHOW_ID}"
    transport = _StaticTransport(
        {
            embed_url: PublicHttpResponse(
                status=200,
                content_type="text/html",
                body=_embed_html(release_date={"future": "shape"}).encode(),
            )
        }
    )
    detector = PublicDetector(
        journal,
        diagnostics,
        client=PublicShowClient(transport),
    )

    detector.observe_sources((_source(),), observed_at=NOW)
    detector.observe_sources((_source(),), observed_at=NOW + timedelta(minutes=1))

    drift = [
        event
        for event in diagnostic_store.list_events(limit=20)
        if event.event_name == "public_detector_metadata_drift"
    ]
    assert len(drift) == 1
    assert drift[0].details == {
        "detector_outcome": "metadata_drift",
        "public_release_date_shape": "unsupported_object",
    }


def test_public_detector_failure_preserves_identity_and_emits_bounded_diagnostic(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    diagnostics, diagnostic_store = _diagnostics(store)
    journal = PublicDetectorJournal(store)
    journal.initialize()
    journal.record_success(_observation(EPISODE_A))
    client = _SequenceClient([PublicShowHttpError(503)])
    detector = PublicDetector(journal, diagnostics, client=client)  # type: ignore[arg-type]

    summary = detector.observe_sources((_source(),), observed_at=NOW + timedelta(minutes=1))

    assert summary.failure_count == 1
    state = journal.get(SourceId("source"), SHOW_ID)
    assert state is not None
    assert state.last_episode_id == EPISODE_A
    assert state.consecutive_detector_failures == 1
    event = next(
        event
        for event in diagnostic_store.list_events(limit=10)
        if event.event_name == "public_detector_failed"
    )
    assert event.details == {
        "consecutive_failure_count": 1,
        "detector_outcome": "http_failure",
        "http_status": 503,
    }
