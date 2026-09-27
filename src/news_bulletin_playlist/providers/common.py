from __future__ import annotations

import re
from collections.abc import Callable, Collection
from datetime import UTC, date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

type ClockFormat = Literal["24h", "12h"]
type NumericDateOrder = Literal["dmy", "mdy", "ymd"]
type TwoDigitYearPolicy = Literal["pivot", "2000"]
type MonthResolver = Callable[[str], int | None]

MADRID = ZoneInfo("Europe/Madrid")
NEW_YORK = ZoneInfo("America/New_York")

_SPANISH_MONTHS = {
    "ENE": 1,
    "FEB": 2,
    "MAR": 3,
    "ABR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AGO": 8,
    "SEP": 9,
    "SEPT": 9,
    "OCT": 10,
    "NOV": 11,
    "DIC": 12,
}
SPANISH_MONTH_NAMES = tuple(_SPANISH_MONTHS)


def _validate_date_grammar(order: NumericDateOrder, year_digits: tuple[int, ...]) -> None:
    if order not in {"dmy", "mdy", "ymd"}:
        raise ValueError("unsupported numeric date order")
    if not year_digits or any(width not in {2, 4} for width in year_digits):
        raise ValueError("year_digits must contain only 2 and/or 4")


def _year_pattern(year_digits: tuple[int, ...]) -> str:
    return "|".join(rf"[0-9]{{{width}}}" for width in dict.fromkeys(year_digits))


def clock_24h_pattern(*, separators: str, minute_optional: bool = False) -> str:
    """Build a reusable named-group regex fragment for a strict 24-hour clock."""
    if not separators:
        raise ValueError("clock separators must not be empty")
    minute = rf"[{re.escape(separators)}](?P<minute>[0-5][0-9])"
    if minute_optional:
        minute = rf"(?:{minute})?"
    return rf"(?P<hour>[01]?[0-9]|2[0-3]){minute}"


def clock_12h_pattern(*, separators: str, minute_optional: bool = False) -> str:
    """Build a reusable named-group regex fragment for a strict 12-hour clock."""
    if not separators:
        raise ValueError("clock separators must not be empty")
    minute = rf"[{re.escape(separators)}](?P<minute>[0-5][0-9])"
    if minute_optional:
        minute = rf"(?:{minute})?"
    return rf"(?P<hour>0?[1-9]|1[0-2]){minute}"


def numeric_date_pattern(
    *,
    order: NumericDateOrder,
    separators: str,
    year_digits: tuple[int, ...] = (4,),
) -> str:
    """Build a reusable named-group regex fragment for a numeric calendar date."""
    _validate_date_grammar(order, year_digits)
    if not separators:
        raise ValueError("date separators must not be empty")

    components = {
        "d": r"(?P<day>[0-9]{1,2})",
        "m": r"(?P<month>[0-9]{1,2})",
        "y": rf"(?P<year>{_year_pattern(year_digits)})",
    }
    separator = rf"[{re.escape(separators)}]"
    return separator.join(components[token] for token in order)


def named_month_date_pattern(
    *,
    order: NumericDateOrder,
    month_names: Collection[str],
    year_digits: tuple[int, ...] = (4,),
) -> str:
    """Build a named-group date fragment whose month comes from a reviewed lexicon."""
    _validate_date_grammar(order, year_digits)
    if not month_names or any(not name for name in month_names):
        raise ValueError("month_names must not be empty")

    month_pattern = "|".join(
        re.escape(name) for name in sorted(set(month_names), key=lambda name: (-len(name), name))
    )
    components = {
        "d": r"(?P<day>[0-9]{1,2})",
        "m": rf"(?P<month>{month_pattern})",
        "y": rf"(?P<year>{_year_pattern(year_digits)})",
    }
    return r"\s+".join(components[token] for token in order)


def expand_two_digit_year(
    year: int,
    *,
    policy: TwoDigitYearPolicy = "pivot",
) -> int:
    """Expand a two-digit year with an explicit, provider-reviewed policy."""
    if policy not in {"pivot", "2000"}:
        raise ValueError("unsupported two-digit year policy")
    if year < 0:
        raise ValueError("year must not be negative")
    if year >= 100:
        return year
    if policy == "2000":
        return 2000 + year
    return 2000 + year if year <= 68 else 1900 + year


