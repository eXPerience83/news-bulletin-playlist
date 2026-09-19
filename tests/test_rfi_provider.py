from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

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
