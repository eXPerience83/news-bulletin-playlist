from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.collection import normalize_rss_source
from news_bulletin_playlist.providers.rfi import RfiJournalMondeParser

UTC = ZoneInfo("UTC")


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Journal 02/09/2026 03h00 GMT", datetime(2026, 9, 2, 3, 0, tzinfo=UTC)),
        ("Journal 05/07/2026 18h00 GMT", datetime(2026, 7, 5, 18, 0, tzinfo=UTC)),
    ],
)
def test_rfi_journal_monde_parser_accepts_semantic_gmt_titles(
    title: str,
    expected: datetime,
) -> None:
    parsed = RfiJournalMondeParser().parse(title)
    assert parsed is not None
    assert parsed.edition_at == expected


@pytest.mark.parametrize(
    "title",
    [
        "Tranche d'information 25/08 18h00 GMT",
        "Journal Afrique 25/08/2026 18h00 GMT",
        "Journal 31/02/2026 18h00 GMT",
        "Journal 25/08/2026 25h00 GMT",
    ],
)
def test_rfi_journal_monde_parser_rejects_other_products_or_invalid_titles(title: str) -> None:
    assert RfiJournalMondeParser().parse(title) is None


def test_rfi_mixed_feed_keeps_only_journal_monde_items() -> None:
    source = BUILTIN_CATALOG.source("rfi_fr")
    payload = b"""<?xml version='1.0' encoding='UTF-8'?>
<rss version='2.0' xmlns:itunes='http://www.itunes.com/dtds/podcast-1.0.dtd'>
  <channel>
    <item>
      <title>Journal 02/09/2026 03h00 GMT</title>
      <guid>journal</guid>
      <pubDate>Wed, 02 Sep 2026 03:10:00 +0000</pubDate>
      <itunes:duration>10:00</itunes:duration>
    </item>
    <item>
      <title>Tranche d'information 02/09 18h00 GMT</title>
      <guid>tranche</guid>
      <pubDate>Wed, 02 Sep 2026 18:00:00 +0000</pubDate>
      <itunes:duration>30:00</itunes:duration>
    </item>
    <item>
      <title>Magazine international</title>
      <guid>magazine</guid>
      <pubDate>Wed, 02 Sep 2026 12:00:00 +0000</pubDate>
      <itunes:duration>60:00</itunes:duration>
    </item>
  </channel>
</rss>
"""

    editions = normalize_rss_source(source, payload)

    assert [edition.source_native_id for edition in editions] == ["journal"]
    assert editions[0].edition_at == datetime(2026, 9, 2, 3, 0, tzinfo=UTC)
    assert editions[0].duration_seconds == 600