def parse_numeric_date_value(
    value: str,
    *,
    order: NumericDateOrder,
    separators: str,
    year_digits: tuple[int, ...] = (4,),
    compact: bool = False,
    two_digit_year_policy: TwoDigitYearPolicy = "pivot",
) -> date | None:
    """Parse one reviewed numeric date token, optionally including compact forms."""
    _validate_date_grammar(order, year_digits)
    if not separators:
        raise ValueError("date separators must not be empty")

    token = value.strip()
    separated = re.fullmatch(
        numeric_date_pattern(order=order, separators=separators, year_digits=year_digits),
        token,
    )
    if separated is not None:
        values = separated.groupdict()
        return _date_from_components(
            values["year"],
            values["month"],
            values["day"],
            two_digit_year_policy=two_digit_year_policy,
        )

    if not compact or re.fullmatch(r"[0-9]+", token) is None:
        return None
    year_width = next(
        (width for width in dict.fromkeys(year_digits) if len(token) == width + 4),
        None,
    )
    if year_width is None:
        return None

    widths = {"d": 2, "m": 2, "y": year_width}
    components: dict[str, str] = {}
    offset = 0
    for component in order:
        width = widths[component]
        components[component] = token[offset : offset + width]
        offset += width
    return _date_from_components(
        components["y"],
        components["m"],
        components["d"],
        two_digit_year_policy=two_digit_year_policy,
    )


def _date_from_components(
    year_value: str,
    month_value: str,
    day_value: str,
    *,
    two_digit_year_policy: TwoDigitYearPolicy,
) -> date | None:
    year = int(year_value)
    if len(year_value) == 2:
        year = expand_two_digit_year(year, policy=two_digit_year_policy)
    try:
        return date(year, int(month_value), int(day_value))
    except ValueError:
        return None


def parse_24h_clock_value(
    value: str,
    *,
    separators: str,
    minute_optional: bool = False,
    compact: bool = False,
) -> tuple[int, int] | None:
    """Parse one reviewed 24-hour clock token, optionally including HHMM/HMM forms."""
    token = value.strip()
    separated = re.fullmatch(
        clock_24h_pattern(separators=separators, minute_optional=minute_optional),
        token,
    )
    if separated is not None:
        minute = separated.group("minute")
        return int(separated.group("hour")), int(minute) if minute is not None else 0

    if not compact or re.fullmatch(r"[0-9]{3,4}", token) is None:
        return None
    hour = int(token[:-2])
    minute = int(token[-2:])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def unique_aware_datetime(
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int,
    *,
    tz: ZoneInfo,
) -> datetime | None:
    """Resolve a local wall time only when it proves one unique physical instant."""
    try:
        wall_time = datetime(year, month, day, hour, minute)
        instants: set[datetime] = set()
        for fold in (0, 1):
            candidate = wall_time.replace(tzinfo=tz, fold=fold).astimezone(UTC)
            if candidate.astimezone(tz).replace(tzinfo=None) == wall_time:
                instants.add(candidate)
    except ValueError, OverflowError:
        return None
    if len(instants) != 1:
        return None
    return next(iter(instants)).astimezone(tz)


def datetime_from_match(
    match: re.Match[str],
    *,
    tz: ZoneInfo,
    clock: ClockFormat = "24h",
    month_resolver: MonthResolver | None = None,
    two_digit_year_policy: TwoDigitYearPolicy = "pivot",
) -> datetime | None:
    """Build one validated aware datetime from common named temporal groups."""
    year_value = match.group("year")
    month_value = match.group("month")
    day_value = match.group("day")
    hour_value = match.group("hour")
    minute_value = match.groupdict().get("minute")

    year = int(year_value)
    if len(year_value) == 2:
        year = expand_two_digit_year(year, policy=two_digit_year_policy)
    month = int(month_value) if month_resolver is None else month_resolver(month_value)
    if month is None:
        return None

    hour = int(hour_value)
    minute = int(minute_value) if minute_value is not None else 0
    if clock == "12h":
        ampm = match.groupdict().get("ampm")
        if ampm is None:
            return None
        meridiem = ampm.casefold()
        if meridiem not in {"am", "pm"} or not 1 <= hour <= 12:
            return None
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
    elif clock == "24h":
        if not 0 <= hour <= 23:
            return None
    else:
        raise ValueError("unsupported clock format")
    if not 0 <= minute <= 59:
        return None

    return unique_aware_datetime(year, month, int(day_value), hour, minute, tz=tz)


def spanish_month(value: str) -> int | None:
    token = re.sub(r"[^A-ZÁÉÍÓÚÜÑ]", "", value.upper())
    return _SPANISH_MONTHS.get(token)
