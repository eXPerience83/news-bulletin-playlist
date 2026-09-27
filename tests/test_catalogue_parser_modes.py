from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from _spotify_catalogue import catalogue_page

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.models import ParsedEdition, SourceId
from news_bulletin_playlist.providers.base import CatalogueParseContext, parse_title
from news_bulletin_playlist.spotify.first_catalogue import fetch_source_catalogue
from news_bulletin_playlist.spotify.first_models import TimestampSource
from news_bulletin_playlist.spotify.public_detector import spotify_show_id

NOW = datetime(2026, 9, 25, 17, 0, tzinfo=UTC)


class _CatalogueClient:
    def __init__(self, show_id: str, title: str) -> None:
        episode_id = "Z" * 22
        self._show_id = show_id
        self._page = catalogue_page(
            [
                {
                    "id": episode_id,
                    "uri": f"spotify:episode:{episode_id}",
                    "name": title,
                    "release_date": "2026-09-25",
                    "release_date_precision": "day",
                    "duration_ms": 60_000,
                }
            ]
        )

    def show_episodes(
        self,
        show_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        assert show_id == self._show_id
        assert limit == 50
        assert offset == 0
        return self._page


class _ContextThenExactParser:
    provider_id = "context_then_exact"

    def parse(self, title: str) -> ParsedEdition | None:
        return ParsedEdition(self.provider_id, title, NOW)

    def parse_catalogue(
        self,
        title: str,
        *,
        context: CatalogueParseContext,
    ) -> ParsedEdition | None:
        del title, context
        return None


def test_contextual_parser_can_fall_back_to_its_exact_title_path() -> None:
    attempt = parse_title(
        _ContextThenExactParser(),
        "usable exact title",
        context=CatalogueParseContext(
            release_date="2026-09-25",
            release_date_precision="day",
            timezone=ZoneInfo("Europe/Madrid"),
        ),
    )

    assert attempt.mode == "exact"
    assert attempt.parsed is not None
    assert attempt.parsed.edition_at == NOW


@pytest.mark.parametrize(
    ("source_id", "title", "outcome", "timestamp_source"),
    [
        (
            "ser",
            "Las noticias de la SER, 12:00 (25/09/2026)",
            "exact",
            TimestampSource.TITLE,
        ),
        (
            "ser",
            "Las noticias de la SER - 9.30 25-09-26",
            "provider_compat",
            TimestampSource.TITLE,
        ),
        (
            "rne",
            "NOTICIAS\u00a0RNE\u00a0–\u00a025.09.2026\u00a0–\u00a009.30H",
            "normalized",
            TimestampSource.TITLE,
        ),
        (
            "abc",
            "Las Noticias de ABC",
            "fallback",
            TimestampSource.FIRST_SEEN,
        ),
    ],
)
def test_catalogue_evidence_reports_actual_parser_path(
    source_id: str,
    title: str,
    outcome: str,
    timestamp_source: TimestampSource,
) -> None:
    source = next(item for item in BUILTIN_CATALOG.sources if item.id == SourceId(source_id))
    show_id = spotify_show_id(source)
    snapshot = fetch_source_catalogue(
        _CatalogueClient(show_id, title),
        source,
        now=NOW,
        cutoff=NOW - timedelta(days=2),
        collect_evidence=True,
    )

    assert len(snapshot.evidence) == 1
    assert snapshot.evidence[0].parser_outcome == outcome
    assert snapshot.episodes[0].timestamp_source is timestamp_source
    assert snapshot.parser_fallback_count == (1 if outcome == "fallback" else 0)


@pytest.mark.parametrize("date", ["25/10/2026", "29/03/2026"])
def test_unproven_dst_title_remains_in_catalogue_with_first_seen_fallback(date: str) -> None:
    source = next(item for item in BUILTIN_CATALOG.sources if item.id == SourceId("ser"))
    title = f"Las noticias de la SER, 02:30 ({date})"
    snapshot = fetch_source_catalogue(
        _CatalogueClient(spotify_show_id(source), title),
        source,
        now=NOW,
        cutoff=NOW - timedelta(days=2),
        collect_evidence=True,
    )

    assert len(snapshot.episodes) == 1
    episode = snapshot.episodes[0]
    assert episode.title == title
    assert episode.edition_at is None
    assert episode.timestamp_source is TimestampSource.FIRST_SEEN
    assert snapshot.parser_fallback_count == 1
    assert snapshot.evidence[0].parser_outcome == "fallback"
