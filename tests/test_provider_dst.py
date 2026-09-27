from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from news_bulletin_playlist.providers.cnn import CnnParser
from news_bulletin_playlist.providers.common import unique_aware_datetime
from news_bulletin_playlist.providers.cope import CopeParser
from news_bulletin_playlist.providers.ondacero import OndaCeroParser
from news_bulletin_playlist.providers.rfi import RfiJournalMondeParser
from news_bulletin_playlist.providers.rne import RneParser
from news_bulletin_playlist.providers.ser import SerParser


@pytest.mark.parametrize(
    ("zone", "month", "day", "hour"),
    [
        ("Europe/Madrid", 3, 29, 2),
        ("Europe/Madrid", 10, 25, 2),
        ("America/New_York", 3, 8, 2),
        ("America/New_York", 11, 1, 1),
    ],
)
def test_unique_wall_time_rejects_dst_gap_and_overlap(
    zone: str, month: int, day: int, hour: int
) -> None:
    assert unique_aware_datetime(2026, month, day, hour, 30, tz=ZoneInfo(zone)) is None


@pytest.mark.parametrize(
    ("zone", "month", "day", "hour", "utc_hour"),
    [
        ("Europe/Madrid", 3, 29, 1, 0),
        ("Europe/Madrid", 3, 29, 3, 1),
        ("Europe/Madrid", 10, 25, 1, 23),
        ("Europe/Madrid", 10, 25, 3, 2),
        ("America/New_York", 3, 8, 1, 6),
        ("America/New_York", 3, 8, 3, 7),
        ("America/New_York", 11, 1, 0, 4),
        ("America/New_York", 11, 1, 2, 7),
        ("UTC", 3, 29, 2, 2),
        ("GMT", 10, 25, 2, 2),
    ],
)
def test_unique_wall_time_preserves_zone_and_physical_instant(
    zone: str, month: int, day: int, hour: int, utc_hour: int
) -> None:
    tz = ZoneInfo(zone)
    result = unique_aware_datetime(2026, month, day, hour, 30, tz=tz)
    assert result is not None
    assert result.tzinfo is tz
    assert result.replace(tzinfo=None) == datetime(2026, month, day, hour, 30)
    utc_day = day - 1 if utc_hour == 23 else day
    assert result.astimezone(UTC) == datetime(2026, month, utc_day, utc_hour, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("month", "day", "hour", "minute"),
    [(2, 30, 12, 0), (1, 1, 24, 0), (1, 1, 12, 60)],
)
def test_unique_wall_time_fails_soft_for_invalid_components(
    month: int, day: int, hour: int, minute: int
) -> None:
    assert unique_aware_datetime(2026, month, day, hour, minute, tz=ZoneInfo("UTC")) is None


@pytest.mark.parametrize("day,month", [(29, 3), (25, 10)])
def test_spanish_historical_parsers_reject_unproven_time(day: int, month: int) -> None:
    date = f"{day:02}/{month:02}/2026"
    month_name = "MAR" if month == 3 else "OCT"
    cases = [
        (SerParser(), f"Las noticias de la SER, 02:30 ({date})"),
        (OndaCeroParser(), f"Las noticias de Onda Cero de las 02:30h ({date})"),
        (RneParser(), f"NOTICIAS RNE - {day:02}.{month:02}.2026 - 02.30H"),
        (CopeParser(), f"02:30H | {day} {month_name} 2026 | BOLETÍN"),
    ]
    for parser, title in cases:
        assert parser.parse(title) is None
        assert parser.parse_with_recovery(title).parsed is None


@pytest.mark.parametrize(
    "title",
    [
        "CNN 5 cosas 11/01/2026 1:30 am",
        "CNN 5 cosas 03/08/2026 2:30 am",
    ],
)
def test_cnn_rejects_unproven_time(title: str) -> None:
    assert CnnParser().parse(title) is None


@pytest.mark.parametrize("day", ["29/03/2026", "25/10/2026"])
def test_gmt_title_is_unaffected_by_european_dst(day: str) -> None:
    parsed = RfiJournalMondeParser().parse(f"Journal {day} 02h30 GMT")
    assert parsed is not None
    d, m, y = map(int, day.split("/"))
    assert parsed.edition_at == datetime(y, m, d, 2, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("day", "month", "offset"),
    [(28, 3, 1), (30, 3, 2), (24, 10, 2), (26, 10, 1)],
)
def test_spanish_parsers_preserve_ordinary_dates(day: int, month: int, offset: int) -> None:
    date = f"{day:02}/{month:02}/2026"
    month_name = "MAR" if month == 3 else "OCT"
    cases = [
        (SerParser(), f"Las noticias de la SER, 03:30 ({date})"),
        (OndaCeroParser(), f"Las noticias de Onda Cero de las 03:30h ({date})"),
        (RneParser(), f"NOTICIAS RNE - {day:02}.{month:02}.2026 - 03.30H"),
        (CopeParser(), f"03:30H | {day} {month_name} 2026 | BOLETÍN"),
    ]
    for parser, title in cases:
        parsed = parser.parse(title)
        assert parsed is not None
        assert parsed.edition_at.tzinfo == ZoneInfo("Europe/Madrid")
        assert parsed.edition_at.astimezone(UTC) == datetime(
            2026, month, day, 3 - offset, 30, tzinfo=UTC
        )


@pytest.mark.parametrize(
    ("month", "day", "utc_hour"),
    [(3, 7, 8), (3, 9, 7), (10, 31, 7), (11, 2, 8)],
)
def test_cnn_preserves_ordinary_dates(month: int, day: int, utc_hour: int) -> None:
    parsed = CnnParser().parse(f"CNN 5 cosas {month:02}/{day:02}/2026 3:30 am")
    assert parsed is not None
    assert parsed.edition_at.tzinfo == ZoneInfo("America/New_York")
    assert parsed.edition_at.astimezone(UTC) == datetime(2026, month, day, utc_hour, 30, tzinfo=UTC)
