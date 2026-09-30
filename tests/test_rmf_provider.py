from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from _spotify_catalogue import catalogue_page

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.models import SourceId, TemporalStrategy
from news_bulletin_playlist.providers.base import CatalogueParseContext
from news_bulletin_playlist.providers.common import clock_24h_pattern
from news_bulletin_playlist.providers.rmf import RmfFaktyParser
from news_bulletin_playlist.spotify.first_catalogue import fetch_source_catalogue
from news_bulletin_playlist.spotify.first_models import TimestampSource

WARSAW = ZoneInfo("Europe/Warsaw")
NOW = datetime(2026, 9, 25, 17, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("separators", "token"),
    [
        (":", "6:30"),
        (":.", "6.30"),
        (":.,", "16,00"),
        ("hH", "18h00"),
        ("hH", "18H00"),
    ],
)
def test_shared_clock_pattern_supports_common_24h_station_formats(
    separators: str,
    token: str,
) -> None:
    match = re.fullmatch(clock_24h_pattern(separators=separators), token)
    assert match is not None
    assert 0 <= int(match.group("hour")) <= 23
    assert 0 <= int(match.group("minute")) <= 59


def test_shared_clock_pattern_requires_an_explicit_separator_policy() -> None:
    with pytest.raises(ValueError, match="separators"):
        clock_24h_pattern(separators="")


def _context(release_date: str, precision: str = "day") -> CatalogueParseContext:
    return CatalogueParseContext(
        release_date=release_date,
        release_date_precision=precision,
        timezone=WARSAW,
    )


@pytest.mark.parametrize(
    ("title", "release_date", "expected"),
    [
        ("16:00 Fakty", "2026-09-25", datetime(2026, 9, 25, 14, 0, tzinfo=UTC)),
        ("15:30 Fakty", "2026-09-25", datetime(2026, 9, 25, 13, 30, tzinfo=UTC)),
        ("6:30 Fakty", "2026-09-25", datetime(2026, 9, 25, 4, 30, tzinfo=UTC)),
        ("6.30 Fakty sportowe", "2026-09-25", datetime(2026, 9, 25, 4, 30, tzinfo=UTC)),
        ("00:30 Fakty", "2026-07-05", datetime(2026, 7, 4, 22, 30, tzinfo=UTC)),
        ("16:00 Fakty", "2026-01-15", datetime(2026, 1, 15, 15, 0, tzinfo=UTC)),
        ("16:00 Fakty", "2026-07-15", datetime(2026, 7, 15, 14, 0, tzinfo=UTC)),
    ],
)
def test_rmf_fakty_parser_combines_release_day_with_local_title_time(
    title: str,
    release_date: str,
    expected: datetime,
) -> None:
    parsed = RmfFaktyParser().parse_catalogue(title, context=_context(release_date))
    assert parsed is not None
    assert parsed.edition_at == expected


@pytest.mark.parametrize(
    ("title", "release_date", "precision"),
    [
        ("Fakty", "2026-09-25", "day"),
        ("24:00 Fakty", "2026-09-25", "day"),
        ("6:3 Fakty", "2026-09-25", "day"),
        ("6-30 Fakty", "2026-09-25", "day"),
        ("6:30Fakty", "2026-09-25", "day"),
        ("16:00", "2026-09-25", "day"),
        ("16:00 Completely unrelated podcast", "2026-09-25", "day"),
        ("16:00 Faktycznie", "2026-09-25", "day"),
        ("6:30 Fakty", "2026-09", "month"),
        ("6:30 Fakty", "2026-09-31", "day"),
        ("02:30 Fakty", "2026-03-29", "day"),
        ("02:30 Fakty", "2026-10-25", "day"),
    ],
)
def test_rmf_fakty_parser_fails_soft_without_exact_supported_evidence(
    title: str,
    release_date: str,
    precision: str,
) -> None:
    parser = RmfFaktyParser()
    assert parser.parse_catalogue(title, context=_context(release_date, precision)) is None
    assert parser.parse(title) is None


def test_rmf_fakty_parser_fails_soft_without_source_timezone() -> None:
    context = CatalogueParseContext(
        release_date="2026-09-25",
        release_date_precision="day",
        timezone=None,
    )
    assert RmfFaktyParser().parse_catalogue("16:00 Fakty", context=context) is None


@pytest.mark.parametrize(
    ("title", "release_date"),
    [
        ("16:00 Completely unrelated podcast", "2026-09-25"),
        ("02:30 Fakty", "2026-03-29"),
        ("02:30 Fakty", "2026-10-25"),
    ],
)
def test_rmf_unproven_title_or_dst_clock_uses_first_seen(title: str, release_date: str) -> None:
    source = next(
        source for source in BUILTIN_CATALOG.sources if source.id == SourceId("rmf_fakty")
    )
    if "unrelated" in title:
        # AUTO does not inherit the explicitly configured leading-clock contract.
        source = replace(source, temporal_strategy=TemporalStrategy.AUTO)
    item = _episode("Q", title)
    item["release_date"] = release_date
    snapshot = fetch_source_catalogue(
        _CatalogueClient([item]),
        source,
        now=NOW,
        cutoff=NOW - timedelta(days=2),
        collect_evidence=True,
    )

    assert len(snapshot.episodes) == 1
    assert snapshot.episodes[0].edition_at is None
    assert snapshot.episodes[0].timestamp_source is TimestampSource.FIRST_SEEN
    assert snapshot.evidence[0].parser_outcome == "fallback"


class _CatalogueClient:
    def __init__(self, items: list[object]) -> None:
        self._page = catalogue_page(items)

    def show_episodes(
        self,
        show_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        assert show_id == "2BeZqsvzZUSldlZWvjeGe3"
        assert limit == 50
        assert offset == 0
        return self._page


def _episode(letter: str, title: str) -> dict[str, object]:
    episode_id = letter * 22
    return {
        "id": episode_id,
        "uri": f"spotify:episode:{episode_id}",
        "name": title,
        "release_date": "2026-09-25",
        "release_date_precision": "day",
        "duration_ms": 300_000,
    }


def test_rmf_catalogue_reports_contextual_title_timestamps_without_filtering_variants() -> None:
    source = next(
        source for source in BUILTIN_CATALOG.sources if source.id == SourceId("rmf_fakty")
    )
    snapshot = fetch_source_catalogue(
        _CatalogueClient(
            [
                _episode("A", "16:00 Fakty"),
                _episode("B", "08:30 Fakty sportowe"),
            ]
        ),
        source,
        now=NOW,
        cutoff=NOW - timedelta(days=2),
        collect_evidence=True,
    )

    assert snapshot.filter_rejected_count == 0
    assert snapshot.parser_fallback_count == 0
    assert [episode.title for episode in snapshot.episodes] == [
        "16:00 Fakty",
        "08:30 Fakty sportowe",
    ]
    assert [episode.edition_at for episode in snapshot.episodes] == [
        datetime(2026, 9, 25, 14, 0, tzinfo=UTC),
        datetime(2026, 9, 25, 6, 30, tzinfo=UTC),
    ]
    assert all(episode.timestamp_source is TimestampSource.TITLE for episode in snapshot.episodes)
    assert [item.parser_outcome for item in snapshot.evidence] == [
        "catalogue_context",
        "catalogue_context",
    ]
    assert all(item.filter_outcome == "accepted" for item in snapshot.evidence)
