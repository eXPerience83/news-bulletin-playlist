from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from news_bulletin_playlist.collection import normalize_rss_source
from news_bulletin_playlist.models import (
    CanonicalEdition,
    CountryCode,
    ExternalReference,
    LanguageTag,
    ParserId,
    SourceDefinition,
    SourceId,
)
from news_bulletin_playlist.persistence import MatchStatus, SQLiteStore
from news_bulletin_playlist.spotify.matcher import match_source_editions


def test_release_date_title_source_keeps_semantic_edition_time_unset() -> None:
    source = SourceDefinition(
        id=SourceId("abc"),
        display_name="ABC — Las Noticias de ABC",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        timezone=ZoneInfo("Europe/Madrid"),
        enabled=True,
        parser_id=ParserId("release_date_title"),
        endpoint_url="https://example.test/abc.rss",
    )
    payload = """<rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel>
      <item>
        <guid>abc-2026-09-04</guid>
        <title>Resumen de las principales noticias del día</title>
        <pubDate>Fri, 04 Sep 2026 05:15:00 +0000</pubDate>
        <itunes:duration>05:12</itunes:duration>
      </item>
    </channel></rss>""".encode()

    editions = normalize_rss_source(source, payload)

    assert len(editions) == 1
    edition = editions[0]
    assert edition.title == "Resumen de las principales noticias del día"
    assert edition.published_at == datetime(2026, 9, 4, 5, 15, tzinfo=UTC)
    assert edition.edition_at is None
    assert edition.duration_seconds == 312


@pytest.mark.parametrize(
    ("precision", "value", "expected"),
    [
        ("day", "2026-09-04", MatchStatus.MATCHED),
        ("month", "2026-09", MatchStatus.PENDING),
        ("year", "2026", MatchStatus.PENDING),
    ],
)
def test_release_date_title_precision(
    tmp_path: Path, precision: str, value: str, expected: MatchStatus
) -> None:
    source = SourceDefinition(
        id=SourceId("abc"),
        display_name="ABC",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        timezone=ZoneInfo("Europe/Madrid"),
        enabled=True,
        parser_id=ParserId("release_date_title"),
        external_references=(ExternalReference("spotify", "show", "show-abc"),),
    )
    edition = CanonicalEdition(
        source_id=source.id,
        source_native_id="abc-2026-09-04",
        title="Exact recurring title",
        published_at=datetime(2026, 9, 4, 5, 15, tzinfo=UTC),
        edition_at=None,
    )
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    store.upsert_editions((edition,), observed_at=datetime(2026, 9, 4, 6, tzinfo=UTC))

    class Client:
        def show_episodes(
            self, show_id: str, *, limit: int = 50, offset: int = 0
        ) -> dict[str, Any]:
            del show_id, limit, offset
            return {
                "items": [
                    {
                        "uri": f"spotify:episode:{precision}",
                        "name": edition.title,
                        "release_date": value,
                        "release_date_precision": precision,
                        "duration_ms": 60_000,
                    }
                ],
                "next": None,
            }

    result = match_source_editions(
        Client(), store, source, (edition,), now=datetime(2026, 9, 4, 6, tzinfo=UTC)
    )

    assert result.outcomes[0].status is expected
    if expected is MatchStatus.PENDING:
        assert "release_date_precision_insufficient=1" in result.outcomes[0].diagnostics
