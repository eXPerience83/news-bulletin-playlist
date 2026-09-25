from __future__ import annotations

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from news_bulletin_playlist.providers.common import (
    SPANISH_MONTH_NAMES,
    clock_12h_pattern,
    clock_24h_pattern,
    datetime_from_match,
    expand_two_digit_year,
    named_month_date_pattern,
    numeric_date_pattern,
    parse_24h_clock_value,
    parse_numeric_date_value,
    spanish_month,
)


@pytest.mark.parametrize(
    ("separators", "value", "expected"),
    [
        (":", "6:30", {"hour": "6", "minute": "30"}),
        (":.", "06.30", {"hour": "06", "minute": "30"}),
        (":.,", "23,59", {"hour": "23", "minute": "59"}),
        ("h", "18h05", {"hour": "18", "minute": "05"}),
    ],
)
def test_clock_24h_pattern_supports_reviewed_separators(
    separators: str,
    value: str,
    expected: dict[str, str],
) -> None:
    match = re.fullmatch(clock_24h_pattern(separators=separators), value)
    assert match is not None
    assert match.groupdict() == expected


@pytest.mark.parametrize("value", ["24:00", "23:60", "6-30", "6:3"])
def test_clock_24h_pattern_rejects_invalid_or_unconfigured_shapes(value: str) -> None:
    assert re.fullmatch(clock_24h_pattern(separators=":."), value) is None


def test_clock_24h_pattern_can_make_minutes_optional_without_loosening_hour_range() -> None:
    pattern = clock_24h_pattern(separators=".:,", minute_optional=True)

    hour_only = re.fullmatch(pattern, "9")
    assert hour_only is not None
    assert hour_only.groupdict() == {"hour": "9", "minute": None}
    assert re.fullmatch(pattern, "24") is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("6", {"hour": "6", "minute": None}),
        ("06", {"hour": "06", "minute": None}),
        ("6:30", {"hour": "6", "minute": "30"}),
        ("12:59", {"hour": "12", "minute": "59"}),
    ],
)
def test_clock_12h_pattern_supports_optional_minutes(
    value: str,
    expected: dict[str, str | None],
) -> None:
    pattern = clock_12h_pattern(separators=":", minute_optional=True)
    match = re.fullmatch(pattern, value)
    assert match is not None
    assert match.groupdict() == expected


@pytest.mark.parametrize("value", ["0", "00", "13", "12:60", "6-30"])
def test_clock_12h_pattern_rejects_invalid_or_unconfigured_shapes(value: str) -> None:
    pattern = clock_12h_pattern(separators=":", minute_optional=True)
    assert re.fullmatch(pattern, value) is None


@pytest.mark.parametrize(
    ("order", "separators", "year_digits", "value", "expected"),
    [
        (
            "dmy",
            "/",
            (4,),
            "1/9/2026",
            {"day": "1", "month": "9", "year": "2026"},
        ),
        (
            "mdy",
            "/",
            (2, 4),
            "09/25/26",
            {"month": "09", "day": "25", "year": "26"},
        ),
        (
            "ymd",
            "-",
            (4,),
            "2026-09-25",
            {"year": "2026", "month": "09", "day": "25"},
        ),
        (
            "dmy",
            "./-",
            (2, 4),
            "17.09.26",
            {"day": "17", "month": "09", "year": "26"},
        ),
    ],
)
def test_numeric_date_pattern_supports_reviewed_orders_and_separators(
    order: str,
    separators: str,
    year_digits: tuple[int, ...],
    value: str,
    expected: dict[str, str],
) -> None:
    pattern = numeric_date_pattern(
        order=order,  # type: ignore[arg-type]
        separators=separators,
        year_digits=year_digits,
    )
    match = re.fullmatch(pattern, value)
    assert match is not None
    assert match.groupdict() == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1.9.26", date(2026, 9, 1)),
        ("01.09.2026", date(2026, 9, 1)),
        ("010926", date(2026, 9, 1)),
        ("01092026", date(2026, 9, 1)),
    ],
)
def test_numeric_date_value_supports_separated_and_compact_forms(
    value: str,
    expected: date,
) -> None:
    parsed = parse_numeric_date_value(
        value,
        order="dmy",
        separators=".",
        year_digits=(2, 4),
        compact=True,
        two_digit_year_policy="2000",
    )
    assert parsed == expected


