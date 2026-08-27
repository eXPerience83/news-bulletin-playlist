from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

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


def aware_datetime(
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int,
    *,
    tz: ZoneInfo,
) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=tz)


def expand_two_digit_year(year: int) -> int:
    if year >= 100:
        return year
    return 2000 + year if year <= 68 else 1900 + year


def spanish_month(value: str) -> int | None:
    token = re.sub(r"[^A-ZÁÉÍÓÚÜÑ]", "", value.upper())
    return _SPANISH_MONTHS.get(token)
