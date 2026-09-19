from datetime import datetime
from zoneinfo import ZoneInfo

from news_bulletin_playlist.providers.rne import RneParser

MADRID = ZoneInfo("Europe/Madrid")


def test_rne_accepts_current_rne_noticias_dotted_hour_titles() -> None:
    parser = RneParser()
    cases = {
        "RNE NOTICIAS - 12.09.2026 - 16.00H": datetime(2026, 9, 12, 16, 0, tzinfo=MADRID),
        "RNE NOTICIAS - 12.09.2026 - 15.00H": datetime(2026, 9, 12, 15, 0, tzinfo=MADRID),
        "RNE NOTICIAS - 12.09.2026 - 13.00H": datetime(2026, 9, 12, 13, 0, tzinfo=MADRID),
    }

    for title, expected in cases.items():
        parsed = parser.parse(title)
        assert parsed is not None
        assert parsed.edition_at == expected


def test_rne_accepts_current_noticias_rne_date_punctuation_variants() -> None:
    parser = RneParser()
    cases = {
        "NOTICIAS RNE - 17.09.2026. - 00.00H": datetime(2026, 9, 17, 0, 0, tzinfo=MADRID),
        "NOTICIAS RNE - 16.09.2026 - 21.00H": datetime(2026, 9, 16, 21, 0, tzinfo=MADRID),
        "NOTICIAS RNE - 16.09.26 - 19.00H": datetime(2026, 9, 16, 19, 0, tzinfo=MADRID),
    }

    for title, expected in cases.items():
        parsed = parser.parse(title)
        assert parsed is not None
        assert parsed.edition_at == expected


def test_rne_keeps_previous_noticias_rne_shape() -> None:
    parsed = RneParser().parse("NOTICIAS RNE - 25.08.2026 - 18.30 H")

    assert parsed is not None
    assert parsed.edition_at == datetime(2026, 8, 25, 18, 30, tzinfo=MADRID)


def test_rne_current_shape_does_not_accept_unrelated_products() -> None:
    assert RneParser().parse("RNE DEPORTES - 12.09.2026 - 16.00H") is None


def test_rne_dotted_date_rejects_unsupported_year_width_and_invalid_date() -> None:
    parser = RneParser()

    assert parser.parse("NOTICIAS RNE - 16.09.026 - 19.00H") is None
    assert parser.parse("NOTICIAS RNE - 31.02.26 - 19.00H") is None
