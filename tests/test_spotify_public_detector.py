from __future__ import annotations

import json
import logging
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
from news_bulletin_playlist.persistence import SQLiteStore
from news_bulletin_playlist.runtime_diagnostics import OperationalDiagnostics
from news_bulletin_playlist.spotify.public_detector import (
    DETECTOR_SCHEMA_VERSION,
    PublicDetectorJournal,
    PublicDetectorOutcome,
    PublicDetectorShadow,
    PublicEpisodeObservation,
    PublicHttpResponse,
    PublicShowClient,
    PublicShowConfigurationError,
    PublicShowHttpError,
    PublicShowResponseError,
    parse_public_show_embed,
    spotify_show_id,
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
                            "releaseDate": "2026-09-19T10:00:00Z",
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
        endpoint_url="https://legacy.example.test/feed.xml",
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
    ("html", "message"),
    [
        ("<html></html>", "__NEXT_DATA__"),
        (
            '<script id="__NEXT_DATA__">{bad json</script>',
            "invalid JSON",
        ),
        (_embed_html(entity_type="show"), "not an episode"),
    ],
)
def test_parse_public_show_embed_rejects_invalid_contract(html: str, message: str) -> None:
    with pytest.raises(PublicShowResponseError, match=message):
        parse_public_show_embed(
            html,
            source_id=SourceId("source"),
            spotify_show_id=SHOW_ID,
            observed_at=NOW,
        )


def test_parse_public_show_embed_rejects_duplicate_next_data() -> None:
    html = _embed_html() + _embed_html()
    with pytest.raises(PublicShowResponseError, match="exactly one"):
        parse_public_show_embed(
            html,
            source_id=SourceId("source"),
            spotify_show_id=SHOW_ID,
            observed_at=NOW,
        )


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


def test_spotify_show_id_requires_one_valid_show_reference() -> None:
    assert spotify_show_id(_source()) == SHOW_ID

    with pytest.raises(PublicShowConfigurationError, match="exactly one"):
        spotify_show_id(_source(show_ids=()))

    with pytest.raises(PublicShowConfigurationError, match="exactly one"):
        spotify_show_id(_source(show_ids=(SHOW_ID, "T" * 22)))


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
    journal.record_success(
        _observation(EPISODE_B, observed_at=NOW + timedelta(minutes=10))
    )

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
    journal.record_success(
        _observation(EPISODE_A, observed_at=NOW + timedelta(hours=20))
    )

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


def test_shadow_logs_change_only_on_identity_transition_and_oembed_on_change(
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
    shadow = PublicDetectorShadow(journal, diagnostics, client=client)  # type: ignore[arg-type]
    source = _source()

    first = shadow.observe_sources((source,), observed_at=NOW)
    second = shadow.observe_sources((source,), observed_at=NOW + timedelta(minutes=1))
    third = shadow.observe_sources((source,), observed_at=NOW + timedelta(minutes=2))

    assert (first.changed_count, first.unchanged_count, first.failure_count) == (1, 0, 0)
    assert (second.changed_count, second.unchanged_count, second.failure_count) == (0, 1, 0)
    assert (third.changed_count, third.unchanged_count, third.failure_count) == (1, 0, 0)
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


def test_shadow_failure_preserves_identity_and_emits_bounded_diagnostic(tmp_path: Path) -> None:
    store = _store(tmp_path)
    diagnostics, diagnostic_store = _diagnostics(store)
    journal = PublicDetectorJournal(store)
    journal.initialize()
    journal.record_success(_observation(EPISODE_A))
    client = _SequenceClient([PublicShowHttpError(503)])
    shadow = PublicDetectorShadow(journal, diagnostics, client=client)  # type: ignore[arg-type]

    summary = shadow.observe_sources((_source(),), observed_at=NOW + timedelta(minutes=1))

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
