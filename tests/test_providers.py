from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from news_bulletin_playlist.providers import (
    CnnParser,
    CopeParser,
    OndaCeroParser,
    RneParser,
    SerParser,
)

MADRID = ZoneInfo("Europe/Madrid")
NEW_YORK = ZoneInfo("America/New_York")


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Las noticias de la SER, 23:03 (26/08/2026)", datetime(2026, 8, 26, 23, 3, tzinfo=MADRID)),
        (
            "Las noticias de la SER, 19:22 (25/08/2026)",
            datetime(2026, 8, 25, 19, 22, tzinfo=MADRID),
        ),
        ("Las noticias de la SER 07:00 (1/8/2026)", datetime(2026, 8, 1, 7, 0, tzinfo=MADRID)),
    ],
)
def test_ser(title: str, expected: datetime) -> None:
    parsed = SerParser().parse(title)
    assert parsed is not None
    assert parsed.edition_at == expected


def test_ser_rejects_unknown_shape() -> None:
    assert SerParser().parse("Hoy por Hoy: noticias 08:00") is None


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("NOTICIAS RNE - 25.08.2026 - 18.30 H", datetime(2026, 8, 25, 18, 30, tzinfo=MADRID)),
        ("NOTICIAS RNE - 23.08.2026- 19H", datetime(2026, 8, 23, 19, 0, tzinfo=MADRID)),
        ("NOTICIAS RNE -20.06.2026- 18,30H", datetime(2026, 6, 20, 18, 30, tzinfo=MADRID)),
        ("NOTICIAS RNE - 17.04.2026 - 1930H", datetime(2026, 4, 17, 19, 30, tzinfo=MADRID)),
        ("NOTICIAS RNE - 24.08.2026 - 12.00H", datetime(2026, 8, 24, 12, 0, tzinfo=MADRID)),
    ],
)
def test_rne_variants(title: str, expected: datetime) -> None:
    parsed = RneParser().parse(title)
    assert parsed is not None
    assert parsed.edition_at == expected


def test_rne_rejects_invalid_time() -> None:
    assert RneParser().parse("NOTICIAS RNE - 25.08.2026 - 25.30 H") is None


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        (
            "Las noticias de Onda Cero de las 4:00h (25/8/2026)",
            datetime(2026, 8, 25, 4, 0, tzinfo=MADRID),
        ),
        (
            "Las noticias de Onda Cero de las 19:00h (25/8/2026)",
            datetime(2026, 8, 25, 19, 0, tzinfo=MADRID),
        ),
    ],
)
def test_ondacero(title: str, expected: datetime) -> None:
    parsed = OndaCeroParser().parse(title)
    assert parsed is not None
    assert parsed.edition_at == expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("CNN 5 cosas 06/25/2026 6 pm", datetime(2026, 6, 25, 18, 0, tzinfo=NEW_YORK)),
        ("CNN 5 cosas 07/29/26 6pm", datetime(2026, 7, 29, 18, 0, tzinfo=NEW_YORK)),
        ("CNN 5 cosas 06/22/2026 6 am", datetime(2026, 6, 22, 6, 0, tzinfo=NEW_YORK)),
        ("CNN 5 cosas 06/22/2026 12 am", datetime(2026, 6, 22, 0, 0, tzinfo=NEW_YORK)),
        ("CNN 5 cosas 06/22/2026 12 pm", datetime(2026, 6, 22, 12, 0, tzinfo=NEW_YORK)),
    ],
)
def test_cnn(title: str, expected: datetime) -> None:
    parsed = CnnParser().parse(title)
    assert parsed is not None
    assert parsed.edition_at == expected


def test_cnn_rejects_24_hour_shape() -> None:
    assert CnnParser().parse("CNN 5 cosas 06/25/2026 18 pm") is None


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("2:00H | 30 ABR 2026 | BOLETÍN", datetime(2026, 4, 30, 2, 0, tzinfo=MADRID)),
        ("14:30H | 1 SEP 2026 | BOLETIN", datetime(2026, 9, 1, 14, 30, tzinfo=MADRID)),
    ],
)
def test_cope_national(title: str, expected: datetime) -> None:
    parsed = CopeParser().parse(title)
    assert parsed is not None
    assert parsed.edition_at == expected


@pytest.mark.parametrize(
    "title",
    [
        "2:00H | 30 ABR 2026 | BOLETÍN VALENCIA",
        "2:00H | 30 ABR 2026 | BOLETÍN ANDALUCÍA",
        "2:00H | 30 XYZ 2026 | BOLETÍN",
    ],
)
def test_cope_rejects_non_national_or_unknown(title: str) -> None:
    assert CopeParser().parse(title) is None