@pytest.mark.parametrize("value", ["321326", "01092", "01/09/26", "０１０９２６"])
def test_numeric_date_value_rejects_invalid_or_unconfigured_forms(value: str) -> None:
    assert (
        parse_numeric_date_value(
            value,
            order="dmy",
            separators=".",
            year_digits=(2, 4),
            compact=True,
            two_digit_year_policy="2000",
        )
        is None
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("19", (19, 0)),
        ("18.30", (18, 30)),
        ("18,30", (18, 30)),
        ("930", (9, 30)),
        ("1930", (19, 30)),
    ],
)
def test_24h_clock_value_supports_separated_compact_and_hour_only_forms(
    value: str,
    expected: tuple[int, int],
) -> None:
    assert (
        parse_24h_clock_value(
            value,
            separators=".,",
            minute_optional=True,
            compact=True,
        )
        == expected
    )


@pytest.mark.parametrize("value", ["24.00", "23.60", "999", "09:30", "０９３０"])
def test_24h_clock_value_rejects_invalid_or_unconfigured_forms(value: str) -> None:
    assert (
        parse_24h_clock_value(
            value,
            separators=".,",
            minute_optional=True,
            compact=True,
        )
        is None
    )


def test_named_month_date_pattern_uses_reviewed_lexicon() -> None:
    pattern = named_month_date_pattern(
        order="dmy",
        month_names=SPANISH_MONTH_NAMES,
        year_digits=(4,),
    )
    match = re.fullmatch(pattern, "1 SEPT 2026", re.IGNORECASE)
    assert match is not None
    assert match.groupdict() == {"day": "1", "month": "SEPT", "year": "2026"}
    assert re.fullmatch(pattern, "1 XYZ 2026", re.IGNORECASE) is None


def test_datetime_from_match_supports_12h_clock_and_two_digit_year() -> None:
    date_pattern = numeric_date_pattern(order="mdy", separators="/", year_digits=(2, 4))
    clock_pattern = clock_12h_pattern(separators=":", minute_optional=True)
    pattern = re.compile(
        rf"^{date_pattern}\s+{clock_pattern}\s*(?P<ampm>am|pm)$",
        re.IGNORECASE,
    )
    match = pattern.fullmatch("07/29/26 12 am")
    assert match is not None

    parsed = datetime_from_match(match, tz=ZoneInfo("America/New_York"), clock="12h")

    assert parsed == datetime(2026, 7, 29, 0, 0, tzinfo=ZoneInfo("America/New_York"))


def test_datetime_from_match_supports_named_month_resolver() -> None:
    date_pattern = named_month_date_pattern(
        order="dmy",
        month_names=SPANISH_MONTH_NAMES,
        year_digits=(4,),
    )
    clock_pattern = clock_24h_pattern(separators=":")
    pattern = re.compile(rf"^{clock_pattern}\s+{date_pattern}$", re.IGNORECASE)
    match = pattern.fullmatch("14:30 1 SEP 2026")
    assert match is not None

    parsed = datetime_from_match(
        match,
        tz=ZoneInfo("Europe/Madrid"),
        month_resolver=spanish_month,
    )

    assert parsed == datetime(2026, 9, 1, 14, 30, tzinfo=ZoneInfo("Europe/Madrid"))


def test_two_digit_year_policy_is_explicit() -> None:
    assert expand_two_digit_year(69) == 1969
    assert expand_two_digit_year(69, policy="2000") == 2069


def test_common_patterns_reject_invalid_configuration() -> None:
    with pytest.raises(ValueError, match="clock separators"):
        clock_24h_pattern(separators="")
    with pytest.raises(ValueError, match="clock separators"):
        clock_12h_pattern(separators="")
    with pytest.raises(ValueError, match="date separators"):
        numeric_date_pattern(order="dmy", separators="")
    with pytest.raises(ValueError, match="year_digits"):
        numeric_date_pattern(order="dmy", separators="/", year_digits=(3,))
    with pytest.raises(ValueError, match="date order"):
        numeric_date_pattern(order="ydm", separators="/")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="month_names"):
        named_month_date_pattern(order="dmy", month_names=())
    with pytest.raises(ValueError, match="two-digit year policy"):
        expand_two_digit_year(26, policy="future")  # type: ignore[arg-type]
