from zoneinfo import ZoneInfo

from news_bulletin_playlist.collection import normalize_rss_source
from news_bulletin_playlist.models import (
    CountryCode,
    LanguageTag,
    ParserId,
    SourceDefinition,
    SourceId,
)


def test_enclosure_identity_precedes_shared_editorial_link() -> None:
    source = SourceDefinition(
        id=SourceId("ser"),
        display_name="Cadena SER",
        countries=(CountryCode("ES"),),
        languages=(LanguageTag("es"),),
        timezone=ZoneInfo("Europe/Madrid"),
        enabled=True,
        parser_id=ParserId("ser"),
        endpoint_url="https://example.test/ser.xml",
    )
    payload = b"""\
<rss><channel>
  <item>
    <title>Las noticias de la SER, 11:00 (30/08/2026)</title>
    <pubDate>Sun, 30 Aug 2026 09:05:00 +0000</pubDate>
    <link>https://example.test/editorial-page</link>
    <enclosure url="https://cdn.example.test/audio-a.mp3" type="audio/mpeg" />
  </item>
  <item>
    <title>Las noticias de la SER, 11:00 (30/08/2026)</title>
    <pubDate>Sun, 30 Aug 2026 09:05:00 +0000</pubDate>
    <link>https://example.test/editorial-page</link>
    <enclosure url="https://cdn.example.test/audio-b.mp3" type="audio/mpeg" />
  </item>
</channel></rss>
"""

    editions = normalize_rss_source(source, payload)

    assert [edition.source_native_id for edition in editions] == [
        "https://cdn.example.test/audio-a.mp3",
        "https://cdn.example.test/audio-b.mp3",
    ]
