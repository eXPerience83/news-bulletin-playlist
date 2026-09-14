from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from news_bulletin_playlist import collection
from news_bulletin_playlist.diagnostics import DiagnosticEventStore, DiagnosticSeverity
from news_bulletin_playlist.engine import SourceCycleOutcome
from news_bulletin_playlist.engine_observability import _source_diagnostic_details
from news_bulletin_playlist.models import (
    CountryCode,
    LanguageTag,
    ParserId,
    SourceDefinition,
    SourceId,
)

NOW = datetime(2026, 9, 14, 8, 0, tzinfo=UTC)


def _source() -> SourceDefinition:
    return SourceDefinition(
        id=SourceId("rmf_fakty"),
        display_name="RMF FM — Fakty",
        countries=(CountryCode("PL"),),
        languages=(LanguageTag("pl"),),
        timezone=ZoneInfo("Europe/Warsaw"),
        enabled=True,
        parser_id=ParserId("release_date_title"),
        endpoint_url="https://example.test/feed.xml",
    )


def _rss() -> bytes:
    return b"""<rss><channel><item>
        <guid>rmf-1</guid>
        <title>08:00 Fakty</title>
        <pubDate>Mon, 14 Sep 2026 08:00:00 +0200</pubDate>
    </item></channel></rss>"""


def test_collection_merges_fetch_observation_into_success_diagnostics() -> None:
    payload = collection._ObservedFeedPayload(
        _rss(),
        rss_fetch_duration_ms=321,
        rss_redirect_count=1,
    )

    result = collection._collect_source(_source(), lambda _url: payload)

    assert result.ok
    assert len(result.editions) == 1
    assert result.collection_diagnostics.rss_fetch_duration_ms == 321
    assert result.collection_diagnostics.rss_redirect_count == 1
    assert result.collection_diagnostics.rss_fetch_failure_kind is None


def test_collection_maps_observed_failure_without_exposing_raw_detail() -> None:
    error = TimeoutError("https://provider-secret.invalid/feed?token=secret")
    collection._attach_fetch_failure_observation(error, duration_ms=20_004, redirect_count=1)

    def failing_fetcher(_url: str) -> bytes:
        raise error

    result = collection._collect_source(_source(), failing_fetcher)

    assert not result.ok
    assert result.collection_diagnostics.failure_reason == "fetch_error"
    assert result.collection_diagnostics.rss_fetch_duration_ms == 20_004
    assert result.collection_diagnostics.rss_fetch_failure_kind == "timeout"
    assert result.collection_diagnostics.rss_redirect_count == 1

    source_outcome = SourceCycleOutcome(
        source_id=SourceId("rmf_fakty"),
        collection_ok=False,
        matching_ok=None,
        edition_count=0,
        matched_count=0,
        last_success_at=NOW,
        error=result.error,
        collection_diagnostics=result.collection_diagnostics,
    )
    details = _source_diagnostic_details(source_outcome, phase="collection")

    assert details["collection_failure_reason"] == "fetch_error"
    assert details["rss_fetch_failure_kind"] == "timeout"
    assert details["rss_fetch_duration_ms"] == 20_004
    assert details["rss_redirect_count"] == 1
    serialized = repr(details)
    assert "provider-secret" not in serialized
    assert "token=secret" not in serialized


def test_diagnostic_store_accepts_allowlisted_rss_fetch_observation(tmp_path: Path) -> None:
    store = DiagnosticEventStore(tmp_path / "state.sqlite3")
    store.initialize()
    store.record(
        occurred_at=NOW,
        severity=DiagnosticSeverity.ERROR,
        component="source",
        event_name="source_collection_failed",
        source_id="rmf_fakty",
        details={
            "phase": "collection",
            "collection_failure_reason": "fetch_error",
            "rss_fetch_duration_ms": 20_004,
            "rss_fetch_failure_kind": "timeout",
            "rss_redirect_count": 1,
        },
    )

    event = store.list_events(limit=1)[0]

    assert event.details["rss_fetch_duration_ms"] == 20_004
    assert event.details["rss_fetch_failure_kind"] == "timeout"
    assert event.details["rss_redirect_count"] == 1


def test_diagnostic_store_rejects_arbitrary_rss_fetch_failure_label(tmp_path: Path) -> None:
    store = DiagnosticEventStore(tmp_path / "state.sqlite3")
    store.initialize()

    with pytest.raises(ValueError, match="unsupported label"):
        store.record(
            occurred_at=NOW,
            severity=DiagnosticSeverity.ERROR,
            component="source",
            event_name="source_collection_failed",
            source_id="rmf_fakty",
            details={"rss_fetch_failure_kind": "https://provider-secret.invalid/feed"},
        )
