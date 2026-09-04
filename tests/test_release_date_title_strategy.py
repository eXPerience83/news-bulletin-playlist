from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from news_bulletin_playlist.collection import normalize_rss_source
from news_bulletin_playlist.models import (
    CountryCode,
    LanguageTag,
    ParserId,
    SourceDefinition,
    SourceId,
)


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
