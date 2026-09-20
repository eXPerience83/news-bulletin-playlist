from __future__ import annotations

import re

from news_bulletin_playlist.models import ParsedEdition
from news_bulletin_playlist.providers.base import RecoveringTitleParserBase
from news_bulletin_playlist.providers.common import MADRID, aware_datetime

_PATTERN = re.compile(
    r"^(?:NOTICIAS\s+RNE|RNE\s+NOTICIAS)\s*(?:-\s*)?"
    r"(?P<date>[0-9]{1,2}\.[0-9]{1,2}\.(?:[0-9]{4}|[0-9]{2})\.?|[0-9]{6}|[0-9]{8})\s*-\s*"
    r"(?P<time>[0-9]{1,2}(?:[\.,][0-9]{2})?|[0-9]{3,4})\s*H?$",
    re.IGNORECASE,
)


def _parse_date(value: str) -> tuple[int, int, int] | None:
    if "." in value:
        parts = value.removesuffix(".").split(".")
        if len(parts) != 3:
            return None
        day_s, month_s, year_s = parts
        day, month, year = int(day_s), int(month_s), int(year_s)
        if len(year_s) == 2:
            year += 2000
    elif len(value) == 8:
        day, month, year = int(value[:2]), int(value[2:4]), int(value[4:])
    elif len(value) == 6:
        day, month, year = int(value[:2]), int(value[2:4]), 2000 + int(value[4:])
    else:
        return None
    return year, month, day


def _parse_time(value: str) -> tuple[int, int] | None:
    compact = value.replace(",", ".")
    if "." in compact:
        hour_s, minute_s = compact.split(".", 1)
        hour, minute = int(hour_s), int(minute_s)
    elif len(compact) in {3, 4}:
        hour, minute = divmod(int(compact), 100)
    else:
        hour, minute = int(compact), 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


class RneParser(RecoveringTitleParserBase):
    provider_id = "rne"

    def _parse_strict(self, title: str) -> ParsedEdition | None:
        match = _PATTERN.match(title.strip())
        if match is None:
            return None
        parsed_date = _parse_date(match.group("date"))
        parsed_time = _parse_time(match.group("time"))
        if parsed_date is None or parsed_time is None:
            return None
        year, month, day = parsed_date
        hour, minute = parsed_time
        try:
            edition_at = aware_datetime(
                year,
                month,
                day,
                hour,
                minute,
                tz=MADRID,
            )
        except ValueError:
            return None
        return ParsedEdition(self.provider_id, title, edition_at)
